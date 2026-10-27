# -*- coding: utf-8 -*-
"""Полнота сбора: отчёт «ожидалось / собрано» по каждому объекту обхода.

Механизм: src/parsers/completeness.py.

Ключевое решение плана: полнота считается ПО СТРАНИЦАМ, а не по лотам. У заказчика
с шестью страницами выдачи последняя почти всегда неполная (51 лот вместо 60), и
сравнение по лотам давало бы ложную тревогу на каждом успешном обходе.
"""

import pytest

from src.parsers import completeness as comp
from src.core import collect


# ── Определение статуса (FR-001, FR-002) ─────────────────────────────────────


def test_all_pages_walked_is_complete():
    """Обошли все обещанные страницы — объект полный, даже если лотов меньше
    произведения страниц на размер страницы."""
    r = comp.build_unit_report(
        "firm", 'АО "РОМАШКА"', expected_pages=6, collected_pages=6, collected_lots=51
    )
    assert r["status"] == comp.UNIT_COMPLETE
    assert r["reason"] == ""


def test_stopped_midway_is_partial():
    r = comp.build_unit_report(
        "firm", 'АО "РОМАШКА"', expected_pages=6, collected_pages=2, collected_lots=20
    )
    assert r["status"] == comp.UNIT_PARTIAL


def test_no_expected_number_is_unknown_not_partial():
    """Поиск по фразе не даёт ожидаемого числа — это не повод объявлять неполноту."""
    r = comp.build_unit_report("phrase", "Насосы", collected_lots=230)
    assert r["status"] == comp.UNIT_UNKNOWN
    assert r["expected_pages"] is None


def test_more_lots_than_expected_is_complete():
    """Лот добавился во время обхода — расхождение в плюс не считается неполнотой."""
    r = comp.build_unit_report(
        "firm", "ООО «НМ»", expected_lots=5, collected_lots=7, expected_pages=1, collected_pages=1
    )
    assert r["status"] == comp.UNIT_COMPLETE


def test_zero_lots_with_zero_expected_is_complete():
    r = comp.build_unit_report(
        "firm", "ООО «ПРОМХИМ - УКК»", expected_pages=1, collected_pages=1,
        expected_lots=0, collected_lots=0,
    )
    assert r["status"] == comp.UNIT_COMPLETE


def test_exact_lot_counter_detects_shortfall():
    """Классическая вёрстка даёт точное «Актуально • N» — по нему ловим недобор."""
    r = comp.build_unit_report(
        "firm", 'АО "РОМАШКА"', expected_pages=1, collected_pages=1,
        expected_lots=53, collected_lots=20,
    )
    assert r["status"] == comp.UNIT_PARTIAL


def test_not_visited_unit_is_skipped():
    r = comp.build_unit_report("firm", "Кто-то", visited=False)
    assert r["status"] == comp.UNIT_SKIPPED


# ── Причины (FR-008) ─────────────────────────────────────────────────────────


def test_reason_is_kept_for_partial():
    r = comp.build_unit_report(
        "firm", "Ы", expected_pages=6, collected_pages=1, reason=comp.REASON_RATE_LIMITED
    )
    assert r["reason"] == comp.REASON_RATE_LIMITED


def test_partial_without_reason_falls_back_to_unknown():
    r = comp.build_unit_report("firm", "Ы", expected_pages=6, collected_pages=1)
    assert r["reason"] == comp.REASON_UNKNOWN


def test_complete_unit_has_no_reason():
    r = comp.build_unit_report(
        "firm", "Ы", expected_pages=2, collected_pages=2, reason=comp.REASON_RATE_LIMITED
    )
    assert r["status"] == comp.UNIT_COMPLETE
    assert r["reason"] == ""


@pytest.mark.parametrize(
    "key",
    [
        comp.REASON_RATE_LIMITED,
        comp.REASON_NOT_LOGGED_IN,
        comp.REASON_NETWORK,
        comp.REASON_STOPPED,
        comp.REASON_UNKNOWN,
    ],
)
def test_every_reason_has_human_text(key):
    """У каждой причины есть текст без технических терминов."""
    text = comp.reason_text(key)
    assert text and text[0].isupper() or text[0].islower()
    low = text.lower()
    for banned in ("капча", "captcha", "rate", "session", "cookie", "page_count"):
        assert banned not in low, f"в тексте причины технический термин: {banned}"


def test_unknown_reason_key_does_not_crash():
    assert comp.reason_text("что-то новое")


