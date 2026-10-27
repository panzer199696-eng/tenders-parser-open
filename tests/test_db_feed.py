"""
Тесты ленты из единой базы (T017) и пересчёта оценок.

Главное, что проверяется: **что зависит от даты, а что нет**. Оценка лота
(насколько он наш) считается один раз при сборе и лежит в базе. Срочность и
истечение срока считаются при показе — сохранённые, они назавтра врали бы:
вчерашний аврал «горел» бы вечно, а истёкшее предлагалось бы подать.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, load_feed, load_lot
from src.db.feed import days_left

TODAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 10, 0)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _add(conn, lot_id, **over):
    row = {
        "lot_key": f"ROS|{lot_id}",
        "platform": "Росэлторг",
        "lot_id": lot_id,
        "name": "Компаунд заливочный",
        "company": "АО Рудник",
        "date_deadline": "10.09.2026",
        "score": 50,
        "score_explain": "Материалы цеха +30",
        "tags": '["reagents"]',
        "client_level": "market",
        "core_profile": 1,
        "below_threshold": 0,
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()


# ── Корзины ──────────────────────────────────────────────────────────────────


def test_профильный_лот_попадает_в_ядро(db):
    _add(db, "1")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["core"] == 1


def test_лот_с_близким_дедлайном_горит(db):
    _add(db, "1", date_deadline="03.09.2026")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["urgent"] == 1
    assert feed["counts"]["core"] == 0


def test_срочность_считается_на_сегодня_а_не_хранится(db):
    """Тот же лот вчера горел, а через неделю — уже нет."""
    _add(db, "1", date_deadline="03.09.2026")
    early = load_feed(db, today=date(2026, 9, 1), now=datetime(2026, 9, 1, 10, 0))
    later = load_feed(db, today=date(2026, 8, 20), now=datetime(2026, 8, 20, 10, 0))
    assert early["counts"]["urgent"] == 1
    assert later["counts"]["urgent"] == 0, "за две недели до дедлайна ничего не горит"
    assert later["counts"]["core"] == 1


def test_истёкший_лот_уходит_из_работы(db):
    _add(db, "1", date_deadline="20.08.2026")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["expired"] == 1
    assert feed["counts"]["core"] == 0


def test_истёкший_лот_из_миграции_с_iso_датой_тоже_истёк(db):
    # Миграция из старой базы записала дедлайн как «2026-08-28 06:54»; лента
    # его не понимала, и 1320 истёкших лотов висели среди рабочих (16.10.2026).
    _add(db, "1", date_deadline="2026-08-28 06:54", client_level="full")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["expired"] == 1
    assert feed["counts"]["core"] == 0
    assert feed["counts"]["client"] == 0


def test_лот_ниже_порога_в_своей_корзине(db):
    _add(db, "1", below_threshold=1)
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["below"] == 1


def test_лот_закреплённого_клиента_отделён_от_профиля(db):
    _add(db, "1", core_profile=0, client_level="full", tags='["parts"]')
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["client"] == 1


def test_лот_без_темы_уходит_на_проверку(db):
    """Тема не распозналась вовсе — прятать некуда, копим в «На проверку»
    (прежняя корзина «Прочее» исчезла по ТЗ-5)."""
    _add(db, "1", core_profile=0, client_level="market", tags="[]", score=0)
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["review"] == 1


def test_слабое_совпадение_на_проверке(db):
    """needs_review = 1 (тема угадана слабо или заказчик незнаком)."""
    _add(db, "1", core_profile=0, client_level="market", needs_review=1, score=11)
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["review"] == 1
    assert feed["counts"]["core"] == 0


def test_чужая_отрасль_в_не_наш_профиль(db):
    _add(db, "1", core_profile=0, client_level="market", off_industry=1, score=0)
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["foreign"] == 1


def test_подряд_тоже_в_не_наш_профиль(db):
    _add(db, "1", core_profile=0, client_level="market", off_profile=1, score=0)
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["foreign"] == 1


def test_чужой_лот_не_горит_даже_с_горящим_дедлайном(db):
    """«Кислота для ТЭЦ» с дедлайном завтра не должна стоять в «Горит»:
    чужая отрасль проверяется раньше срочности."""
    _add(db, "1", core_profile=1, off_industry=1, date_deadline="02.09.2026")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["foreign"] == 1
    assert feed["counts"]["urgent"] == 0


def test_истёкший_слабый_остаётся_в_истёкших(db):
    _add(
        db,
        "1",
        core_profile=0,
        client_level="market",
        needs_review=1,
        date_deadline="20.08.2026",
    )
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["expired"] == 1
    assert feed["counts"]["review"] == 0


def test_лот_без_дедлайна_не_горит_и_не_истёк(db):
    _add(db, "1", date_deadline="Не указана")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["core"] == 1


# ── Счётчики и порядок ───────────────────────────────────────────────────────


def test_счётчик_считает_все_лоты_а_не_показанные(db):
    """Пользователь должен видеть настоящее число, а не длину куска."""
    for i in range(5):
        _add(db, str(i))
    feed = load_feed(db, today=TODAY, now=NOW, limit_per_bucket=2)
    assert feed["counts"]["core"] == 5
    assert len(feed["buckets"]["core"]) == 2


def test_лоты_идут_по_убыванию_оценки(db):
    _add(db, "1", score=30)
    _add(db, "2", score=90)
    feed = load_feed(db, today=TODAY, now=NOW)
    assert [lot["score"] for lot in feed["buckets"]["core"]] == [90, 30]


def test_дни_до_дедлайна_считаются(db):
    _add(db, "1", date_deadline="05.09.2026")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["buckets"]["core"][0]["days_left"] == 4


@pytest.mark.parametrize(
    "value,expected",
    [
        ("05.09.2026", 4),
        ("05.09.2026 14:00", 4),
        ("2026-09-05 14:00", 4),
        ("2026-09-05T14:00:00+03:00", 4),
        ("Не указана", None),
        ("", None),
        ("мусор", None),
    ],
)
def test_разбор_дедлайна(value, expected):
    assert days_left(value, TODAY) == expected


# ── Карточка лота ────────────────────────────────────────────────────────────


def test_карточка_отдаёт_места_публикации(db):
    _add(db, "1")
    db.execute(
        "INSERT INTO lot_sources (lot_key, platform, href) VALUES (?,?,?)",
        ("ROS|1", "Росэлторг", "https://example.invalid/1"),
    )
    db.commit()
    lot = load_lot(db, "ROS|1")
    assert lot["sources"][0]["platform"] == "Росэлторг"


def test_карточка_показывает_заявку_если_она_есть(db):
    _add(db, "1")
    db.execute(
        "INSERT INTO applications (lot_key, status, manager) VALUES (?,?,?)",
        ("ROS|1", "SELECTED", "Пётр"),
    )
    db.commit()
    lot = load_lot(db, "ROS|1")
    assert lot["application"]["status"] == "SELECTED"
    assert lot["application"]["manager"] == "Пётр"


def test_карточки_несуществующего_лота_нет(db):
    assert load_lot(db, "ROS|нет") is None


# ── Пересчёт оценок ──────────────────────────────────────────────────────────


class _Profile:
    def evaluate(self, tender):
        class V:
            score = 77
            groups = ["reagents"]
            client_id = "pavlik"
            client_level = "partial"
            core_profile = True
            below_threshold = False

            def explain(self):
                return "Материалы цеха +30 · закреплённый клиент +25"

        return V()


def test_пересчёт_поднимает_лоты_без_оценки(db):
    """После миграции старые лоты приехали без оценки и падали в «Прочее»."""
    from src.db import rescore_lots

    _add(
        db,
        "1",
        score=0,
        score_explain=None,
        tags="[]",
        core_profile=0,
        client_level=None,
    )
    assert load_feed(db, today=TODAY, now=NOW)["counts"]["review"] == 1

    assert rescore_lots(db, _Profile())["updated"] == 1
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["counts"]["core"] == 1
    assert feed["buckets"]["core"][0]["score"] == 77


def test_пересчёт_не_трогает_уже_оценённые(db):
    from src.db import rescore_lots

    _add(db, "1")
    assert rescore_lots(db, _Profile())["updated"] == 0
    assert db.execute("SELECT score FROM lots").fetchone()[0] == 50


def test_полный_пересчёт_обновляет_всё(db):
    """Справочник изменился — прежние оценки устарели."""
    from src.db import rescore_lots

    _add(db, "1")
    assert rescore_lots(db, _Profile(), only_missing=False)["updated"] == 1
    assert db.execute("SELECT score FROM lots").fetchone()[0] == 77


# ── Разобранные лоты и темы ──────────────────────────────────────────────────


def test_отклонённый_лот_уходит_из_ленты(db):
    """Жалоба 31.08.2026: «Мимо» с причиной, а лот остаётся в списке.

    Лента — это то, что ещё не разобрано. Иначе отклонённый лот приходится
    отклонять снова и снова.
    """
    _add(db, "1")
    assert load_feed(db, today=TODAY, now=NOW)["total"] == 1

    db.execute(
        "INSERT INTO applications (lot_key, status, reject_reason)"
        " VALUES ('ROS|1','REJECTED','не наш профиль')"
    )
    db.commit()

    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["total"] == 0, "отклонённый лот обязан уйти из ленты"
    assert feed["rejected"] == 1
    assert feed["total_lots"] == 1, "из базы он при этом никуда не делся"


def test_взятый_лот_тоже_уходит_из_ленты(db):
    _add(db, "1")
    db.execute("INSERT INTO applications (lot_key, status) VALUES ('ROS|1','SELECTED')")
    db.commit()
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["total"] == 0
    assert feed["in_work"] == 1


def test_у_лота_есть_тема(db):
    """Тема — первая совпавшая группа номенклатуры, человеческим названием."""
    _add(db, "1", tags='["pumps"]')
    lot = load_feed(db, today=TODAY, now=NOW)["buckets"]["core"][0]
    assert lot["topic_id"] == "pumps"
    assert lot["topic"] and lot["topic"] != "pumps"


def test_лот_без_группы_получает_тему_без_темы(db):
    _add(db, "1", tags="[]", core_profile=0, client_level="full")
    lot = load_feed(db, today=TODAY, now=NOW)["buckets"]["client"][0]
    assert lot["topic"] == "Без темы"


def test_темы_считаются_и_сортируются(db):
    _add(db, "1", tags='["pumps"]')
    _add(db, "2", tags='["valves"]')
    _add(db, "3", tags='["valves"]')
    topics = load_feed(db, today=TODAY, now=NOW)["topics"]
    assert topics[0]["id"] == "valves" and topics[0]["lots"] == 2
    assert {t["id"] for t in topics} == {"valves", "pumps"}


# ── Что нового за ночь ───────────────────────────────────────────────────────
#
# Сценарий владельца: он приходит в 9:00 и должен сразу видеть, что принёс
# ночной сбор. В базе для этого есть `first_seen_at` — момент, когда лот
# увидели впервые, — но до 01.09.2026 его не читал никто, и лот недельной
# давности выглядел так же, как найденный этой ночью.


def _run(conn, started_at, finished_at="2026-09-01T03:30:00", outcome="ok"):
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, outcome, data_complete, summary)"
        " VALUES (?, ?, ?, 1, 'тест')",
        (started_at, finished_at, outcome),
    )
    conn.commit()


def test_новыми_считаются_лоты_последнего_прогона(db):
    _run(db, "2026-09-01T03:00:00")
    _add(db, "1", first_seen_at="2026-09-01T03:12:00")
    _add(db, "2", first_seen_at="2026-08-28T04:00:00")
    feed = load_feed(db, today=TODAY, now=NOW)
    assert feed["fresh"]["total"] == 1
    new_lots = [lot for b in feed["buckets"].values() for lot in b if lot["is_new"]]
    assert [lot["lot_id"] for lot in new_lots] == ["1"]


def test_без_журнала_новыми_считаются_лоты_за_сутки(db, monkeypatch):
    # Ни записи в runs, ни файла журнала: свежий запуск на чистой машине.
    from src import run_journal

    monkeypatch.setattr(run_journal, "read_last_run", lambda: None)
    _add(db, "1", first_seen_at="2026-09-01T02:00:00")
    _add(db, "2", first_seen_at="2026-08-20T02:00:00")
    assert load_feed(db, today=TODAY, now=NOW)["fresh"]["total"] == 1


def test_свежесть_считает_профиль_и_срочное(db):
    _run(db, "2026-09-01T03:00:00")
    _add(
        db,
        "1",
        first_seen_at="2026-09-01T03:05:00",
        core_profile=1,
        date_deadline="02.09.2026",
    )
    _add(
        db,
        "2",
        first_seen_at="2026-09-01T03:06:00",
        core_profile=0,
        client_level="market",
        tags="[]",
        date_deadline="30.09.2026",
    )
    fresh = load_feed(db, today=TODAY, now=NOW)["fresh"]
    assert fresh["total"] == 2
    assert fresh["core"] == 1
    assert fresh["urgent"] == 1
    assert fresh["since"] == "2026-09-01T03:00:00"


def test_лот_без_отметки_о_первой_встрече_новым_не_считается(db):
    _run(db, "2026-09-01T03:00:00")
    _add(db, "1", first_seen_at=None)
    assert load_feed(db, today=TODAY, now=NOW)["fresh"]["total"] == 0
