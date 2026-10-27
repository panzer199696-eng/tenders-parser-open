"""
Глубина обхода по дате публикации (Д1, 16.10.2026).

Tender.pro листал только days_back=1: после ночи без связи лоты
пропущенного дня не собирались никогда. Теперь глубина = дней с последнего
прогона, где площадка собрана полностью (runs.outcome='ok',
run_platforms.complete=1), + 1, не больше 7; база читается только на
чтение; любая ошибка — прежний days_back.
"""

import asyncio
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.core.parser_runner as pr
from src.core.parser_runner import ParserRunner, collection_depth

TODAY = date(2026, 10, 16)


def _db(tmp_path, rows):
    """База с runs/run_platforms: rows — (started_at, outcome, platform, complete)."""
    path = tmp_path / "tenderwork.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT, outcome TEXT)"
    )
    conn.execute(
        "CREATE TABLE run_platforms (run_id INTEGER, platform TEXT, complete INTEGER)"
    )
    for i, (started, outcome, platform, complete) in enumerate(rows, 1):
        conn.execute("INSERT INTO runs VALUES (?, ?, ?)", (i, started, outcome))
        conn.execute(
            "INSERT INTO run_platforms VALUES (?, ?, ?)", (i, platform, complete)
        )
    conn.commit()
    conn.close()
    return path


def test_вчерашний_полный_прогон_даёт_два_дня(tmp_path):
    db = _db(tmp_path, [("2026-10-15T03:00:01", "ok", "Tender.pro", 1)])
    assert collection_depth("Tender.pro", 1, db_path=db, today=TODAY) == 2


def test_ночь_без_связи_добавляет_день(tmp_path):
    db = _db(
        tmp_path,
        [
            ("2026-10-14T03:00:01", "ok", "Tender.pro", 1),
            ("2026-10-15T03:00:01", "ok", "Tender.pro", 0),  # обрыв связи
            ("2026-10-15T07:00:01", "error", "Tender.pro", 1),  # прогон упал
        ],
    )
    assert collection_depth("Tender.pro", 1, db_path=db, today=TODAY) == 3


def test_не_больше_семи_дней(tmp_path):
    db = _db(tmp_path, [("2026-09-20T03:00:01", "ok", "Tender.pro", 1)])
    assert collection_depth("Tender.pro", 1, db_path=db, today=TODAY) == 7


def test_площадка_ни_разу_не_собрана_целиком_семь_дней(tmp_path):
    db = _db(tmp_path, [("2026-10-15T03:00:01", "ok", "Росэлторг", 1)])
    assert collection_depth("Tender.pro", 1, db_path=db, today=TODAY) == 7


def test_не_меньше_настройки(tmp_path):
    db = _db(tmp_path, [("2026-10-15T03:00:01", "ok", "Tender.pro", 1)])
    assert collection_depth("Tender.pro", 10, db_path=db, today=TODAY) == 10


def test_нет_базы_прежний_days_back(tmp_path):
    assert (
        collection_depth("Tender.pro", 1, db_path=tmp_path / "nope.db", today=TODAY)
        == 1
    )


def test_битая_база_прежний_days_back(tmp_path):
    path = tmp_path / "tenderwork.db"
    sqlite3.connect(path).close()  # пустая база без таблиц
    assert collection_depth("Tender.pro", 1, db_path=path, today=TODAY) == 1


def test_база_только_читается(tmp_path):
    db = _db(tmp_path, [("2026-10-15T03:00:01", "ok", "Tender.pro", 1)])
    before = db.read_bytes()
    collection_depth("Tender.pro", 1, db_path=db, today=TODAY)
    assert db.read_bytes() == before


class _Capture:
    instances: list = []

    def __init__(self, browser_manager, companies, days_back, **kw):
        self.days_back = days_back
        type(self).instances.append(self)

    async def parse(self):
        return []


def test_раннер_передаёт_глубину_tender_pro(monkeypatch):
    _Capture.instances = []
    monkeypatch.setattr(pr, "TenderProParser", _Capture)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: ["насос"])
    seen = []

    def fake_depth(platform, default, **kw):
        seen.append((platform, default))
        return 4

    monkeypatch.setattr(pr, "collection_depth", fake_depth)
    runner = ParserRunner(None, days_back=1, settings={}, companies=["Ромашка"])
    asyncio.run(runner._run_tender_pro())
    assert [c.days_back for c in _Capture.instances] == [4]
    assert seen == [("Tender.pro", 1)]
