"""ТЭК-Торг за антиботом Variti: транспорт через окно браузера (12.10.2026).

Сеть и браузер не нужны: подменяем _evaluate (один POST из страницы) и
_warm (прохождение проверки). Проверяем, что ответы окна превращаются в те
же ошибки, что у urllib, — тогда повторы ТЗ-11 и причины неполноты
работают без изменений.
"""

import asyncio
import json
import urllib.error

import pytest

from src.parsers import tektorg_parser as tp_mod
from src.parsers.tektorg_transport import (
    ANTIBOT_TEXT,
    TektorgBrowserTransport,
)

OK_JSON = json.dumps({"total": 0, "totalPages": 0, "data": []})
CHALLENGE_HTML = "<!DOCTYPE html><html><head><title>..</title></head></html>"


@pytest.fixture(autouse=True)
def _без_сети_открытого_api(monkeypatch):
    """Обход списка лотов (16.10.2026) не должен уходить в живую сеть."""

    def сеть_запрещена(url):
        raise AssertionError(f"сеть в тестах запрещена: {url}")

    monkeypatch.setattr(tp_mod, "_fetch_get", сеть_запрещена)


def _transport(responses, warm_ok=True):
    t = TektorgBrowserTransport()
    t.ready = True
    t.page = object()
    calls = {"evaluate": 0, "warm": 0}
    queue = list(responses)

    async def fake_eval(payload):
        calls["evaluate"] += 1
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_warm():
        calls["warm"] += 1
        return warm_ok

    t._evaluate = fake_eval
    t._warm = fake_warm
    return t, calls


def _res(status, text="", retry_after=None, error=None):
    return {"status": status, "text": text, "retryAfter": retry_after, "error": error}


def test_200_json_возвращает_тело():
    t, calls = _transport([_res(200, OK_JSON)])
    assert asyncio.run(t.fetch({})) == OK_JSON
    assert calls == {"evaluate": 1, "warm": 0}


def test_503_становится_HTTPError_с_Retry_After():
    t, _ = _transport([_res(503, "busy", retry_after="7")])
    with pytest.raises(urllib.error.HTTPError) as ei:
        asyncio.run(t.fetch({}))
    assert ei.value.code == 503
    assert ei.value.headers.get("Retry-After") == "7"
    # Парсер считает это временной ошибкой и повторит.
    assert tp_mod._is_temporary_error(ei.value)


def test_антибот_переспросил_проверка_заново_и_повтор():
    t, calls = _transport([_res(403, "blocked"), _res(200, OK_JSON)])
    assert asyncio.run(t.fetch({})) == OK_JSON
    assert calls == {"evaluate": 2, "warm": 1}
    assert t.rewarm_count == 1


def test_html_вместо_json_при_200_тоже_антибот():
    t, calls = _transport([_res(200, CHALLENGE_HTML), _res(200, OK_JSON)])
    assert asyncio.run(t.fetch({})) == OK_JSON
    assert calls["warm"] == 1


def test_антибот_не_отпустил_постоянная_ошибка_403():
    t, _ = _transport([_res(403, "x"), _res(403, "x")])
    with pytest.raises(urllib.error.HTTPError) as ei:
        asyncio.run(t.fetch({}))
    assert ei.value.code == 403
    assert ANTIBOT_TEXT in str(ei.value)
    # 403 не повторяется — иначе обход растянется на 19 с × 75 объектов.
    assert not tp_mod._is_temporary_error(ei.value)


def test_обрыв_запроса_в_странице_временная_сетевая_ошибка():
    t, _ = _transport([_res(0, error="TypeError: Failed to fetch")])
    with pytest.raises(urllib.error.URLError) as ei:
        asyncio.run(t.fetch({}))
    assert tp_mod._is_temporary_error(ei.value)


def test_упавшая_страница_временная_сетевая_ошибка():
    t, _ = _transport([RuntimeError("Target closed")])
    with pytest.raises(urllib.error.URLError) as ei:
        asyncio.run(t.fetch({}))
    assert tp_mod._is_temporary_error(ei.value)


def test_get_открытого_api_через_окно_с_тем_же_разбором_ошибок():
    """Обход списка лотов (16.10.2026) ходит GET-ом — антибот и коды те же."""
    t, calls = _transport([])
    queue = [_res(200, CHALLENGE_HTML), _res(200, OK_JSON), _res(404, "nf")]
    urls = []

    async def fake_get(url):
        urls.append(url)
        return queue.pop(0)

    t._evaluate_get = fake_get
    url = "https://api.tektorg.ru/open-api/procedures/1"
    assert asyncio.run(t.fetch_get(url)) == OK_JSON
    assert calls["warm"] == 1 and urls == [url, url]
    with pytest.raises(urllib.error.HTTPError) as ei:
        asyncio.run(t.fetch_get(url))
    assert ei.value.code == 404


