# -*- coding: utf-8 -*-
"""Полнота сбора по каждой площадке (ТЗ-8).

Механизм отчёта живёт в общем модуле
``src/parsers/completeness.py``: неполный сбор, выглядящий полным, дороже
честного «собрано не всё, вот почему». Здесь проверяется, что отчёты
действительно идут со ВСЕХ парсеров, сводка разносит их по площадкам и
называет виновника неполноты по имени — словами, а не кодами.

Сеть не трогается: ответы площадок берутся из фикстур/подмен.

Запуск:
    pytest tests/test_completeness_platforms.py -v
"""

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import run_journal  # noqa: E402
from src.parsers import completeness as comp  # noqa: E402
from src.core import collect  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


# ── 1. summarize_by_platform: разбивка и подсчёт статусов ─────────────────────


def test_summarize_by_platform_groups_and_counts():
    """Отчёты разносятся по площадкам, статусы считаются по каждой."""
    reports = [
        comp.build_unit_report(
            "phrase", "Насосы", platform="Bidzaar",
            expected_pages=3, collected_pages=3,
        ),
        comp.build_unit_report(
            "phrase", "Задвижки", platform="Bidzaar",
            expected_pages=5, collected_pages=2, reason=comp.REASON_PAGE_CAP,
        ),
        comp.build_unit_report("firm", "АО «Х»", platform="ТЭК-Торг", visited=False),
        comp.build_unit_report("phrase", "Кабель", platform="ЕИС", collected_lots=7),
    ]
    rows = {r["platform"]: r for r in comp.summarize_by_platform(reports)}
    assert set(rows) == {"Bidzaar", "ТЭК-Торг", "ЕИС"}
    bidzaar = rows["Bidzaar"]
    assert bidzaar["units"] == 2
    assert bidzaar["complete"] == 1 and bidzaar["partial"] == 1
    assert rows["ТЭК-Торг"]["skipped"] == 1
    assert rows["ЕИС"]["unknown"] == 1


def test_platform_without_incomplete_units_is_complete():
    """Ни одного partial/skipped — площадка собрана целиком (unknown не мешает:
    площадка, не сообщившая объём, — это не неполнота)."""
    reports = [
        comp.build_unit_report(
            "phrase", "А", platform="Bidzaar", expected_pages=2, collected_pages=2
        ),
        comp.build_unit_report("phrase", "Б", platform="Bidzaar", collected_lots=5),
    ]
    [row] = comp.summarize_by_platform(reports)
    assert row["data_complete"] is True
    assert row["reason"] == ""


def test_one_failing_platform_ruins_run_but_not_other_platforms():
    """Одна недобравшая площадка делает ПРОГОН неполным, её строку — тоже,
    а остальные площадки в разбивке остаются полными."""
    reports = [
        comp.build_unit_report(
            "phrase", "Задвижки", platform="Bidzaar",
            expected_pages=5, collected_pages=2, reason=comp.REASON_PAGE_CAP,
        ),
        comp.build_unit_report(
            "firm", "АО «Х»", platform="ТЭК-Торг",
            expected_pages=6, collected_pages=6,
        ),
    ]
    assert comp.summarize_units(reports)["data_complete"] is False

    rows = {r["platform"]: r for r in comp.summarize_by_platform(reports)}
    assert rows["Bidzaar"]["data_complete"] is False
    assert rows["ТЭК-Торг"]["data_complete"] is True


def test_reason_in_summary_is_human_words():
    """Причина в сводке — словами, не кодом: «page_cap» не значит ничего
    ни менеджеру, ни руководителю."""
    reports = [
        comp.build_unit_report(
            "phrase", "Задвижки", platform="Bidzaar",
            expected_pages=5, collected_pages=2, reason=comp.REASON_PAGE_CAP,
        ),
    ]
    [row] = comp.summarize_by_platform(reports)
    assert "упёрлись в потолок страниц" in row["reason"]
    assert comp.REASON_PAGE_CAP not in row["reason"]


