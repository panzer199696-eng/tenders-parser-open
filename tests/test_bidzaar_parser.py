"""
Офлайн-тесты BidzaarParser (bidzaar.com) — БЕЗ сети, БЕЗ браузера.

Ответы API берутся из фикстур tests/fixtures/ (сложены по контракту API,
проверенному живьём 03.09.2026), сетевой загрузчик
подменяется монкипатчем:
  • bidzaar_keyword_response.json — поиск по фразе «дозатор плунжерный»:
    2 лота, в том числе «РАСЦЕНКА. ЗИП к плунжерным дозаторам»
    ООО «ВОСТОЧНАЯ МЕДЬ»;
  • bidzaar_company_response.json — поиск по заказчику «Восточная медь»:
    лот самого заказчика и ЧУЖОЙ лот, где заказчик лишь упомянут в
    названии (проверка честного organizer_match=False).

Покрывают: маппинг полей; перевод дедлайна из UTC в московское время;
пустой ответ; битый JSON и сетевую ошибку; работу обоих режимов вместе;
search_key режима заказчика; organizer_match; пагинацию с потолком
MAX_PAGES; дедупликацию по id между режимами; дефолтную настройку и
подключение в раннере.

Запуск:
    pytest tests/test_bidzaar_parser.py -v
"""

import asyncio
import copy
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers import completeness  # noqa: E402  (ТЗ-8)
import src.parsers.bidzaar_parser as mod
from src.parsers.bidzaar_parser import BidzaarParser

FIXTURES = Path(__file__).parent / "fixtures"
KEYWORD_FIXTURE = FIXTURES / "bidzaar_keyword_response.json"
COMPANY_FIXTURE = FIXTURES / "bidzaar_company_response.json"

# id лотов из фикстур (могут встречаться в обеих — для теста дедупа).
LOT_ZIP_ID = "1cdf7785-3f2e-4a5b-8c9d-0e1f2a3b4c5d"
# Ссылка строится по procedureId — карточка лота живёт по нему (04.09.2026).
LOT_ZIP_PROCEDURE_ID = "01a06ad3-3a16-7c33-88fe-ae3405bcfd61"
LOT_HITACHI_ID = "3d0f99a7-5b4c-6d7e-8f90-1a2b3c4d5e6f"

PHRASE = "дозатор плунжерный"
CLIENT = "Восточная медь"


# «Сегодня» для сторожа свежести: фикстуры сняты 01.09.2026, и с часами ПК
# каждый тест через пару дней видел бы «лента не обновляется».
FIXTURE_TODAY = date(2026, 9, 2)


def _parser(companies=None, keyword_phrases=None, client_names=None, today=FIXTURE_TODAY):
    """BidzaarParser без браузера (сеть в тестах подменяется)."""
    return BidzaarParser(
        browser_manager=None,
        companies=companies if companies is not None else [],
        days_back=1,
        keyword_phrases=keyword_phrases,
        client_names=client_names,
        today=today,
    )


def _kw_text() -> str:
    return KEYWORD_FIXTURE.read_text(encoding="utf-8")


def _company_text() -> str:
    return COMPANY_FIXTURE.read_text(encoding="utf-8")


def _patch_loader(monkeypatch, pages):
    """Подменить сетевой загрузчик списком ответов по страницам.

    Возвращает словарь со счётчиком вызовов и всеми отправленными
    наборами параметров (для проверки контракта запроса). Паузы между
    страницами обнуляем, чтобы тесты не спали по 1–2 секунды.
    """
    calls = {"n": 0, "payloads": []}

    def fake_fetch(params):
        idx = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        calls["payloads"].append(copy.deepcopy(params))
        return pages[idx]

    monkeypatch.setattr(mod, "_fetch_tenders", fake_fetch)
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    return calls


# ── 1. Маппинг полей ──────────────────────────────────────────────────────────


