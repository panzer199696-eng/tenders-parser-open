"""
Тесты записи лотов в единую базу с оценкой профиля (T013–T015).

Смысл этапа: оценка считается при СБОРЕ и ложится в базу вместе с лотом.
Раньше она жила только в интерфейсе второй программы — в отчёты и Excel
объяснение «почему лот в списке» не попадало вовсе.

Профиль в тестах подменяется заглушкой: здесь проверяется хранение оценки,
а не правила номенклатуры (они покрыты тестами профиля).
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, save_lots


@dataclass
class _Verdict:
    score: int = 0
    groups: list = field(default_factory=list)
    urgent: bool = False
    core_profile: bool = False
    below_threshold: bool = False
    client_id: str | None = None
    client_level: str | None = None
    text: str = ""
    # Признаки разбора профиля (ТЗ-4): с ТЗ-5 они обязаны переживать прогон.
    strength: str = "none"
    industry: str = "unknown"
    needs_review: bool = False
    off_industry: bool = False
    off_profile: bool = False
    matched_terms: list = field(default_factory=list)

    def explain(self) -> str:
        return self.text


class _Profile:
    """Заглушка профиля: считает по названию, как настоящий — по справочнику."""

    def __init__(self, verdict: _Verdict | None = None, fail: bool = False):
        self.verdict = verdict or _Verdict(
            score=70,
            groups=["reagents"],
            core_profile=True,
            text="Материалы цеха +30 · закреплённый клиент +40 · совпало: «компаунд»",
        )
        self.fail = fail
        self.calls = 0

    def evaluate(self, tender):
        self.calls += 1
        if self.fail:
            raise RuntimeError("справочник повреждён")
        return self.verdict


def _tender(**over):
    base = {
        "platform": "ТЭК-Торг",
        "lot_id": "555",
        "name": "Компаунд заливочный 18 т",
        "company": 'ПАО ГМК "Северный металл"',
        "inn": "7700000001",
        "href": "https://tektorg.ru/lot/555",
        "sum": 5_000_000.0,
        "date_published": "20.08.2026",
        "date_deadline": "05.09.2026",
        "search_key": "компаунд заливочный",
    }
    base.update(over)
    return base


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


# ── Оценка при сборе ─────────────────────────────────────────────────────────


def test_оценка_ложится_в_базу_вместе_с_лотом(db):
    """FR-014: объяснение сохраняется, а не пересчитывается при показе."""
    save_lots(db, [_tender()], _Profile())
    row = db.execute("SELECT * FROM lots").fetchone()
    assert row["score"] == 70
    assert "Материалы цеха +30" in row["score_explain"]
    assert json.loads(row["tags"]) == ["reagents"]
    assert row["core_profile"] == 1


def test_лот_ниже_порога_помечается_а_не_выбрасывается(db):
    """T015: молча выброшенный лот — это лот, о котором никто не узнает."""
    verdict = _Verdict(
        score=30, below_threshold=True, text="Материалы цеха +30 · ниже порога"
    )
    save_lots(db, [_tender(sum=200_000.0)], _Profile(verdict))
    row = db.execute("SELECT * FROM lots").fetchone()
    assert row is not None, "лот обязан остаться в базе"
    assert row["below_threshold"] == 1


def test_перспективным_считается_профиль_или_закреплённый_клиент(db):
    """`promising` — свойство лота, а не сегодняшней даты.

    Срочность («горит») сюда не пишется: она зависит от того, какое сегодня
    число, и сохранённая протухла бы к следующему утру. Её считает лента
    при показе — см. tests/test_db_feed.py.
    """
    save_lots(db, [_tender()], _Profile(_Verdict(score=85, core_profile=True)))
    assert db.execute("SELECT promising FROM lots").fetchone()[0] == 1


def test_чужой_лот_не_перспективный(db):
    verdict = _Verdict(score=0, core_profile=False, text="не наш профиль")
    save_lots(db, [_tender(lot_id="777")], _Profile(verdict))
    row = db.execute("SELECT promising, client_level FROM lots WHERE lot_id='777'").fetchone()
    assert row["promising"] == 0


def test_уровень_клиента_сохраняется(db):
    """Лента раскладывает по корзинам, опираясь на уровень заказчика."""
    verdict = _Verdict(score=65, text="закреплённый клиент +40")
    verdict.client_level = "full"
    verdict.client_id = "vasilyevsky"
    save_lots(db, [_tender()], _Profile(verdict))
    row = db.execute("SELECT client_id, client_level, promising FROM lots").fetchone()
    assert row["client_level"] == "full"
    assert row["client_id"] == "vasilyevsky"
    assert row["promising"] == 1, "закреплённый клиент — перспективный лот"


def test_ошибка_профиля_не_стоит_нам_лота(db):
    """Повреждённый справочник не должен превращаться в потерю данных."""
    summary = save_lots(db, [_tender()], _Profile(fail=True))
    assert summary["added"] == 1
    assert summary["scored"] == 0
    row = db.execute("SELECT * FROM lots").fetchone()
    assert row["name"] == "Компаунд заливочный 18 т"
    assert row["score"] == 0


def test_без_профиля_лот_сохраняется_как_есть(db):
    save_lots(db, [_tender(score=42, tags=["reagents"])], None)
    row = db.execute("SELECT * FROM lots").fetchone()
    assert row["score"] == 42


# ── Повторные встречи ────────────────────────────────────────────────────────


def test_повторная_встреча_не_задваивает(db):
    profile = _Profile()
    save_lots(db, [_tender()], profile)
    summary = save_lots(db, [_tender()], profile)
    assert summary["added"] == 0
    assert summary["updated"] == 1
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_повторная_встреча_двигает_время_но_хранит_первое(db):
    save_lots(db, [_tender()], _Profile())
    first = db.execute("SELECT first_seen_at, last_seen_at FROM lots").fetchone()
    save_lots(db, [_tender(name="Компаунд заливочный 18 т, партия 2")], _Profile())
    second = db.execute("SELECT first_seen_at, last_seen_at FROM lots").fetchone()
    assert second["first_seen_at"] == first["first_seen_at"]


def test_пустое_значение_не_затирает_заполненное(db):
    """Во второй раз площадка может отдать меньше, чем в первый."""
    save_lots(db, [_tender()], _Profile())
    save_lots(db, [_tender(sum=None, date_deadline="Не указана", inn="")], _Profile())
    row = db.execute("SELECT * FROM lots").fetchone()
    assert row["sum"] == 5_000_000.0
    assert row["date_deadline"] == "05.09.2026"
    assert row["inn"] == "7700000001"


def test_новое_значение_обновляет_поле(db):
    save_lots(db, [_tender()], _Profile())
    save_lots(db, [_tender(date_deadline="10.09.2026")], _Profile())
    assert db.execute("SELECT date_deadline FROM lots").fetchone()[0] == "10.09.2026"


# ── Ключи и площадки ─────────────────────────────────────────────────────────


def test_один_номер_на_двух_площадках_это_разные_лоты(db):
    # После ТЗ-7 одинаковые name+заказчик+день уже склеиваются, поэтому
    # здесь даём разные названия: проверяем именно ключ «площадка|номер» —
    # lot_id уникален внутри площадки, а не между ними.
    save_lots(
        db,
        [
            _tender(name="Компаунд заливочный 18 т"),
            _tender(name="Компаунд заливочный 20 т", platform="Росэлторг"),
        ],
        _Profile(),
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 2


def test_место_публикации_запоминается(db):
    save_lots(db, [_tender()], _Profile())
    row = db.execute("SELECT * FROM lot_sources").fetchone()
    assert row["platform"] == "ТЭК-Торг"
    assert row["href"] == "https://tektorg.ru/lot/555"


def test_лот_без_номера_пропускается(db):
    summary = save_lots(db, [_tender(lot_id="")], _Profile())
    assert summary["skipped"] == 1
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 0


def test_предоплата_по_умолчанию_неизвестна(db):
    """До этапа Э6 состояние не определяется — но и не выдаётся за «нет»."""
    save_lots(db, [_tender()], _Profile())
    assert db.execute("SELECT prepayment FROM lots").fetchone()[0] == "unknown"


# ── Признаки разбора профиля (ТЗ-5) ───────────────────────────────────────────


def test_признаки_разбора_ложатся_в_базу_вместе_с_лотом(db):
    """Слабое совпадение и незнакомая отрасль — свойства лота, а не сессии.

    Без записи в базу корзины «На проверку» и «Не наш профиль» не могли бы
    считаться при показе: лоты следующего утра снова были бы «прочими».
    """
    verdict = _Verdict(
        score=11,
        groups=["pumps"],
        strength="weak",
        industry="unknown",
        needs_review=True,
        matched_terms=[
            {"term": "насос", "group": "pumps", "strength": "weak"}
        ],
        text="Насосы и насосное оборудование +11 (общее слово «насос», без уточнения)",
    )
    save_lots(db, [_tender()], _Profile(verdict))
    row = db.execute("SELECT * FROM lots").fetchone()
    assert row["match_strength"] == "weak"
    assert row["industry"] == "unknown"
    assert row["needs_review"] == 1
    assert row["off_industry"] == 0
    assert row["off_profile"] == 0
    assert json.loads(row["matched_terms"]) == [
        {"term": "насос", "group": "pumps", "strength": "weak"}
    ]


def test_чужая_отрасль_и_подряд_сохраняются_как_отсеянные(db):
    """Лист «Не наш профиль» строится по этим флагам — их потеря значит,
    что отсеянные лоты станут «просто найденными»."""
    verdict = _Verdict(
        score=0,
        industry="foreign",
        off_industry=True,
        text="не наш профиль: совпало «кислота», но заказчик из чужой отрасли (ТЭЦ)",
    )
    save_lots(db, [_tender()], _Profile(verdict))
    row = db.execute("SELECT industry, off_industry, off_profile FROM lots").fetchone()
    assert row["industry"] == "foreign"
    assert row["off_industry"] == 1
    assert row["off_profile"] == 0


def test_без_совпадений_matched_terms_пустой_а_не_мусор(db):
    save_lots(db, [_tender()], _Profile())
    assert db.execute("SELECT matched_terms FROM lots").fetchone()[0] is None


def test_повторная_встреча_затирает_устаревший_список_терминов(db):
    """Справочник изменился, совпадений больше нет — старый JSON обязан уйти:
    хранить вчерашние «совпало точно» рядом с нынешним «нет совпадений» —
    вранье в базе."""
    first = _Verdict(
        score=30,
        groups=["reagents"],
        strength="strong",
        matched_terms=[{"term": "компаунд заливочный", "group": "reagents", "strength": "strong"}],
        text="Материалы цеха +30",
    )
    save_lots(db, [_tender()], _Profile(first))
    save_lots(db, [_tender()], _Profile(_Verdict(score=0, text="нет совпадений")))
    row = db.execute("SELECT score, matched_terms FROM lots").fetchone()
    assert row["score"] == 0
    assert row["matched_terms"] is None


def test_пересчёт_пишет_признаки_разбора(db):
    """rescore_lots поднимает не только оценку, но и признаки корзин."""
    from src.db import rescore_lots

    verdict = _Verdict(
        score=18,
        groups=["pumps"],
        strength="weak",
        industry="unknown",
        needs_review=True,
        text="проверить: тема угадана слабо",
    )
    db.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, name, date_deadline)"
        " VALUES ('EIS|9', 'ЕИС', '9', 'Насос', '10.09.2026')"
    )
    db.commit()
    assert rescore_lots(db, _Profile(verdict), only_missing=False)["updated"] == 1
    row = db.execute(
        "SELECT match_strength, industry, needs_review FROM lots"
    ).fetchone()
    assert row["match_strength"] == "weak"
    assert row["industry"] == "unknown"
    assert row["needs_review"] == 1


# ── Миграция: база без новых колонок (ТЗ-5) ──────────────────────────────────


def test_миграция_добавляет_колонки_и_не_теряет_данные(tmp_path):
    """База версии 3 без колонок признаков обновляется на месте.

    База у пользователя одна: пересоздание означает потерю истории, поэтому
    колонки добавляются ALTER'ом к живым данным.
    """
    import sqlite3

    from src.db import connect, ensure_schema, schema_version

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        """
        CREATE TABLE lots (
            lot_key TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            lot_id TEXT NOT NULL,
            name TEXT,
            score INTEGER DEFAULT 0,
            tags TEXT,
            score_explain TEXT,
            client_level TEXT,
            core_profile INTEGER DEFAULT 0,
            below_threshold INTEGER DEFAULT 0,
            date_deadline TEXT
        )
        """
    )
    old.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, name, score, date_deadline)"
        " VALUES ('EIS|1', 'ЕИС', '1', 'Компаунд заливочный', 70, '10.09.2026')"
    )
    old.commit()
    old.close()

    conn = connect(path)
    ensure_schema(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lots)")}
    assert {"match_strength", "industry", "needs_review", "off_industry",
            "off_profile", "matched_terms"} <= cols
    row = conn.execute("SELECT name, score FROM lots").fetchone()
    assert row["name"] == "Компаунд заливочный" and row["score"] == 70
    # Сверяем с константой, а не с числом: схема растёт (появились признаки
    # разбора, дедуп, состав закупки), и прибитый номер ломал тест при каждом
    # расширении, хотя миграция отрабатывала верно.
    from src.db.schema import SCHEMA_VERSION

    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()
