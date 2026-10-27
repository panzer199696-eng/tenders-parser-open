"""Регрессии ревью 10.09.2026: FR-001–004, только временные данные и подмены."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

import main_ui as ui
from src.core import collect
from src.core import context as ctx
from src import run_journal
from src.parsers import completeness as comp


@pytest.mark.parametrize("reason", [comp.REASON_NETWORK, comp.REASON_PAGE_CAP,
                                    comp.REASON_STOPPED, comp.REASON_RATE_LIMITED])
def test_unknown_volume_does_not_hide_failure(reason):
    report = comp.build_unit_report(
        "phrase", "Насосы", platform="ЕИС", collected_pages=1,
        collected_lots=20, reason=reason,
    )
    assert report["status"] == comp.UNIT_PARTIAL
    assert report["reason"] == reason
    assert comp.summarize_units([report])["data_complete"] is False
    assert comp.summarize_by_platform([report])[0]["data_complete"] is False
    state = collect._build_completeness([report], lambda _: None)
    assert state["data_complete"] is False
    record = run_journal.build_record(
        started_at=datetime.now(), finished_at=datetime.now(), app_version="test",
        outcome=run_journal.OUTCOME_OK, settings={}, unit_reports=[report],
        lots_found=20, lots_new=20,
    )
    assert comp.reason_text(reason) in run_journal.describe(record)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Настоящие временные БД/журнал, но никаких браузеров и диагностики."""
    import src.core.parser_runner as pr
    import src.utils.browser_manager as bm
    import src.settings_store as ss
    import src.keywords_store as ks
    import src.profile as profile

    monkeypatch.setattr(collect, "_parse_state", {
        **collect._parse_state, "running": False, "stopping": False, "stopped": False,
        "error": None, "progress": [], "tenders": [], "excel_path": None,
        "data_saved": False,
    })
    monkeypatch.setattr(collect, "_parse_cancel", {"loop": None, "task": None})
    monkeypatch.setattr(ctx, "UNIFIED_DB_PATH", tmp_path / "unified.db")
    monkeypatch.setattr(run_journal, "journal_dir", lambda: tmp_path / "journal")
    monkeypatch.setattr(collect, "_preflight_problems", lambda: [])
    monkeypatch.setattr(ctx, "load_companies", lambda: [])
    monkeypatch.setattr(ks, "enabled_phrases", lambda: [])
    settings = {"parse_roseltorg": True, "schedule": {"enabled": True}}
    monkeypatch.setattr(ss, "load_settings", lambda: settings)
    monkeypatch.setattr(profile, "Profile", lambda: None)

    browser = Mock()
    browser.start = AsyncMock()
    browser.close = AsyncMock()
    monkeypatch.setattr(bm, "BrowserManager", Mock(return_value=browser))
    tenders = [{"platform": "Росэлторг", "lot_id": "test-123",
                "href": "https://example.invalid/test-123", "name": "Насосы"}]
    runner = Mock(unit_reports=[comp.build_unit_report(
        "firm", "Тест", platform="Росэлторг", expected_pages=1, collected_pages=1,
    )])
    runner.run_all = AsyncMock(return_value=tenders)
    monkeypatch.setattr(pr, "ParserRunner", Mock(return_value=runner))
    excel = Mock(return_value=tmp_path / "report.xlsx")
    monkeypatch.setattr(collect, "_save_excel_file", excel)
    return {"settings": settings, "browser": browser, "tenders": tenders,
            "runner": runner, "excel": excel}


def test_catchup_reserves_run_before_thread_starts(run_env, monkeypatch):
    import src.schedule as schedule

    monkeypatch.setattr(schedule, "normalize", lambda x: x)
    monkeypatch.setattr(schedule, "missed_run", lambda *a: datetime(2026, 9, 10, 3))
    seen = []
    thread = Mock()
    thread.start.side_effect = lambda: seen.append(collect._parse_state["running"])
    factory = Mock(return_value=thread)
    monkeypatch.setattr(collect.threading, "Thread", factory)
    collect._catch_up_if_needed()
    assert seen == [True]
    assert collect._parse_state["started_at"]
    with ui.app.test_client() as client:
        assert client.post("/api/parse/start", json={"force": True}).status_code == 409
    collect._catch_up_if_needed()
    assert factory.call_count == 1


