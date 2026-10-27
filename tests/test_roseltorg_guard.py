"""
Сторож формата Росэлторга (Д1, 16.10.2026) и срок со временем.

Раньше: HTML антибота с кодом 200 разбирался как пустая выдача («закупок
нет»), переименованный атрибут карточки давал ноль лотов при десяти
карточках, исключение в обходе одного ИНН обрывало все оставшиеся, а срок
«04.09.2026 в 14:00» терял время. Фикстуры — настоящие ответы 31.08.2026;
«сломанные» варианты получены из них правкой.
"""

import asyncio
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.parsers.roseltorg_parser as mod
from src.parsers import completeness
from src.parsers.roseltorg_parser import RoseltorgParser

FIXTURES = ROOT / "tests" / "fixtures"
INN = (FIXTURES / "roseltorg_inn_response.html").read_text(encoding="utf-8")
EMPTY = (FIXTURES / "roseltorg_empty_response.html").read_text(encoding="utf-8")
CAPTCHA = "<html><body>Проверка: подтвердите, что вы не робот</body></html>"


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)


def _patch_fetch(monkeypatch, answers):
    """Подменить сетевой запрос: строка — тело ответа, исключение — бросить."""
    queue = list(answers)

    def fake_fetch(url):
        item = queue.pop(0) if queue else EMPTY
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(RoseltorgParser, "_fetch", staticmethod(fake_fetch))


def _report(p, label):
    [r] = [r for r in p.unit_reports if r["label"] == label]
    return r


def test_настоящая_выдача_собрана_целиком(monkeypatch):
    _patch_fetch(monkeypatch, [INN, EMPTY])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    assert len(asyncio.run(p.parse())) == 10
    assert _report(p, "7700000001")["status"] == completeness.UNIT_COMPLETE


def test_пустая_выдача_это_нет_закупок(monkeypatch):
    _patch_fetch(monkeypatch, [EMPTY])
    p = RoseltorgParser(None, [], client_inns=["7700000000"])
    assert asyncio.run(p.parse()) == []
    assert _report(p, "7700000000")["status"] == completeness.UNIT_COMPLETE


def test_антибот_с_кодом_200_это_блокировка(monkeypatch):
    _patch_fetch(monkeypatch, [CAPTCHA])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    assert asyncio.run(p.parse()) == []
    report = _report(p, "7700000001")
    assert report["status"] == completeness.UNIT_SKIPPED
    assert report["reason"] == completeness.REASON_BLOCKED


def test_http_403_это_блокировка(monkeypatch):
    _patch_fetch(
        monkeypatch, [urllib.error.HTTPError(mod.BASE_URL, 403, "Forbidden", {}, None)]
    )
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    asyncio.run(p.parse())
    assert _report(p, "7700000001")["reason"] == completeness.REASON_BLOCKED


def test_обрыв_связи_остаётся_обрывом(monkeypatch):
    _patch_fetch(monkeypatch, [OSError("timed out")])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    asyncio.run(p.parse())
    assert _report(p, "7700000001")["reason"] == completeness.REASON_NETWORK


def test_пустой_ответ_200_это_смена_формата(monkeypatch):
    _patch_fetch(monkeypatch, [""])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    asyncio.run(p.parse())
    assert _report(p, "7700000001")["reason"] == completeness.REASON_FORMAT_CHANGED


def test_переименованный_атрибут_карточки_это_смена_формата(monkeypatch):
    broken = INN.replace(
        "data-feature-favorite-lots-procedure-number", "data-procedure-number"
    )
    _patch_fetch(monkeypatch, [broken])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    assert asyncio.run(p.parse()) == []
    assert _report(p, "7700000001")["reason"] == completeness.REASON_FORMAT_CHANGED


def test_переименованный_класс_карточки_это_смена_формата(monkeypatch):
    broken = INN.replace("search-results__item", "search-results__card")
    _patch_fetch(monkeypatch, [broken])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    asyncio.run(p.parse())
    assert _report(p, "7700000001")["reason"] == completeness.REASON_FORMAT_CHANGED


def test_сбой_одного_инн_не_обрывает_остальные(monkeypatch):
    _patch_fetch(monkeypatch, [INN, EMPTY])
    real_walk = RoseltorgParser._walk

    async def flaky(self, query, by_inn):
        if query == "1111111111":
            raise RuntimeError("неожиданное")
        return await real_walk(self, query, by_inn)

    monkeypatch.setattr(RoseltorgParser, "_walk", flaky)
    p = RoseltorgParser(None, [], client_inns=["1111111111", "7700000001"])
    assert len(asyncio.run(p.parse())) == 10
    failed = _report(p, "1111111111")
    assert failed["status"] == completeness.UNIT_SKIPPED
    assert failed["reason"] == completeness.REASON_CRASHED
    assert _report(p, "7700000001")["status"] == completeness.UNIT_COMPLETE


def test_срок_сохраняет_время():
    """«04.09.2026 в 14:00» — время подачи важно: в 15:00 заявку уже не подать."""
    rows = RoseltorgParser(None, [])._parse_page(INN, "7700000001", by_inn=True)
    assert rows[0]["date_deadline"] == "04.09.2026 14:00"
    assert rows[0]["date_to_display"] == "04.09.2026 14:00"


def test_завершённые_и_отменённые_не_берём():
    html = INN.replace("Прием заявок", "Процедура завершена", 1)
    html = html.replace("Прием заявок", "Отменена", 1)
    rows = RoseltorgParser(None, [])._parse_page(html, "7700000001", by_inn=True)
    assert len(rows) == 8


def test_все_карточки_завершены_это_не_смена_формата(monkeypatch):
    """Отсеянные статусом карточки — узнанные, это не «распознано 0»."""
    _patch_fetch(monkeypatch, [INN.replace("Прием заявок", "Завершена"), EMPTY])
    p = RoseltorgParser(None, [], client_inns=["7700000001"])
    assert asyncio.run(p.parse()) == []
    assert _report(p, "7700000001")["status"] == completeness.UNIT_COMPLETE
