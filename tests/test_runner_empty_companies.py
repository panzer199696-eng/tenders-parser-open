# -*- coding: utf-8 -*-
"""
Тесты ParserRunner.run_all() при пустом списке компаний (Задача 1).

Раньше пустой self.companies обрывал весь парсинг через ранний ``return []``
в run_all(). Теперь парсеры, которым компании не нужны (parse_zakupki —
keyword-only), запускаются даже с пустым списком, а парсеры, требующие
компаний (parse_gpb, parse_tender_pro), пропускаются с предупреждением, не
роняя цикл.

Сеть и браузер не используются; все парсеры подменяются fakes.

Запуск:
    pytest tests/test_runner_empty_companies.py -v
"""

import asyncio
import logging
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _base_settings(**overrides):
    """Полные настройки раннера; по умолчанию все парсеры выключены."""
    base = {
        "days_back": 1,
        "min_price": 0,
        "max_price": 0,
        "include_no_price": True,
        "parse_tender_pro": False,
        "parse_gpb": False,
        "parse_zakupki": False,
        "browser_headless": True,
        "keyword_search_enabled": False,
        "keyword_filter_enabled": False,
    }
    base.update(overrides)
    return base


class _FakeParser:
    """Минимальный fake парсера: фиксирует факт вызова, возвращает [].

    Конструктор принимает любые параметры (включая keyword_phrases), чтобы
    подмена работала для любого парсера.

    Каждый подкласс имеет свой счётчик ``instances`` (см. _patch_all_parsers).
    """

    instances = None  # переопределяется в _patch_all_parsers

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def parse(self):
        return []


def _patch_all_parsers(monkeypatch):
    """Подменить парсеры в модуле раннера на fakes.

    Возвращает словарь {ключ: класс} с независимыми счётчиками ``instances``
    для каждого парсера (важно: общий родительский список работал бы
    неправильно — все классы насыпали бы в одну кучу).
    """

    def _make(name):
        return type(name, (_FakeParser,), {"instances": []})

    fakes = {
        "gpb": _make("gpb"),
        "tender_pro": _make("tender_pro"),
        "zakupki": _make("zakupki"),
    }

    monkeypatch.setattr("src.core.parser_runner.GPBParser", fakes["gpb"])
    monkeypatch.setattr("src.core.parser_runner.TenderProParser", fakes["tender_pro"])
    monkeypatch.setattr("src.core.parser_runner.ZakupkiParser", fakes["zakupki"])

    return fakes


# ── Задача 1: keyword-only площадка запускается при пустых компаниях ─────────


def test_run_all_reaches_keyword_only_when_companies_empty(monkeypatch):
    """При companies=[] и parse_zakupki=True run_all() ДОЛЖЕН дойти до
    вызова _run_zakupki() (не возвращать [] на входе)."""
    fakes = _patch_all_parsers(monkeypatch)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: ["Насосы"])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner(
        browser_manager=None,
        settings=_base_settings(parse_zakupki=True),
        companies=[],
    )
    result = asyncio.run(runner.run_all())

    assert isinstance(result, list)
    assert len(fakes["zakupki"].instances) == 1, (
        "run_all не должен обрываться при пустых компаниях, если включён ЕИС"
    )
    # gpb/tender_pro выключены в настройках — их fake-классы не вызываются
    assert len(fakes["gpb"].instances) == 0
    assert len(fakes["tender_pro"].instances) == 0


def test_run_all_logs_info_when_companies_empty(monkeypatch, caplog):
    """При пустых компаниях логируется warning, но не возвращается [] сразу."""
    _patch_all_parsers(monkeypatch)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: ["Насосы"])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner(
        browser_manager=None,
        settings=_base_settings(parse_zakupki=True),
        companies=[],
    )
    caplog.set_level(logging.INFO, logger="src.core.parser_runner")
    asyncio.run(runner.run_all())
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Список компаний пуст" in msgs
    # При этом run_all продолжил работу — настройки тоже залогированы (INFO)
    assert "Настройки:" in msgs


# ── Задача 1: GPB/Tender.pro пропускаются с логом при пустых компаниях ───────


def test_gpb_skipped_when_companies_empty(monkeypatch, caplog):
    """parse_gpb=True, companies=[] → GPB-таск пропущен с логом,
    run_all() возвращает [] без исключений."""
    fakes = _patch_all_parsers(monkeypatch)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: [])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner(
        browser_manager=None,
        settings=_base_settings(parse_gpb=True),
        companies=[],
    )
    caplog.set_level(logging.WARNING, logger="src.core.parser_runner")
    result = asyncio.run(runner.run_all())

    assert result == []
    assert len(fakes["gpb"].instances) == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "GPB" in msgs and "пропущен" in msgs


def test_tender_pro_skipped_when_companies_empty(monkeypatch, caplog):
    """Аналогично для parse_tender_pro."""
    fakes = _patch_all_parsers(monkeypatch)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: [])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner(
        browser_manager=None,
        settings=_base_settings(parse_tender_pro=True),
        companies=[],
    )
    caplog.set_level(logging.WARNING, logger="src.core.parser_runner")
    result = asyncio.run(runner.run_all())

    assert result == []
    assert len(fakes["tender_pro"].instances) == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Tender.pro" in msgs and "пропущен" in msgs


def test_keyword_only_skipped_only_when_no_phrases(monkeypatch):
    """Если parse_zakupki=True, но нет активных фраз — _run_zakupki сам
    возвращает [] до создания парсера. Компании не при чём."""
    fakes = _patch_all_parsers(monkeypatch)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: [])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner(
        browser_manager=None,
        settings=_base_settings(parse_zakupki=True),
        companies=[],
    )
    result = asyncio.run(runner.run_all())

    assert result == []
    assert len(fakes["zakupki"].instances) == 0


# ── Задача 1: ничего не включено → run_all() не падает даже при [] ───────────


def test_all_disabled_no_crash_with_empty_companies(monkeypatch):
    """Защита от регрессии: при всех выключенных парсерах и пустых компаниях
    run_all() должен вернуть [], не выбрасывая исключений."""
    _patch_all_parsers(monkeypatch)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: [])

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner(
        browser_manager=None,
        settings=_base_settings(),
        companies=[],
    )
    result = asyncio.run(runner.run_all())
    assert result == []
