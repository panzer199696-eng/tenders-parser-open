"""
Корзина «Пропал — проверить» (решение владельца 16.10.2026, Г5).

Лот, снятый площадкой (отмена, перенос под новым номером), до сих пор
висел в «Горит» и «Профиле» до своего срока: программа помнила, что его
видела, и не замечала, что больше не видит. Признак «пропал» — площадка,
которая отдаёт ВСЁ активное (Росэлторг Center, Bidzaar, Росэлторг, ТЭК-Торг),
обошлась полностью и с лотами хотя бы два прогона подряд после последней
встречи лота — а лота в выдаче не было.

Tender.pro не участвует: у него окно по дате публикации, и старый
лот выпадает из выдачи, оставаясь живым. Ничего не удаляется.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, load_feed
from src.db.feed import missing_lot_keys

NOW = datetime(2026, 10, 16, 10, 0)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _lot(conn, lot_id, sources, deadline="30.10.2026 10:00"):
    """Лот с местами публикации: sources — [(площадка, seen_at)]."""
    key = f"{sources[0][0]}|{lot_id}"
    conn.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, name, date_deadline, score,"
        " tags, core_profile, last_seen_at) VALUES (?, ?, ?, ?, ?, 30,"
        " '[\"reagents\"]', 1, ?)",
        (
            key,
            sources[0][0],
            lot_id,
            f"Лот {lot_id}",
            deadline,
            max(s for _, s in sources),
        ),
    )
    for platform, seen in sources:
        conn.execute(
            "INSERT INTO lot_sources (lot_key, platform, lot_id, seen_at)"
            " VALUES (?, ?, ?, ?)",
            (key, platform, lot_id, seen),
        )
    conn.commit()
    return key


def _run(conn, started, platforms, trigger=None, outcome="ok"):
    """Прогон: platforms — {площадка: (complete, lots)}."""
    cur = conn.execute(
        "INSERT INTO runs (started_at, finished_at, outcome, trigger)"
        " VALUES (?, ?, ?, ?)",
        (started, started, outcome, trigger),
    )
    for platform, (complete, lots) in platforms.items():
        conn.execute(
            "INSERT INTO run_platforms (run_id, platform, complete, lots)"
            " VALUES (?, ?, ?, ?)",
            (cur.lastrowid, platform, complete, lots),
        )
    conn.commit()


ROS_OK = {"Росэлторг": (1, 120)}


def test_две_полные_ночи_без_лота_пропал(db):
    key = _lot(db, "1", [("Росэлторг", "2026-10-14T03:30:00")])
    _run(db, "2026-10-15T03:00:00", ROS_OK)
    _run(db, "2026-10-16T03:00:00", ROS_OK)
    assert missing_lot_keys(db) == {key}
    feed = load_feed(db, now=NOW, today=NOW.date())
    assert [lot["lot_key"] for lot in feed["buckets"]["missing"]] == [key]
    assert feed["counts"]["core"] == 0
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1, (
        "ничего не удаляем"
    )


def test_одной_ночи_мало(db):
    _lot(db, "1", [("Росэлторг", "2026-10-15T03:30:00")])
    _run(db, "2026-10-15T03:00:00", ROS_OK)  # в этом прогоне лот видели
    _run(db, "2026-10-16T03:00:00", ROS_OK)
    assert missing_lot_keys(db) == set()


@pytest.mark.parametrize(
    "trigger,outcome,platform_row",
    [
        ("catchup", "ok", (1, 120)),  # догон не ночной прогон
        (None, "error", (1, 120)),  # прогон не довёлся
        (None, "ok", (0, 120)),  # площадка собрана не целиком
        (None, "ok", (1, 0)),  # площадка дала ноль — сломана, а не пуста
        (None, "ok", (1, None)),  # до 16.10.2026 лоты не записывались
    ],
)
def test_неполноценный_прогон_не_считается(db, trigger, outcome, platform_row):
    _lot(db, "1", [("Росэлторг", "2026-10-14T03:30:00")])
    _run(db, "2026-10-15T03:00:00", ROS_OK)
    _run(db, "2026-10-16T03:00:00", {"Росэлторг": platform_row}, trigger, outcome)
    assert missing_lot_keys(db) == set()


def test_площадки_с_окном_по_публикации_не_участвуют(db):
    _lot(db, "1", [("Tender.pro", "2026-10-01T03:30:00")])
    full = {"Tender.pro": (1, 40)}
    _run(db, "2026-10-15T03:00:00", full)
    _run(db, "2026-10-16T03:00:00", full)
    assert missing_lot_keys(db) == set()


def test_нужны_все_источники(db):
    """Лот, который Росэлторг больше не показывает, а Bidzaar показывает, — жив."""
    _lot(
        db,
        "1",
        [("Росэлторг", "2026-10-14T03:30:00"), ("Bidzaar", "2026-10-16T03:10:00")],
    )
    both = {"Росэлторг": (1, 120), "Bidzaar": (1, 100)}
    _run(db, "2026-10-15T03:00:00", both)
    _run(db, "2026-10-16T03:00:00", both)
    assert missing_lot_keys(db) == set()


def test_все_источники_молчат_пропал(db):
    key = _lot(
        db,
        "1",
        [("Росэлторг", "2026-10-13T03:30:00"), ("Bidzaar", "2026-10-13T03:10:00")],
    )
    both = {"Росэлторг": (1, 120), "Bidzaar": (1, 100)}
    _run(db, "2026-10-14T03:00:00", both)
    _run(db, "2026-10-15T03:00:00", {"Росэлторг": (1, 120), "Bidzaar": (1, 0)})
    _run(db, "2026-10-16T03:00:00", both)
    # У Bidzaar полноценных ночей после встречи только две (14-е и 16-е) —
    # этого хватает; у Росэлторг — три.
    assert missing_lot_keys(db) == {key}


def test_свежая_встреча_на_неучаствующей_площадке_оставляет_лот_живым(db):
    _lot(
        db,
        "1",
        [("Росэлторг", "2026-10-13T03:30:00"), ("Tender.pro", "2026-10-16T03:20:00")],
    )
    _run(db, "2026-10-15T03:00:00", ROS_OK)
    _run(db, "2026-10-16T03:00:00", ROS_OK)
    assert missing_lot_keys(db) == set()


def test_истёкший_остаётся_истёкшим(db):
    _lot(db, "1", [("Росэлторг", "2026-10-10T03:30:00")], deadline="15.10.2026 10:00")
    _run(db, "2026-10-15T03:00:00", ROS_OK)
    _run(db, "2026-10-16T03:00:00", ROS_OK)
    feed = load_feed(db, now=NOW, today=NOW.date())
    assert feed["counts"]["expired"] == 1
    assert feed["counts"]["missing"] == 0


def test_взятый_в_работу_не_пропадает_из_реестра(db):
    key = _lot(db, "1", [("Росэлторг", "2026-10-14T03:30:00")])
    db.execute(
        "INSERT INTO applications (lot_key, status) VALUES (?, 'SELECTED')", (key,)
    )
    db.commit()
    _run(db, "2026-10-15T03:00:00", ROS_OK)
    _run(db, "2026-10-16T03:00:00", ROS_OK)
    assert missing_lot_keys(db) == set()


def test_корзина_свёрнута_и_стоит_перед_истёкшими(db):
    feed = load_feed(db, now=NOW, today=NOW.date())
    keys = [t["key"] for t in feed["titles"]]
    assert keys.index("missing") == keys.index("expired") - 1
    missing = next(t for t in feed["titles"] if t["key"] == "missing")
    assert missing["collapsed"] is True
    assert missing["title"] == "Пропал — проверить"