def test_unknown_platform_reports_grouped_honestly():
    """Отчёт без поля platform не теряется: группа «Без площадки» честнее
    догадки (так приходят отчёты старой версии программы)."""
    reports = [
        comp.build_unit_report("phrase", "Старый формат", collected_lots=3),
    ]
    [row] = comp.summarize_by_platform(reports)
    assert row["platform"] == comp.PLATFORM_UNKNOWN


# ── 2. Площадки, не сообщившие объём: unknown — не ошибка ─────────────────────


def test_unknown_volume_does_not_spoil_run():
    """Площадка не сообщила объём — статус unknown, и прогон НЕ становится
    неполным: честное «не знаю» лучше выдуманного «всё собрано»."""
    reports = [
        comp.build_unit_report("phrase", "Подшипники", platform="ЕИС", collected_lots=4),
        comp.build_unit_report("phrase", "Кабель", platform="Росэлторг"),
    ]
    s = comp.summarize_units(reports)
    assert s["data_complete"] is True
    assert s["unknown"] == 2
    assert all(r["data_complete"] for r in comp.summarize_by_platform(reports))


# ── 3. Bidzaar: totalCount даёт ожидаемое число страниц ───────────────────────


def _bidzaar_pages(total: int, items: list, n: int) -> list[str]:
    """n одинаковых страниц ответа Bidzaar с заданным totalCount."""
    return [json.dumps({"totalCount": total, "items": items}) for _ in range(n)]


def _kw_items() -> list:
    text = (FIXTURES / "bidzaar_keyword_response.json").read_text(encoding="utf-8")
    return json.loads(text)["items"]


def _bidzaar_parser(monkeypatch, pages):
    import src.parsers.bidzaar_parser as mod

    # today — дата фикстуры: иначе сторож свежести ленты добавит отчёт
    # «не обновляется» о площадке целиком (16.10.2026).
    p = mod.BidzaarParser(
        None, [], days_back=1, keyword_phrases=["дозатор"], today=date(2026, 9, 2)
    )
    queue = list(pages)

    def fake_fetch(params):
        return queue.pop(0) if queue else json.dumps({"totalCount": 0, "items": []})

    monkeypatch.setattr(mod, "_fetch_tenders", fake_fetch)
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    return p


def test_bidzaar_total_count_250_means_three_pages_complete(monkeypatch):
    """totalCount=250 при PAGE_SIZE=100 → ожидается 3 страницы; собрали 3 —
    complete, хотя последняя страница неполная по лотам (полнота — по
    страницам, не по лотам)."""
    import src.parsers.bidzaar_parser as mod

    assert mod.PAGE_SIZE == 100
    # Три страницы: счётчик достигается на третьей (3 × 100 ≥ 250).
    p = _bidzaar_parser(monkeypatch, _bidzaar_pages(250, _kw_items(), 3))
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["platform"] == "Bidzaar"
    assert report["expected_pages"] == 3
    assert report["collected_pages"] == 3
    assert report["status"] == comp.UNIT_COMPLETE


def test_bidzaar_page_cap_is_partial(monkeypatch):
    """Собрали 2 из 3 страниц (упор в MAX_PAGES) — partial с причиной
    page_cap: обрезанный сбор не имеет права выглядеть полным."""
    import src.parsers.bidzaar_parser as mod

    monkeypatch.setattr(mod, "MAX_PAGES", 2)
    p = _bidzaar_parser(monkeypatch, _bidzaar_pages(250, _kw_items(), 5))
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["status"] == comp.UNIT_PARTIAL
    assert report["reason"] == comp.REASON_PAGE_CAP
    assert report["collected_pages"] == 2 and report["expected_pages"] == 3


# ── 4. ТЭК-Торг: totalPages даёт ожидаемое число страниц ──────────────────────


def _tektorg_pages(monkeypatch, pages):
    import src.parsers.tektorg_parser as mod

    p = mod.TektorgParser(None, ["не используется"], days_back=1,
                          keyword_phrases=["герметик"])
    queue = list(pages)

    def fake_fetch(payload):
        return queue.pop(0) if queue else json.dumps(
            {"totalPages": 1, "data": []}
        )

    monkeypatch.setattr(mod, "_fetch_procedures", fake_fetch)
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    return p


