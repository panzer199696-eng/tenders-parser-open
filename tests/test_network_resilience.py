"""Устойчивость сбора к сетевым сбоям (решение владельца 12.10.2026).

За 21 ночь Bidzaar, Tender.pro и Росэлторг падали «обрывом связи»
10 раз, ТЭК-Торг — почти каждую ночь. Площадка, не ответившая один раз,
терялась до следующей ночи: повторного захода не было, а итог выглядел
обычным «ок». Здесь три защиты:

1. проверка связи с площадками перед сбором (с повторами) и внятная
   строка «проверьте подключение», если площадка так и не ответила;
2. второй заход в конце прогона по площадкам с «обрывом связи»;
3. выбор площадок для догоняющего прогона в 07:00 по журналу ночи.

Все тесты офлайн: сеть подменяется, паузы нулевые.
"""

import asyncio
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import net_preflight
from src.core.catchup import platforms_to_retry
from src.core.net_preflight import probe as real_probe
from src.core.parser_runner import ParserRunner
from src.parsers.completeness import REASON_NETWORK, build_unit_report

# ── 1. Проверка связи перед сбором ──────────────────────────────────────────


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_любой_http_ответ_значит_площадка_доступна():
    """503 или 403 — площадка на связи (антибот — отдельная история,
    у ТЭК-Торга своя проба); недоступна — только когда ответа нет вовсе."""

    def opener_503(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    def opener_down(req, timeout):
        raise urllib.error.URLError("timed out")

    assert real_probe("https://x.test/", opener=lambda r, timeout: _Resp())
    assert real_probe("https://x.test/", opener=opener_503)
    assert not real_probe("https://x.test/", opener=opener_down)


def test_проверяются_только_включённые_площадки():
    seen = []

    def probe(url):
        seen.append(url)
        return True

    settings = {
        "parse_bidzaar": True,
        "parse_tender_pro": False,
        "parse_zakupki": True,
    }
    result = net_preflight.check_platforms(
        settings, probe_fn=probe, sleep=lambda s: None
    )
    assert set(result) == {"Bidzaar", "ЕИС"}
    assert all(result.values())
    assert len(seen) == 2


def test_недоступная_площадка_проверяется_повторно_с_паузой():
    """Ночью сеть после простоя поднимается не сразу — один промах ещё не
    приговор. Повторяем только упавшие, с паузами между попытками."""
    calls = {"bidzaar": 0, "other": 0}
    pauses = []

    def probe(url):
        if "bidzaar" in url:
            calls["bidzaar"] += 1
            return calls["bidzaar"] >= 3  # ответила с третьей попытки
        calls["other"] += 1
        return True

    settings = {"parse_bidzaar": True, "parse_zakupki": True}
    result = net_preflight.check_platforms(
        settings, probe_fn=probe, attempts=3, pauses=(5, 7), sleep=pauses.append
    )
    assert result == {"Bidzaar": True, "ЕИС": True}
    assert calls == {"bidzaar": 3, "other": 1}
    assert pauses == [5, 7]


def test_площадка_не_ответила_за_все_попытки():
    settings = {"parse_roseltorg": True}
    result = net_preflight.check_platforms(
        settings,
        probe_fn=lambda url: False,
        attempts=3,
        pauses=(0, 0),
        sleep=lambda s: None,
    )
    assert result == {"Росэлторг": False}
    text = net_preflight.describe(result)
    assert "Росэлторг" in text and "VPN" in text


def test_всё_доступно_описание_пустое():
    assert net_preflight.describe({"Bidzaar": True}) == ""


# ── 2. Второй заход по площадкам с обрывом связи ────────────────────────────


def _runner(settings):
    runner = ParserRunner.__new__(ParserRunner)
    runner.browser_manager = None
    runner.unit_reports = []
    runner.settings = {
        "network_retry_pause_s": 0,
        "keyword_filter_enabled": False,
        "include_no_price": True,
        "min_price": 0,
        **settings,
    }
    runner.days_back = 1
    runner.companies = ["Кедр"]
    return runner


def _lot(n):
    return {
        "name": f"Герметик, лот {n}",
        "company": "АО Кедр",
        "href": f"https://bidzaar.test/{n}",
        # площадку парсеры заполняют всегда — по ней сторож считает лоты
        "platform": "Bidzaar",
        "date_deadline": (datetime.now() + timedelta(days=10)).strftime("%d.%m.%Y"),
        "sum": None,
    }


def _report(reason=""):
    return build_unit_report(
        "company",
        "Кедр",
        platform="Bidzaar",
        expected_pages=1,
        collected_pages=0 if reason else 1,
        reason=reason,
    )


def test_площадка_с_обрывом_связи_обходится_повторно():
    runner = _runner({"parse_bidzaar": True})
    calls = {"n": 0}

    async def fake_bidzaar():
        calls["n"] += 1
        if calls["n"] == 1:
            runner.unit_reports.append(_report(REASON_NETWORK))
            return []
        runner.unit_reports.append(_report())
        return [_lot(1)]

    runner._run_bidzaar = fake_bidzaar
    result = asyncio.run(runner.run_all())
    assert calls["n"] == 2
    assert [t["href"] for t in result] == ["https://bidzaar.test/1"]
    # отчёт первого захода заменён отчётом второго — обрыва в сводке нет
    assert [r["reason"] for r in runner.unit_reports] == [""]


def test_без_обрыва_связи_второго_захода_нет():
    runner = _runner({"parse_bidzaar": True})
    calls = {"n": 0}

    async def fake_bidzaar():
        calls["n"] += 1
        runner.unit_reports.append(_report())
        return [_lot(1)]

    runner._run_bidzaar = fake_bidzaar
    asyncio.run(runner.run_all())
    assert calls["n"] == 1


def test_второй_заход_тоже_упал_отчёт_не_удваивается_лоты_не_теряются():
    runner = _runner({"parse_bidzaar": True})
    calls = {"n": 0}

    async def fake_bidzaar():
        calls["n"] += 1
        runner.unit_reports.append(_report(REASON_NETWORK))
        # первый заход успел собрать лот до обрыва, второй — ничего
        return [_lot(1)] if calls["n"] == 1 else []

    runner._run_bidzaar = fake_bidzaar
    result = asyncio.run(runner.run_all())
    assert calls["n"] == 2
    assert len(runner.unit_reports) == 1
    assert [t["href"] for t in result] == ["https://bidzaar.test/1"]


def test_второй_заход_не_дублирует_лоты():
    runner = _runner({"parse_bidzaar": True})
    calls = {"n": 0}

    async def fake_bidzaar():
        calls["n"] += 1
        if calls["n"] == 1:
            runner.unit_reports.append(_report(REASON_NETWORK))
            return [_lot(1)]
        runner.unit_reports.append(_report())
        return [_lot(1), _lot(2)]

    runner._run_bidzaar = fake_bidzaar
    result = asyncio.run(runner.run_all())
    assert sorted(t["href"] for t in result) == [
        "https://bidzaar.test/1",
        "https://bidzaar.test/2",
    ]


def test_второй_заход_выключается_настройкой():
    runner = _runner({"parse_bidzaar": True, "network_retry": False})
    calls = {"n": 0}

    async def fake_bidzaar():
        calls["n"] += 1
        runner.unit_reports.append(_report(REASON_NETWORK))
        return []

    runner._run_bidzaar = fake_bidzaar
    asyncio.run(runner.run_all())
    assert calls["n"] == 1


# ── 3. Догоняющий прогон в 07:00 ────────────────────────────────────────────

_NIGHT = datetime(2026, 10, 13, 3, 0)


def _record(complete, units, started=_NIGHT):
    return {
        "started_at": started.isoformat(timespec="seconds"),
        "outcome": "ok",
        "data_complete": complete,
        "incomplete_units": units,
    }


def _unit(platform, reason="network"):
    return {"platform": platform, "reason": reason, "label": "x"}


def test_полная_ночь_догонять_нечего():
    assert platforms_to_retry(_record(True, []), now=_NIGHT + timedelta(hours=4)) == []


def test_догоняем_только_площадки_с_обрывом_связи():
    rec = _record(
        False,
        [
            _unit("Bidzaar"),
            _unit("ТЭК-Торг"),
            _unit("ТЭК-Торг"),
            # потолок страниц повторный заход не вылечит
            _unit("ЕИС", reason="page_cap"),
        ],
    )
    assert platforms_to_retry(rec, now=_NIGHT + timedelta(hours=4)) == [
        "parse_bidzaar",
        "parse_tektorg",
    ]


def test_старый_прогон_не_догоняем():
    """Ночного прогона не было (ПК выключен) — догонять вчерашний поздно,
    этим занимается догон пропущенного слота при запуске программы."""
    rec = _record(False, [_unit("Bidzaar")])
    assert platforms_to_retry(rec, now=_NIGHT + timedelta(hours=20)) == []


def test_догон_после_догона_не_запускается():
    rec = _record(False, [_unit("Bidzaar")])
    rec["trigger"] = "catchup"
    assert platforms_to_retry(rec, now=_NIGHT + timedelta(hours=5)) == []


def test_нет_журнала_нечего_догонять():
    assert platforms_to_retry(None, now=_NIGHT) == []


# ── Новизна «что принесла ночь» не сбивается догоняющим прогоном ────────────


def test_новизна_считается_от_ночного_прогона_а_не_от_догона(tmp_path):
    from src.db import connect, ensure_schema
    from src.db.feed import fresh_since
    from src.db.runs import record_run

    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    record_run(conn, {"started_at": "2026-10-13T03:00:01", "outcome": "ok"})
    record_run(
        conn,
        {"started_at": "2026-10-13T07:00:01", "outcome": "ok", "trigger": "catchup"},
    )
    assert fresh_since(conn, datetime(2026, 10, 13, 9, 0)) == "2026-10-13T03:00:01"
    conn.close()
