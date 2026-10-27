"""
Офлайн-тесты TektorgParser (ТЭК-Торг / tektorg.ru) — БЕЗ сети, БЕЗ браузера.

Ответы API берутся из фикстур tests/fixtures/ (реальные ответы площадки,
31.08.2026), сетевой загрузчик подменяется монкипатчем:
  • tektorg_keyword_response.json — поиск по фразе «антисептик»: 4 лота,
    все со статусом «Архив» (организаторы ПАО ГМК «Северный металл»
    и ООО ГРК «Северное»);
  • tektorg_section_response.json — секция nornikel: 15 лотов, все
    «Приём заявок».

Покрывают: маппинг полей; отсев архивных; остановку секционного обхода на
странице без активных; отбор по client_inns и по названию из companies;
битый JSON (не роняет прогон и не теряет собранное); даты ДД.ММ.ГГГГ и
«Не указана»; в запросе всегда sort="actual" и limit ≤ 50.

Запуск:
    pytest tests/test_tektorg_parser.py -v
"""

import asyncio
import copy
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.parsers.tektorg_parser as mod
from src.parsers import completeness
from src.parsers.tektorg_parser import TektorgParser

FIXTURES = Path(__file__).parent / "fixtures"
KEYWORD_FIXTURE = FIXTURES / "tektorg_keyword_response.json"
SECTION_FIXTURE = FIXTURES / "tektorg_section_response.json"

# ИНН организаторов из фикстур (для тестов отбора).
INN_NORNICKEL = "7700000001"
INN_BYSTRINSKOYE = "7700000002"
INN_KOLSKAYA = "5191431170"
INN_AIRPORT = "2457067174"

EMPTY_PAGE = '{"total": 0, "totalPages": 1, "currentPage": 1, "data": []}'


@pytest.fixture(autouse=True)
def _без_сети_открытого_api(monkeypatch):
    """Обход списка лотов (16.10.2026) не должен уходить в живую сеть."""

    def сеть_запрещена(url):
        raise AssertionError(f"сеть в тестах запрещена: {url}")

    monkeypatch.setattr(mod, "_fetch_get", сеть_запрещена)


def _parser(companies=None, keyword_phrases=None, client_inns=None):
    """TektorgParser без браузера (сеть в тестах подменяется)."""
    return TektorgParser(
        browser_manager=None,
        companies=companies if companies is not None else ["не используется"],
        days_back=1,
        keyword_phrases=keyword_phrases,
        client_inns=client_inns,
    )


def _kw_text() -> str:
    return KEYWORD_FIXTURE.read_text(encoding="utf-8")


def _sec_text() -> str:
    return SECTION_FIXTURE.read_text(encoding="utf-8")


def _sec_page_without_active() -> str:
    """Секционная страница, где все лоты ушли в архив (граница активных)."""
    data = json.loads(_sec_text())
    for lot in data["data"]:
        lot["statusName"] = "Архив"
        lot["statusLots"] = ["Архив"]
    return json.dumps(data, ensure_ascii=False)


def _patch_loader(monkeypatch, pages, delays=False, sections=None):
    """Подменить сетевой загрузчик списком ответов по страницам.

    Возвращает словарь со счётчиком вызовов и всеми отправленными телами
    запросов (для проверки sort/limit). Паузы между страницами обнуляем,
    чтобы тесты не спали по 1–2 секунды.
    """
    calls = {"n": 0, "payloads": []}

    def fake_fetch(payload):
        idx = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        calls["payloads"].append(copy.deepcopy(payload))
        return pages[idx]

    monkeypatch.setattr(mod, "_fetch_procedures", fake_fetch)
    if not delays:
        monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
        monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    if sections is not None:
        monkeypatch.setattr(mod, "PROFILE_SECTIONS", sections)
    return calls


# ── Маппинг полей ─────────────────────────────────────────────────────────────


