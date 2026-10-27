"""
ГПБ (Газпромбанк) parser - FIXED
Site often doesn't respond, so this is disabled by default
"""

import re
import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from ._common import parse_price

logger = logging.getLogger(__name__)


class GPBParser:
    def __init__(self, browser_manager, companies: List[str], days_back: int = 1):
        self.browser_manager = browser_manager
        self.companies = companies
        self.days_back = days_back
        self.platform = "ГПБ"

    def _build_url(self, company: str) -> str:
        from urllib.parse import quote

        today = datetime.now()
        date_from = (today - timedelta(days=self.days_back)).strftime("%d.%m.%Y")
        date_to = today.strftime("%d.%m.%Y")

        # GPB search URL format
        return (
            f"https://new.etpgpb.ru/procedures/?"
            f"customer={quote(company)}"
            f"&datePublishedFrom={date_from}"
            f"&datePublishedTo={date_to}"
        )

    # Маркеры контекста дат в карточке ГПБ. «до 15.08.2026» ловим отдельным
    # регэкспом со словесной границей, чтобы не срабатывать на «договор» и т.п.
    _DEADLINE_HINTS = ("оконч", "актуальн", "приём", "прием", "подач", "истека")
    _PUBLISHED_HINTS = ("опубликован", "публикац", "размещ")
    _DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
    _UNTIL_DATE_RE = re.compile(r"\bдо\b[:\s]*\d{2}\.\d{2}\.\d{4}", re.IGNORECASE)

    @staticmethod
    def _date_sort_key(d: str):
        # "дд.мм.гггг" → (гггг, мм, дд) для сравнения
        return (d[6:10], d[3:5], d[0:2])

    @classmethod
    def _extract_dates(cls, card) -> tuple:
        """(published, deadline) из текстов карточки по контексту.

        Карточка ГПБ содержит несколько дат (публикация, окончание подачи);
        раньше первая попавшаяся писалась в дедлайн — часто это была дата
        публикации. Теперь:
          1. дата в тексте с маркером дедлайна («актуально до», «окончание…»,
             «до дд.мм.гггг») → дедлайн; с маркером публикации → публикация;
          2. без маркеров: из нескольких дат дедлайн — самая поздняя,
             публикация — самая ранняя;
          3. единственная дата без маркеров → дедлайн (в выдаче ГПБ это
             «актуально до»), публикация не выдумывается.
        """
        published = ""
        deadline = ""
        all_dates = []
        for text in card.stripped_strings:
            m = cls._DATE_RE.search(text)
            if not m:
                continue
            all_dates.append(m.group())
            low = text.lower()
            is_deadline = cls._UNTIL_DATE_RE.search(low) or any(
                h in low for h in cls._DEADLINE_HINTS
            )
            is_published = any(h in low for h in cls._PUBLISHED_HINTS)
            if is_deadline and not deadline:
                deadline = m.group()
            elif is_published and not published:
                published = m.group()
        if not deadline:
            rest = [d for d in all_dates if not published or d != published]
            if rest:
                deadline = max(rest, key=cls._date_sort_key)
        if not published and len(all_dates) >= 2:
            cands = [d for d in all_dates if d != deadline]
            if cands:
                published = min(cands, key=cls._date_sort_key)
        return published, deadline

    def _parse_card(self, card, company: str) -> Optional[Dict]:
        try:
            name_el = card.select_one("a[href*='/procedure/']")
            if not name_el:
                return None

            name = name_el.get_text(strip=True)
            href = name_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://new.etpgpb.ru" + href

            # № процедуры из href (/procedure/<id>/) — служит lot_id/tender_no
            m_id = re.search(r"/procedure/(\d+)", href)
            lot_id = m_id.group(1) if m_id else ""

            # Find price
            price_val = None
            for text in card.stripped_strings:
                if "₽" in text or "руб" in text:
                    price_val = parse_price(text)
                    if price_val:
                        break

            published, deadline = self._extract_dates(card)

            return {
                "platform": self.platform,
                "name": name,
                "tender_no": lot_id,
                "tender_type": "",
                "lot_id": lot_id,
                "company": company,
                "search_key": company,
                # URL фильтрует по customer=company → адресная выдача,
                # все лоты принадлежат искомой компании (как в Tender.pro).
                "organizer_match": True,
                "href": href,
                "sum": price_val,
                "date_published": published or "Не указана",
                "date_deadline": deadline or "Не указана",
                "date_to_display": deadline or "Не указана",
            }
        except Exception as e:
            logger.debug(f"ГПБ parse: {e}")
            return None

    async def parse(self) -> List[Dict]:
        logger.info(f"ГПБ: поиск по {len(self.companies)} компаниям")
        all_tenders = []
        seen = set()
        page = await self.browser_manager.new_page()

        try:
            for company in self.companies:
                try:
                    url = self._build_url(company)
                    logger.info(f"ГПБ: '{company}'")

                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)

                    soup = BeautifulSoup(await page.content(), "lxml")

                    # Find procedure links
                    procedure_links = soup.select("a[href*='/procedure/']")
                    logger.debug(f"Found {len(procedure_links)} procedure links")

                    # Get parent cards
                    cards = []
                    for link in procedure_links:
                        parent = link.find_parent(["div", "tr", "article"])
                        if parent and parent not in cards:
                            cards.append(parent)

                    logger.debug(f"Extracted {len(cards)} unique cards")

                    found = 0
                    for card in cards:
                        t = self._parse_card(card, company)
                        if t and t["href"] and t["href"] not in seen:
                            seen.add(t["href"])
                            all_tenders.append(t)
                            found += 1

                    if found > 0:
                        logger.info(f"ГПБ: '{company}' → {found}")

                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"ГПБ '{company}': {e}")
        finally:
            await page.close()

        logger.info(f"ГПБ: итого {len(all_tenders)}")
        return all_tenders
