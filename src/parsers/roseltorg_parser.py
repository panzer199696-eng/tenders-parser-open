"""
Росэлторг (roseltorg.ru) parser — HTML-фрагмент выдачи, без браузера.

Разведка 31.08.2026 нашла путь без авторизации, кук и браузера — площадка
отвечает даже с иностранного IP:

    GET https://www.roseltorg.ru/procedures/search_ajax
        ?sale=1&query_field=<фраза или ИНН>
        &status[]=5&status[]=0&status[]=1&currency=all&page=<N>

Отдаёт кусок разметки выдачи (не JSON), 10 карточек на страницу.

ГЛАВНЫЙ РЕЖИМ ЗДЕСЬ — ПОИСК ПО ИНН, и вот почему. Поле `query_field` принимает
и фразу, и ИНН, но поиск по фразе морфологический и слабый: «насос
центробежный» возвращает всё, где есть слово «насос». Отсеивать это должен
профиль, а не парсер: задача парсера — принести
всё, что площадка отдала, и честно сказать, по какому ключу нашла. А вот запрос
по ИНН отрабатывает точно и даёт лоты конкретного заказчика — это адресный
обход заказчика.

ГРАБЛИ, проверенные живьём:
  • Листается ТОЛЬКО параметром `page=N` (1-индексный). Параметры `p`, `from`
    и `offset` не дают ошибки — они молча возвращают первую страницу, и обход
    крутился бы по кругу, набирая дубли.
  • Одна процедура может дать несколько карточек: это разные ЛОТЫ одной
    процедуры, а не дубли. Поэтому ключ лота — «номер процедуры-номер лота»,
    как и в ссылке /procedure/<номер>/<лот>.
  • Даты публикации в выдаче нет вообще. Ставим «Не указана» и не выдумываем.

Словарь тендера совместим с остальными парсерами (см. tektorg_parser).
"""

import asyncio
import copy
import logging
import random
import re
import urllib.error
import urllib.request
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..dates import normalize_deadline, parse_deadline
from ._common import clean_field, looks_blocked, parse_price
from .completeness import (
    REASON_BLOCKED,
    REASON_CRASHED,
    REASON_FORMAT_CHANGED,
    REASON_NETWORK,
    REASON_PAGE_CAP,
    build_unit_report,
    cap_expected_pages,
    failed_unit,
    reason_text,
)
from .tender_pro_parser import geo_hint

logger = logging.getLogger(__name__)

PLATFORM = "Росэлторг"
BASE_URL = "https://www.roseltorg.ru"
SEARCH_PATH = "/procedures/search_ajax"

# Статусы выдачи: 5, 0, 1 — те же, что ставит сама площадка в форме поиска
# («приём заявок» и соседние живые состояния). Меняем только вместе с проверкой
# живьём: набор недокументирован. Разбор 16.10.2026: в фикстуре 31.08 все
# десять карточек — «Прием заявок» (status__icon--acceptance), какому коду
# какое состояние соответствует, по фикстурам доказать нельзя. Поэтому код
# запроса не трогаем, а отсеиваем карточку по её собственной надписи —
# только явно конечные состояния (см. _FINISHED_MARKERS).
STATUS_PARAMS = "status%5B%5D=5&status%5B%5D=0&status%5B%5D=1"

# Надпись статуса карточки, после которой подавать заявку некуда.
_FINISHED_MARKERS = ("заверш", "отмен", "не состоял", "архив")

# Признак ответа выдачи (Д1, 16.10.2026): карточки лотов либо плашка «Нет
# результатов». Без них страница — антибот или новая вёрстка, и раньше она
# молча читалась как «закупок нет».
CARD_SELECTOR = ".search-results__item"
NO_RESULTS_SELECTOR = ".search-autocomplete__no-result"
BLOCKED_HTTP_CODES = (401, 403, 429)

# Потолок страниц на один запрос. По 10 карточек — это до 200 лотов на ключ,
# с запасом для крупного заказчика. Потолок нужен как предохранитель: если
# площадка начнёт отдавать одну и ту же страницу, обход не станет вечным.
MAX_PAGES = 20

PAGE_DELAY_MIN = 1.0
PAGE_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_INN_MARKER_RE = re.compile(r"Найдено в ИНН\s*(\d{8,12})")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
# Номера страниц в data-href пагинации (0-индексные: page=0 — первая).
_PAGE_PARAM_RE = re.compile(r"page=(\d+)")