def test_section_lot_mapped_to_tender(monkeypatch):
    """Лот секции → словарь тендера: все совместимые ключи на месте.

    Берём лоты «Аэропорта Северск» — у них есть и etpLink, и цена
    («173981.72 ₽»), в отличие от лотов Северметалла без ссылки.
    """
    _patch_loader(monkeypatch, [_sec_text()], sections=("nornikel",))
    tenders = asyncio.run(_parser(companies=[], client_inns=[INN_AIRPORT]).parse())
    assert len(tenders) == 2
    t = next(x for x in tenders if x["lot_id"] == "19728618")
    assert t["platform"] == "ТЭК-Торг"
    assert t["name"] == "Поставка электрооборудования"
    assert t["tender_no"] == "ЗП6081799"
    assert t["tender_type"] == "Запрос котировок"
    assert t["company"] == (
        'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "АЭРОПОРТ "СЕВЕРСК"'
    )
    assert t["search_key"] == "nornikel"  # секционный режим: ключ = секция
    assert t["organizer_href"] is None
    assert t["organizer_match"] is True  # отбор по ИНН сработал
    assert t["href"] == (
        "https://zakupki.tektorg.ru/#com/procedure/view/procedure/227720"
    )
    assert t["sum"] == 173981.72  # «173981.72 ₽» → float, как у остальных
    assert t["date_published"] == "24.08.2026"
    # Срок со временем по Москве (16.10.2026): подача до 10:00, а не весь день.
    assert t["date_deadline"] == "31.08.2026 10:00"
    assert t["date_to_display"] == "31.08.2026 10:00"
    assert t["inn"] == INN_AIRPORT


def test_keyword_lot_search_key_is_phrase(monkeypatch):
    """Keyword-режим: search_key — фраза, organizer_match=False, отбор
    по организатору не применяется (искали по фразе, как на ЕИС)."""
    data = json.loads(_sec_text())
    text = json.dumps(data, ensure_ascii=False)
    _patch_loader(monkeypatch, [text])
    tenders = asyncio.run(
        _parser(companies=["чужая компания"], keyword_phrases=["герметик"]).parse()
    )
    assert len(tenders) == 15  # все активные лоты секции, без отбора
    assert all(t["search_key"] == "герметик" for t in tenders)
    assert all(t["organizer_match"] is False for t in tenders)


# ── Отсев статусов ────────────────────────────────────────────────────────────


def test_keyword_fixture_all_archived_yields_nothing(monkeypatch):
    """Реальный ответ по «антисептику»: 4 лота, все «Архив» → пусто."""
    calls = _patch_loader(monkeypatch, [_kw_text()])
    tenders = asyncio.run(_parser(keyword_phrases=["антисептик"]).parse())
    assert tenders == []
    assert calls["n"] == 1  # totalPages=1 — второй страницы не запрашиваем


def test_cancelled_status_skipped(monkeypatch):
    """«Отменён» отбрасывается так же, как «Архив»."""
    data = json.loads(_sec_text())
    data["data"][0]["statusName"] = "Отменён"
    _patch_loader(monkeypatch, [json.dumps(data, ensure_ascii=False)])
    tenders = asyncio.run(_parser(companies=[], client_inns=[INN_NORNICKEL]).parse())
    assert len(tenders) == 1  # из двух лотов Северметалла один отменён


# ── Секционный режим: остановка и отбор ───────────────────────────────────────


def test_section_walk_stops_on_page_without_active(monkeypatch):
    """sort=actual держит активные впереди: страница без активных — стоп.

    Инкрементальный сбор «по дате» невозможен (страницы перемешаны),
    поэтому обход секции идёт до первой неактивной страницы.
    """
    calls = _patch_loader(
        monkeypatch,
        [_sec_text(), _sec_page_without_active(), EMPTY_PAGE],
        sections=("nornikel",),
    )
    tenders = asyncio.run(_parser(companies=[], client_inns=[INN_KOLSKAYA]).parse())
    assert calls["n"] == 2, "после страницы без активных обход должен прекратиться"
    assert len(tenders) == 8  # все лоты Кольской ГМК со страницы 1


def test_section_select_by_client_inns(monkeypatch):
    """Отбор по ИНН (фильтра на площадке нет — см. шапку парсера)."""
    _patch_loader(monkeypatch, [_sec_text()], sections=("nornikel",))
    tenders = asyncio.run(
        _parser(companies=[], client_inns=[INN_NORNICKEL, INN_BYSTRINSKOYE]).parse()
    )
    assert {t["inn"] for t in tenders} == {INN_NORNICKEL, INN_BYSTRINSKOYE}
    assert len(tenders) == 4  # 2 Северметалл + 2 Северное
    assert all(t["organizer_match"] is True for t in tenders)