def test_незапущенный_транспорт_не_ходит_в_сеть():
    t = TektorgBrowserTransport()
    with pytest.raises(urllib.error.URLError):
        asyncio.run(t.fetch({}))


def test_парсер_ходит_через_транспорт_а_не_urllib(monkeypatch):
    """С транспортом urllib не вызывается вовсе."""

    def urllib_запрещён(payload):
        raise AssertionError("urllib не должен вызываться при транспорте")

    monkeypatch.setattr(tp_mod, "_fetch_procedures", urllib_запрещён)

    class FakeTransport:
        def __init__(self):
            self.payloads = []

        async def fetch(self, payload):
            self.payloads.append(payload)
            return OK_JSON

    tr = FakeTransport()
    parser = tp_mod.TektorgParser(
        None, [], days_back=1, keyword_phrases=["Антисептик"], transport=tr
    )
    asyncio.run(parser.parse())
    assert tr.payloads
    assert tr.payloads[0]["params"]["name"] == "Антисептик"


def _run_runner(monkeypatch, verdict):
    import src.core.parser_runner as pr

    events = []

    class FakeTransport:
        def __init__(self, *a, **kw):
            events.append("init")

        async def start(self):
            events.append("start")
            return True

        async def close(self):
            events.append("close")

    seen = []

    class FakeParser:
        def __init__(self, *a, **kw):
            seen.append(kw.get("transport"))
            self.unit_reports = []

        async def parse(self):
            return []

        @staticmethod
        def _dedup(t):
            return t

    monkeypatch.setattr(pr, "TektorgBrowserTransport", FakeTransport)
    monkeypatch.setattr(pr, "TektorgParser", FakeParser)
    monkeypatch.setattr(pr, "probe_direct", lambda fetch: verdict)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: ["Известь"])
    monkeypatch.setattr(
        "src.client_inns_store.load_client_inns", lambda *a, **kw: ["2457002628"]
    )
    runner = pr.ParserRunner.__new__(pr.ParserRunner)
    runner.browser_manager = None
    runner.companies = ["Северметалл"]
    runner.days_back = 1
    runner.settings = {}
    runner._take_reports = lambda parser: None
    asyncio.run(runner._run_tektorg())
    return events, seen, FakeTransport


def test_runner_антибот_окно_одно_на_оба_режима_и_закрывается(monkeypatch):
    events, seen, FT = _run_runner(monkeypatch, "antibot")
    assert events == ["init", "start", "close"]
    assert len(seen) == 2 and all(isinstance(t, FT) for t in seen)


def test_runner_прямой_путь_работает_окно_не_открывается(monkeypatch):
    """Прямой доступ к API есть: быстрый urllib, без Chromium."""
    events, seen, _ = _run_runner(monkeypatch, "ok")
    assert events == []
    assert seen == [None, None]


def test_runner_площадка_лежит_окно_не_поможет(monkeypatch):
    events, seen, _ = _run_runner(monkeypatch, "error")
    assert events == []
    assert seen == [None, None]


@pytest.mark.parametrize(
    "behaviour, expected",
    [
        (lambda p: OK_JSON, "ok"),
        (lambda p: CHALLENGE_HTML, "antibot"),
        ("307", "antibot"),
        ("403", "antibot"),
        ("503", "error"),
        ("net", "error"),
    ],
)
def test_probe_direct(behaviour, expected):
    from src.parsers.tektorg_transport import _http_error, probe_direct

    def fetch(payload):
        if callable(behaviour):
            return behaviour(payload)
        if behaviour == "net":
            raise urllib.error.URLError(OSError("reset"))
        raise _http_error(int(behaviour), "x", None)

    assert probe_direct(fetch) == expected


def test_проба_с_limit_который_API_принимает():
    """limit < 10 даёт HTTP 422 (12.10.2026) — проба врала «площадка лежит»."""
    from src.parsers.tektorg_transport import PROBE_PAYLOAD

    params = PROBE_PAYLOAD["params"]
    assert 10 <= params["limit"] <= tp_mod.PAGE_LIMIT
    assert params["sort"] == tp_mod.SORT_ACTUAL
