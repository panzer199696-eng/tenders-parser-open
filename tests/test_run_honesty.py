"""Честные итоги прогона (ревью 16.10.2026, В1).

Инциденты: журнал писал «434 объектов обойдены целиком», хотя у 75 объём
был неизвестен; в run_platforms лежало число ОБЪЕКТОВ обхода, и ноль лотов
площадки за ним не был виден; раннер писал в лог «все 0 объектов обойдены
целиком». Теперь по каждой площадке хранятся лоты до фильтров, сколько из
них впервые увидены, самая свежая публикация и причина кодом.
"""

import asyncio
import json
from datetime import datetime

from src import run_journal
from src.core.parser_runner import ParserRunner
from src.db import connect, ensure_schema
from src.db.runs import new_lots_by_platform, record_run
from src.parsers import completeness as c

СТАРТ = datetime(2026, 10, 16, 3, 0, 1)


def _ok(platform, label="фраза"):
    return c.build_unit_report(
        "phrase", label, platform=platform, expected_pages=1, collected_pages=1
    )


def _unknown(platform, label="фраза"):
    return c.build_unit_report("phrase", label, platform=platform)


def _record(reports, **extra):
    return run_journal.build_record(
        started_at=СТАРТ,
        finished_at=datetime(2026, 10, 16, 3, 30),
        app_version="test",
        outcome=run_journal.OUTCOME_OK,
        settings={"parse_bidzaar": True, "parse_tektorg": True},
        unit_reports=reports,
        lots_found=10,
        lots_new=2,
        **extra,
    )


# ── Запись прогона: лоты, новые, свежайшая публикация, причина кодом ────────


def test_строка_площадки_несёт_лоты_новые_свежайшую_и_причину():
    rec = _record(
        [_ok("Bidzaar"), c.platform_unit("ТЭК-Торг", c.REASON_FORMAT_CHANGED)],
        platform_lots={"Bidzaar": 158, "ТЭК-Торг": 0},
        platform_new={"Bidzaar": 4},
        platform_newest={"Bidzaar": "2026-10-13"},
    )
    rows = {r["platform"]: r for r in rec["by_platform"]}
    assert rows["Bidzaar"]["lots"] == 158
    assert rows["Bidzaar"]["lots_new"] == 4
    assert rows["Bidzaar"]["newest_published"] == "2026-10-13"
    assert rows["Bidzaar"]["reason_code"] is None
    assert rows["ТЭК-Торг"]["lots"] == 0
    # нет записи о новых у площадки с лотами — значит, новых ноль
    assert rows["ТЭК-Торг"]["lots_new"] == 0
    assert rows["ТЭК-Торг"]["newest_published"] is None
    assert rows["ТЭК-Торг"]["reason_code"] == c.REASON_FORMAT_CHANGED


def test_без_данных_о_лотах_поля_неизвестны_а_не_ноль():
    rows = _record([_ok("Bidzaar")])["by_platform"]
    assert rows[0]["lots"] is None
    assert rows[0]["lots_new"] is None


def test_причина_кодом_главная_нужна_правка_важнее_обрыва():
    net = c.build_unit_report(
        "phrase",
        "а",
        platform="ТЭК-Торг",
        expected_pages=2,
        collected_pages=1,
        reason=c.REASON_NETWORK,
    )
    rec = _record([net, c.platform_unit("ТЭК-Торг", c.REASON_BLOCKED)])
    row = rec["by_platform"][0]
    assert row["reason_code"] == c.REASON_BLOCKED
    assert row["reason_codes"] == [c.REASON_NETWORK, c.REASON_BLOCKED]


