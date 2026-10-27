"""Плитка «Сегодня» считает по единой базе (рефакторинг 16.10.2026).

Раньше она читала старую tenders.db, куда сбор писал только ради этой
плитки; лента, Excel и журнал давно живут в tenderwork.db. «Сегодня» —
лоты, впервые увиденные с полуночи по Москве (сроки и «сейчас» в программе
московские); first_seen_at хранится по часам ПК.
"""

from datetime import datetime, time, timedelta

import pytest

import main_ui
from src.core import context as ctx
from src.dates import MSK
from src.db import connect, ensure_schema
from src.db.runs import new_lots_today


def _local(msk: datetime) -> str:
    """Московское время → метка first_seen_at (часы ПК, без пояса)."""
    return (
        msk.replace(tzinfo=MSK)
        .astimezone()
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def _fill(path, now_msk):
    conn = connect(path)
    ensure_schema(conn)
    midnight = datetime.combine(now_msk.date(), time())
    for key, seen in (
        ("вчера", midnight - timedelta(minutes=1)),
        ("ночью", midnight + timedelta(minutes=1)),
        ("утром", now_msk - timedelta(hours=1)),
    ):
        conn.execute(
            "INSERT INTO lots (lot_key, platform, lot_id, first_seen_at, last_seen_at)"
            " VALUES (?, 'Росэлторг', ?, ?, ?)",
            (f"Росэлторг|{key}", key, _local(seen), _local(seen)),
        )
    conn.commit()
    return conn


def test_сегодня_это_с_полуночи_по_москве(tmp_path):
    now = datetime(2026, 10, 16, 12, 0)
    conn = _fill(tmp_path / "t.db", now)
    assert new_lots_today(conn, now_msk=now) == 2
    conn.close()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(ctx, "UNIFIED_DB_PATH", tmp_path / "tenderwork.db")
    main_ui.app.config.update(TESTING=True)
    return main_ui.app.test_client()


def test_плитка_сегодня_из_единой_базы(client, tmp_path, monkeypatch):
    from src import dates

    now = dates.now_msk()
    _fill(tmp_path / "tenderwork.db", now).close()
    expected = 2 if (now - timedelta(hours=1)).date() == now.date() else 1
    assert client.get("/api/status").get_json()["counters"]["tenders_today"] == expected


def test_плитка_без_базы_ноль(client):
    assert client.get("/api/status").get_json()["counters"]["tenders_today"] == 0
