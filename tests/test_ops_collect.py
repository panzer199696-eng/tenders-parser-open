"""Сбор как процесс: замок, порядок «журнал → книга», коды выхода, таймауты.

Ревью 16.10.2026 (А4, В1, В2, Д2): только временные базы и журнал, браузер
и площадки подменены. Живые данные и сеть не трогаются.
"""

from unittest.mock import AsyncMock, Mock

import pytest

import main_ui as ui
from src.core import collect
from src.core import context as ctx
from src import collect_lock, run_journal
from src.parsers import completeness as comp


@pytest.fixture
def сбор(monkeypatch, tmp_path):
    """Настоящие временные базы и журнал; браузера, сети и диагностики нет."""
    import src.core.parser_runner as pr
    import src.keywords_store as ks
    import src.settings_store as ss
    import src.utils.browser_manager as bm
    from src import profile

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
    monkeypatch.setattr(collect, "_preflight_problems", list)
    monkeypatch.setattr(ctx, "load_companies", list)
    monkeypatch.setattr(ks, "enabled_phrases", list)
    settings = {"parse_roseltorg": True, "parse_bidzaar": True}
    monkeypatch.setattr(ss, "load_settings", lambda: settings)
    monkeypatch.setattr(profile, "Profile", lambda: None)

    browser = Mock()
    browser.start = AsyncMock()
    browser.close = AsyncMock()
    monkeypatch.setattr(bm, "BrowserManager", Mock(return_value=browser))
    tenders = [
        {
            "platform": "Росэлторг",
            "lot_id": "b-1",
            "href": "https://example.invalid/1",
            "name": "Насосы",
            "date_published": "15.10.2026 10:00",
        },
        {
            "platform": "Bidzaar",
            "lot_id": "z-1",
            "href": "https://bz.invalid/1",
            "name": "Герметик",
            "date_published": "14.10.2026",
        },
    ]
    runner = Mock()
    runner.unit_reports = [
        comp.build_unit_report(
            "firm", "Тест", platform="Росэлторг", expected_pages=1, collected_pages=1
        ),
        comp.build_unit_report(
            "phrase",
            "Герметик",
            platform="Bidzaar",
            expected_pages=1,
            collected_pages=1,
        ),
    ]
    runner.platform_lots = {"Росэлторг": 7, "Bidzaar": 3}
    runner.platform_newest = {"Росэлторг": "2026-10-15", "Bidzaar": "2026-10-14"}
    runner.run_all = AsyncMock(return_value=tenders)
    monkeypatch.setattr(pr, "ParserRunner", Mock(return_value=runner))
    return {"settings": settings, "runner": runner, "tenders": tenders, "tmp": tmp_path}


def _db():
    from src.db import connect

    return connect(ctx.UNIFIED_DB_PATH)


# ── А4: межпроцессный замок ─────────────────────────────────────────────────


def test_второй_collect_при_занятом_замке_выходит_с_кодом_1(сбор, caplog):
    held = collect_lock.acquire()
    try:
        with caplog.at_level("WARNING"):
            assert collect._run_collect_once() == 1
    finally:
        held.release()
    assert collect._parse_state["running"] is False
    assert "уже идёт" in caplog.text
    сбор["runner"].run_all.assert_not_awaited()


def test_кнопка_запуска_при_занятом_замке_объясняет_причину(сбор):
    held = collect_lock.acquire()
    try:
        with ui.app.test_client() as client:
            response = client.post("/api/parse/start", json={"force": True})
    finally:
        held.release()
    assert response.status_code == 409
    assert "уже идёт" in response.get_json()["error"]
    assert collect._parse_state["running"] is False


def test_замок_снимается_после_прогона(сбор):
    assert collect._run_collect_once() == 0
    collect_lock.acquire().release()


# ── Д2: коды выхода --collect ───────────────────────────────────────────────


def test_полный_прогон_код_0(сбор):
    assert collect._run_collect_once() == collect.EXIT_OK


def test_неполный_прогон_код_2(сбор):
    сбор["runner"].unit_reports.append(
        comp.platform_unit("Bidzaar", comp.REASON_FORMAT_CHANGED)
    )
    assert collect._run_collect_once() == collect.EXIT_INCOMPLETE


def test_упавший_прогон_код_1(сбор):
    сбор["runner"].run_all.side_effect = RuntimeError("сломалось")
    assert collect._run_collect_once() == collect.EXIT_ERROR


# ── В1: честные итоги по площадкам в журнале и базе ─────────────────────────


def test_журнал_и_база_несут_лоты_новые_и_свежайшую_по_площадкам(сбор):
    assert collect._run_collect_once() == collect.EXIT_OK
    rows = {r["platform"]: r for r in run_journal.read_last_run()["by_platform"]}
    assert rows["Росэлторг"]["lots"] == 7
    assert rows["Росэлторг"]["lots_new"] == 1
    assert rows["Bidzaar"]["newest_published"] == "2026-10-14"
    conn = _db()
    try:
        got = {
            r["platform"]: (r["lots"], r["lots_new"], r["newest_published"])
            for r in conn.execute("SELECT * FROM run_platforms")
        }
    finally:
        conn.close()
    assert got["Росэлторг"] == (7, 1, "2026-10-15")
    assert got["Bidzaar"] == (3, 1, "2026-10-14")