def _tektorg_page(total_pages: int) -> str:
    data = json.loads(
        (FIXTURES / "tektorg_section_response.json").read_text(encoding="utf-8")
    )
    data["totalPages"] = total_pages
    return json.dumps(data, ensure_ascii=False)


def test_tektorg_total_pages_collected_all_is_complete(monkeypatch):
    """totalPages=5, собрано 5 — complete."""
    p = _tektorg_pages(monkeypatch, [_tektorg_page(5)] * 5)
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["platform"] == "ТЭК-Торг"
    assert report["expected_pages"] == 5
    assert report["collected_pages"] == 5
    assert report["status"] == comp.UNIT_COMPLETE


def test_tektorg_network_error_midway_is_partial(monkeypatch):
    """totalPages=5, на четвёртой странице обрыв сети — partial с причиной
    network: собранное сохранено, но обход не доведён."""
    p = _tektorg_pages(
        monkeypatch,
        [_tektorg_page(5)] * 3 + ["{это не JSON"],
    )
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["status"] == comp.UNIT_PARTIAL
    assert report["reason"] == comp.REASON_NETWORK
    assert report["collected_pages"] == 3 and report["expected_pages"] == 5


def test_tektorg_first_page_network_error_is_skipped(monkeypatch):
    """Площадка не ответила вовсе — до объекта не дошли: skipped/network."""
    p = _tektorg_pages(monkeypatch, ["{это не JSON"])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["status"] == comp.UNIT_SKIPPED
    assert report["reason"] == comp.REASON_NETWORK


# ── 5. ParserRunner собирает отчёты со всех парсеров ──────────────────────────


def test_runner_collects_reports_from_every_parser(monkeypatch):
    """Отчёты каждого парсера доезжают до ParserRunner.unit_reports с полем
    platform: сводка должна видеть каждую площадку."""
    from src.core import parser_runner as pr

    sample = comp.build_unit_report(
        "phrase", "Насосы", platform="Bidzaar",
        expected_pages=3, collected_pages=3,
    )

    class _FakeParser:
        def __init__(self, *args, **kwargs):
            self.unit_reports = [dict(sample)]

        async def parse(self):
            return []

    for name in (
        "BidzaarParser",
        "ZakupkiParser",
        "RoseltorgParser",
        "TektorgParser",
        "TenderProParser",
    ):
        monkeypatch.setattr(pr, name, _FakeParser)
    monkeypatch.setattr(
        "src.keywords_store.enabled_phrases", lambda: ["насос"]
    )
    monkeypatch.setattr(
        "src.client_inns_store.load_client_inns", lambda: ["0000000000"]
    )

    runner = pr.ParserRunner.__new__(pr.ParserRunner)
    runner.browser_manager = None
    runner.companies = ["Клиент"]
    runner.days_back = 1
    runner.settings = {
        "parse_tender_pro": True,
        "parse_zakupki": True,
        "parse_roseltorg": True,
        "parse_tektorg": True,
        "parse_bidzaar": True,
        "keyword_search_enabled": True,
    }

    for method in (
        "_run_tender_pro",
        "_run_zakupki",
        "_run_roseltorg",
        "_run_tektorg",
        "_run_bidzaar",
    ):
        asyncio.run(getattr(runner, method)())

    # ТЭК-Торг создаёт ДВА парсера (фразы + секции) — остальные по одному.
    assert len(runner.unit_reports) == 6
    assert all(r["platform"] == "Bidzaar" for r in runner.unit_reports)


