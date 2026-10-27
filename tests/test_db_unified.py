"""
Тесты единой базы: схема (T001) и миграция трёх старых баз (T002, T003).

Старые базы создаются синтетически в tmp_path — тесты не зависят ни от сети,
ни от данных на конкретной машине. Схемы источников повторяют реальные:
`tenders` из программы сбора и `applications` + `history` из реестра.

Ключевые проверки — про смысл, а не про механику переноса:
  • статус FOUND в старом реестре означал «лот найден», а не «заявка», и
    такие записи обязаны попасть в ленту, а не в реестр заявок;
  • переход «ничего → найден» — это время первой встречи лота, а не история
    работы с ним;
  • повторный запуск миграции ничего не удваивает (на этом уже споткнулись:
    без уникального индекса второй прогон удваивал историю).
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, make_lot_key, migrate
from src.db.schema import SCHEMA_VERSION, schema_version

OLD_TENDERS_DDL = """
CREATE TABLE tenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, name TEXT, company TEXT,
    href TEXT UNIQUE, sum REAL, date_published TEXT, date_deadline TEXT,
    date_to_display TEXT, created_at TEXT, search_key TEXT, tender_no TEXT,
    lot_id TEXT, tender_type TEXT, organizer_match INTEGER, organizer_href TEXT,
    construction INTEGER, tags TEXT, score INTEGER, promising INTEGER
);
"""

OLD_REGISTRY_DDL = """
CREATE TABLE applications (
    lot_id TEXT PRIMARY KEY, platform TEXT, name TEXT, company TEXT, href TEXT,
    client_id TEXT, client_level TEXT, manager TEXT, sum REAL, deadline TEXT,
    score INTEGER, groups TEXT, status TEXT, reject_reason TEXT, decision TEXT,
    decision_at TEXT, decision_by TEXT, supplier TEXT, purchase_price REAL,
    logistics REAL, cost REAL, markup REAL, bid_price REAL, price_approved_by TEXT,
    price_approved_at TEXT, submitted_at TEXT, submission_no TEXT, result TEXT,
    winner_price REAL, deviation REAL, loss_reason TEXT, contact TEXT,
    next_touch TEXT, note TEXT, created_at TEXT, updated_at TEXT,
    assessment TEXT, volume_t REAL, packaging TEXT, hazard INTEGER, docs_needed TEXT
);
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT, from_status TEXT,
    to_status TEXT, at TEXT, who TEXT, comment TEXT
);
"""


@pytest.fixture
def old_tenders(tmp_path):
    """База сбора: два лота одной площадки."""
    p = tmp_path / "tenders.db"
    c = sqlite3.connect(p)
    c.executescript(OLD_TENDERS_DDL)
    c.executemany(
        "INSERT INTO tenders (platform, name, company, href, sum, date_published,"
        " date_deadline, created_at, lot_id, search_key, score)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "Росэлторг",
                "Антисептик калия",
                'ООО ГРК "Северное"',
                "https://example.invalid/1",
                5_000_000.0,
                "01.08.2026",
                "10.09.2026",
                "2026-08-01 10:00",
                "111",
                "антисептик",
                70,
            ),
            (
                "Росэлторг",
                "Футеровка мельницы",
                "АО Рудник",
                "https://example.invalid/2",
                None,
                "02.08.2026",
                "11.09.2026",
                "2026-08-02 10:00",
                "222",
                "футеровка",
                40,
            ),
        ],
    )
    c.commit()
    c.close()
    return p


@pytest.fixture
def old_registry(tmp_path):
    """Реестр: один найденный лот, одна реальная заявка, история обоих."""
    p = tmp_path / "registry.db"
    c = sqlite3.connect(p)
    c.executescript(OLD_REGISTRY_DDL)
    c.executemany(
        "INSERT INTO applications (lot_id, platform, name, company, href, sum,"
        " deadline, score, groups, status, manager, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "333",
                "Росэлторг",
                "Материалы цеха",
                "АО Кедр",
                "https://example.invalid/3",
                1_000_000.0,
                "12.09.2026",
                55,
                "Реагенты",
                "FOUND",
                None,
                "2026-08-03 10:00",
                "2026-08-03 10:00",
            ),
            (
                "444",
                "Росэлторг",
                "Сорбционная колонна",
                "АО Восход",
                "https://example.invalid/4",
                9_000_000.0,
                "13.09.2026",
                80,
                "Оборудование",
                "SELECTED",
                "Пётр",
                "2026-08-04 10:00",
                "2026-08-05 12:00",
            ),
        ],
    )
    c.executemany(
        "INSERT INTO history (lot_id, from_status, to_status, at, who) VALUES (?,?,?,?,?)",
        [
            ("333", None, "FOUND", "2026-08-03 10:00", None),
            ("444", None, "FOUND", "2026-08-04 10:00", None),
            ("444", "FOUND", "SELECTED", "2026-08-05 12:00", "Пётр"),
        ],
    )
    c.commit()
    c.close()
    return p


# ── Схема ────────────────────────────────────────────────────────────────────


def test_схема_создаётся_и_версионируется(tmp_path):
    c = connect(tmp_path / "t.db")
    ensure_schema(c)
    tables = {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert {
        "lots",
        "lot_sources",
        "applications",
        "history",
        "runs",
        "run_platforms",
    } <= tables
    assert schema_version(c) == SCHEMA_VERSION


def test_повторный_вызов_схемы_безопасен(tmp_path):
    """ensure_schema зовётся на каждом запуске программы."""
    c = connect(tmp_path / "t.db")
    ensure_schema(c)
    c.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, name) VALUES ('ROS|1','ROS','1','Лот')"
    )
    c.commit()
    ensure_schema(c)
    assert c.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_ключ_лота_включает_площадку():
    """Голый номер ключом быть не может: номера уникальны только внутри площадки."""
    assert make_lot_key("Росэлторг", "4564500") == "Росэлторг|4564500"
    assert make_lot_key("ТЭК-Торг", "4564500") != make_lot_key("Росэлторг", "4564500")


def test_один_номер_на_двух_площадках_не_конфликтует(tmp_path):
    c = connect(tmp_path / "t.db")
    ensure_schema(c)
    c.execute(
        "INSERT INTO lots (lot_key, platform, lot_id) VALUES ('ROS|500','ROS','500')"
    )
    c.execute(
        "INSERT INTO lots (lot_key, platform, lot_id) VALUES ('ТЭК|500','ТЭК','500')"
    )
    c.commit()
    assert c.execute("SELECT count(*) FROM lots").fetchone()[0] == 2


def test_повтор_лота_на_одной_площадке_запрещён(tmp_path):
    c = connect(tmp_path / "t.db")
    ensure_schema(c)
    c.execute(
        "INSERT INTO lots (lot_key, platform, lot_id) VALUES ('ROS|500','ROS','500')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO lots (lot_key, platform, lot_id) VALUES ('ROS|500x','ROS','500')"
        )


def test_предоплата_по_умолчанию_неизвестна(tmp_path):
    """«Не знаем» и «предоплаты нет» — разные вещи (FR-012)."""
    c = connect(tmp_path / "t.db")
    ensure_schema(c)
    c.execute("INSERT INTO lots (lot_key, platform, lot_id) VALUES ('ROS|1','ROS','1')")
    c.commit()
    assert c.execute("SELECT prepayment FROM lots").fetchone()[0] == "unknown"


def test_заявка_без_лота_не_создаётся(tmp_path):
    """Реестр ссылается на ленту, а не живёт сам по себе."""
    c = connect(tmp_path / "t.db")
    ensure_schema(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO applications (lot_key, status) VALUES ('ROS|нет-такого','SELECTED')"
        )
        c.commit()


# ── Миграция ─────────────────────────────────────────────────────────────────


def test_лоты_переносятся(tmp_path, old_tenders):
    target = tmp_path / "new.db"
    report = migrate([old_tenders], target)
    c = connect(target)
    assert report.lots_added == 2
    assert c.execute("SELECT count(*) FROM lots").fetchone()[0] == 2
    row = c.execute("SELECT * FROM lots WHERE lot_id='111'").fetchone()
    assert row["name"] == "Антисептик калия"
    assert row["company"] == 'ООО ГРК "Северное"'
    assert row["score"] == 70


def test_найденные_лоты_реестра_едут_в_ленту_а_не_в_заявки(tmp_path, old_registry):
    """FOUND — это лот, а не заявка: 1543 такие записи не должны стать работой."""
    target = tmp_path / "new.db"
    migrate([old_registry], target)
    c = connect(target)
    assert c.execute("SELECT count(*) FROM lots").fetchone()[0] == 2
    assert c.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
    assert (
        c.execute("SELECT lot_key FROM applications").fetchone()[0] == "Росэлторг|444"
    )


def test_заявка_сохраняет_менеджера_и_статус(tmp_path, old_registry):
    target = tmp_path / "new.db"
    migrate([old_registry], target)
    c = connect(target)
    row = c.execute("SELECT * FROM applications").fetchone()
    assert row["status"] == "SELECTED"
    assert row["manager"] == "Пётр"


def test_первая_встреча_не_становится_историей(tmp_path, old_registry):
    """«ничего → найден» — это время обнаружения лота, а не работа с ним."""
    target = tmp_path / "new.db"
    migrate([old_registry], target)
    c = connect(target)
    assert c.execute("SELECT count(*) FROM history").fetchone()[0] == 1
    row = c.execute("SELECT * FROM history").fetchone()
    assert (row["from_status"], row["to_status"]) == ("FOUND", "SELECTED")
    seen = c.execute("SELECT first_seen_at FROM lots WHERE lot_id='333'").fetchone()[0]
    assert seen == "2026-08-03 10:00"


def test_повторная_миграция_ничего_не_удваивает(tmp_path, old_tenders, old_registry):
    """На этом уже спотыкались: без уникального индекса история удваивалась."""
    target = tmp_path / "new.db"
    migrate([old_tenders, old_registry], target)
    c = connect(target)
    before = (
        c.execute("SELECT count(*) FROM lots").fetchone()[0],
        c.execute("SELECT count(*) FROM applications").fetchone()[0],
        c.execute("SELECT count(*) FROM history").fetchone()[0],
    )
    c.close()

    second = migrate([old_tenders, old_registry], target)
    c = connect(target)
    after = (
        c.execute("SELECT count(*) FROM lots").fetchone()[0],
        c.execute("SELECT count(*) FROM applications").fetchone()[0],
        c.execute("SELECT count(*) FROM history").fetchone()[0],
    )
    assert before == after
    assert second.lots_added == 0
    assert second.history_added == 0


def test_один_лот_из_двух_источников_не_дублируется(tmp_path, old_tenders):
    """Две копии базы содержат один и тот же лот — в единой он один."""
    copy = tmp_path / "tenders2.db"
    copy.write_bytes(old_tenders.read_bytes())
    target = tmp_path / "new.db"
    report = migrate([old_tenders, copy], target)
    c = connect(target)
    assert c.execute("SELECT count(*) FROM lots").fetchone()[0] == 2
    assert report.lots_added == 2
    assert report.lots_updated + report.lots_seen_again == 2


def test_цифры_отчёта_сходятся(tmp_path, old_tenders):
    """Прочитано = уникальные + повторные. Иначе отчёт выглядит как потеря данных."""
    copy = tmp_path / "tenders2.db"
    copy.write_bytes(old_tenders.read_bytes())
    report = migrate([old_tenders, copy], tmp_path / "new.db")
    read = report.lots_added + report.lots_updated + report.lots_seen_again
    assert read == 4
    assert "Прочитано записей: 4" in report.as_text()


def test_лот_без_номера_пропускается_с_объяснением(tmp_path):
    src = tmp_path / "broken.db"
    c = sqlite3.connect(src)
    c.executescript(OLD_TENDERS_DDL)
    c.execute(
        "INSERT INTO tenders (platform, name, href, lot_id) VALUES ('ROS','Без номера','h',NULL)"
    )
    c.commit()
    c.close()
    report = migrate([src], tmp_path / "new.db")
    assert report.lots_added == 0
    assert report.skipped and "без площадки или номера" in report.skipped[0]


def test_отсутствующий_источник_не_роняет_миграцию(tmp_path, old_tenders):
    report = migrate([tmp_path / "нет.db", old_tenders], tmp_path / "new.db")
    assert report.lots_added == 2
    assert any("не найден" in n for n in report.notes)


def test_старые_базы_не_изменяются(tmp_path, old_tenders, old_registry):
    """Откат миграции — это удаление новой базы, источники должны быть целы."""
    before = (old_tenders.read_bytes(), old_registry.read_bytes())
    migrate([old_tenders, old_registry], tmp_path / "new.db")
    assert (old_tenders.read_bytes(), old_registry.read_bytes()) == before


def test_места_публикации_записаны(tmp_path, old_tenders):
    """FR-003: у лота известно, где он встречен."""
    target = tmp_path / "new.db"
    migrate([old_tenders], target)
    c = connect(target)
    row = c.execute(
        "SELECT * FROM lot_sources WHERE lot_key = 'Росэлторг|111'"
    ).fetchone()
    assert row["platform"] == "Росэлторг"
    assert row["href"] == "https://example.invalid/1"


def test_старая_база_получает_новые_колонки(tmp_path):
    """База пользователя уже существует — ронять её ради нового поля нельзя."""
    import sqlite3

    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.executescript(
        "CREATE TABLE lots (lot_key TEXT PRIMARY KEY, platform TEXT NOT NULL,"
        " lot_id TEXT NOT NULL, score INTEGER DEFAULT 0, promising INTEGER DEFAULT 0);"
    )
    old.execute("INSERT INTO lots (lot_key, platform, lot_id) VALUES ('ROS|1','ROS','1')")
    old.commit()
    old.close()

    c = connect(p)
    ensure_schema(c)
    columns = {row[1] for row in c.execute("PRAGMA table_info(lots)")}
    assert {"core_profile", "below_threshold"} <= columns
    assert c.execute("SELECT count(*) FROM lots").fetchone()[0] == 1, "данные на месте"