def test_lot_mapped_to_tender(monkeypatch):
    """Лот из ответа → словарь тендера со всеми полями по таблице ТЗ-6."""
    _patch_loader(monkeypatch, [_kw_text()])
    tenders = asyncio.run(_parser(keyword_phrases=[PHRASE]).parse())
    assert len(tenders) == 2
    t = next(x for x in tenders if x["lot_id"] == LOT_ZIP_ID)
    assert t["platform"] == "Bidzaar"
    assert t["name"] == "РАСЦЕНКА. ЗИП к плунжерным дозаторам"
    assert t["company"] == 'ООО "ВОСТОЧНАЯ МЕДЬ"'
    assert t["search_key"] == PHRASE
    assert t["href"] == f"https://bidzaar.com/app/process/light/{LOT_ZIP_PROCEDURE_ID}"
    assert t["lot_id"] == LOT_ZIP_ID
    assert t["tender_no"] == "369-685"
    assert t["date_published"] == "01.09.2026"
    assert t["date_deadline"] == "04.09.2026 12:00"
    assert t["date_to_display"] == "04.09.2026 12:00"
    assert t["sum"] is None  # цену площадка не отдаёт
    assert t["organizer_match"] is False  # keyword-режим: искали по фразе


# ── 2. Дедлайн переводится в московское время ────────────────────────────────


def test_deadline_converted_from_utc_to_msk():
    """«2026-09-04T09:00:00Z» — это 12:00 по Москве, а не 09:00."""
    assert BidzaarParser._fmt_deadline("2026-09-04T09:00:00Z") == "04.09.2026 12:00"
    assert BidzaarParser._fmt_deadline("2026-09-03T21:30:00Z") == "04.09.2026 00:30"


def test_published_date_is_msk_date():
    """Дата публикации тоже по Москве: 23:00 UTC — уже следующий день МСК."""
    assert BidzaarParser._fmt_published("2026-09-01T23:00:00Z") == "02.09.2026"


def test_missing_dates_become_not_specified():
    """Нет даты — «Не указана», не выдумываем (конвенция остальных парсеров)."""
    assert BidzaarParser._fmt_deadline(None) == "Не указана"
    assert BidzaarParser._fmt_deadline("") == "Не указана"
    assert BidzaarParser._fmt_deadline("завтра") == "Не указана"
    assert BidzaarParser._fmt_published(None) == "Не указана"


# ── 3. Пустой ответ ───────────────────────────────────────────────────────────


def test_empty_items_yield_empty_list(monkeypatch):
    """items: [] не роняет парсер и даёт пустой список."""
    _patch_loader(monkeypatch, [json.dumps({"totalCount": 0, "items": []})])
    assert asyncio.run(_parser(keyword_phrases=[PHRASE]).parse()) == []


# ── 4. Битый JSON и HTTP-ошибка ───────────────────────────────────────────────


def test_broken_json_returns_empty_with_warning(monkeypatch, caplog):
    """Битый JSON — warning с причиной и пустой результат, не исключение."""
    _patch_loader(monkeypatch, ["{это не JSON"])
    with caplog.at_level(logging.WARNING, logger="src.parsers.bidzaar_parser"):
        tenders = asyncio.run(_parser(keyword_phrases=[PHRASE]).parse())
    assert tenders == []
    assert any("ошибка загрузки/разбора" in r.message for r in caplog.records)


def test_http_error_returns_empty_with_warning(monkeypatch, caplog):
    """Сетевая ошибка — warning и [], прогон не падает."""

    def boom(params):
        raise OSError("connection timed out")

    monkeypatch.setattr(mod, "_fetch_tenders", boom)
    with caplog.at_level(logging.WARNING, logger="src.parsers.bidzaar_parser"):
        tenders = asyncio.run(_parser(keyword_phrases=[PHRASE]).parse())
    assert tenders == []
    assert any("connection timed out" in r.message for r in caplog.records)


def test_not_a_json_object_treated_as_broken(monkeypatch, caplog):
    """JSON-массив вместо объекта — тот же мягкий отказ, не исключение."""
    _patch_loader(monkeypatch, ["[1, 2, 3]"])
    with caplog.at_level(logging.WARNING, logger="src.parsers.bidzaar_parser"):
        assert asyncio.run(_parser(keyword_phrases=[PHRASE]).parse()) == []
    assert any("JSON" in r.message for r in caplog.records)


