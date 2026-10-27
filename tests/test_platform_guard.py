"""
Общий сторож площадок в раннере (16.10.2026).

Инцидент: ТЭК-Торг вернул ноль лотов, а сводка написала «все 77 объектов
обойдены целиком»; Bidzaar вообще не попадал в журнал. Сторож добавляет
отчёт «площадка целиком», когда парсер промолчал:
  • площадка упала или не прислала ни одного отчёта о полноте;
  • все объекты «обойдены», но лотов ноль.
"""

import asyncio

from src.core.parser_runner import ParserRunner
from src.parsers import completeness as c


def _runner(settings):
    base = {
        "parse_gpb": False,
        "parse_tender_pro": False,
        "parse_zakupki": False,
        "parse_tektorg": False,
        "parse_roseltorg": False,
        "parse_bidzaar": False,
        "keyword_filter_enabled": False,
        "network_retry": False,
    }
    base.update(settings)
    return ParserRunner(browser_manager=None, companies=["АО Рудник"], settings=base)


def _ok(platform):
    return c.build_unit_report(
        "phrase", "герметик", platform=platform, expected_pages=1, collected_pages=1
    )


def test_ноль_лотов_при_полном_обходе_это_подозрительный_ноль():
    runner = _runner({"parse_tektorg": True})

    async def fake():
        runner.unit_reports.append(_ok("ТЭК-Торг"))
        return []

    runner._run_tektorg = fake
    asyncio.run(runner.run_all())
    reasons = {r["platform"]: r["reason"] for r in runner.unit_reports if r["reason"]}
    assert reasons == {"ТЭК-Торг": c.REASON_EMPTY_PLATFORM}
    assert runner.platform_lots == {"ТЭК-Торг": 0}
    row = c.summarize_by_platform(runner.unit_reports)[0]
    assert row["data_complete"] is False


def test_площадка_без_отчётов_не_пропадает_из_сводки():
    runner = _runner({"parse_bidzaar": True})

    async def fake():
        return [{"platform": "Bidzaar", "name": "x"}]

    runner._run_bidzaar = fake
    asyncio.run(runner.run_all())
    assert [(r["platform"], r["reason"]) for r in runner.unit_reports] == [
        ("Bidzaar", c.REASON_NO_REPORT)
    ]


def test_упавшая_площадка_помечена_сбоем():
    runner = _runner({"parse_roseltorg": True})

    async def fake():
        raise RuntimeError("селектор не найден")

    runner._run_roseltorg = fake
    asyncio.run(runner.run_all())
    assert [(r["platform"], r["reason"]) for r in runner.unit_reports] == [
        ("Росэлторг", c.REASON_CRASHED)
    ]


def test_здоровая_площадка_без_лишних_отчётов():
    runner = _runner({"parse_tektorg": True})

    async def fake():
        runner.unit_reports.append(_ok("ТЭК-Торг"))
        return [{"platform": "ТЭК-Торг", "name": "Герметик"}]

    runner._run_tektorg = fake
    asyncio.run(runner.run_all())
    assert [r["reason"] for r in runner.unit_reports] == [""]
    assert runner.platform_lots == {"ТЭК-Торг": 1}
