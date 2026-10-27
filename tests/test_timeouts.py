"""Таймауты площадок и прогона (ревью 16.10.2026, Д2).

27–30.09 и 10–11.10 прогоны шли по три часа: площадка без связи висела,
и всё остальное ждало её. Теперь у каждой площадки свой предел
(platform_timeout_min, по умолчанию 90), у прогона — общий (run_timeout_min,
180): зависшая площадка получает отчёт «не уложилась в время», а лоты
остальных сохраняются.
"""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

from src.core.parser_runner import ParserRunner
from src.parsers import completeness as c
from src.core import context as ctx
from src.core import collect


def _runner(**settings):
    base = {
        "parse_bidzaar": True,
        "parse_tektorg": True,
        "keyword_filter_enabled": False,
        "network_retry": False,
        "include_no_price": True,
        "min_price": 0,
    }
    base.update(settings)
    return ParserRunner(browser_manager=None, companies=["АО Рудник"], settings=base)


def _wire(runner):
    async def fast():
        runner.unit_reports.append(
            c.build_unit_report(
                "phrase", "ф", platform="Bidzaar", expected_pages=1, collected_pages=1
            )
        )
        return [{"platform": "Bidzaar", "name": "Герметик", "href": "https://bz/1"}]

    async def hang():
        await asyncio.sleep(30)
        return [{"platform": "ТЭК-Торг", "name": "никогда"}]

    runner._run_bidzaar = fast
    runner._run_tektorg = hang


def test_зависшая_площадка_прерывается_а_лоты_остальных_остаются():
    runner = _runner(platform_timeout_min=0.002)  # ~0,1 с
    _wire(runner)
    started = time.monotonic()
    result = asyncio.run(runner.run_all())
    assert time.monotonic() - started < 10
    assert [t["href"] for t in result] == ["https://bz/1"]
    tek = [r for r in runner.unit_reports if r["platform"] == "ТЭК-Торг"]
    assert [(r["kind"], r["reason"]) for r in tek] == [
        (c.KIND_PLATFORM, c.REASON_TIMEOUT)
    ]
    assert runner.platform_lots == {"Bidzaar": 1, "ТЭК-Торг": 0}


def test_срок_прогона_ограничивает_площадки():
    runner = _runner()  # предел площадки — 90 мин по умолчанию
    runner.deadline = time.monotonic() + 0.1
    _wire(runner)
    started = time.monotonic()
    result = asyncio.run(runner.run_all())
    assert time.monotonic() - started < 10
    assert len(result) == 1
    assert any(r["reason"] == c.REASON_TIMEOUT for r in runner.unit_reports)


def test_причина_таймаута_словами():
    assert "не уложилась" in c.reason_text(c.REASON_TIMEOUT)


# ── Предел всего прогона в main_ui ──────────────────────────────────────────


def test_прогон_сверх_срока_прерывается_ошибкой(monkeypatch, tmp_path):
    import src.core.parser_runner as pr
    import src.keywords_store as ks
    import src.settings_store as ss
    import src.utils.browser_manager as bm
    from src import run_journal

    monkeypatch.setattr(collect, "_parse_state",
        {
            **collect._parse_state,
            "running": False,
            "stopping": False,
            "stopped": False,
            "error": None,
            "progress": [],
            "tenders": [],
            "excel_path": None,
            "data_saved": False,
        },
    )
    monkeypatch.setattr(collect, "_parse_cancel", {"loop": None, "task": None})
    monkeypatch.setattr(ctx, "UNIFIED_DB_PATH", tmp_path / "unified.db")
    monkeypatch.setattr(run_journal, "journal_dir", lambda: tmp_path / "journal")
    monkeypatch.setattr(ctx, "load_companies", list)
    monkeypatch.setattr(ks, "enabled_phrases", list)
    monkeypatch.setattr(
        ss, "load_settings", lambda: {"parse_bidzaar": True, "run_timeout_min": 0.002}
    )
    monkeypatch.setattr(collect, "RUN_TIMEOUT_GRACE_S", 0)
    browser = Mock(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(bm, "BrowserManager", Mock(return_value=browser))

    async def hang():
        await asyncio.sleep(30)

    runner = Mock(unit_reports=[], run_all=hang)
    monkeypatch.setattr(pr, "ParserRunner", Mock(return_value=runner))

    started = time.monotonic()
    assert collect._run_collect_once() == collect.EXIT_ERROR
    assert time.monotonic() - started < 15
    assert runner.deadline > 0
    record = run_journal.read_last_run()
    assert record["outcome"] == run_journal.OUTCOME_ERROR
    assert "не уложился" in record["error"]
    browser.close.assert_awaited_once()