# ── 5. Оба режима работают вместе ────────────────────────────────────────────


def test_both_modes_requested_together(monkeypatch):
    """Фразы И заказчики: запрашивается и то, и другое (не «или-или»)."""
    calls = _patch_loader(monkeypatch, [_kw_text(), _company_text()])
    asyncio.run(
        _parser(
            companies=[CLIENT],
            keyword_phrases=[PHRASE],
            client_names=[CLIENT],
        ).parse()
    )
    searches = [p["search"] for p in calls["payloads"]]
    assert searches == [PHRASE, CLIENT]


def test_request_contract_search_param_and_paging(monkeypatch):
    """Параметр поиска — строго `search` (не searchText/query), size ≤ 200.

    Опечатка в имени параметра не даёт ошибки — площадка молча возвращает
    всю базу в 105 тысяч лотов, поэтому имя зафиксировано и проверяется
    тестом (контракт из ТЗ-6, проверен живьём 03.09.2026).
    """
    calls = _patch_loader(monkeypatch, [_kw_text()])
    asyncio.run(_parser(keyword_phrases=[PHRASE]).parse())
    assert calls["payloads"], "должен был уйти хотя бы один запрос"
    for params in calls["payloads"]:
        assert params["search"] == PHRASE
        assert params["paging.page"] >= 1
        assert 0 < params["paging.size"] <= 200
        assert params["sorting.key"] == "acceptanceStartDate"
        assert params["sorting.direction"] == "desc"
        # Никаких «почти правильных» имён параметра поиска.
        assert "searchText" not in params
        assert "query" not in params


# ── 6–7. Режим заказчика: search_key и organizer_match ───────────────────────


def test_company_mode_search_key_is_client_name(monkeypatch):
    """search_key лота из режима заказчика — название заказчика, не фраза."""
    _patch_loader(monkeypatch, [_company_text()])
    tenders = asyncio.run(_parser(client_names=[CLIENT]).parse())
    assert len(tenders) == 2
    assert all(t["search_key"] == CLIENT for t in tenders)


def test_organizer_match_true_for_own_lots_false_for_mentions(monkeypatch):
    """Совпадение названия клиента с company.name — честная сверка.

    Поиск по заказчику нестрогий: «Восточная медь» приводит и ЧУЖОЙ лот,
    где заказчик лишь упомянут в названии (Hitachi) — ему False.
    """
    _patch_loader(monkeypatch, [_company_text()])
    tenders = asyncio.run(_parser(client_names=[CLIENT]).parse())
    own = next(t for t in tenders if t["lot_id"] == LOT_ZIP_ID)
    mention = next(t for t in tenders if t["lot_id"] == LOT_HITACHI_ID)
    assert own["organizer_match"] is True
    assert mention["organizer_match"] is False


def test_companies_used_as_client_names_when_omitted(monkeypatch):
    """client_names не передан — адресный обход идёт по companies."""
    _patch_loader(monkeypatch, [_company_text()])
    tenders = asyncio.run(_parser(companies=[CLIENT]).parse())
    assert all(t["search_key"] == CLIENT for t in tenders)


# ── 8. Пагинация и потолок MAX_PAGES ─────────────────────────────────────────


def test_pagination_requests_next_page(monkeypatch):
    """totalCount больше страницы — запрашивается следующая страница."""
    page1 = json.loads(_kw_text())
    page1["totalCount"] = 250  # 1 × 100 < 250 — должна быть вторая страница
    page2 = {"totalCount": 250, "items": []}
    calls = _patch_loader(
        monkeypatch, [json.dumps(page1, ensure_ascii=False), json.dumps(page2)]
    )
    tenders = asyncio.run(_parser(keyword_phrases=[PHRASE]).parse())
    assert [p["paging.page"] for p in calls["payloads"]] == [1, 2]
    assert len(tenders) == 2  # обе позиции с первой страницы