def test_section_select_by_company_name(monkeypatch):
    """Без ИНН отбор идёт по organizerName через _norm_org из Tender.pro."""
    _patch_loader(monkeypatch, [_sec_text()], sections=("nornikel",))
    tenders = asyncio.run(_parser(companies=["ГРК «Северное»"]).parse())
    assert len(tenders) == 2
    assert all("Северное" in t["company"] for t in tenders)
    assert all(t["organizer_match"] is True for t in tenders)


def test_company_name_match_catches_branches(monkeypatch):
    """«Северный металл» ловит и филиалы: ЗФ/МТФ ПАО — те же ИНН/бренд."""
    _patch_loader(monkeypatch, [_sec_text()], sections=("nornikel",))
    tenders = asyncio.run(_parser(companies=["Северный металл"]).parse())
    assert len(tenders) == 2
    assert all("Северный металл" in t["company"] for t in tenders)


def test_no_phrases_no_companies_no_inns_skips_stage(monkeypatch):
    """Нечем отбирать — этап пропущен, в сеть не идём."""
    выстрелов = {"n": 0}

    def сеть_запрещена(payload):
        выстрелов["n"] += 1
        raise AssertionError("парсер не должен ходить в сеть без отбора")

    monkeypatch.setattr(mod, "_fetch_procedures", сеть_запрещена)
    assert asyncio.run(_parser(companies=[]).parse()) == []
    assert выстрелов["n"] == 0


# ── Устойчивость ──────────────────────────────────────────────────────────────


def test_broken_json_keeps_collected(monkeypatch, caplog):
    """Битый JSON на второй странице фразы — warning, собранное сохранено."""
    calls = _patch_loader(monkeypatch, [_sec_text(), "{это не JSON"])
    with caplog.at_level(logging.WARNING, logger="src.parsers.tektorg_parser"):
        tenders = asyncio.run(
            _parser(keyword_phrases=["герметик", "антисептик"]).parse()
        )
    # Первая фраза: страница 1 собрана, страница 2 — битый JSON, стоп.
    # Вторая фраза: фейк повторяет последнюю (битую) страницу — тоже стоп.
    assert calls["n"] == 3
    assert len(tenders) == 15
    assert any("ошибка загрузки/разбора" in r.message for r in caplog.records)


def test_network_error_returns_empty_with_warning(monkeypatch, caplog):
    """Сетевая ошибка — warning и [], прогон не падает."""

    def boom(payload):
        raise OSError("connection timed out")

    monkeypatch.setattr(mod, "_fetch_procedures", boom)
    with caplog.at_level(logging.WARNING, logger="src.parsers.tektorg_parser"):
        tenders = asyncio.run(_parser(keyword_phrases=["герметик"]).parse())
    assert tenders == []
    assert any("connection timed out" in r.message for r in caplog.records)


def test_not_a_json_object_treated_as_broken(monkeypatch, caplog):
    """JSON-массив вместо объекта — тот же мягкий отказ, не исключение."""
    _patch_loader(monkeypatch, ["[1, 2, 3]"])
    with caplog.at_level(logging.WARNING, logger="src.parsers.tektorg_parser"):
        assert asyncio.run(_parser(keyword_phrases=["герметик"]).parse()) == []
    assert any("JSON" in r.message for r in caplog.records)


# ── Даты ──────────────────────────────────────────────────────────────────────


def test_iso_dates_become_dd_mm_yyyy():
    """«2026-08-21T13:48:26+03:00» → «21.08.2026» (без времени)."""
    assert TektorgParser._fmt_date("2026-08-21T13:48:26+03:00") == "21.08.2026"
    assert TektorgParser._fmt_date("2025-12-31T23:59:59+03:00") == "31.12.2025"


def test_missing_dates_become_not_specified():
    """Нет даты — «Не указана», не выдумываем (как на ЕИС с дедлайном)."""
    assert TektorgParser._fmt_date(None) == "Не указана"
    assert TektorgParser._fmt_date("") == "Не указана"
    assert TektorgParser._fmt_date("завтра") == "Не указана"


def test_lot_without_dates_in_fixture(monkeypatch):
    """Лот с активным статусом, но без dates — даты «Не указана», лот остаётся."""
    data = json.loads(_sec_text())
    lot = data["data"][0]
    lot.pop("dates", None)
    _patch_loader(monkeypatch, [json.dumps(data, ensure_ascii=False)])
    tenders = asyncio.run(_parser(keyword_phrases=["герметик"]).parse())
    first = next(t for t in tenders if t["lot_id"] == str(lot["id"]))
    assert first["date_published"] == "Не указана"
    assert first["date_deadline"] == "Не указана"
    assert first["date_to_display"] == "Не указана"


