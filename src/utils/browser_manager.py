"""
Browser manager using Playwright — с антибот-мерами.

Нужен площадкам, которые отдают выдачу только браузеру (Tender.pro, ГПБ).
Без авторизации: программа работает только с открытыми разделами площадок.
"""

import logging
import os
import random

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext

load_dotenv()
logger = logging.getLogger(__name__)

PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()

# Пул реалистичных User-Agent строк
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

# JS-скрипт для скрытия признаков автоматизации
_STEALTH_SCRIPT = """
    // Скрываем navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Эмулируем нормальные плагины браузера
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });

    // Убираем следы Chrome automation
    window.chrome = { runtime: {} };

    // Корректный navigator.languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['ru-RU', 'ru', 'en-US', 'en']
    });
"""


class BrowserManager:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None

    async def start(self):
        self.playwright = await async_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-extensions",
        ]
        # Классический headless Chromium (chrome-headless-shell) часть площадок
        # распознаёт и сразу отдаёт капчу «превышен лимит скорости просмотра»
        # (headless=True → капча, --headless=new → выдача). Поэтому браузер
        # всегда запускаем «обычным», а невидимость включаем флагом
        # --headless=new: окна нет, но детект не срабатывает.
        if self.headless:
            args.append("--headless=new")

        launch_kwargs = {"headless": False, "args": args}

        if PROXY_SERVER:
            launch_kwargs["proxy"] = {"server": PROXY_SERVER}
            logger.info(f"BrowserManager: прокси {PROXY_SERVER}")

        self.browser = await self.playwright.chromium.launch(**launch_kwargs)

        ua = random.choice(_USER_AGENTS)
        # Слегка рандомизируем viewport чтобы не выглядеть ботом
        w = random.randint(1270, 1370)
        h = random.randint(870, 920)

        context_kwargs = {
            "viewport": {"width": w, "height": h},
            "user_agent": ua,
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "extra_http_headers": {
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
        }

        self.context = await self.browser.new_context(**context_kwargs)

        # Применяем стелс-скрипт ко всем страницам
        await self.context.add_init_script(_STEALTH_SCRIPT)

        logger.info(
            f"Браузер запущен ({'фоновый режим' if self.headless else 'видимое окно'}"
            f", viewport={w}x{h})"
        )

    async def new_page(self):
        if not self.context:
            await self.start()
        return await self.context.new_page()

    async def close(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Браузер закрыт")