# ── Сводка прогона (FR-004) ──────────────────────────────────────────────────


def test_summary_counts_by_status():
    reports = [
        comp.build_unit_report("firm", "A", expected_pages=1, collected_pages=1),
        comp.build_unit_report("firm", "B", expected_pages=6, collected_pages=2),
        comp.build_unit_report("firm", "C", visited=False),
        comp.build_unit_report("phrase", "D", collected_lots=10),
    ]
    s = comp.summarize_units(reports)
    assert s["complete"] == 1
    assert s["partial"] == 1
    assert s["skipped"] == 1
    assert s["unknown"] == 1
    assert s["data_complete"] is False
    assert [u["label"] for u in s["incomplete_units"]] == ["B", "C"]


def test_summary_is_complete_when_nothing_missing():
    reports = [
        comp.build_unit_report("firm", "A", expected_pages=1, collected_pages=1),
        comp.build_unit_report("phrase", "B", collected_lots=5),
    ]
    s = comp.summarize_units(reports)
    assert s["data_complete"] is True
    assert s["incomplete_units"] == []


def test_empty_summary_is_complete():
    s = comp.summarize_units([])
    assert s["data_complete"] is True
    assert s["complete"] == 0


def test_runner_collects_reports_from_parser(monkeypatch):
    """Раннер забирает отчёты у парсера, не меняя контракт run_all() -> список лотов."""
    import asyncio

    from src.core import parser_runner as pr

    sample = [comp.build_unit_report("firm", "A", expected_pages=6, collected_pages=2)]

    class _FakeParser:
        def __init__(self, *a, **kw):
            self.unit_reports = list(sample)

        async def parse(self):
            return [{"name": "лот", "href": "https://x/1", "lot_id": "1"}]

    monkeypatch.setattr(pr, "BidzaarParser", _FakeParser)
    runner = pr.ParserRunner(
        browser_manager=None,
        settings={"parse_bidzaar": True, "days_back": 1},
        companies=["A"],
    )

    lots = asyncio.run(runner._run_bidzaar())

    assert len(lots) == 1
    assert runner.unit_reports == sample


def test_runner_keeps_reports_when_parser_fails(monkeypatch):
    """Сбой парсера не должен прятать уже собранную картину неполноты."""
    import asyncio

    from src.core import parser_runner as pr

    class _BrokenParser:
        def __init__(self, *a, **kw):
            self.unit_reports = [
                comp.build_unit_report("firm", "A", visited=False)
            ]

        async def parse(self):
            raise RuntimeError("площадка недоступна")

    monkeypatch.setattr(pr, "BidzaarParser", _BrokenParser)
    runner = pr.ParserRunner(
        browser_manager=None,
        settings={"parse_bidzaar": True, "days_back": 1},
        companies=["A"],
    )

    lots = asyncio.run(runner._run_bidzaar())

    assert lots == []
    assert len(runner.unit_reports) == 1
    assert runner.unit_reports[0]["status"] == comp.UNIT_SKIPPED


def test_ui_summary_speaks_human_language():
    """Сводка для интерфейса не должна содержать технических терминов."""

    reports = [
        comp.build_unit_report(
            "firm", 'АО "РОМАШКА"', expected_pages=6, collected_pages=2,
            collected_lots=20, reason=comp.REASON_RATE_LIMITED,
        ),
        comp.build_unit_report("phrase", "Насосы", visited=False),
    ]
    messages = []
    c = collect._build_completeness(reports, messages.append)

    assert c["checked"] is True
    assert c["data_complete"] is False
    assert c["partial"] == 1 and c["skipped"] == 1
    assert c["units"][0]["reason"] == "площадка ограничила скорость просмотра"

    text = " ".join(messages).lower()
    for banned in ("капча", "captcha", "page_count", "cookie", "traceback"):
        assert banned not in text


def test_ui_summary_when_everything_collected():

    reports = [comp.build_unit_report("firm", "A", expected_pages=2, collected_pages=2)]
    messages = []
    c = collect._build_completeness(reports, messages.append)

    assert c["data_complete"] is True
    assert c["units"] == []
    assert any("полнота" in m.lower() for m in messages)


def test_ui_summary_without_reports_is_not_checked():
    """Площадки без счётчика (Tender.pro, ГПБ) не должны выглядеть как «неполнота»."""

    c = collect._build_completeness([], lambda _m: None)
    assert c["checked"] is False
    assert c["data_complete"] is True