def test_walk_stops_at_max_pages_with_warning(monkeypatch, caplog):
    """Потолок MAX_PAGES останавливает обход, и об этом пишется в лог."""
    # Гигантская выдача (как вся база в 105 тысяч лотов) — обход обязан
    # упереться в потолок, а не листать вечно.
    full = json.dumps({"totalCount": 105597, "items": json.loads(_kw_text())["items"]})
    calls = _patch_loader(monkeypatch, [full] * (mod.MAX_PAGES + 5))
    monkeypatch.setattr(mod, "MAX_PAGES", 3)  # меньше страниц — быстрее тест
    with caplog.at_level(logging.WARNING, logger="src.parsers.bidzaar_parser"):
        tenders = asyncio.run(_parser(keyword_phrases=[PHRASE]).parse())
    assert calls["n"] == 3, "обход обязан остановиться на MAX_PAGES"
    # Одна и та же страница трижды: до дедупа 6, после дедупа по id — 2.
    assert len(tenders) == 2
    assert any("потолок обхода" in r.message for r in caplog.records)


# ── 9. Дедупликация между режимами ───────────────────────────────────────────


def test_same_id_from_both_modes_deduplicated(monkeypatch):
    """Один и тот же id из режима фразы и режима заказчика — одна запись.

    Лот «ЗИП к плунжерным дозаторам» есть в обеих фикстурах: найден и
    по фразе, и поиском по заказчику «Восточная медь».
    """
    _patch_loader(monkeypatch, [_kw_text(), _company_text()])
    tenders = asyncio.run(
        _parser(
            companies=[CLIENT],
            keyword_phrases=[PHRASE],
            client_names=[CLIENT],
        ).parse()
    )
    ids = [t["lot_id"] for t in tenders]
    assert len(ids) == len(set(ids)) == 3  # 2 фразовых + 1 чужой заказчий
    assert ids.count(LOT_ZIP_ID) == 1


# ── Настройки по умолчанию ────────────────────────────────────────────────────


def test_bidzaar_включен_по_умолчанию():
    """Площадка бесплатная, без авторизации, отдаёт нашу номенклатуру —
    включаем сразу (решение ТЗ-6)."""
    from src.settings_store import DEFAULTS

    assert DEFAULTS["parse_bidzaar"] is True


# ── Подключение в раннере ─────────────────────────────────────────────────────


