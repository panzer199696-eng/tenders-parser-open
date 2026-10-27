"""Прогресс сбора — от раннера, а не регулярками по логам (16.10.2026).

Раньше «N из M» считалось по строкам логов парсеров, а M — копией логики
раннера в main_ui; ТЭК-Торг, Росэлторг и Bidzaar в регулярках не было, и
прогресс по ним стоял. Теперь раннер сообщает «площадка начата / закончена»
(лоты, объекты обхода), общее число — из реестра площадок.
"""

import asyncio

import main_ui as ui
from src.core import collect
from src.core.parser_runner import ParserRunner
from src.parsers import completeness as c


def _runner(events, **settings):
    base = {
        "parse_bidzaar": True,
        "parse_roseltorg": True,
        "keyword_filter_enabled": False,
        "network_retry": False,
        "include_no_price": True,
        "min_price": 0,
    }
    base.update(settings)
    runner = ParserRunner(browser_manager=None, companies=["АО Рудник"], settings=base)
    runner.progress = lambda event, platform, **info: events.append(
        (event, platform, info)
    )

    async def bidzaar():
        for i in range(3):
            runner.unit_reports.append(
                c.build_unit_report(
                    "phrase",
                    f"ф{i}",
                    platform="Bidzaar",
                    expected_pages=1,
                    collected_pages=1,
                )
            )
        return [
            {"platform": "Bidzaar", "name": f"лот {i}", "href": f"https://bz/{i}"}
            for i in range(5)
        ]

    async def roseltorg():
        await asyncio.sleep(0.01)
        raise RuntimeError("сломалось")

    runner._run_bidzaar = bidzaar
    runner._run_roseltorg = roseltorg
    return runner


def test_раннер_сообщает_начало_и_конец_площадки():
    events = []
    asyncio.run(_runner(events).run_all())
    assert events[0] == ("plan", None, {"platforms": ["Росэлторг", "Bidzaar"]})
    starts = [p for e, p, _ in events if e == "start"]
    assert sorted(starts) == ["Bidzaar", "Росэлторг"]
    done = {p: info for e, p, info in events if e == "done"}
    assert done["Bidzaar"] == {"lots": 5, "units": 3, "ok": True}
    assert done["Росэлторг"]["ok"] is False


def test_сбой_колбэка_не_роняет_сбор():
    events = []
    runner = _runner(events)

    def boom(*a, **k):
        raise ValueError("ошибка интерфейса")

    runner.progress = boom
    result = asyncio.run(runner.run_all())
    assert len(result) == 5


def test_состояние_интерфейса_по_событиям(monkeypatch):
    monkeypatch.setattr(collect, "_parse_state",
        {
            **collect._parse_state,
            "running": True,
            "units_done": 0,
            "total_units": 6,
            "current_company": "", "platforms_running": [], "platforms_done": {}, "plan_received": False,
            "found_so_far": 0,
            "progress": [],
        },
    )
    collect._on_progress("plan", None, platforms=["Bidzaar", "ТЭК-Торг"])
    assert collect._parse_state["total_units"] == 2
    collect._on_progress("start", "Bidzaar")
    collect._on_progress("start", "ТЭК-Торг")
    assert collect._parse_state["current_company"] == "Идут: Bidzaar, ТЭК-Торг"
    collect._on_progress("done", "Bidzaar", lots=158, units=116, ok=True)
    assert collect._parse_state["units_done"] == 1
    assert collect._parse_state["found_so_far"] == 158
    assert collect._parse_state["current_company"] == "Идут: ТЭК-Торг"
    assert any(
        "Bidzaar: 158 лотов, объектов 116" in s for s in collect._parse_state["progress"]
    )
    # второй заход по площадке (обрыв связи): она снова «идёт», не «готова»
    collect._on_progress("start", "Bidzaar")
    assert collect._parse_state["units_done"] == 0
    # второй план (повторный заход) общее число не сбивает
    collect._on_progress("plan", None, platforms=["Bidzaar"])
    assert collect._parse_state["total_units"] == 2


def test_старые_регулярки_прогресса_убраны():
    assert not hasattr(ui, "_UNIT_START_RE")
    assert not hasattr(ui, "_unit_label")
