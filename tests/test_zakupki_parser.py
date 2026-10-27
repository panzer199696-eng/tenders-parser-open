# -*- coding: utf-8 -*-
"""
Офлайн-тесты ZakupkiParser (ЕИС / zakupki.gov.ru) — БЕЗ сети, БЕЗ браузера.

RSS берётся из фикстуры tests/fixtures/zakupki_rss_sample.xml (реальный ответ
площадки), сетевой загрузчик подменяется монкипатчем. Покрывают:
  • разбор фикстуры: 4 лота, поля первого предмета/заказчика/цены/ФЗ/дат/
    lot_id/href;
  • цену: «Начальная цена контракта: 2467180.00» → 2467180.0, отсутствие → None;
  • очистку HTML-мнемоник в названии и заказчике;
  • date_deadline == «Не указана» (в RSS этой даты нет);
  • пустой RSS → []; битый XML → [] + warning;
  • пагинацию: пустая вторая страница останавливает обход; дедуп по lot_id.

Запуск:
    pytest tests/test_zakupki_parser.py -v
"""

import asyncio
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.parsers.zakupki_parser as mod  # noqa: E402
from src.parsers.zakupki_parser import ZakupkiParser  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "zakupki_rss_sample.xml"

EMPTY_RSS = '<rss version="2.0"><channel><title>пусто</title></channel></rss>'


def _parser(keyword_phrases=None):
    """ZakupkiParser без браузера (сеть в тестах подменяется)."""
    return ZakupkiParser(
        browser_manager=None,
        companies=["не используется"],
        days_back=1,
        keyword_phrases=keyword_phrases or ["герметик"],
    )


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _patch_loader(monkeypatch, pages, delays=True):
    """Подменить сетевой загрузчик списком ответов по страницам.

    Возвращает счётчик вызовов. Паузы между страницами обнуляем, чтобы тесты
    не спали по 1–2 секунды.
    """
    calls = {"n": 0}

    def fake_fetch(url):
        idx = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        return pages[idx]

    monkeypatch.setattr(mod, "_fetch_rss", fake_fetch)
    if not delays:
        monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
        monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    return calls


# ── Разбор фикстуры ──────────────────────────────────────────────────────────


def test_fixture_yields_four_lots(monkeypatch):
    """Фикстура — реальный ответ ЕИС: из 4 item должны получиться 4 тендера."""
    _patch_loader(monkeypatch, [_fixture_text()], delays=False)
    tenders = asyncio.run(_parser().parse())
    assert len(tenders) == 4


def test_first_lot_fields(monkeypatch):
    """Первый лот фикстуры: предмет, заказчик, цена, ФЗ, дата, lot_id, href."""
    _patch_loader(monkeypatch, [_fixture_text()], delays=False)
    t = asyncio.run(_parser().parse())[0]
    assert t["platform"] == "ЕИС"
    assert t["name"] == (
        "Поставка Герметика Ультрафлока К-350 для нужд Южного филиала "
        "АО ПРИМЕР-СЕТИ (закупка среди субъектов МСП) (614/2026)"
    )
    assert t["company"] == (
        'АКЦИОНЕРНОЕ ОБЩЕСТВО "СИБИРСКО-УРАЛЬСКАЯ ЭНЕРГЕТИЧЕСКАЯ КОМПАНИЯ"'
    )
    assert t["sum"] == 565575.0
    assert t["law"] == "223-ФЗ"
    assert t["stage"] == "Подача заявок"
    assert t["date_published"] == "26.08.2026"
    assert t["date_to_display"] == "26.08.2026"  # «Обновлено»
    assert t["lot_id"] == "32616324428"
    assert t["href"] == (
        "https://zakupki.gov.ru/223/purchase/public/purchase/info/"
        "common-info.html?regNumber=32616324428"
    )
    assert t["tender_no"] == "32616324428"
    assert t["tender_type"].startswith("Аукцион в электронной форме")
    assert t["search_key"] == "герметик"
    assert t["organizer_match"] is True


def test_title_number_with_inner_no(monkeypatch):
    """«Закрытый аукцион … Закона № 44-ФЗ №0371100002120000092»: номер —
    последний, № 44-ФЗ внутри названия номером не считается."""
    _patch_loader(monkeypatch, [_fixture_text()], delays=False)
    tenders = asyncio.run(_parser().parse())
    by_lot = {t["lot_id"]: t for t in tenders}
    t = by_lot["0371100002120000092"]
    assert t["tender_no"] == "0371100002120000092"
    assert t["tender_type"] == (
        "Закрытый аукцион с учетом положений ст. 111 Закона № 44-ФЗ"
    )


# ── Цена ─────────────────────────────────────────────────────────────────────


def _fields_of(description_html):
    return ZakupkiParser._description_fields(description_html)


def test_price_line_becomes_float():
    """«Начальная цена контракта: 2467180.00» → 2467180.0 (не строка)."""
    fields = _fields_of(
        "<strong>Начальная цена контракта: </strong>2467180.00"
        "<strong> Валюта: </strong>Российский рубль<br/>"
    )
    assert ZakupkiParser._parse_price(fields["price"]) == 2467180.0


def test_missing_price_is_none():
    """Меток цены нет — sum должен стать None, а не 0 и не строкой."""
    fields = _fields_of("<strong>Размещено: </strong>26.08.2026<br/>")
    assert "price" not in fields
    assert ZakupkiParser._parse_price(fields.get("price", "")) is None


