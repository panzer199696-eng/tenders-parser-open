"""
ЕИС (zakupki.gov.ru) parser — RSS расширенного поиска по ключевым фразам.

Часть закупок крупных предприятий идёт в ЕИС по 44-ФЗ и 223-ФЗ, а не на
коммерческих площадках. Разведка показала рабочий путь без браузера,
авторизации и капчи:

    https://zakupki.gov.ru/epz/order/extendedsearch/rss.html
    ?searchString=<фраза, urlencode>&morphology=on&fz44=on&fz223=on
    &recordsPerPage=_50&pageNumber=<1..N>&af=on
    &sortBy=UPDATE_DATE&sortDirection=false

Отдаёт application/rss+xml, 50 элементов на страницу, HTTP 200. Нужен
User-Agent браузера. Поэтому сеть — стандартной библиотекой urllib.request
(браузер не нужен), запросы уходят в отдельный поток через asyncio.to_thread,
чтобы не блокировать цикл.

Структура item: title («Электронный аукцион №0373100086926000559» — тип
процедуры и номер, БЕЗ предмета закупки), link (…?regNumber=…), description
(HTML с <br/>, метки полей после снятия тегов), pubDate, author. Даты
окончания подачи заявок в RSS НЕТ — ставим «Не указана», не выдумываем.

Словарь тендера совместим с остальными парсерами (platform, name,
tender_no, company, href, сроки) плюс law («44-ФЗ»/«223-ФЗ») и stage (этап
размещения). Названия и заказчиков чистим через _common.clean_field — в
описаниях есть HTML-мнемоники.
"""

import asyncio
import html as html_lib
import logging
import random
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, quote, urlsplit

from ._common import clean_field, parse_price
from .completeness import (
    REASON_NETWORK,
    REASON_PAGE_CAP,
    build_unit_report,
    cap_expected_pages,
    reason_text,
)

from ..net_ssl import ssl_context_ru

logger = logging.getLogger(__name__)

PLATFORM = "ЕИС"
RSS_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html"

# Потолок страниц на фразу. Страница вмещает 50 лотов: 10 страниц = до 500
# лотов по одной фразе — за глаза для нашей узкой номенклатуры. Потолок нужен
# как предохранитель: без него зависшая выдача (например, сервер перестал
# отдавать пустую страницу) крутил бы обход бесконечно.
MAX_PAGES = 10

# Пауза между запросами страниц: не долбим площадку (живой RSS ЕИС не
# блокирует, но лимиты вежливости никто не отменял).
PAGE_DELAY_MIN = 1.0
PAGE_DELAY_MAX = 2.0

REQUEST_TIMEOUT = 30

# Обычный UA браузера: без него ЕИС отдаёт 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ── Разбор description ───────────────────────────────────────────────────────
#
# description — HTML: «Параметры поиска» (игнорируем) + блок «Найданный
# результат» с метками полей, разделёнными <br/>. Снимаем теги, превращаем
# <br/> в переводы строк и читаем значения по меткам.

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# Метка → ключ словаря полей. Порядок важен: уникальные префиксы строк.
_LABELS = (
    ("Наименование объекта закупки:", "name"),
    ("Размещение выполняется по:", "law"),
    ("Наименование Заказчика:", "company"),
    ("Начальная цена контракта:", "price"),
    ("Размещено:", "placed"),
    ("Обновлено:", "updated"),
    ("Этап размещения:", "stage"),
)

_RESULT_MARKER = "Найденный результат:"

_TITLE_NO_RE = re.compile(r"№\s*(\d+)\s*$")
_REG_NUMBER_RE = re.compile(r"regNumber=(\d+)")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
_PRICE_NUM_RE = re.compile(r"\d[\d\s\u00a0.,]*")