def test_run_platforms_получает_новые_колонки(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    rec = _record(
        [_ok("Bidzaar"), c.platform_unit("ТЭК-Торг", c.REASON_EMPTY_PLATFORM)],
        platform_lots={"Bidzaar": 158, "ТЭК-Торг": 0},
        platform_new={"Bidzaar": 4},
        platform_newest={"Bidzaar": "2026-10-13"},
    )
    run_id = record_run(conn, rec)
    got = {
        r["platform"]: dict(r)
        for r in conn.execute(
            "SELECT platform, lots, lots_new, newest_published, reason_code"
            " FROM run_platforms WHERE run_id = ?",
            (run_id,),
        )
    }
    conn.close()
    assert got["Bidzaar"] == {
        "platform": "Bidzaar",
        "lots": 158,
        "lots_new": 4,
        "newest_published": "2026-10-13",
        "reason_code": None,
    }
    assert got["ТЭК-Торг"]["lots"] == 0
    assert got["ТЭК-Торг"]["reason_code"] == c.REASON_EMPTY_PLATFORM


def test_новые_по_площадкам_считаются_от_начала_прогона(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    for key, platform, seen in (
        ("a", "Bidzaar", "2026-10-15T03:10:00"),
        ("b", "Bidzaar", "2026-10-16T03:10:00"),
        ("c", "ТЭК-Торг", "2026-10-16T03:20:00"),
        ("d", "ТЭК-Торг", "2026-10-16T03:21:00"),
    ):
        conn.execute(
            "INSERT INTO lots (lot_key, platform, lot_id, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"{platform}|{key}", platform, key, seen, seen),
        )
    conn.commit()
    assert new_lots_by_platform(conn, СТАРТ) == {"Bidzaar": 1, "ТЭК-Торг": 2}
    conn.close()


# ── Свежайшая публикация площадки ───────────────────────────────────────────


def test_свежайшая_публикация_разбирается_во_всех_форматах():
    tenders = [
        {"platform": "Tender.pro", "date_published": "15.10.2026 10:00"},
        {"platform": "Tender.pro", "date_published": "14.10.2026"},
        {"platform": "Bidzaar", "date_published": "2026-10-13T22:00:00+00:00"},
        {"platform": "Bidzaar", "date_published": "Не указана"},
        {"platform": "Росэлторг", "date_published": None},
    ]
    assert run_journal.newest_by_platform(tenders) == {
        "Tender.pro": "2026-10-15",
        # 22:00 UTC — это уже 14.10 по Москве
        "Bidzaar": "2026-10-14",
    }


def test_раннер_запоминает_свежайшую_публикацию_до_фильтров():
    runner = ParserRunner(
        browser_manager=None,
        companies=["АО Рудник"],
        settings={
            "parse_bidzaar": True,
            "keyword_filter_enabled": False,
            "network_retry": False,
            # фильтр цены отсечёт всё — свежайшая публикация всё равно видна
            "min_price": 10**12,
            "include_no_price": False,
        },
    )

    async def fake():
        runner.unit_reports.append(_ok("Bidzaar"))
        return [
            {"platform": "Bidzaar", "name": "x", "date_published": "12.10.2026"},
            {"platform": "Bidzaar", "name": "y", "date_published": "13.10.2026"},
        ]

    runner._run_bidzaar = fake
    assert asyncio.run(runner.run_all()) == []
    assert runner.platform_lots == {"Bidzaar": 2}
    assert runner.platform_newest == {"Bidzaar": "2026-10-13"}


# ── Сводка словами: «целиком» и «объём неизвестен» раздельно ───────────────


def test_сводка_не_выдаёт_неизвестный_объём_за_полный():
    reports = [_ok("Tender.pro", f"ф{i}") for i in range(3)] + [
        _unknown("ЕИС", f"у{i}") for i in range(2)
    ]
    text = _record(reports)["summary"]
    assert "5 объектов обойдены целиком" not in text
    assert "3 объектов обойдены целиком" in text
    assert "у 2 объём неизвестен" in text
    assert "данные полные" in text


def test_раннер_не_пишет_все_0_объектов_целиком(caplog):
    runner = ParserRunner(
        browser_manager=None,
        companies=["АО Рудник"],
        settings={
            "parse_zakupki": True,
            "keyword_filter_enabled": False,
            "network_retry": False,
        },
    )

    async def fake():
        runner.unit_reports.append(_unknown("ЕИС"))
        return [{"platform": "ЕИС", "name": "x"}]

    runner._run_zakupki = fake
    with caplog.at_level("INFO"):
        asyncio.run(runner.run_all())
    assert "все 0 объектов" not in caplog.text
    assert "объём неизвестен у 1" in caplog.text


# ── runs.jsonl не раздувается списком неполных объектов ─────────────────────


def test_история_обрезает_неполные_объекты_до_50(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "journal_dir", lambda: tmp_path)
    bad = [
        c.failed_unit("phrase", f"ф{i}", "Bidzaar", c.REASON_NETWORK)
        for i in range(120)
    ]
    run_journal.write_run(_record(bad))
    line = json.loads((tmp_path / run_journal.HISTORY_NAME).read_text("utf-8"))
    assert len(line["incomplete_units"]) == 50
    assert line["incomplete_units_total"] == 120
    last = run_journal.read_last_run(tmp_path / run_journal.LAST_RUN_NAME)
    assert len(last["incomplete_units"]) == 120
