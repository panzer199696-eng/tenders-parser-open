"""
Сторож формата ответа ТЭК-Торга (16.10.2026).

Ночью API перестал присылать поле `data` (в ответе только totalPages,
currentPage, limitPage, hash). Парсер счёл это «закупок нет» и отчитался
«все 77 объектов обойдены целиком» — площадка молча дала ноль.

Правило: заглушка без `data` и `total` на первом же запросе — поиск мёртв,
парсер уходит в обход списка лотов (tests/test_tektorg_open.py). Если хоть
один объект лоты получил, отсутствие поля у остальных — просто пустая
выдача. Если и список лотов отвечает без `data` — секция «не обойдена» с
причиной «площадка изменила формат».
"""

import asyncio
import json

import pytest

import src.parsers.tektorg_parser as mod
from src.parsers import completeness
from tests.test_tektorg_parser import _parser, _patch_loader, _sec_text

NEW_FORMAT = json.dumps(
    {"totalPages": 1, "currentPage": 1, "limitPage": 50, "hash": "ebc3887d"}
)
EMPTY_LOTS = json.dumps(
    {"total": 0, "totalPages": 0, "currentPage": 1, "limitPage": 50, "data": []}
)


@pytest.fixture(autouse=True)
def _open_api(monkeypatch):
    """Открытый API — без сети: по умолчанию пустой список лотов."""
    calls = []

    def fake_get(url):
        calls.append(url)
        return EMPTY_LOTS

    monkeypatch.setattr(mod, "_fetch_get", fake_get)
    monkeypatch.setattr(mod, "watch_names", list)
    return calls


def test_заглушка_у_первого_объекта_переключает_на_список_лотов(monkeypatch, _open_api):
    calls = _patch_loader(monkeypatch, [NEW_FORMAT], sections=("nornikel", "market"))
    parser = _parser(companies=[], client_inns=["7700000001"])
    assert asyncio.run(parser.parse()) == []
    # Мёртвый поиск стоил один запрос, дальше — список лотов секций.
    assert calls["n"] == 1
    assert len(_open_api) == 2
    reasons = {r["reason"] for r in parser.unit_reports}
    assert completeness.REASON_FORMAT_CHANGED not in reasons
    assert {r["label"] for r in parser.unit_reports} == {"nornikel", "market"}


def test_список_лотов_без_data_это_смена_формата(monkeypatch):
    monkeypatch.setattr(mod, "_fetch_get", lambda url: NEW_FORMAT)
    _patch_loader(monkeypatch, [NEW_FORMAT], sections=("nornikel", "market"))
    parser = _parser(companies=[], client_inns=["7700000001"])
    assert asyncio.run(parser.parse()) == []
    assert len(parser.unit_reports) == 2
    for report in parser.unit_reports:
        assert report["status"] == completeness.UNIT_SKIPPED
        assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_пустые_фразы_рядом_с_живыми_не_тревога(monkeypatch, _open_api):
    # Первая фраза получила лоты, вторая — ответ без поля data: это пустая
    # выдача по фразе, а не смена формата и не повод уходить в список.
    _patch_loader(monkeypatch, [_sec_text(), NEW_FORMAT])
    parser = _parser(keyword_phrases=["герметик", "мелющие тела"])
    tenders = asyncio.run(parser.parse())
    assert tenders
    reasons = {r["reason"] for r in parser.unit_reports}
    assert completeness.REASON_FORMAT_CHANGED not in reasons
    assert _open_api == []