def test_runner_takes_reports_even_when_parser_fails(monkeypatch):
    """Сбой парсера не должен прятать уже собранную картину неполноты."""
    from src.core import parser_runner as pr

    class _BrokenParser:
        def __init__(self, *args, **kwargs):
            self.unit_reports = [
                comp.build_unit_report(
                    "phrase", "Насосы", platform="Bidzaar", visited=False,
                    reason=comp.REASON_NETWORK,
                )
            ]

        async def parse(self):
            raise RuntimeError("площадка недоступна")

    monkeypatch.setattr(pr, "BidzaarParser", _BrokenParser)
    monkeypatch.setattr(
        "src.keywords_store.enabled_phrases", lambda: ["насос"]
    )
    runner = pr.ParserRunner.__new__(pr.ParserRunner)
    runner.browser_manager = None
    runner.companies = []
    runner.days_back = 1
    runner.settings = {"parse_bidzaar": True}

    assert asyncio.run(runner._run_bidzaar()) == []
    assert len(runner.unit_reports) == 1
    assert runner.unit_reports[0]["status"] == comp.UNIT_SKIPPED


# ── 7. Журнал и таблица run_platforms ─────────────────────────────────────────


def _record_with_two_platforms() -> dict:
    reports = [
        comp.build_unit_report(
            "phrase", "Насосы", platform="Bidzaar",
            expected_pages=3, collected_pages=3,
        ),
        comp.build_unit_report(
            "phrase", "Задвижки", platform="Bidzaar",
            expected_pages=5, collected_pages=2, reason=comp.REASON_PAGE_CAP,
        ),
        comp.build_unit_report(
            "firm", "АО «Ромашка»", platform="ТЭК-Торг",
            expected_pages=6, collected_pages=6,
        ),
    ]
    return run_journal.build_record(
        started_at=datetime(2026, 9, 4, 3, 0),
        finished_at=datetime(2026, 9, 4, 3, 10),
        app_version="2.3.0",
        outcome=run_journal.OUTCOME_OK,
        settings={"parse_tektorg": True, "parse_bidzaar": True},
        unit_reports=reports,
        lots_found=100,
        lots_new=5,
    )


def test_journal_record_has_by_platform_breakdown():
    """Запись прогона несёт разбивку по площадкам: её читают таблица
    run_platforms и сводка человеческим языком."""
    record = _record_with_two_platforms()
    rows = {r["platform"]: r for r in record["by_platform"]}
    assert set(rows) == {"Bidzaar", "ТЭК-Торг"}
    assert rows["Bidzaar"]["data_complete"] is False
    assert rows["ТЭК-Торг"]["data_complete"] is True


def test_journal_summary_names_culprit_platform():
    """Сводка называет виновника по имени и объясняет причину словами,
    а не безликим «часть объектов не собрана»."""
    text = run_journal.describe(_record_with_two_platforms())
    assert "Bidzaar" in text
    assert "1 объектов из 2" in text  # на Bidzaar не собран 1 объект из двух
    assert "упёрлись в потолок страниц" in text
    # Полная площадка не обвиняется.
    assert "ТЭК-Торг" not in text


def test_run_platforms_gets_row_per_platform():
    """После прогона в таблице run_platforms — по строке на площадку
    (таблица заведена под это и до ТЗ-8 стояла пустой)."""
    from src.db.runs import record_run
    from src.db.schema import connect, ensure_schema

    record = _record_with_two_platforms()
    conn = connect(":memory:")
    try:
        ensure_schema(conn)
        run_id = record_run(conn, record)
        rows = {
            r["platform"]: r
            for r in conn.execute(
                "SELECT platform, collected, complete, reason FROM run_platforms"
                " WHERE run_id = ?",
                (run_id,),
            )
        }
    finally:
        conn.close()

    assert set(rows) == {"Bidzaar", "ТЭК-Торг"}, rows
    assert rows["Bidzaar"]["collected"] == 2
    assert rows["Bidzaar"]["complete"] == 0
    assert rows["Bidzaar"]["reason"] == "упёрлись в потолок страниц"
    assert rows["ТЭК-Торг"]["collected"] == 1
    assert rows["ТЭК-Торг"]["complete"] == 1


# ── 8. Сводка в UI/ленте: площадки видны ──────────────────────────────────────


