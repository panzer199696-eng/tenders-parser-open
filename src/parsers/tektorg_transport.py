"""
ТЭК-Торг: запросы к JSON API из окна браузера — обход антибота Variti.

С ~20.09.2026 площадка закрыта антиботом Variti, и прямой POST к
/api/getProcedures больше не проходит (живая проверка 12.10.2026):

  • urllib / curl — `307 TemporaryRedirect` на тот же адрес с куками
    ipp_uid/ipp_key, повтор с куками отдаёт HTML с JS-проверкой;
  • headless Chromium И `--headless=new` (который выручает на других площадках) —
    `403` «сеанс прерван системой защиты от ботов» на каждый запрос;
  • обычное окно Chromium — проверку проходит с первой попытки, API
    отвечает `200` JSON (18 744 процедуры в секции nornikel);
  • куки, полученные окном, в urllib НЕ переносятся — снова HTML-проверка
    (привязка к самому браузеру), поэтому ходим в API ИЗ страницы.

Восемнадцать ночей подряд (20.09–12.10) ТЭК-Торг отдавал ноль с вердиктом
«обрыв связи»: причину знал только лог (`HTTP Error 307`).

Окно ставится за край экрана (`--window-position`) — ночью его никто не
видит, днём оно не мешает. Ошибки отдаются как urllib.error.HTTPError /
URLError, чтобы логика повторов парсера (ТЗ-11) работала без изменений.
"""

from __future__ import annotations

import asyncio
import email.message
import json
import logging
import time
import urllib.error

logger = logging.getLogger(__name__)

HOME_URL = "https://www.tektorg.ru/"
API_URL = "https://www.tektorg.ru/api/getProcedures"
API_PATH = "/api/getProcedures"

# Окно за краем экрана: Variti отличает headless по самому браузеру, а не
# по видимости окна. Позицию -2400 Windows не «возвращает» на экран
# (проверено 12.10.2026).
WINDOW_ARGS = (
    "--window-position=-2400,-2400",
    "--window-size=1280,900",
    "--disable-blink-features=AutomationControlled",
)

# Проверка Variti проходит за 1–3 с; ждём с запасом, но ограниченно.
WARM_TIMEOUT = 45.0
WARM_POLL = 2.5
# Таймаут одного запроса из страницы (мс) — как REQUEST_TIMEOUT парсера.
FETCH_TIMEOUT_MS = 30000

# Короткий пробный запрос: первая страница секции nornikel.
# ⚠️ limit < 10 API отклоняет с HTTP 422 (проверено 12.10.2026: 1 → 422,
# 10/20/50 → 200). С limit=1 проба считала живую площадку «не ответившей»,
# а окно браузера не прогрелось бы никогда.
PROBE_LIMIT = 10
PROBE_PAYLOAD = {
    "params": {
        "sectionsCodes[0]": "nornikel",
        "page": 1,
        "sort": "actual",
        "limit": PROBE_LIMIT,
    }
}

# Подпись антибота в журнале и в причине неполноты.
ANTIBOT_TEXT = "антибот площадки (Variti) не пропустил запрос"

_FETCH_JS = """
async ([path, body, timeoutMs]) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
        const r = await fetch(path, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
            },
            body: body,
            signal: ctl.signal,
        });
        return {
            status: r.status,
            text: await r.text(),
            retryAfter: r.headers.get('Retry-After'),
            error: null,
        };
    } catch (e) {
        return {status: 0, text: '', retryAfter: null, error: String(e)};
    } finally {
        clearTimeout(timer);
    }
}
"""

# GET открытого API (api.tektorg.ru): с 16.10.2026 поиск getProcedures отдаёт
# заглушку, и лоты собираются обходом списка лотов и карточек процедур.
_FETCH_GET_JS = """
async ([url, timeoutMs]) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
        const r = await fetch(url, {
            method: 'GET',
            headers: {'Accept': 'application/json, text/plain, */*'},
            signal: ctl.signal,
        });
        return {
            status: r.status,
            text: await r.text(),
            retryAfter: r.headers.get('Retry-After'),
            error: null,
        };
    } catch (e) {
        return {status: 0, text: '', retryAfter: null, error: String(e)};
    } finally {
        clearTimeout(timer);
    }
}
"""


def _looks_like_json(text: str) -> bool:
    return text.lstrip()[:1] in ("{", "[")


def _http_error(
    status: int, msg: str, retry_after: str | None
) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(API_URL, status, msg, hdrs, None)