def test_второй_прогон_тех_же_лотов_новых_ноль(сбор):
    collect._run_collect_once()
    # первый прогон — «прошлой ночью»: в тесте оба укладываются в секунду
    conn = _db()
    conn.execute("UPDATE lots SET first_seen_at = '2026-10-15T03:10:00'")
    conn.commit()
    conn.close()
    collect._run_collect_once()
    rows = {r["platform"]: r for r in run_journal.read_last_run()["by_platform"]}
    assert rows["Росэлторг"]["lots_new"] == 0


def test_сбор_не_пишет_старую_базу(сбор):
    """tenders.db больше не ведётся: плитка «Сегодня» читает единую базу."""
    assert collect._run_collect_once() == collect.EXIT_OK
    assert not (ctx.BASE_DIR / "data" / "tenders.db").exists()
    assert not any("Сохранено новых в БД" in s for s in collect._parse_state["progress"])


def test_итог_в_окне_не_выдаёт_неизвестный_объём_за_полный():
    lines = []
    reports = [
        comp.build_unit_report("phrase", "а", platform="Tender.pro"),
        comp.build_unit_report(
            "phrase", "б", platform="Росэлторг", expected_pages=1, collected_pages=1
        ),
    ]
    collect._build_completeness(reports, lines.append)
    text = "\n".join(lines)
    assert "все 2 объектов обойдены целиком" not in text
    assert "целиком 1" in text and "объём неизвестен у 1" in text


# ── В3: межпрогонная проверка в прогоне ─────────────────────────────────────


def _прошлые_ночи(lots=100, new=1):
    from datetime import date, datetime, timedelta

    from src.db import ensure_schema
    from src.db.runs import record_run

    conn = _db()
    ensure_schema(conn)
    for i in range(1, 15):
        day = date.today() - timedelta(days=i)
        started = datetime(day.year, day.month, day.day, 3, 0, 1).isoformat()
        record_run(conn, {
            "started_at": started, "finished_at": started, "outcome": "ok",
            "platforms": {},
            "by_platform": [
                {"platform": p, "units": 1, "data_complete": True, "lots": lots,
                 "lots_new": new}
                for p in ("Росэлторг", "Bidzaar")
            ],
        })
    conn.close()


def test_площадка_в_разы_меньше_обычного_делает_прогон_неполным(сбор):
    _прошлые_ночи()
    сбор["runner"].platform_lots = {"Росэлторг": 95, "Bidzaar": 3}
    assert collect._run_collect_once() == collect.EXIT_INCOMPLETE
    record = run_journal.read_last_run()
    rows = {r["platform"]: r for r in record["by_platform"]}
    assert rows["Bidzaar"]["reason_code"] == comp.REASON_ANOMALY
    assert rows["Росэлторг"]["data_complete"] is True
    assert "Bidzaar: лотов вчера 100, сегодня 3 (обычно 100)" in record["summary"]
    assert any("вчера 100, сегодня 3" in s for s in collect._parse_state["progress"])
    assert collect._parse_state["completeness"]["data_complete"] is False


def test_догон_с_прошлыми_ночами_не_сравнивается(сбор, monkeypatch):
    _прошлые_ночи()
    сбор["runner"].platform_lots = {"Росэлторг": 95, "Bidzaar": 3}
    monkeypatch.setattr(collect.run_journal, "read_last_run", lambda: None)
    assert collect._start_parse([], background=False, trigger="catchup")
    assert run_journal.read_runs(1)[0]["data_complete"] is True


# ── В4: догон в 07:00 ───────────────────────────────────────────────────────


def _ночь(reports, outcome="ok"):
    from datetime import datetime, timedelta

    started = datetime.now() - timedelta(hours=4)
    record = run_journal.build_record(
        started_at=started, finished_at=started + timedelta(minutes=30),
        app_version="test", outcome=outcome,
        settings={"parse_roseltorg": True, "parse_bidzaar": True},
        unit_reports=reports, lots_found=1, lots_new=0,
    )
    run_journal.write_run(record)


def test_догон_не_запускается_когда_нужна_правка(сбор, caplog):
    _ночь([comp.platform_unit("Bidzaar", comp.REASON_FORMAT_CHANGED)])
    with caplog.at_level("WARNING"):
        assert collect._retry_incomplete_once() == collect.EXIT_INCOMPLETE
    assert "нужна правка программы или проверка площадки: Bidzaar" in caplog.text
    сбор["runner"].run_all.assert_not_awaited()


def test_упавшая_ночь_догоняет_все_площадки(сбор):
    import src.core.parser_runner as pr

    _ночь(None, outcome="error")
    assert collect._retry_incomplete_once() == collect.EXIT_OK
    settings = pr.ParserRunner.call_args.kwargs["settings"]
    assert settings["parse_roseltorg"] and settings["parse_bidzaar"]
    assert run_journal.read_last_run()["trigger"] == "catchup"