# ── Цена ──────────────────────────────────────────────────────────────────────


def test_price_parsing():
    """«173981.72 ₽» → float; «НМЦ не установлена» → None."""
    assert TektorgParser._parse_price("173981.72 ₽") == 173981.72
    assert TektorgParser._parse_price("3 986 488,86 ₽") == 3986488.86
    assert TektorgParser._parse_price("НМЦ не установлена") is None
    assert TektorgParser._parse_price(None) is None
    assert TektorgParser._parse_price("") is None


# ── Контракт запроса: sort/limit ──────────────────────────────────────────────


def test_keyword_requests_always_actual_sort_and_limit_50(monkeypatch):
    """sort только "actual", limit ≤ 50 — другие значения ломают ответ API."""
    calls = _patch_loader(monkeypatch, [_kw_text()])
    asyncio.run(_parser(keyword_phrases=["антисептик"]).parse())
    assert calls["n"] >= 1
    for payload in calls["payloads"]:
        params = payload["params"]
        assert params["sort"] == "actual"
        assert isinstance(params["limit"], int) and 0 < params["limit"] <= 50
        assert params["name"] == "антисептик"
        # Фраза ищется по всем профильным секциям сразу.
        for i, code in enumerate(mod.PROFILE_SECTIONS):
            assert params[f"sectionsCodes[{i}]"] == code


def test_section_requests_have_no_name_and_page_counts(monkeypatch):
    """Секционный обход: без name, page растёт, sort/limit те же."""
    calls = _patch_loader(
        monkeypatch,
        [_sec_text(), _sec_page_without_active()],
        sections=("nornikel",),
    )
    asyncio.run(_parser(companies=[], client_inns=[INN_KOLSKAYA]).parse())
    assert [p["params"]["page"] for p in calls["payloads"]] == [1, 2]
    for payload in calls["payloads"]:
        params = payload["params"]
        assert "name" not in params
        assert params["sort"] == "actual"
        assert params["limit"] <= 50
        assert params["sectionsCodes[0]"] == "nornikel"


def test_dedup_across_phrases(monkeypatch):
    """Одна и та же страница по двум фразам → дедуп по lot_id (конвенция)."""
    _patch_loader(monkeypatch, [_sec_text(), _sec_text()])
    tenders = asyncio.run(
        _parser(keyword_phrases=["герметик", "герметик К-350"]).parse()
    )
    assert len(tenders) == 15
    assert len({t["lot_id"] for t in tenders}) == 15


# ── Настройки по умолчанию ────────────────────────────────────────────────────


def test_тэкторг_включен_по_умолчанию():
    """JSON API без авторизации, секции Северметалла и En+ — включён сразу."""
    from src.settings_store import DEFAULTS

    assert DEFAULTS["parse_tektorg"] is True


# ── Конвейер: оба режима работают вместе ─────────────────────────────────────
#
# Фразы в keywords.json включены почти всегда, поэтому связка «if phrases …
# else …» означала бы, что адресный обход по заказчикам не запускается
# никогда. А нужен именно он: закреплённый клиент прячет нашу позицию внутри
# сборного лота («Расходники, реактивы, посуда»), где фраза не сработает.


