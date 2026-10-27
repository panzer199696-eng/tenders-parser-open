"""
Офлайн-тесты повтора временных ошибок ТЭК-Торга (ТЗ-11) — БЕЗ сети.

Восемь ночных прогонов (04–09.09.2026) теряли объекты ТЭК-Торга из-за
одиночных HTTP 503: _load_page на любой ошибке возвращал None, а _walk на
None прекращал обход объекта. Теперь временные ошибки (429/500/502/503/504,
таймаут, обрыв сети) повторяются с паузами RETRY_DELAYS, постоянные (404,
битый JSON) не повторяются вовсе, а после временной ошибки объект листается
вдвое медленнее.

Сеть подменяется монкипатчем _fetch_procedures; asyncio.sleep — на
мгновенный, чтобы тесты не спали по 2–12 секунд на каждый повтор.

Запуск:
    pytest tests/test_tektorg_retry.py -v
"""

import asyncio
import json
import logging
import sys
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers import completeness  # noqa: E402  (ТЗ-8)
import src.parsers.tektorg_parser as mod
from src.parsers.tektorg_parser import TektorgParser

FIXTURES = Path(__file__).parent / "fixtures"
SECTION_FIXTURE = FIXTURES / "tektorg_section_response.json"

# Минимально валидный ответ для _load_page (содержимое тут не важно).
GOOD_PAGE = '{"total": 0, "totalPages": 1, "currentPage": 1, "data": []}'

PAYLOAD = TektorgParser._build_payload(["nornikel"], 1)


@pytest.fixture(autouse=True)
def _без_сети_открытого_api(monkeypatch):
    """Обход списка лотов (16.10.2026) не должен уходить в живую сеть."""

    def сеть_запрещена(url):
        raise AssertionError(f"сеть в тестах запрещена: {url}")

    monkeypatch.setattr(mod, "_fetch_get", сеть_запрещена)


def _parser():
    """TektorgParser без браузера (сеть в тестах подменяется)."""
    return TektorgParser(
        browser_manager=None,
        companies=[],
        days_back=1,
        keyword_phrases=None,
        client_inns=None,
    )


def _walk_nornikel(parser):
    """Обойти одну секцию без отбора по заказчику (все лоты собираются)."""
    return parser._walk(
        "nornikel",
        phrase=None,
        section="nornikel",
        require_active=True,
        select_organizer=False,
    )