def _fetch_rss(url: str) -> str:
    """GET RSS-выдачи ЕИС (синхронная; вызывается через asyncio.to_thread)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    # ЕИС работает на сертификате НУЦ Минцифры: обычный контекст (и даже
    # truststore с хранилищем Windows) падает с «обработка прервана на корневом
    # сертификате, у которого отсутствует отношение доверия» — проверено
    # 27.08.2026. Контекст из net_ssl доверяет системным корням И российскому;
    # проверка цепочки остаётся включённой.
    ctx = ssl_context_ru()
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


class ZakupkiParser:
    """Парсер RSS расширенного поиска ЕИС по ключевым фразам.

    Интерфейс повторяет TenderProParser/BidzaarParser (browser_manager,
    days_back, публичный async-метод parse → список словарей тендеров).
    Браузер и companies не используются — ЕИС отдаёт RSS без авторизации,
    поиск идёт только по фразам.
    """

    def __init__(
        self,
        browser_manager,
        companies: list[str] | None = None,
        days_back: int = 1,
        keyword_phrases: list[str] | None = None,
    ):
        self.browser_manager = browser_manager  # не используется: сеть через urllib
        self.companies = companies or []
        self.days_back = days_back
        self.platform = PLATFORM
        self.keyword_phrases: list[str] = []
        if keyword_phrases:
            seen = set()
            for ph in keyword_phrases:
                if not isinstance(ph, str):
                    continue
                ph_s = ph.strip()
                if not ph_s or ph_s.casefold() in seen:
                    continue
                seen.add(ph_s.casefold())
                self.keyword_phrases.append(ph_s)
        # Отчёты о полноте по каждой фразе (см. completeness.py): площадка
        # выключена по умолчанию, но когда включена — её полнота обязана
        # быть в общем журнале наравне с остальными (ТЗ-8).
        self.unit_reports: list[dict] = []

    # ── URL ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_url(phrase: str, page_number: int) -> str:
        """URL RSS-страницы выдачи по фразе (page_number — 1-индексный)."""
        return (
            f"{RSS_URL}?searchString={quote(phrase)}"
            f"&morphology=on&fz44=on&fz223=on"
            f"&recordsPerPage=_50&pageNumber={page_number}&af=on"
            f"&sortBy=UPDATE_DATE&sortDirection=false"
        )

    # ── Разбор description ─────────────────────────────────────────────────────

    @staticmethod
    def _description_fields(description: str) -> dict[str, str]:
        """Метка → значение из HTML-description (блок «Параметры поиска»
        отбрасывается: берём часть после «Найденный результат:»)."""
        text = html_lib.unescape(html_lib.unescape(description or ""))
        # Блок «Параметры поиска» в начале description не содержит нужных
        # меток, но отсекаем его явно — по требованию разведки.
        pos = text.find(_RESULT_MARKER)
        if pos != -1:
            text = text[pos:]
        text = _BR_RE.sub("\n", text)
        text = _TAG_RE.sub("", text)

        fields: dict[str, str] = {}
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            for label, key in _LABELS:
                if line.startswith(label):
                    value = line[len(label) :].strip()
                    # В строке цены за значением идёт «Валюта: …» — её не берём.
                    if key == "price":
                        m = _PRICE_NUM_RE.search(value)
                        value = m.group() if m else ""
                    if key not in fields:  # первая метка важнее повторных
                        fields[key] = value
                    break
        return fields

    # ── Цена ───────────────────────────────────────────────────────────────────

    # «2467180.00» → 2467180.0; мусор/пусто → None — общий разбор площадок.
    _parse_price = staticmethod(parse_price)

    # ── Разбор одного item ─────────────────────────────────────────────────────

    def _item_to_tender(self, item, phrase: str) -> dict | None:
        try:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            author = (item.findtext("author") or "").strip()
            fields = self._description_fields(item.findtext("description") or "")

            m_no = _TITLE_NO_RE.search(title)
            tender_no = m_no.group(1) if m_no else ""
            tender_type = title[: m_no.start()].strip() if m_no else title

            lot_id = ""
            m_reg = _REG_NUMBER_RE.search(link)
            if m_reg:
                lot_id = m_reg.group(1)
            if not lot_id:
                qs = parse_qs(urlsplit(link).query)
                lot_id = (qs.get("regNumber") or [""])[0]

            name = clean_field(fields.get("name", "")) or title or "Без названия"
            company = clean_field(fields.get("company", "")) or clean_field(author)

            law = fields.get("law", "")
            if law not in ("44-ФЗ", "223-ФЗ"):
                law = clean_field(law)

            m_placed = _DATE_RE.search(fields.get("placed", ""))
            date_published = m_placed.group(1) if m_placed else "Не указана"
            m_updated = _DATE_RE.search(fields.get("updated", ""))
            date_to_display = m_updated.group(1) if m_updated else date_published

            return {
                "platform": self.platform,
                "name": name,
                "tender_no": tender_no,
                "tender_type": tender_type,
                "lot_id": lot_id,
                "company": company,
                "search_key": phrase,
                "organizer_href": None,
                "organizer_match": True,  # искали по фразе, организатор не проверяем
                "href": link,
                "sum": self._parse_price(fields.get("price", "")),
                "date_published": date_published,
                "date_deadline": "Не указана",  # в RSS этой даты нет — не выдумываем
                "date_to_display": date_to_display,
                "law": law,
                "stage": clean_field(fields.get("stage", "")),
            }
        except Exception as e:
            logger.debug(f"ЕИС: разбор item не удался: {e}")
            return None

    # ── Разбор RSS-страницы ────────────────────────────────────────────────────

    @staticmethod
    def _parse_rss(xml_text: str) -> list:
        """item-элементы страницы. Битый XML → ET.ParseError наружу (логирует
        вызывающий код: warning и пустой результат по фразе)."""
        root = ET.fromstring(xml_text)
        return root.findall(".//item")

    # ── Обход одной фразы (с пагинацией) ───────────────────────────────────────

    async def _parse_phrase(self, phrase: str) -> list[dict]:
        results: list[dict] = []
        # Площадка молча игнорирует pageNumber на RSS-выдаче: страница 2 отдаёт
        # те же лоты, что страница 1 (проверено 27.08.2026 — по фразе
        # из пяти слов пять уникальных лотов превращались в пятьдесят записей).
        # Поэтому обход прекращаем, как только страница не принесла новых
        # номеров: это единственный признак, что листание не работает.
        seen_ids: set[str] = set()
        # Метрики полноты фразы (правила — в completeness.py). RSS не отдаёт
        # общего числа результатов, а листание площадкой игнорируется:
        # естественный конец обхода — честный unknown, а не выдуманное
        # «всё собрано».
        collected_pages = 0
        expected_pages: int | None = None
        reason = ""
        visited = True
        for page_no in range(1, MAX_PAGES + 1):
            if page_no > 1:
                # Не долбим площадку: пауза между страницами.
                await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))
            url = self._build_url(phrase, page_no)
            logger.debug(f"ЕИС: фраза '{phrase}' стр.{page_no}: {url}")
            try:
                xml_text = await asyncio.to_thread(_fetch_rss, url)
                items = self._parse_rss(xml_text)
            except Exception as e:
                # Сетевая ошибка / таймаут / битый XML — не роняем прогон:
                # warning и прекращаем пагинацию фразы (что успели — оставляем).
                logger.warning(
                    f"ЕИС: фраза '{phrase}' стр.{page_no} — ошибка загрузки/разбора "
                    f"({e}), прекращаю обход фразы"
                )
                if collected_pages == 0:
                    visited = False
                reason = REASON_NETWORK
                break
            if not items:
                # Пустая страница — данные закончились.
                break
            added = 0
            for item in items:
                tender = self._item_to_tender(item, phrase)
                if not tender:
                    continue
                key = tender.get("lot_id") or tender.get("href", "").split("#")[0]
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                results.append(tender)
                added += 1
            collected_pages += 1
            logger.debug(
                f"ЕИС: фраза '{phrase}' стр.{page_no}: items={len(items)}, "
                f"новых {added}"
            )
            if not added:
                if page_no > 1:
                    logger.debug(
                        f"ЕИС: фраза '{phrase}' стр.{page_no} повторила предыдущую "
                        f"(площадка игнорирует pageNumber) — стоп"
                    )
                break
        else:
            # for-else: исчерпали MAX_PAGES без повтора и пустой страницы —
            # выдача ещё не кончилась. Это неполнота с причиной page_cap,
            # и она обязана быть видна наружу (ТЗ-8).
            reason = REASON_PAGE_CAP
            expected_pages = cap_expected_pages(expected_pages, collected_pages)
        report = build_unit_report(
            "phrase",
            phrase,
            platform=self.platform,
            expected_pages=expected_pages,
            collected_pages=collected_pages,
            reason=reason,
            visited=visited,
        )
        self.unit_reports.append(report)
        if report["status"] == "partial":
            logger.warning(
                f"ЕИС ⚠ фраза '{phrase}': собрано страниц {collected_pages} "
                f"из {expected_pages} — {reason_text(report['reason'])}"
            )
        return results

    # ── Дедупликация (конвенция TenderProParser._dedup) ────────────────────────

    @staticmethod
    def _dedup(tenders: list[dict]) -> list[dict]:
        seen, out = set(), []
        for t in tenders:
            key = t.get("lot_id") or t.get("href", "").split("#")[0]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    # ── Точка входа ────────────────────────────────────────────────────────────

    async def parse(self) -> list[dict]:
        if not self.keyword_phrases:
            logger.info("ЕИС: нет ключевых фраз — этап пропущен")
            return []
        logger.info(f"ЕИС: старт, {len(self.keyword_phrases)} ключевых фраз")
        all_tenders: list[dict] = []
        for phrase in self.keyword_phrases:
            logger.info(f"ЕИС → фраза: '{phrase}'")
            try:
                res = await self._parse_phrase(phrase)
                if res:
                    logger.info(f"ЕИС: фраза '{phrase}' → {len(res)} тендеров")
                all_tenders.extend(res)
            except Exception as e:
                logger.warning(f"ЕИС '{phrase}': {e}")

        deduped = self._dedup(all_tenders)
        logger.info(f"ЕИС: итого {len(deduped)} (до дедупа {len(all_tenders)})")
        return deduped