def test_ui_summary_carries_platforms():
    """Блок состояния в ленте показывает, какая площадка недобрала: у сводки
    есть разбивка по площадкам, и строка каждого неполного объекта называет
    свою площадку."""

    reports = [
        comp.build_unit_report(
            "phrase", "Задвижки", platform="Bidzaar",
            expected_pages=5, collected_pages=2, reason=comp.REASON_PAGE_CAP,
        ),
        comp.build_unit_report(
            "firm", "АО «Х»", platform="ТЭК-Торг",
            expected_pages=2, collected_pages=2,
        ),
    ]
    messages: list[str] = []
    c = collect._build_completeness(reports, messages.append)

    assert c["checked"] is True
    assert c["data_complete"] is False
    platforms = {row["platform"]: row for row in c["platforms"]}
    assert platforms["Bidzaar"]["data_complete"] is False
    assert platforms["ТЭК-Торг"]["data_complete"] is True
    # Строка неполного объекта называет площадку — это же читает лист Excel
    # «Полнота сбора» (строка на площадку, а не общая безликая сводка).
    assert c["units"][0]["platform"] == "Bidzaar"
    assert c["units"][0]["label"].startswith("Bidzaar")
    assert any("Bidzaar" in m for m in messages)


def test_молчащая_площадка_не_получает_вердикт_целиком(tmp_path):
    """Включена, но не отчиталась — это «неизвестно», а не «всё собрано».

    04.09.2026 в живой базе у пяти площадок стояло «целиком» без единой
    цифры: запись бралась из общего флага прогона, хотя обход этих площадок
    никто не проверял. Такое «всё хорошо» опаснее прямого «не знаю».
    """
    from src.db import connect, ensure_schema
    from src.db.runs import last_run_platforms, record_run

    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    record_run(
        conn,
        {
            "started_at": "2026-09-04T03:00:00",
            "finished_at": "2026-09-04T03:30:00",
            "outcome": "ok",
            "data_complete": True,
            "summary": "тест",
            "platforms": {"parse_tektorg": True, "parse_bidzaar": True},
            "by_platform": [],
        },
    )
    rows = {r["platform"]: r for r in last_run_platforms(conn)}
    assert rows, "строки площадок должны появиться"
    for name, row in rows.items():
        assert row["complete"] is None, f"{name}: молчание выдано за полноту"
        assert "не отчиталась" in (row["reason"] or "")


def test_вердикт_различает_полноту_и_неизвестность():
    """«Собрано целиком» и «не знаем, сколько там было» — разные вещи.

    Площадка без счётчика выдачи на живом прогоне дала 73 объекта и ноль
    подтверждённо полных: площадка не сообщает объём выдачи. Формально
    прогон полон (unknown — не неполнота), но вердикт «собрано целиком»
    здесь означал бы проверку, которой не было.
    """
    from src.parsers.completeness import platform_verdict

    все_известно = {"units": 10, "complete": 10, "unknown": 0, "data_complete": True}
    объем_неизвестен = {"units": 73, "complete": 0, "unknown": 73, "data_complete": True}
    недобрали = {
        "units": 79, "complete": 78, "unknown": 0, "data_complete": False,
        "reason": "упёрлись в потолок страниц",
    }

    assert platform_verdict(все_известно) == "собрано целиком"
    assert "не сообщает объём" in platform_verdict(объем_неизвестен)
    assert platform_verdict(недобрали) == "упёрлись в потолок страниц"


def test_пустая_выдача_заказчика_не_считается_недобором():
    """Страницу открыли, лотов нет — обход состоялся целиком.

    На Tender.pro у 36 заказчиков из 40 нет активных закупок. Прежний счёт
    «страниц 0 из 1» объявлял такой обход неполным, и весь ночной прогон
    получал статус «данные неполные» на ровном месте (найдено 04.09.2026).
    """
    from src.parsers.completeness import build_unit_report

    report = build_unit_report(
        "company", "Ромашка", platform="Tender.pro",
        expected_pages=1, collected_pages=1, collected_lots=0,
    )
    assert report["status"] == "complete", report