class TektorgBrowserTransport:
    """Окно Chromium на tektorg.ru, через которое идут запросы к API.

    Использование (parser_runner):
        transport = TektorgBrowserTransport(proxy=PROXY_SERVER)
        if await transport.start(): ...TektorgParser(..., transport=transport)
        await transport.close()

    fetch() возвращает тело ответа (str) или поднимает HTTPError/URLError
    — ровно как _fetch_procedures на urllib, поэтому повторы, Retry-After и
    причины неполноты в парсере не меняются.
    """

    def __init__(self, proxy: str | None = None, warm_timeout: float = WARM_TIMEOUT):
        self.proxy = proxy
        self.warm_timeout = warm_timeout
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self.ready = False
        # Сколько раз за прогон пришлось заново проходить проверку — в итог.
        self.rewarm_count = 0

    # ── Жизненный цикл ──────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Запустить окно и пройти проверку Variti. False — не удалось.

        Не бросает: ТЭК-Торг — одна площадка из семи, и его антибот не
        должен ронять весь ночной прогон.
        """
        try:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            launch = {"headless": False, "args": list(WINDOW_ARGS)}
            if self.proxy:
                launch["proxy"] = {"server": self.proxy}
            self._browser = await self._pw.chromium.launch(**launch)
            self._context = await self._browser.new_context(
                locale="ru-RU", timezone_id="Europe/Moscow"
            )
            self.page = await self._context.new_page()
        except Exception as e:
            logger.warning(f"ТЭК-Торг: не удалось запустить окно браузера ({e})")
            await self.close()
            return False
        self.ready = await self._warm()
        if not self.ready:
            logger.warning(
                f"ТЭК-Торг: {ANTIBOT_TEXT} за {self.warm_timeout:.0f} с — "
                "площадка в этом прогоне не обходится"
            )
        else:
            logger.info("ТЭК-Торг: проверка антибота пройдена, API отвечает")
        return self.ready

    async def close(self) -> None:
        for obj, meth in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._pw, "stop"),
        ):
            if obj is None:
                continue
            try:
                await getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._context = self.page = None
        self.ready = False

    # ── Запросы ─────────────────────────────────────────────────────────────

    async def _evaluate(self, payload: dict) -> dict:
        """Один POST из страницы. Вынесено отдельно — тесты подменяют его."""
        body = json.dumps(payload, ensure_ascii=False)
        return await self.page.evaluate(_FETCH_JS, [API_PATH, body, FETCH_TIMEOUT_MS])

    async def _evaluate_get(self, url: str) -> dict:
        """Один GET из страницы. Вынесено отдельно — тесты подменяют его."""
        return await self.page.evaluate(_FETCH_GET_JS, [url, FETCH_TIMEOUT_MS])

    async def _open_home(self) -> None:
        await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)

    async def _warm(self) -> bool:
        """Открыть главную и ждать, пока пробный запрос не вернёт JSON."""
        try:
            await self._open_home()
        except Exception as e:
            logger.warning(f"ТЭК-Торг: главная не открылась ({e})")
            return False
        deadline = time.monotonic() + self.warm_timeout
        while True:
            await asyncio.sleep(WARM_POLL)
            try:
                res = await self._evaluate(PROBE_PAYLOAD)
            except Exception as e:
                logger.debug(f"ТЭК-Торг: прогрев — ошибка страницы ({e})")
                res = None
            if (
                res
                and res.get("status") == 200
                and _looks_like_json(res.get("text") or "")
            ):
                return True
            if time.monotonic() >= deadline:
                return False

    @staticmethod
    def _is_challenge(res: dict) -> bool:
        """Ответ антибота, а не API: 403/307 или HTML вместо JSON при 200."""
        status = res.get("status")
        if status in (403, 307):
            return True
        return status == 200 and not _looks_like_json(res.get("text") or "")

    async def fetch(self, payload: dict) -> str:
        return await self._request(lambda: self._evaluate(payload))

    async def fetch_get(self, url: str) -> str:
        """GET открытого API из окна — тот же разбор ошибок, что у fetch."""
        return await self._request(lambda: self._evaluate_get(url))

    async def _request(self, call) -> str:
        if not self.ready or self.page is None:
            raise urllib.error.URLError(OSError("окно ТЭК-Торга не запущено"))
        try:
            res = await call()
        except Exception as e:
            # Страница упала/закрылась — сетевой сбой, парсер повторит.
            raise urllib.error.URLError(OSError(f"страница браузера: {e}")) from e

        if self._is_challenge(res):
            # Variti может переспросить посреди прогона (истекли куки).
            # Один раз проходим проверку заново и повторяем запрос.
            self.rewarm_count += 1
            logger.info("ТЭК-Торг: антибот переспросил — прохожу проверку заново")
            if await self._warm():
                try:
                    res = await call()
                except Exception as e:
                    raise urllib.error.URLError(
                        OSError(f"страница браузера: {e}")
                    ) from e
            if self._is_challenge(res):
                raise _http_error(403, ANTIBOT_TEXT, None)

        status = res.get("status") or 0
        if status == 0:
            raise urllib.error.URLError(OSError(res.get("error") or "обрыв запроса"))
        if status != 200:
            raise _http_error(status, f"HTTP {status}", res.get("retryAfter"))
        return res.get("text") or ""


# ── Выбор пути: прямой urllib или окно ──────────────────────────────────────
#
# 12.10.2026 выяснилось, что антибот режет не «бота», а ИНОСТРАННЫЙ IP: с
# зарубежного адреса прямой POST получал 307, а с российского тот же urllib
# снова отдаёт JSON. Окно браузера — это ~5 с запуска и лишний Chromium на
# весь обход, поэтому сначала пробуем дешёвый прямой путь и открываем окно,
# только если его режет антибот (зарубежный адрес, сменилась политика).


def probe_direct(fetch) -> str:
    """Проверить прямой путь одним запросом. fetch — _fetch_procedures.

    Возвращает "ok" (JSON пришёл), "antibot" (307/403 или HTML вместо JSON
    — нужен браузер) или "error" (сеть/площадка лежит — браузер не поможет,
    пусть отработают обычные повторы и причины неполноты).
    """
    try:
        raw = fetch(PROBE_PAYLOAD)
    except urllib.error.HTTPError as e:
        return "antibot" if e.code in (307, 403) else "error"
    except Exception:
        return "error"
    return "ok" if _looks_like_json(raw or "") else "antibot"