class ScriptedFetch:
    """Сетевой фейк: выдаёт сценарий по порядку; Exception бросает, str
    возвращает как тело ответа. Последний элемент повторяется — так
    «вечный 503» описывается одним элементом сценария."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, payload):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _http_error(code: int, retry_after=None) -> urllib.error.HTTPError:
    """HTTPError с опциональным числовым заголовком Retry-After."""
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://www.tektorg.ru/api/getProcedures", code, "boom", headers, None
    )


def _instant_sleep(monkeypatch) -> list:
    """asyncio.sleep → мгновенный; возвращает список запрошенных пауз."""
    pauses: list = []

    async def instant(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", instant)
    return pauses


def _section_pages(n: int) -> list[str]:
    """Секционную фикстуру (15 активных лотов) разложить на n страниц."""
    data = json.loads(SECTION_FIXTURE.read_text(encoding="utf-8"))
    lots = data["data"]
    size = -(-len(lots) // n)  # ceil: страницы заполнены, хвост в последней
    pages = []
    for i in range(n):
        page = dict(
            data, data=lots[i * size : (i + 1) * size], totalPages=n,
            currentPage=i + 1,
        )
        pages.append(json.dumps(page, ensure_ascii=False))
    return pages


# ── Повтор выручил ────────────────────────────────────────────────────────────


def test_503_once_then_json_is_retried(monkeypatch, caplog):
    """Тест 1 ТЗ-11: один 503, второй вызов нормальный → данные, вызовов 2."""
    fetch = ScriptedFetch(_http_error(503), GOOD_PAGE)
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    with caplog.at_level(logging.INFO, logger="src.parsers.tektorg_parser"):
        data = asyncio.run(_parser()._load_page(PAYLOAD))
    assert isinstance(data, dict)
    assert fetch.calls == 2
    # Диагностика различает «повторили и вышло» (ТЗ-11, критерий 3):
    assert any("повтор 1 из 3" in r.message for r in caplog.records)
    assert any("повтор удался" in r.message for r in caplog.records)


def test_timeout_then_json_is_retried(monkeypatch):
    """Тест 5 ТЗ-11: TimeoutError — временная ошибка, повтор выручает."""
    fetch = ScriptedFetch(TimeoutError("timed out"), GOOD_PAGE)
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    data = asyncio.run(_parser()._load_page(PAYLOAD))
    assert isinstance(data, dict)
    assert fetch.calls == 2


# ── Повторы исчерпаны ─────────────────────────────────────────────────────────


def test_three_503_in_a_row_exhausts_retries(monkeypatch, caplog):
    """Тест 2 ТЗ-11: три подряд 503 → None после ровно 4 вызовов (1+3),
    прежний warning с причиной — полнота считается как раньше."""
    fetch = ScriptedFetch(_http_error(503))
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    with caplog.at_level(logging.INFO, logger="src.parsers.tektorg_parser"):
        assert asyncio.run(_parser()._load_page(PAYLOAD)) is None
    assert fetch.calls == 4
    # «Повторили и не вышло»: видны все три повтора и финальный warning.
    assert any("повтор 3 из 3" in r.message for r in caplog.records)
    assert any("ошибка загрузки/разбора" in r.message for r in caplog.records)


def test_walk_marks_unit_network_after_exhausted_retries(monkeypatch):
    """Тест 2 ТЗ-11 (вторая половина): объект в _walk помечается прежней
    причиной network — правила полноты не тронуты."""
    page1, _ = _section_pages(2)
    fetch = ScriptedFetch(page1, _http_error(503))  # стр.2 — всегда 503
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    p = _parser()
    lots = asyncio.run(_walk_nornikel(p))
    assert fetch.calls == 1 + 4  # стр.1 + четыре попытки стр.2 (все 503)
    assert len(lots) == 8  # собранное со стр.1 не теряем
    [report] = p.unit_reports
    assert report["status"] == completeness.UNIT_PARTIAL
    assert report["reason"] == completeness.REASON_NETWORK


# ── Постоянные ошибки не повторяем ────────────────────────────────────────────


def test_404_is_not_retried(monkeypatch, caplog):
    """Тест 3 ТЗ-11: 404 постоянен — один вызов и None. Повтор вернул бы
    то же самое и лишь растянул бы ночной прогон."""
    fetch = ScriptedFetch(_http_error(404))
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="src.parsers.tektorg_parser"):
        assert asyncio.run(_parser()._load_page(PAYLOAD)) is None
    assert fetch.calls == 1
    assert any("404" in r.message for r in caplog.records)


def test_broken_json_is_not_retried(monkeypatch):
    """Тест 4 ТЗ-11: битый JSON постоянен — один вызов и None."""
    fetch = ScriptedFetch("не json")
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    assert asyncio.run(_parser()._load_page(PAYLOAD)) is None
    assert fetch.calls == 1


# ── Retry-After ───────────────────────────────────────────────────────────────


def test_retry_after_header_used_and_capped(monkeypatch):
    """Тест 6 ТЗ-11: Retry-After: 3 → пауза 3 с из заголовка;
    Retry-After: 600 обрезается до 30 — чужой заголовок не тянет прогон."""
    fetch = ScriptedFetch(_http_error(503, retry_after=3), GOOD_PAGE)
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    pauses = _instant_sleep(monkeypatch)
    asyncio.run(_parser()._load_page(PAYLOAD))
    assert pauses == [3.0]

    fetch = ScriptedFetch(_http_error(503, retry_after=600), GOOD_PAGE)
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    pauses = _instant_sleep(monkeypatch)
    asyncio.run(_parser()._load_page(PAYLOAD))
    assert pauses == [30.0]


# ── Живой сценарий обхода ─────────────────────────────────────────────────────


def test_walk_survives_single_503_mid_walk(monkeypatch):
    """Тест 7 ТЗ-11: стр.1 отдаётся, стр.2 один раз 503 и затем ок —
    объект обойдён целиком, причина пустая, лоты обеих страниц на месте."""
    page1, page2 = _section_pages(2)
    fetch = ScriptedFetch(page1, _http_error(503), page2)
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    _instant_sleep(monkeypatch)
    p = _parser()
    lots = asyncio.run(_walk_nornikel(p))
    assert fetch.calls == 3
    assert len(lots) == 15
    assert len({t["lot_id"] for t in lots}) == 15
    [report] = p.unit_reports
    assert report["status"] == completeness.UNIT_COMPLETE
    assert report["reason"] == ""


def test_page_pause_doubles_after_temporary_error(monkeypatch):
    """ТЗ-11 §2: после временной ошибки пауза между страницами удваивается
    до конца объекта — площадка троттлит частые запросы, прежний темп
    означал бы второй 503."""
    p1, p2, p3 = _section_pages(3)
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 3.0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 3.0)
    fetch = ScriptedFetch(p1, _http_error(503), p2, p3)
    monkeypatch.setattr(mod, "_fetch_procedures", fetch)
    pauses = _instant_sleep(monkeypatch)
    asyncio.run(_walk_nornikel(_parser()))
    # Паузы по порядку: перед стр.2 обычная (3 с), повтор после 503 (2 с),
    # перед стр.3 — удвоенная (6 с): объект словил 503 и замедлился.
    assert pauses == [3.0, 2.0, 6.0]