def test_failed_thread_start_releases_run(run_env, monkeypatch):
    thread = Mock()
    thread.start.side_effect = RuntimeError("cannot start thread")
    monkeypatch.setattr(collect.threading, "Thread", Mock(return_value=thread))
    ui.app.config["TESTING"] = False
    with ui.app.test_client() as client:
        response = client.post("/api/parse/start", json={"force": True})
    assert response.status_code == 500
    assert response.is_json
    assert collect._parse_state["running"] is False
    assert collect._parse_state["error"]


def test_cancel_before_browser_is_journaled(run_env, monkeypatch):
    # Изолированная регрессия именно ранней отмены, независимо от старта CLI:
    # отмена прилетает на проверке связи, до запуска браузера.
    import src.core.net_preflight as net_preflight

    collect._parse_state["running"] = True
    monkeypatch.setattr(
        net_preflight, "describe", Mock(side_effect=asyncio.CancelledError())
    )
    collect._run_parse_async([])
    assert collect._parse_state["running"] is False
    assert collect._parse_state["stopped"] is True
    assert run_journal.read_last_run()["outcome"] == run_journal.OUTCOME_STOPPED
    run_env["browser"].start.assert_not_awaited()


def test_stop_during_preflight_finishes_and_allows_next_run(run_env, monkeypatch):
    import src.core.net_preflight as net_preflight

    def stop_while_checking(settings):
        # Проверка связи идёт в отдельном потоке: «Стоп» жмут, пока она
        # ещё не вернулась, — отмена прилетает именно в это ожидание.
        with ui.app.test_client() as client:
            response = client.post("/api/parse/stop")
        assert response.status_code == 200
        return {}

    monkeypatch.setattr(net_preflight, "check_platforms", stop_while_checking)
    assert collect._run_collect_once() != 0
    assert collect._parse_state["running"] is False
    assert collect._parse_state["stopping"] is False
    assert collect._parse_state["stopped"] is True
    assert collect._parse_cancel == {"loop": None, "task": None}
    assert run_journal.read_last_run()["outcome"] == run_journal.OUTCOME_STOPPED
    run_env["browser"].start.assert_not_awaited()
    monkeypatch.setattr(net_preflight, "check_platforms", lambda settings: {})
    assert collect._run_collect_once() == 0
    assert collect._parse_state["stopped"] is False


def test_early_settings_error_is_failure_in_cli_and_journal(run_env, monkeypatch):
    import src.settings_store as ss

    monkeypatch.setattr(ss, "load_settings", Mock(side_effect=OSError("settings error")))
    assert collect._run_collect_once() == 1
    assert collect._parse_state["running"] is False
    assert "settings error" in collect._parse_state["error"]
    assert run_journal.read_last_run()["outcome"] == run_journal.OUTCOME_ERROR


# Сбой Excel с 16.10.2026 — не ошибка прогона, а предупреждение (В2):
# см. tests/test_excel_after_journal.py.
@pytest.mark.parametrize("stage", ["connect", "schema", "save"])
def test_storage_failure_is_visible_and_preserves_collected_data(run_env, monkeypatch, stage):
    import src.db as db

    failure = OSError("simulated storage failure")
    target = {"connect": "connect", "schema": "ensure_schema", "save": "save_lots"}[stage]
    monkeypatch.setattr(db, target, Mock(side_effect=failure))

    assert collect._run_collect_once() == 1
    assert collect._parse_state["running"] is False
    assert collect._parse_state["error"]
    assert collect._parse_state["tenders"] == run_env["tenders"]
    assert collect._parse_state["completeness"]["data_complete"] is True
    assert collect._parse_state["excel_path"] is None
    record = run_journal.read_last_run()
    assert record["outcome"] == run_journal.OUTCOME_ERROR
    assert record["lots_found"] == 1
    assert not any("✅ Готово" in line for line in collect._parse_state["progress"])
    assert collect._parse_state["data_saved"] is False
    run_env["excel"].assert_not_called()


def test_run_stays_reserved_until_browser_finishes(run_env, monkeypatch):
    async def close():
        assert collect._parse_state["running"] is True
        with ui.app.test_client() as client:
            assert client.post("/api/parse/start", json={"force": True}).status_code == 409

    run_env["browser"].close.side_effect = close
    assert collect._run_collect_once() == 0
    assert collect._parse_state["running"] is False
    assert collect._parse_cancel == {"loop": None, "task": None}


def test_success_still_saves_and_reports_ok(run_env):
    assert collect._run_collect_once() == 0
    assert collect._parse_state["error"] is None
    assert collect._parse_state["tenders"] == run_env["tenders"]
    assert run_journal.read_last_run()["outcome"] == run_journal.OUTCOME_OK
    run_env["browser"].close.assert_awaited_once()
    run_env["excel"].assert_called_once()