class RoseltorgParser:
    def __init__(
        self,
        browser_manager,
        companies: list[str],
        days_back: int = 1,
        keyword_phrases: list[str] | None = None,
        client_inns: list[str] | None = None,
    ):
        # browser_manager площадке не нужен — принимаем для единообразия
        # с остальными парсерами, чтобы конвейер создавал их одинаково.
        self.browser_manager = browser_manager
        self.companies = companies or []
        self.days_back = days_back
        self.platform = PLATFORM
        self.keyword_phrases = self._clean_list(keyword_phrases)
        self.client_inns = self._clean_list(client_inns)
        # Отчёты о полноте по каждому ключу поиска (см. completeness.py):
        # обрыв сети или потолок страниц обязаны быть видны наружу (ТЗ-8).
        self.unit_reports: list[dict] = []

    @staticmethod
    def _clean_list(values: list[str] | None) -> list[str]:
        """Убрать пустые и повторы, сохранив порядок (он задаёт приоритет)."""
        out, seen = [], set()
        for value in values or []:
            text = str(value).strip()
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    # ── Сеть ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_url(query: str, page: int) -> str:
        return (
            f"{BASE_URL}{SEARCH_PATH}?sale=1&query_field={quote(query)}"
            f"&{STATUS_PARAMS}&currency=all&page={page}"
        )

    @staticmethod
    def _fetch(url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")

    async def _load_page(self, url: str) -> str | None:
        """Страница выдачи или None. Сеть — в отдельном потоке, чтобы не
        блокировать цикл (браузер не нужен, но ждать ответа всё равно долго).

        Почему None — в self._load_failure: «не пускают» (403/429) и обрыв
        связи лечатся по-разному, а повтор при блокировке бесполезен.
        """
        self._load_failure = REASON_NETWORK
        try:
            return await asyncio.to_thread(self._fetch, url)
        except urllib.error.HTTPError as e:
            if e.code in BLOCKED_HTTP_CODES:
                self._load_failure = REASON_BLOCKED
            logger.warning("Росэлторг: страница не загрузилась (HTTP %s)", e.code)
            return None
        except Exception as e:
            logger.warning("Росэлторг: страница не загрузилась%s: %s", geo_hint(e), e)
            return None

    @staticmethod
    def _page_problem(soup, html: str) -> str:
        """Почему ответ — не выдача (пусто — выдача, в т.ч. пустая)."""
        if soup.select_one(CARD_SELECTOR) or soup.select_one(NO_RESULTS_SELECTOR):
            return ""
        return REASON_BLOCKED if looks_blocked(html) else REASON_FORMAT_CHANGED

    # ── Разбор карточки ───────────────────────────────────────────────────────

    @staticmethod
    def _text(node, selector: str) -> str:
        el = node.select_one(selector)
        return el.get_text(" ", strip=True) if el else ""

    @staticmethod
    def _parse_sum(data_text: str) -> float | None:
        """НМЦ из блока данных. «НМЦ не указано» → None, а не ноль.

        Ноль означал бы «бесплатно» и увёл бы лот под любой ценовой порог.
        """
        if not data_text or "не указан" in data_text.lower():
            return None
        return parse_price(data_text)

    def _card_to_tender(self, card, search_key: str, by_inn: bool) -> dict | None:
        procedure_no = (
            card.get("data-feature-favorite-lots-procedure-number") or ""
        ).strip()
        lot_number = (card.get("data-feature-favorite-lots-lot-number") or "").strip()
        if not procedure_no:
            return None

        link = card.select_one('a[href*="/procedure/"]')
        href = link.get("href", "") if link else ""
        if href.startswith("/"):
            href = f"{BASE_URL}{href}"

        name = clean_field(self._text(card, ".search-results__link")) or "Без названия"

        # «Организатор ООО …» — слово-подпись отрезаем, оно не часть названия.
        # Внутри того же блока площадка держит теги-подсказки поиска
        # («юридическое сопровождение», «правовое сопровождение»): без их
        # удаления они приклеиваются к названию заказчика, и сопоставление
        # с нашим справочником перестаёт находить кого бы то ни было.
        company = ""
        customer = card.select_one(".search-results__customer")
        if customer is not None:
            # Название лежит в .search-results__tooltip (рядом с подписью
            # «Организатор» и блоком тегов). Берём его, а если разметка
            # изменится — вычищаем подпись и теги и берём остаток.
            tooltip = customer.select_one(".search-results__tooltip")
            if tooltip is not None:
                company = tooltip.get_text(" ", strip=True)
            else:
                block = copy.copy(customer)
                for junk in block.select(
                    ".search-results__title, .search-results__tags, .procedure-tags, .chip"
                ):
                    junk.decompose()
                company = block.get_text(" ", strip=True)
        company = clean_field(re.sub(r"^\s*Организатор\s*", "", company))

        # Даты и цена лежат в колонках блока данных вперемешку, поэтому
        # разбираем их по подписям, а не по порядку колонок: порядок площадка
        # меняет от типа процедуры.
        deadline = ""
        price_text = ""
        for col in card.select(".search-results__data-col"):
            text = col.get_text(" ", strip=True)
            low = text.lower()
            if "окончание приема заявок" in low or "окончание приёма заявок" in low:
                # «04.09.2026 в 14:00» — время сохраняем: раньше брали только
                # дату, и лот жил до 23:59, хотя приём кончался в 14:00.
                found = _DATE_RE.search(text)
                if found and parse_deadline(text[found.start() :]) is not None:
                    deadline = normalize_deadline(text[found.start() :])
            elif "нмц" in low:
                price_text = text

        inn = ""
        marker = _INN_MARKER_RE.search(card.get_text(" ", strip=True))
        if marker:
            inn = marker.group(1)
        elif by_inn:
            inn = search_key

        lot_id = f"{procedure_no}-{lot_number}" if lot_number else procedure_no

        return {
            "platform": self.platform,
            "name": name,
            "tender_no": procedure_no,
            "tender_type": clean_field(self._text(card, ".search-results__section")),
            "lot_id": lot_id,
            "company": company,
            "search_key": search_key,
            "organizer_href": None,
            # Найденное по ИНН — это адресный обход конкретного заказчика;
            # найденное по фразе организатором не проверялось.
            "organizer_match": bool(by_inn),
            "href": href,
            "sum": self._parse_sum(price_text),
            # Площадка не отдаёт дату публикации в выдаче — не выдумываем её.
            "date_published": "Не указана",
            "date_deadline": deadline or "Не указана",
            "date_to_display": deadline or "Не указана",
            "inn": inn,
        }

    @staticmethod
    def _card_finished(card) -> bool:
        """Карточка в конечном состоянии: завершена, отменена, не состоялась."""
        status = card.select_one(".search-results__status")
        text = status.get_text(" ", strip=True).lower() if status else ""
        return any(marker in text for marker in _FINISHED_MARKERS)

    def _parse_cards(self, cards, search_key: str, by_inn: bool) -> tuple[list[dict], int]:
        """Карточки → (активные лоты, сколько карточек узнано вообще).

        Узнанная, но завершённая карточка — не «распознано 0»: её отсеял
        статус, а не смена вёрстки.
        """
        tenders, recognized = [], 0
        for card in cards:
            tender = self._card_to_tender(card, search_key, by_inn)
            if not tender:
                continue
            recognized += 1
            if not self._card_finished(card):
                tenders.append(tender)
        return tenders, recognized

    def _parse_page(self, html: str, search_key: str, by_inn: bool) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        return self._parse_cards(soup.select(CARD_SELECTOR), search_key, by_inn)[0]

    # ── Обход ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _expected_pages_from_html(html: str) -> int | None:
        """Ожидаемое число страниц из пагинации выдачи (или None).

        Счётчика результатов выдача не отдаёт — только номера страниц в
        data-href кнопок пагинации (0-индексные). Если видны ВСЕ номера
        (нет многоточия-разделителя), последний даёт объём выдачи.
        Многоточие («окно» пагинации) означает, что конец не виден, —
        тогда честный ответ unknown, а не догадка по видному куску.
        """
        if "pagination__separator" in html:
            return None
        numbers = [int(n) for n in _PAGE_PARAM_RE.findall(html)]
        return max(numbers) + 1 if numbers else None

    async def _walk(self, query: str, by_inn: bool) -> list[dict]:
        """Все страницы выдачи по одному ключу поиска."""
        found: list[dict] = []
        # Метрики полноты ключа (правила — в completeness.py): полнота
        # считается по страницам, объём выдачи — из пагинации, если виден.
        collected_pages = 0
        expected_pages: int | None = None
        reason = ""
        visited = True
        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))
            html = await self._load_page(self._build_url(query, page))
            if html is None:
                # Сеть не ответила или не пускают: ни одной страницы — до
                # объекта не дошли (skipped), иначе — partial.
                if collected_pages == 0:
                    visited = False
                reason = getattr(self, "_load_failure", REASON_NETWORK)
                break
            from_pagination = self._expected_pages_from_html(html)
            if from_pagination is not None:
                expected_pages = from_pagination
            try:
                soup = BeautifulSoup(html, "html.parser")
                problem = self._page_problem(soup, html)
                cards = soup.select(CARD_SELECTOR)
                page_tenders, recognized = self._parse_cards(cards, query, by_inn)
                if not problem and cards and not recognized:
                    # Карточки есть, а узнать ни одну не вышло — площадка
                    # переименовала атрибуты, а не «закупок нет».
                    problem = REASON_FORMAT_CHANGED
            except Exception as e:
                logger.warning("Росэлторг: разбор страницы %d не удался: %s", page, e)
                problem = REASON_FORMAT_CHANGED
            if problem:
                logger.warning(
                    "Росэлторг: '%s' стр.%d — не выдача: %s",
                    query,
                    page,
                    reason_text(problem),
                )
                # Собранное сохраняем, статус честно говорит, что обход не
                # доведён и почему.
                if collected_pages == 0:
                    visited = False
                reason = problem
                break
            if not cards:
                # Пустая страница — площадка отдаёт всё, что есть: даже если
                # объём из пагинации был неизвестен, собранное — целиком.
                expected_pages = collected_pages
                break
            found.extend(page_tenders)
            collected_pages += 1
        else:
            # for-else: исчерпали MAX_PAGES без пустой страницы — выдача
            # ещё не кончилась. Это неполнота с причиной page_cap, и она
            # обязана быть видна наружу, а не только в логе.
            reason = REASON_PAGE_CAP
            expected_pages = cap_expected_pages(expected_pages, collected_pages)
        report = build_unit_report(
            "inn" if by_inn else "phrase",
            query,
            platform=self.platform,
            expected_pages=expected_pages,
            collected_pages=collected_pages,
            reason=reason,
            visited=visited,
        )
        self.unit_reports.append(report)
        if report["status"] == "partial":
            logger.warning(
                "Росэлторг ⚠ '%s': собрано страниц %d из %s — %s",
                query,
                collected_pages,
                expected_pages,
                reason_text(report["reason"]),
            )
        return found

    @staticmethod
    def _dedup(tenders: list[dict]) -> list[dict]:
        """Один лот мог прийти и по ИНН, и по фразе — оставляем первый."""
        seen, out = set(), []
        for tender in tenders:
            key = tender.get("lot_id") or tender.get("href", "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(tender)
        return out

    async def _walk_safe(self, query: str, by_inn: bool) -> list[dict]:
        """_walk, который не обрывает обход остальных ключей.

        Раньше исключение в одном ИНН роняло parse целиком: оставшиеся ИНН
        не обходились и не попадали в отчёт — пропадали из сводки.
        """
        try:
            return await self._walk(query, by_inn=by_inn)
        except Exception as e:
            logger.exception("Росэлторг: сбой обхода '%s': %s", query, e)
            self.unit_reports.append(
                failed_unit("inn" if by_inn else "phrase", query, self.platform, REASON_CRASHED)
            )
            return []

    async def parse(self) -> list[dict]:
        collected: list[dict] = []

        if self.client_inns:
            logger.info("Росэлторг: адресный обход по %d ИНН", len(self.client_inns))
            for inn in self.client_inns:
                rows = await self._walk_safe(inn, by_inn=True)
                if rows:
                    logger.info("Росэлторг: ИНН %s → %d лотов", inn, len(rows))
                collected.extend(rows)

        if self.keyword_phrases:
            logger.info("Росэлторг: поиск по %d фразам", len(self.keyword_phrases))
            for phrase in self.keyword_phrases:
                rows = await self._walk_safe(phrase, by_inn=False)
                if rows:
                    logger.info("Росэлторг: фраза «%s» → %d лотов", phrase, len(rows))
                collected.extend(rows)

        deduped = self._dedup(collected)
        logger.info("Росэлторг: итого %d (до дедупа %d)", len(deduped), len(collected))
        return deduped