class _FakeBidzaar:
    """Fake парсера: фиксирует, с какими аргументами его создали."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def parse(self):
        return [{"lot_id": "42", "name": "Дозатор плунжерный"}]


def _runner_with_fake(monkeypatch, companies, phrases):
    _FakeBidzaar.instances = []
    monkeypatch.setattr("src.core.parser_runner.BidzaarParser", _FakeBidzaar)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: phrases)

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner.__new__(ParserRunner)
    runner.browser_manager = None
    runner.companies = companies
    runner.days_back = 1
    runner.settings = {"parse_bidzaar": True}
    return runner


def test_раннер_передаёт_фразы_и_названия_заказчиков(monkeypatch):
    """Оба режима в одном парсере: фразы из keywords.json, названия — из
    companies.json (фильтра по companyIds у API нет — см. ТЗ-6)."""
    runner = _runner_with_fake(monkeypatch, [CLIENT], [PHRASE])
    tenders = asyncio.run(runner._run_bidzaar())

    assert len(_FakeBidzaar.instances) == 1
    kwargs = _FakeBidzaar.instances[0].kwargs
    assert kwargs["keyword_phrases"] == [PHRASE]
    assert kwargs["client_names"] == [CLIENT]
    assert len(tenders) == 1


def test_раннер_без_фраз_и_компаний_пропускает_этап(monkeypatch):
    """Ни фраз, ни заказчиков — в сеть не идём, парсер не создаётся."""
    runner = _runner_with_fake(monkeypatch, [], [])
    assert asyncio.run(runner._run_bidzaar()) == []
    assert _FakeBidzaar.instances == []


def test_раннер_только_фразы_без_компаний(monkeypatch):
    """Пустой companies.json — фразовый режим всё равно работает."""
    runner = _runner_with_fake(monkeypatch, [], [PHRASE])
    asyncio.run(runner._run_bidzaar())
    kwargs = _FakeBidzaar.instances[0].kwargs
    assert kwargs["keyword_phrases"] == [PHRASE]
    assert kwargs["client_names"] is None


# ── 10. Отчёт о полноте (ТЗ-8) ────────────────────────────────────────────────


def test_unit_report_complete_for_fixture(monkeypatch):
    """После разбора фикстуры отчёт непуст и обещанное собрано целиком.

    totalCount=2 при PAGE_SIZE=100 — ожидается 1 страница, она и собрана:
    даже полная по лотам последняя страница неполна «по страницам» быть
    не может (полнота считается по страницам — см. completeness.py).
    """
    _patch_loader(monkeypatch, [_kw_text()])
    p = _parser(keyword_phrases=[PHRASE])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["platform"] == "Bidzaar"
    assert report["kind"] == "phrase"
    assert report["label"] == PHRASE
    assert report["expected_pages"] == 1
    assert report["collected_pages"] == 1
    assert report["status"] == completeness.UNIT_COMPLETE


def test_unit_report_company_mode_kind(monkeypatch):
    """Адресный обход помечает объект как заказчика, а не фразу."""
    _patch_loader(monkeypatch, [_company_text()])
    p = _parser(client_names=[CLIENT])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["kind"] == "company"
    assert report["platform"] == "Bidzaar"


def test_ссылка_ведёт_на_карточку_лота_а_не_в_404():
    """Карточка живёт по procedureId; поле id открывает несуществующую страницу.

    Найдено 04.09.2026 по жалобе: все ссылки Bidzaar в выгрузке давали 404.
    Подмена не была видна при разведке, потому что SPA отвечает 200 на любой
    путь — пустую страницу пришлось ловить кликом по списку.
    """
    parser = BidzaarParser(None, [], 1)
    lot = {
        "id": "034c986f-a91f-4f6f-81b5-b6623d91f8be",
        "procedureId": "01a06ad3-3a16-7c33-88fe-ae3405bcfd6e",
        "name": "Герметик",
        "company": {"name": "АО Тест"},
    }
    t = parser._lot_to_tender(lot, "герметик", False)
    assert t["href"] == "https://bidzaar.com/app/process/light/01a06ad3-3a16-7c33-88fe-ae3405bcfd6e"
    assert "requests/external" not in t["href"]


def test_без_procedureId_ссылку_не_выдумываем():
    """Пустая ссылка честнее ведущей в никуда."""
    parser = BidzaarParser(None, [], 1)
    lot = {"id": "abc", "procedureId": None, "name": "Лот", "company": {"name": "АО"}}
    assert parser._lot_to_tender(lot, "фраза", False)["href"] == ""


def test_ссылка_агрегированного_лота_ведёт_на_первоисточник():
    """У чужих лотов своей карточки на Bidzaar нет — ведём на площадку-источник.

    Разобрано 04.09.2026 по жалобе «все ссылки дают 404»: агрегатор отдаёт и
    свои лоты (procedureId заполнен), и собранные с других площадок
    (procedureId пуст, а в externalId — номер процедуры первоисточника).
    """
    from src.parsers.bidzaar_parser import build_lot_href

    # Свой лот Bidzaar
    assert build_lot_href("01a06ad3-3a16", "369-685").endswith("/app/process/light/01a06ad3-3a16")
    # Номер первоисточника из шести-восьми цифр (B2B-Center — публичная ссылка на процедуру)
    assert build_lot_href("", "4586736") == "https://www.b2b-center.ru/market/view.html?id=4586736"
    # Лот из ЕИС: девятнадцать цифр
    assert "zakupki.gov.ru" in build_lot_href("", "0372200052626000377")
    # Номер чужого формата — ссылку не выдумываем
    assert build_lot_href("", "20059101/2") == ""
    assert build_lot_href("", "") == ""


# ── 11. Сторож площадки (В6, 16.10.2026) ─────────────────────────────────────
#
# С 13.10.2026 агрегатор перестал подтягивать лоты других площадок, а отчёт
# каждую ночь писал «собрано целиком» на ~600 старых лотах. Сторож обязан
# сказать «лента не обновляется», «формат сменился», «не пускают».


def _platform_reasons(p):
    return [
        r["reason"] for r in p.unit_reports if r["kind"] == completeness.KIND_PLATFORM
    ]


def _with_items(items, total=None):
    return json.dumps(
        {"totalCount": len(items) if total is None else total, "items": items},
        ensure_ascii=False,
    )


def _lot(lot_id, start, status=1, **extra):
    lot = {
        "id": lot_id,
        "name": f"Лот {lot_id}",
        "externalId": "4639387",
        "status": status,
        "acceptanceStartDate": start,
        "acceptanceEndDate": "2026-10-30T09:00:00Z",
        "company": {"name": "АО Тест"},
        "procedureId": None,
        "finishDate": None,
    }
    lot.update(extra)
    return lot


def test_свежая_лента_без_тревоги(monkeypatch):
    _patch_loader(monkeypatch, [_kw_text()])
    p = _parser(keyword_phrases=[PHRASE])
    asyncio.run(p.parse())
    assert _platform_reasons(p) == []


def test_лента_старше_двух_рабочих_дней_это_stale(monkeypatch):
    """Свежайший лот 01.09, сегодня 10.09 — площадка перестала обновляться."""
    _patch_loader(monkeypatch, [_kw_text()])
    p = _parser(keyword_phrases=[PHRASE], today=date(2026, 9, 10))
    tenders = asyncio.run(p.parse())
    assert len(tenders) == 2  # лоты не выбрасываем — только предупреждаем
    assert _platform_reasons(p) == [completeness.REASON_STALE]


def test_дата_из_будущего_не_делает_ленту_свежей(monkeypatch):
    """Живьём 16.10.2026 у внешних лотов встречалось acceptanceStartDate
    2026-12-31 — это не публикация, по ней свежесть не меряем."""
    items = [_lot("a", "2026-10-09T10:00:00Z"), _lot("b", "2026-12-31T17:00:00Z")]
    _patch_loader(monkeypatch, [_with_items(items)])
    p = _parser(keyword_phrases=[PHRASE], today=date(2026, 10, 16))
    asyncio.run(p.parse())
    assert _platform_reasons(p) == [completeness.REASON_STALE]


def test_завершённые_и_отменённые_не_берём(monkeypatch):
    items = [
        _lot("live", "2026-10-15T10:00:00Z"),
        _lot("done", "2026-10-15T10:00:00Z", status=3),
        _lot("finished", "2026-10-15T10:00:00Z", finishDate="2026-10-15T12:00:00Z"),
    ]
    _patch_loader(monkeypatch, [_with_items(items)])
    p = _parser(keyword_phrases=[PHRASE], today=date(2026, 10, 16))
    tenders = asyncio.run(p.parse())
    assert [t["lot_id"] for t in tenders] == ["live"]
    assert _platform_reasons(p) == []


def test_все_лоты_отсеяны_статусом_это_смена_формата(monkeypatch):
    """Если площадка поменяет значения status, отсев по нему съест всё —
    это не «нет закупок», а повод править программу."""
    items = [_lot("x", "2026-10-15T10:00:00Z", status=7)]
    _patch_loader(monkeypatch, [_with_items(items)])
    p = _parser(keyword_phrases=[PHRASE], today=date(2026, 10, 16))
    assert asyncio.run(p.parse()) == []
    assert completeness.REASON_FORMAT_CHANGED in _platform_reasons(p)


def test_ответ_без_items_это_смена_формата(monkeypatch):
    renamed = json.dumps({"totalCount": 2, "data": json.loads(_kw_text())["items"]})
    _patch_loader(monkeypatch, [renamed])
    p = _parser(keyword_phrases=[PHRASE])
    assert asyncio.run(p.parse()) == []
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["status"] == completeness.UNIT_SKIPPED
    assert unit["reason"] == completeness.REASON_FORMAT_CHANGED
    assert completeness.REASON_FORMAT_CHANGED in _platform_reasons(p)


def test_ответ_без_totalcount_это_смена_формата(monkeypatch):
    renamed = json.dumps({"total": 2, "items": json.loads(_kw_text())["items"]})
    _patch_loader(monkeypatch, [renamed])
    p = _parser(keyword_phrases=[PHRASE])
    asyncio.run(p.parse())
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["reason"] == completeness.REASON_FORMAT_CHANGED


def test_пустой_ответ_200_это_смена_формата(monkeypatch):
    _patch_loader(monkeypatch, [""])
    p = _parser(keyword_phrases=[PHRASE])
    assert asyncio.run(p.parse()) == []
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["reason"] == completeness.REASON_FORMAT_CHANGED


def test_капча_вместо_json_это_блокировка(monkeypatch):
    _patch_loader(
        monkeypatch, ["<html><body>Подтвердите, что вы не робот</body></html>"]
    )
    p = _parser(keyword_phrases=[PHRASE])
    assert asyncio.run(p.parse()) == []
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["reason"] == completeness.REASON_BLOCKED


def test_http_403_это_блокировка(monkeypatch):
    import urllib.error

    def forbidden(params):
        raise urllib.error.HTTPError(mod.API_URL, 403, "Forbidden", {}, None)

    monkeypatch.setattr(mod, "_fetch_tenders", forbidden)
    p = _parser(keyword_phrases=[PHRASE])
    asyncio.run(p.parse())
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["reason"] == completeness.REASON_BLOCKED


def test_лоты_есть_распознано_ноль_это_смена_формата(monkeypatch):
    _patch_loader(monkeypatch, [json.dumps({"totalCount": 2, "items": ["a", "b"]})])
    p = _parser(keyword_phrases=[PHRASE])
    assert asyncio.run(p.parse()) == []
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["reason"] == completeness.REASON_FORMAT_CHANGED


def test_сбой_одного_ключа_оставляет_отчёт_и_не_рвёт_обход(monkeypatch):
    """Исключение в обходе ключа — отчёт «сбой», остальные ключи идут дальше."""
    _patch_loader(monkeypatch, [_company_text()])
    real_walk = BidzaarParser._walk

    async def flaky(self, label, search, organizer_match_mode):
        if label == PHRASE:
            raise RuntimeError("неожиданное")
        return await real_walk(self, label, search, organizer_match_mode)

    monkeypatch.setattr(BidzaarParser, "_walk", flaky)
    p = _parser(keyword_phrases=[PHRASE], client_names=[CLIENT])
    tenders = asyncio.run(p.parse())
    assert tenders  # заказчик обойдён
    by_label = {r["label"]: r for r in p.unit_reports}
    assert by_label[PHRASE]["status"] == completeness.UNIT_SKIPPED
    assert by_label[PHRASE]["reason"] == completeness.REASON_CRASHED
    assert by_label[CLIENT]["status"] == completeness.UNIT_COMPLETE


def test_живой_ответ_16_10_собран_целиком_но_лента_застыла(monkeypatch):
    """Контрактная фикстура: живой ответ на «герметик» 16.10.2026.

    Формат прежний (items/totalCount, status=1), все 11 лотов разобраны,
    объект обойдён целиком. Но свежайшая публикация — 09.10: агрегатор
    перестал подтягивать лоты других площадок (свежий лот другой площадки
    от 14.10 на Bidzaar не находится), и сторож это говорит.
    """
    text = (FIXTURES / "bidzaar_live_keyword_20261016.json").read_text(
        encoding="utf-8"
    )
    _patch_loader(monkeypatch, [text])
    p = _parser(keyword_phrases=["герметик"], today=date(2026, 10, 16))
    tenders = asyncio.run(p.parse())
    assert len(tenders) == 11
    [unit] = [r for r in p.unit_reports if r["kind"] == "phrase"]
    assert unit["status"] == completeness.UNIT_COMPLETE
    assert _platform_reasons(p) == [completeness.REASON_STALE]
    hrefs = {t["tender_no"]: t["href"] for t in tenders}
    assert hrefs["4635453"].startswith("https://www.b2b-center.ru/")
