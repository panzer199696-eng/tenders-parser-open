"""Фразы без отдачи не уходят в keyword-поиск ТЭК-Торга.

Что стережём: площадка ищет по коротким обозначениям позиций (марки,
артикулы) как попало. Такая фраза может каждую ночь листать десятки страниц
чужих закупок, упираться в потолок страниц и давать ошибки 503. При этом на
других площадках эти фразы работают, поэтому из справочника они НЕ убираются —
режутся только для ТЭК-Торга, списком в настройках ``tektorg_skip_phrases``.

Сеть и браузер не трогаются: парсер подменяется, проверяется, что доехало.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.parser_runner import _tektorg_phrases
from src.settings_store import DEFAULTS

# Фразы, которые площадка отрабатывает нормально — они обязаны остаться.
GOOD = ["Задвижка", "Насос центробежный", "Кабель силовой"]
# Короткие марки, по которым поиск площадки бесполезен (пример списка).
BAD = ["НШ 1-15", "НШ 2-12", "Запчасти к насосу НШ", "ЦНС"]
SKIP = {"tektorg_skip_phrases": BAD}


def test_пустые_фразы_отброшены_остальные_целы():
    """Фразы из списка исключений уходят, полезные остаются в прежнем порядке."""
    got = _tektorg_phrases(GOOD[:1] + BAD + GOOD[1:], SKIP)
    assert got == GOOD


def test_список_по_умолчанию_пуст_и_живёт_в_настройках():
    """Настройка живёт в settings_store — правится без похода в код; в
    поставке исключений нет, их задаёт пользователь под свой справочник."""
    assert DEFAULTS["tektorg_skip_phrases"] == []
    assert _tektorg_phrases(GOOD + BAD, {}) == GOOD + BAD


def test_регистр_и_лишние_пробелы_не_спасают_фразу():
    """В справочнике фраза может быть записана иначе — сверяем нормализованно."""
    got = _tektorg_phrases(["  нш   2-12 ", "цнс", "Задвижка"], SKIP)
    assert got == ["Задвижка"]


def test_похожая_но_другая_фраза_остаётся():
    """Режем ровно перечисленное: «ЦНС 60-198» — отдельная фраза."""
    got = _tektorg_phrases(["ЦНС 60-198", "Насос ЦНС"], SKIP)
    assert got == ["ЦНС 60-198", "Насос ЦНС"]


def test_пустой_список_исключений_ничего_не_режет():
    """Список в настройках можно очистить — тогда идут все фразы."""
    got = _tektorg_phrases(GOOD + BAD, {"tektorg_skip_phrases": []})
    assert got == GOOD + BAD


def test_свой_список_исключений_из_настроек():
    """Список задаётся настройкой, а не прибит в коде."""
    got = _tektorg_phrases(
        ["Задвижка", "Кабель силовой"], {"tektorg_skip_phrases": ["Кабель силовой"]}
    )
    assert got == ["Задвижка"]


# ── Доставка в сам обход площадки ────────────────────────────────────────────


class _FakeTektorg:
    """Парсер-заглушка: запоминает, с чем его позвали."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def parse(self):
        return []

    @staticmethod
    def _dedup(tenders):
        return tenders


def test_фразы_без_отдачи_не_доезжают_до_парсера(monkeypatch):
    """Главная проверка: обход ТЭК-Торга не получает мусорные фразы."""
    import asyncio

    _FakeTektorg.instances = []
    monkeypatch.setattr("src.core.parser_runner.TektorgParser", _FakeTektorg)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: GOOD + BAD)
    monkeypatch.setattr("src.client_inns_store.load_client_inns", lambda *a, **kw: [])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner.__new__(ParserRunner)
    runner.browser_manager = None
    runner.companies = []
    runner.days_back = 1
    runner.settings = {"parse_tektorg": True, **SKIP}
    runner.unit_reports = []

    asyncio.run(runner._run_tektorg())

    assert _FakeTektorg.instances, "парсер ТЭК-Торга не был вызван"
    got = _FakeTektorg.instances[0].kwargs.get("keyword_phrases")
    assert got == GOOD
