"""
Метки времени в одном формате ISO с «T» (ревью данных 16.10.2026, Г3).

В живой базе их было три: «2026-08-27 06:30» (миграция 27.08),
«2026-08-31T15:38:04» и с микросекундами. Метки сравниваются строками
(«новое с начала прогона», «площадка не видела лот после…»), а пробел
меньше «T» — и «27.08 08:44» оказывался раньше «27.08 00:00».
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _mixed(conn):
    conn.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, first_seen_at, last_seen_at,"
        " created_at, updated_at) VALUES ('B|1', 'B', '1', '2026-08-27 06:30',"
        " '2026-08-31T10:16:55.421258', '2026-08-27 06:30', '2026-10-12T14:43:37')"
    )
    conn.execute(
        "INSERT INTO lot_sources (lot_key, platform, seen_at)"
        " VALUES ('B|1', 'B', '2026-08-27 08:44')"
    )
    conn.execute(
        "INSERT INTO history (lot_key, from_status, to_status, at)"
        " VALUES ('B|1', NULL, 'SELECTED', '2026-08-27 06:37')"
    )
    conn.execute(
        "INSERT INTO applications (lot_key, status, created_at, updated_at)"
        " VALUES ('B|1', 'SELECTED', '2026-08-27 06:37', '2026-08-27 06:37')"
    )
    conn.execute("DELETE FROM meta WHERE key = 'timestamps_iso'")
    conn.commit()


def _snapshot(conn):
    return (
        tuple(
            conn.execute(
                "SELECT first_seen_at, last_seen_at, created_at, updated_at FROM lots"
            ).fetchone()
        ),
        conn.execute("SELECT seen_at FROM lot_sources").fetchone()[0],
        conn.execute("SELECT at FROM history").fetchone()[0],
        tuple(
            conn.execute("SELECT created_at, updated_at FROM applications").fetchone()
        ),
    )


def test_старые_метки_приводятся_к_iso_с_t(db):
    _mixed(db)
    ensure_schema(db)
    assert _snapshot(db) == (
        (
            "2026-08-27T06:30:00",
            "2026-08-31T10:16:55",
            "2026-08-27T06:30:00",
            "2026-10-12T14:43:37",
        ),
        "2026-08-27T08:44:00",
        "2026-08-27T06:37:00",
        ("2026-08-27T06:37:00", "2026-08-27T06:37:00"),
    )
    assert (
        db.execute("SELECT value FROM meta WHERE key = 'timestamps_iso'").fetchone()
        is not None
    )


def test_миграция_меток_идемпотентна(db):
    _mixed(db)
    ensure_schema(db)
    first = _snapshot(db)
    # И с отметкой, и без неё повторный прогон ничего не меняет.
    ensure_schema(db)
    db.execute("DELETE FROM meta WHERE key = 'timestamps_iso'")
    db.commit()
    ensure_schema(db)
    assert _snapshot(db) == first


def test_совпавшая_после_приведения_история_не_роняет_запуск(db):
    """Тот же переход в той же минуте в двух форматах упёрся бы в
    уникальный индекс истории — запуск программы падать не должен."""
    _mixed(db)
    for at in ("2026-08-28 10:00", "2026-08-28T10:00:00"):
        db.execute(
            "INSERT INTO history (lot_key, from_status, to_status, at)"
            " VALUES ('B|1', 'SELECTED', 'ASSESSED', ?)",
            (at,),
        )
    db.commit()
    ensure_schema(db)
    assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 3


def test_реестр_пишет_метки_в_iso(db):
    from src.registry import Registry

    db.execute("INSERT INTO lots (lot_key, platform, lot_id) VALUES ('B|2', 'B', '2')")
    db.commit()
    Registry(db).take("B|2", "Пётр")
    at = db.execute("SELECT at FROM history").fetchone()[0]
    created = db.execute("SELECT created_at FROM applications").fetchone()[0]
    assert len(at) == 19 and at[10] == "T"
    assert created == at
