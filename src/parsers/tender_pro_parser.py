"""
Tender.pro parser — серверный HTML по организатору.

Архитектура (переписана 2026-06-29 после диагностики):
  Старый URL https://tender.pro/search?keywords=… отдаёт HTTP 404 — страница
  не существует, поэтому парсер стабильно возвращал 0. Реальный endpoint поиска:
    https://www.tender.pro/api/tenders/list?company_name=<запрос>&filtered=1
  (GET, серверный HTML, JS не требуется). Параметр company_name фильтрует по
  ОРГАНИЗАТОРУ — это и есть наша цель (ищем тендеры ОТ целевых компаний).
  Другие параметры (ftext/tender_name/good_name) сервер игнорирует или фильтрует
  слабо; company_name — единственный, дающий чистую адресную выдачу.

  Вёрстка — таблица <tr> с ячейками:
    td.tender__id        → id/№ тендера
    td.tender__name a.link[href*='/api/tender/'] → название + href; <img> — тип
    td (без tender__-класса, дата) → date_published
    td.tender__untill    → date_deadline
    td.tender__company a → организатор + organizer_href
  Пагинация — ul.pagination с a.pagination__link[href*='page=N'] (page 0-индексный;
  ссылка .pagination__link_last даёт индекс последней страницы).

  Словарь тендера совместим с остальными парсерами (platform, name, tender_no, tender_type,
  lot_id, company, search_key, organizer_href, organizer_match, href, sum,
  date_published, date_deadline, date_to_display) — чтобы классификатор и Excel
  работали единообразно.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..dates import today_msk
from ._common import looks_blocked, organizer_match
from .completeness import (
    REASON_BLOCKED,
    REASON_CRASHED,
    REASON_FORMAT_CHANGED,
    REASON_NETWORK,
    REASON_NOT_LOGGED_IN,
    REASON_PAGE_CAP,
    build_unit_report,
    cap_expected_pages,
    failed_unit,
    reason_text,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tender.pro"
LIST_PATH = "/api/tenders/list"
MAX_PAGES = 30
PAGE_DELAY_MIN = 1500
PAGE_DELAY_MAX = 3000
# Пауза между заказчиками, с.
COMPANY_DELAY = 2

# Признак страницы поиска (Д1, 16.10.2026): есть и при пустой выдаче. Без
# него любая страница — капча, вход, новая вёрстка — читалась как «у
# заказчика закупок нет».
SEARCH_PAGE_SELECTOR = ".search-page"
# Адрес ушёл с выдачи на вход в кабинет.
_LOGIN_URL_MARKERS = ("/login", "/auth", "/api/my", "signin")
BLOCKED_HTTP_CODES = (401, 403, 429)

_TENDER_HREF_RE = re.compile(r"/api/tender/(\d+)")
_DATE_RE = re.compile(r"^\s*\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?\s*$")
_PAGE_RE = re.compile(r"([?&])page=(\d+)")


# Российские площадки не пускают зарубежные адреса: с зарубежного IP
# (VPN, прокси; проверено 27.08.2026) соединение отваливается по таймауту, а
# с российского работает. Пишем это словами — иначе «goto err ERR_CONNECTION_
# TIMED_OUT» читается как поломка программы.
_GEO_MARKERS = ("ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "10060", "timeout")


def geo_hint(error: object) -> str:
    """Подсказка про недоступность площадки с текущего адреса (или пустая)."""
    text = str(error)
    if any(m.lower() in text.lower() for m in _GEO_MARKERS):
        return (
            " — площадка не ответила. Обычно это значит, что она не пускает "
            "текущий IP-адрес: проверьте, не включён ли VPN"
        )
    return ""


class TenderProParser:
    def __init__(self, browser_manager, companies: list[str], days_back: int = 1):
        self.browser_manager = browser_manager
        self.companies = companies
        self.days_back = days_back
        self.platform = "Tender.pro"
        # Отчёты о полноте по каждому заказчику (см. completeness.py): обрыв
        # сети или потолок страниц обязаны быть видны наружу (ТЗ-8).
        self.unit_reports: list[dict] = []

    # ── URL ────────────────────────────────────────────────────────────────────

    def _build_url(self, company: str, page_index: int | None = None) -> str:
        """URL адресной выдачи по организатору. page_index — 0-индексная страница."""
        url = f"{BASE_URL}{LIST_PATH}?company_name={quote(company)}&filtered=1"
        if page_index is not None:
            url += f"&page={page_index}"
        return url

    # ── Сверка организатора — по целым словам (общая для всех площадок) ──────

    # ТЭК-Торг и Bidzaar зовут её через этот класс — имя оставлено.
    _organizer_match = staticmethod(organizer_match)

    # ── Фильтр по периоду (days_back) ──────────────────────────────────────────

    def _within_days_back(self, published: str) -> bool:
        """Опубликован ли тендер в последние ``days_back`` дней.

        ``published`` — строка вида «дд.мм.гггг [чч:мм]» после ``_fmt_date``.
        Тендеры без распознанной даты НЕ отбрасываются (лучше лишний, чем
        потерянный). Раньше days_back принимался, но не применялся — выдача
        игнорировала период из настроек.
        """
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", published or "")
        if not m:
            return True
        try:
            pub = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return True
        # Даты площадки — московские, и «сегодня» берём московское: ПК живёт
        # в UTC+7, и в 03:00 по ПК в Москве ещё вчера.
        cutoff = today_msk() - timedelta(days=max(0, self.days_back))
        return pub.date() >= cutoff

    # ── Форматирование даты ────────────────────────────────────────────────────

    @staticmethod
    def _fmt_date(text: str) -> str:
        if not text:
            return "Не указана"
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?", text)
        if m:
            return f"{m.group(1)} {m.group(2)}" if m.group(2) else m.group(1)
        return text.strip() or "Не указана"

    # ── Разбор одной строки-карточки ───────────────────────────────────────────

    def _parse_row(self, tr, company: str) -> dict | None:
        try:
            id_el = tr.select_one("td.tender__id")
            name_a = tr.select_one("td.tender__name a.link[href*='/api/tender/']")
            if not (id_el and name_a):
                return None

            lot_id = id_el.get_text(strip=True)
            m_href = _TENDER_HREF_RE.search(name_a.get("href", ""))
            if not m_href:
                return None

            href = name_a.get("href", "")
            if href and not href.startswith("http"):
                href = BASE_URL + href

            name = re.sub(
                r"\s+", " ", name_a.get_text(" ", strip=True).replace("\xa0", " ")
            ).strip()
            name = re.sub(r"\s*\(id\d+\)\s*$", "", name).strip() or name
            if not name:
                name = "Без названия"

            tender_type = ""
            img = tr.select_one("td.tender__name img[title]")
            if img is not None:
                tender_type = img.get("title", "").strip()

            company_el = tr.select_one("td.tender__company a")
            organizer = ""
            organizer_href = ""
            if company_el is not None:
                organizer = re.sub(
                    r"\s+",
                    " ",
                    company_el.get_text(" ", strip=True).replace("\xa0", " "),
                ).strip()
                organizer_href = company_el.get("href", "")
                if organizer_href and not organizer_href.startswith("http"):
                    organizer_href = BASE_URL + organizer_href

            deadline_text = ""
            untill = tr.select_one("td.tender__untill")
            if untill is not None:
                deadline_text = untill.get_text(" ", strip=True)

            published_text = ""
            for td in tr.find_all("td"):
                cls = " ".join(td.get("class", []))
                if "tender__" in cls:
                    continue
                txt = td.get_text(strip=True)
                if _DATE_RE.match(txt):
                    published_text = txt
                    break

            return {
                "platform": self.platform,
                "name": name,
                "tender_no": lot_id,
                "tender_type": tender_type,
                "lot_id": lot_id,
                "company": organizer or company,
                "search_key": company,
                "organizer_href": organizer_href,
                "organizer_match": self._organizer_match(company, organizer)
                if organizer
                else False,
                "href": href,
                "sum": None,
                "date_published": self._fmt_date(published_text),
                "date_deadline": self._fmt_date(deadline_text),
                "date_to_display": self._fmt_date(deadline_text),
            }
        except Exception as e:
            logger.debug(f"Tender.pro row parse error: {e}")
            return None

    # ── Извлечение строк и пагинации из HTML ───────────────────────────────────

    @staticmethod
    def _select_rows(soup) -> list:
        return soup.select("tr:has(td.tender__id)")

    @staticmethod
    def _pagination_info(soup, current_index: int):
        """(template_href, max_index, has_last) из пагинации текущей страницы.

        template_href — образец href обычной страницы (для подстановки page=N);
        max_index — наибольший видимый индекс страницы (включая ссылку last);
        has_last — есть ли ссылка .pagination__link_last. Её отсутствие означает
        «оконную» пагинацию: конца по ссылкам не видно, обходить надо итеративно.
        """
        template_href = None
        max_index = current_index
        has_last = False
        for a in soup.select("a.pagination__link[href]"):
            href = a.get("href", "") or ""
            classes = a.get("class") or []
            if "pagination__link_last" in classes:
                has_last = True
            m = _PAGE_RE.search(href)
            if not m:
                continue
            idx = int(m.group(2))
            if (
                template_href is None
                and "pagination__link_next" not in classes
                and "pagination__link_last" not in classes
                and "pagination__link_previous" not in classes
                and "pagination__link_first" not in classes
            ):
                template_href = href
            max_index = max(max_index, idx)
        return template_href, max_index, has_last

    @staticmethod
    def _page_url_from_template(template_href: str, idx: int) -> str:
        rel = _PAGE_RE.sub(rf"\1page={idx}", template_href)
        return rel if rel.startswith("http") else BASE_URL + rel

    @staticmethod
    def _discover_page_urls(soup, current_index: int) -> list[str]:
        """Из пагинации текущей страницы строит URL всех последующих страниц."""
        template_href, max_index, _ = TenderProParser._pagination_info(
            soup, current_index
        )
        if template_href is None:
            return []
        urls = []
        for idx in range(current_index + 1, min(max_index, MAX_PAGES - 1) + 1):
            urls.append(TenderProParser._page_url_from_template(template_href, idx))
        return urls

    # ── Основной обход ─────────────────────────────────────────────────────────

    @staticmethod
    def _page_problem(soup, html: str) -> str:
        """Почему страница — не выдача (или пусто, если выдача).

        Требуем признак страницы поиска (он есть и при пустой выдаче);
        без него — антибот, если похоже, иначе смена вёрстки. Строки таблицы
        есть, а строк с номером тендера нет — переименовали ячейки.
        """
        container = soup.select_one(SEARCH_PAGE_SELECTOR)
        if container is None:
            return REASON_BLOCKED if looks_blocked(html) else REASON_FORMAT_CHANGED
        data_rows = [
            tr for tr in container.select("table tr") if len(tr.find_all("td")) >= 3
        ]
        if data_rows and not TenderProParser._select_rows(soup):
            return REASON_FORMAT_CHANGED
        return ""

    async def _fetch_soup(self, page, url: str) -> tuple[BeautifulSoup | None, str]:
        """Страница выдачи → (soup, "") либо (None, причина отказа)."""
        for attempt in (1, 2):
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=60000
                )
            except Exception:
                try:
                    response = await page.goto(
                        url, wait_until="networkidle", timeout=60000
                    )
                except Exception as e2:
                    logger.warning(
                        f"Tender.pro: страница не открылась{geo_hint(e2)}. {e2} ({url})"
                    )
                    return None, REASON_NETWORK
            # Защита площадки отвечает 503 на ПЕРВЫЙ запрос свежего браузера
            # и пропускает повторный (проверено живьём 16.10.2026). Раньше эта
            # страница молча давала «0 строк» — первый заказчик ночи
            # фактически не проверялся.
            status = getattr(response, "status", None)
            if attempt == 1 and status in (502, 503, 504):
                logger.info(f"Tender.pro: HTTP {status} на входе — повтор через 4 с")
                await page.wait_for_timeout(4000)
                continue
            break
        final_url = str(getattr(page, "url", "") or url)
        reason = ""
        if status in BLOCKED_HTTP_CODES:
            reason = REASON_BLOCKED
        elif status == 404:
            # Так в июне 2026 умер старый адрес поиска — месяц давал ноль.
            reason = REASON_FORMAT_CHANGED
        elif status is not None and status >= 500:
            reason = REASON_NETWORK
        elif any(m in final_url.lower() for m in _LOGIN_URL_MARKERS):
            reason = REASON_NOT_LOGGED_IN
        if reason:
            logger.warning(
                f"Tender.pro: HTTP {status}, адрес {final_url} — {reason_text(reason)}"
            )
            return None, reason
        await page.wait_for_timeout(2500)
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")
        reason = self._page_problem(soup, html)
        if reason:
            logger.warning(
                f"Tender.pro: {url} — не страница выдачи: {reason_text(reason)}"
            )
            return None, reason
        return soup, ""

    async def _parse_company(self, page, company: str) -> list[dict]:
        results: list[dict] = []
        # Метрики полноты заказчика (правила — в completeness.py): объём
        # выдачи известен из пагинации — последняя страница даёт ожидаемое
        # число страниц (0-индексный индекс + 1).
        collected_pages = 0
        expected_pages: int | None = None
        reason = ""
        visited = True

        def _finish() -> list[dict]:
            report = build_unit_report(
                "company",
                company,
                platform=self.platform,
                expected_pages=expected_pages,
                collected_pages=collected_pages,
                reason=reason,
                visited=visited,
            )
            self.unit_reports.append(report)
            if report["status"] == "partial":
                logger.warning(
                    f"Tender.pro ⚠ '{company}': собрано страниц {collected_pages} "
                    f"из {expected_pages} — {reason_text(report['reason'])}"
                )
            return results

        url = self._build_url(company, page_index=0)
        logger.info(f"Tender.pro → '{company}': {url}")
        soup, failure = await self._fetch_soup(page, url)
        if soup is None:
            # Первая страница не открылась или это не выдача — до заказчика
            # не дошли вовсе; причина — какой именно отказ.
            visited = False
            reason = failure
            return _finish()

        rows = self._select_rows(soup)
        for tr in rows:
            t = self._parse_row(tr, company)
            if t:
                results.append(t)
        logger.info(
            f"Tender.pro '{company}' стр.1: строк {len(rows)}, распознано {len(results)}"
        )
        if rows and not results:
            # Строки есть, а разобрать ни одну не вышло — сменилась вёрстка.
            visited = False
            reason = REASON_FORMAT_CHANGED
            return _finish()

        template_href, max_index, has_last = self._pagination_info(
            soup, current_index=0
        )
        if template_href is None:
            # Без шаблона страницы листать нельзя. Если и других признаков
            # пагинации нет — вся выдача поместилась на первую страницу
            # (expected=1); если признаки есть (next/last без номеров) —
            # объём неизвестен, честный unknown.
            if not has_last and max_index == 0:
                # Страницу открыли — значит обошли её целиком, даже если
                # строк на ней нет. Пустая выдача у заказчика без активных
                # закупок это не неполнота: считать её недобором значит
                # объявлять неполным каждый прогон (36 заказчиков из 40 на
                # живых данных 04.09.2026).
                expected_pages = 1
                collected_pages = 1
            return _finish()
        if has_last:
            # Есть ссылка на последнюю страницу: ожидаемое число страниц
            # известно точно (0-индексный максимум + 1).
            expected_pages = max_index + 1
        collected_pages = 1  # первая страница просмотрена, пустая она или нет

        # Итеративный обход: при «полной» пагинации (есть ссылка last) идём до
        # max_index; при «оконной» (last-ссылки нет) — пока страница отдаёт строки,
        # иначе страницы за окном теряются. Потолок — MAX_PAGES.
        idx = 1
        while idx <= MAX_PAGES - 1:
            if has_last and idx > max_index:
                break
            await page.wait_for_timeout(
                PAGE_DELAY_MIN + (PAGE_DELAY_MAX - PAGE_DELAY_MIN) * (idx % 2)
            )
            page_url = self._page_url_from_template(template_href, idx)
            soup_n, failure = await self._fetch_soup(page, page_url)
            if soup_n is None:
                # Отказ на середине: собранное сохраняем, статус — partial.
                reason = failure
                break
            rows_n = self._select_rows(soup_n)
            if not rows_n:
                # Пустая страница — данные закончились (важно для оконной пагинации):
                # площадка отдаёт всё, что есть, — собранное целиком.
                expected_pages = collected_pages
                break
            added = 0
            for tr in rows_n:
                t = self._parse_row(tr, company)
                if t:
                    results.append(t)
                    added += 1
            collected_pages += 1
            logger.debug(
                f"Tender.pro '{company}' стр.{idx + 1}: строк {len(rows_n)}, добавлено {added}"
            )
            idx += 1
        else:
            # while-else: потолок страниц исчерпан без естественной остановки —
            # выдача ещё не кончилась. Неполнота с причиной page_cap обязана
            # быть видна наружу, а не только в логе (ТЗ-8).
            reason = REASON_PAGE_CAP
            expected_pages = cap_expected_pages(expected_pages, collected_pages)
        return _finish()

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

    async def parse(self) -> list[dict]:
        logger.info(f"Tender.pro: поиск по {len(self.companies)} компаниям")
        all_tenders: list[dict] = []
        try:
            page = await self.browser_manager.new_page()
        except Exception as e:
            # Без страницы браузера не обойти никого — и каждый заказчик
            # обязан попасть в отчёт, иначе площадка «не отчиталась».
            logger.exception(f"Tender.pro: браузер не открыл страницу: {e}")
            for company in self.companies:
                self.unit_reports.append(
                    failed_unit("company", company, self.platform, REASON_CRASHED)
                )
            return []
        try:
            for company in self.companies:
                try:
                    res = await self._parse_company(page, company)
                    all_tenders.extend(res)
                    if res:
                        logger.info(f"Tender.pro: '{company}' → {len(res)} тендеров")
                    await asyncio.sleep(COMPANY_DELAY)
                except Exception as e:
                    logger.warning(f"Tender.pro '{company}': {e}")
                    # Снаружи до сюда доходит только инфраструктурный сбой
                    # (браузер умер): сам обход свои сбои ловит и отчёт уже
                    # записал. Здесь заказчик не обойдён вовсе — skipped.
                    self.unit_reports.append(
                        failed_unit("company", company, self.platform, REASON_NETWORK)
                    )
        finally:
            await page.close()

        deduped = self._dedup(all_tenders)
        fresh = [
            t for t in deduped if self._within_days_back(t.get("date_published", ""))
        ]
        dropped = len(deduped) - len(fresh)
        if dropped:
            logger.info(
                f"Tender.pro: отфильтровано по периоду ({self.days_back} дн.): "
                f"убрано {dropped} старых"
            )
        logger.info(f"Tender.pro: итого {len(fresh)} (до дедупа {len(all_tenders)})")
        return fresh