class _FakeTektorg:
    """Fake парсера: фиксирует, с какими аргументами его создали."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def parse(self):
        # Оба режима возвращают один и тот же лот — проверим заодно дедуп.
        return [{"lot_id": "42", "name": "Антисептик калия бутиловый"}]

    @staticmethod
    def _dedup(tenders):
        seen, out = set(), []
        for t in tenders:
            key = t.get("lot_id")
            if key and key not in seen:
                seen.add(key)
                out.append(t)
        return out


def _runner_with_fake(monkeypatch, companies, phrases, client_inns=()):
    _FakeTektorg.instances = []
    monkeypatch.setattr("src.core.parser_runner.TektorgParser", _FakeTektorg)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: phrases)
    # Справочник ИНН подменяем всегда: иначе тест зависел бы от реального
    # client_inns.json в корне репозитория (доставка ИНН проверяется
    # отдельно — tests/test_client_inns.py).
    monkeypatch.setattr(
        "src.client_inns_store.load_client_inns", lambda *a, **kw: list(client_inns)
    )

    from src.core.parser_runner import ParserRunner

    # ⚠️ Фразу-образец брать НЕ из tektorg_skip_phrases: с 10.09.2026 раннер
    # режет для ТЭК-Торга фразы без отдачи («антисептик», «НШ 2-12», …), и
    # keyword-режим на них просто не стартует — тест упал бы не по делу.
    runner = ParserRunner.__new__(ParserRunner)
    runner.browser_manager = None
    runner.companies = companies
    runner.days_back = 1
    runner.settings = {"parse_tektorg": True}
    return runner


def test_оба_режима_запускаются_когда_есть_и_фразы_и_компании(monkeypatch):
    """Фразы + компании → keyword-режим И адресный обход, а не «или-или»."""
    runner = _runner_with_fake(monkeypatch, ["Северметалл"], ["герметик"])
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 2, "должны запуститься оба режима"
    keyword_calls = [
        i for i in _FakeTektorg.instances if i.kwargs.get("keyword_phrases")
    ]
    section_calls = [
        i for i in _FakeTektorg.instances if not i.kwargs.get("keyword_phrases")
    ]
    assert len(keyword_calls) == 1
    assert len(section_calls) == 1


def test_один_и_тот_же_лот_из_двух_режимов_дедуплицируется(monkeypatch):
    """Лот закреплённого заказчика приходит обоими путями — в итоге один."""
    runner = _runner_with_fake(monkeypatch, ["Северметалл"], ["герметик"])
    tenders = asyncio.run(runner._run_tektorg())

    assert len(tenders) == 1


def test_без_компаний_и_без_ИНН_идет_только_keyword(monkeypatch):
    """Ни компаний, ни ИНН: адресному обходу не по чему отбирать."""
    runner = _runner_with_fake(monkeypatch, [], ["герметик"])
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 1
    assert _FakeTektorg.instances[0].kwargs.get("keyword_phrases") == ["герметик"]


def test_без_компаний_но_с_ИНН_адресный_обход_идет(monkeypatch):
    """ИНН самодостаточны: пустой companies.json обходу больше не помеха."""
    runner = _runner_with_fake(
        monkeypatch, [], ["герметик"], client_inns=["7700000001"]
    )
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 2
    address = [i for i in _FakeTektorg.instances if not i.kwargs.get("keyword_phrases")]
    assert address and address[0].kwargs.get("client_inns") == ["7700000001"]


def test_без_фраз_идет_только_адресный_обход(monkeypatch):
    """Все фразы выключены — площадка всё равно обходится по заказчикам."""
    runner = _runner_with_fake(monkeypatch, ["Северметалл"], [])
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 1
    assert not _FakeTektorg.instances[0].kwargs.get("keyword_phrases")


# ── Отчёт о полноте (ТЗ-8) ────────────────────────────────────────────────────


def test_unit_report_complete_when_total_pages_walked(monkeypatch):
    """После разбора фикстуры отчёт непуст: totalPages=1, собрали 1 страницу —
    complete, даже полезных лотов ноль (в фикстуре все «Архив»).

    Полнота считается по страницам, а не по лотам: сравнение по лотам
    объявляло бы неполным каждый успешный обход.
    """
    _patch_loader(monkeypatch, [_kw_text()])
    p = _parser(keyword_phrases=["антисептик"])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["platform"] == "ТЭК-Торг"
    assert report["kind"] == "phrase"
    assert report["expected_pages"] == 1
    assert report["collected_pages"] == 1
    assert report["status"] == completeness.UNIT_COMPLETE


def test_unit_report_section_active_stop_is_complete(monkeypatch):
    """Секционный обход останавливается на странице без активных лотов — это
    спроектированная граница (дальше только архив), а не неполнота."""
    _patch_loader(
        monkeypatch,
        [_sec_text(), _sec_page_without_active()],
        sections=("nornikel",),
    )
    p = _parser(companies=[], client_inns=[INN_KOLSKAYA])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["kind"] == "section"
    assert report["status"] == completeness.UNIT_COMPLETE
    # Вторая страница (без активных лотов) тоже считается пройденной — именно
    # она показала границу активных; ожидаемое число страниц приравнено
    # к пройденному, поэтому статус complete, а не partial.
    assert report["collected_pages"] == 2