def test_dirty_price_garbage_is_none():
    """Нераспознаваемое значение цены не должно ронять разбор."""
    assert ZakupkiParser._parse_price("не указана") is None
    assert ZakupkiParser._parse_price("") is None


# ── HTML-мнемоники ───────────────────────────────────────────────────────────


def test_html_entities_cleaned_in_name_and_company():
    """Мнемоники (&quot; и двойные) в названии и заказчике очищаются."""
    item = ET.Element("item")
    ET.SubElement(item, "title").text = "Электронный аукцион №101"
    ET.SubElement(
        item, "link"
    ).text = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=101"
    ET.SubElement(item, "description").text = (
        "<strong>Наименование объекта закупки: </strong>"
        "Поставка реагента &laquo;Известь&amp;quot;Гашеная&amp;quot;&raquo;<br/>"
        "<strong>Наименование Заказчика: </strong>НАО &quot;НИПИ&quot;<br/>"
        "<strong>Начальная цена контракта: </strong>100.00"
        "<strong> Валюта: </strong>Российский рубль<br/>"
    )
    t = _parser()._item_to_tender(item, "известь")
    assert "&quot;" not in t["name"] and "&amp;" not in t["name"]
    assert "laquo" not in t["name"]
    assert "«" in t["name"] or '"' in t["name"]
    assert t["company"] == 'НАО "НИПИ"'
    assert t["sum"] == 100.0


# ── Дедлайн ──────────────────────────────────────────────────────────────────


def test_deadline_always_not_specified(monkeypatch):
    """Даты окончания подачи заявок в RSS ЕИС нет — «Не указана», не выдумываем."""
    _patch_loader(monkeypatch, [_fixture_text()], delays=False)
    tenders = asyncio.run(_parser().parse())
    assert tenders
    assert all(t["date_deadline"] == "Не указана" for t in tenders)


# ── Устойчивость ─────────────────────────────────────────────────────────────


def test_empty_rss_returns_empty_list(monkeypatch):
    """Пустой RSS — не ошибка: [] по фразе, прогон не падает."""
    _patch_loader(monkeypatch, [EMPTY_RSS], delays=False)
    assert asyncio.run(_parser().parse()) == []


def test_broken_xml_returns_empty_with_warning(monkeypatch, caplog):
    """Битый XML — warning в лог и [] по фразе (не «немое» молчание)."""
    _patch_loader(monkeypatch, ["<rss>это не XML</rss-тапки"], delays=False)
    with caplog.at_level(logging.WARNING, logger="src.parsers.zakupki_parser"):
        assert asyncio.run(_parser().parse()) == []
    assert any("ошибка загрузки/разбора" in r.message for r in caplog.records)


def test_network_error_returns_empty_with_warning(monkeypatch, caplog):
    """Сетевая ошибка/таймаут — warning и [] по фразе."""
    def boom(url):
        raise OSError("connection reset")

    monkeypatch.setattr(mod, "_fetch_rss", boom)
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    with caplog.at_level(logging.WARNING, logger="src.parsers.zakupki_parser"):
        assert asyncio.run(_parser().parse()) == []
    assert any("connection reset" in r.message for r in caplog.records)


# ── Пагинация и дедуп ────────────────────────────────────────────────────────


def test_second_empty_page_stops_walk(monkeypatch):
    """Вторая страница без элементов останавливает обход — третьей нет."""
    calls = _patch_loader(
        monkeypatch, [_fixture_text(), EMPTY_RSS], delays=False
    )
    tenders = asyncio.run(_parser().parse())
    assert len(tenders) == 4
    assert calls["n"] == 2


def test_повтор_страницы_останавливает_обход(monkeypatch):
    """Площадка игнорирует pageNumber: страница 2 отдаёт те же лоты.

    Проверено живьём 27.08.2026 — по фразе «Герметик» пять уникальных лотов
    превращались в пятьдесят записей. Признак один: страница не принесла новых
    номеров, значит листать дальше бессмысленно и третий запрос не нужен.
    """
    calls = _patch_loader(
        monkeypatch, [_fixture_text(), _fixture_text(), EMPTY_RSS], delays=False
    )
    tenders = asyncio.run(_parser().parse())
    assert calls["n"] == 2, "после повторной страницы обход должен прекратиться"
    assert len(tenders) == 4
    assert len({t["lot_id"] for t in tenders}) == 4


def test_no_phrases_skips_stage(monkeypatch):
    """Без ключевых фраз этап пропускается — [] без обращения к сети.

    Раньше тест строил парсер через _parser(keyword_phrases=[]), а там стоит
    `keyword_phrases or [...]` — пустой список подменялся фразой по умолчанию,
    и тест уходил в живую сеть вместо проверки пропуска этапа.
    """
    выстрелов = {"n": 0}

    def сеть_запрещена(url):
        выстрелов["n"] += 1
        raise AssertionError("парсер не должен ходить в сеть без фраз")

    monkeypatch.setattr(mod, "_fetch_rss", сеть_запрещена)
    парсер = ZakupkiParser(browser_manager=None, keyword_phrases=[])
    assert asyncio.run(парсер.parse()) == []
    assert выстрелов["n"] == 0


def test_еис_выключен_по_умолчанию():
    """Разведка 27.08.2026: наших заказчиков в ЕИС нет, а поиск по номенклатуре
    тащит медицину и ЖКХ. Парсер готов, но включается осознанно."""
    from src.settings_store import DEFAULTS

    assert DEFAULTS["parse_zakupki"] is False
