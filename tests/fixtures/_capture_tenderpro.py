# -*- coding: utf-8 -*-
"""
Capture fixture для офлайн-тестов Tender.pro:
  - tests/fixtures/tenderpro_search_sample.html — страница 1 выдачи
    (company_name=Ромашка&filtered=1) + первая следующая страница (для теста
    пагинации). Парсер использует Playwright через BrowserManager, как и в бою.

Запуск:  set PYTHONUTF8=1 && python tests/fixtures/_capture_tenderpro.py
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from src.utils.browser_manager import BrowserManager  # noqa: E402
from src.parsers.tender_pro_parser import BASE_URL, LIST_PATH  # noqa: E402

FIX = Path(__file__).parent
COMPANY = "Ромашка"


async def main():
    bm = BrowserManager(headless=False)
    await bm.start()
    page = await bm.new_page()
    try:
        url = f"{BASE_URL}{LIST_PATH}?company_name={quote(COMPANY)}&filtered=1"
        print(f"открыть: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        html = await page.content()
        out = FIX / "tenderpro_search_sample.html"
        out.write_text(html, encoding="utf-8")
        print(f"сохранено: {out.name} ({len(html)} байт)")

        from bs4 import BeautifulSoup
        from src.parsers.tender_pro_parser import TenderProParser

        soup = BeautifulSoup(html, "lxml")
        rows = TenderProParser._select_rows(soup)
        pages = TenderProParser._discover_page_urls(soup, current_index=0)
        print(f"строк на стр.1: {len(rows)}, обнаружено страниц пагинации: {len(pages)}")
    finally:
        await page.close()
        await bm.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
