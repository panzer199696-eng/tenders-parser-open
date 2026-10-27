# -*- coding: utf-8 -*-
"""Заказчики на особом контроле проходят без фильтров отбора.

Правило (решение владельца 16.09.2026): лот заказчика из ``watch_clients``
не отсекается ни порогом цены, ни мягким keyword-фильтром, ни профилем
фирм-операторов, и не прячется в корзину «Не наш профиль». Единственное
исключение — истёкший дедлайн: подавать заявку некуда даже по Кедру.

Все тесты офлайн: настройки подменяются монкейпатчем, как в
tests/test_excel_report.py, сети и браузера нет.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.settings_store as settings_store  # noqa: E402
from src.core.parser_runner import (  # noqa: E402
    apply_keyword_filter,
    apply_price_filter,
)
from src.db.feed import _bucket_of  # noqa: E402
from src.watch_clients import is_watched, watch_names  # noqa: E402

WATCHED = ["Кедр", "Сосна"]

TODAY = date(2026, 9, 16)
NOW = datetime(2026, 9, 16, 10, 0)


@pytest.fixture
def watched_on(monkeypatch):
    """Включить особый контроль: Кедр и Сосна, как в настройках."""
    monkeypatch.setattr(
        settings_store, "load_settings", lambda: {"watch_clients": WATCHED}
    )


def _watched_off(monkeypatch):
    monkeypatch.setattr(
        settings_store, "load_settings", lambda: {"watch_clients": []}
    )


# ── Сверка названий ──────────────────────────────────────────────────────────


def test_организатор_в_любой_форме_записи_узнаётся(watched_on):
    """Площадка пишет «АО "ЗРК "Кедр"», настройка — «Кедр»: точного
    равенства нет, а сверка по целым словам узнаёт. Ровно поэтому не
    годилось освобождение через client_labels (сравнение на равенство)."""
    assert is_watched('АО "ЗРК "Кедр"') is True
    assert is_watched("ООО «Сосна»") is True


def test_сверка_по_целым_словам_а_не_подстроке(watched_on):
    """«Кедрова» — не наш Кедр: подстрока здесь врёт, и пока
    сравнивали подстрокой, на лист особого контроля приезжал санаторий
    имени Кедрова (живая база, 07.09.2026)."""
    assert is_watched("Санаторий имени Кедрова") is False


def test_чужие_и_пустые_названия_не_смотрятся(watched_on):
    assert is_watched("Восход") is False
    assert is_watched("") is False
    assert is_watched(None) is False


def test_пустой_список_особого_контроля_выключает_правило(monkeypatch):
    """Правило выключено — никто не проходит, даже организатор с теми же
    словами. Имена берутся явно, без чтения настроек."""
    _watched_off(monkeypatch)
    assert watch_names() == []
    assert is_watched("АО Кедр") is False
    assert is_watched("АО Кедр", []) is False


# ── Фильтр цены ──────────────────────────────────────────────────────────────


def test_лот_особого_контроля_проходит_фильтр_цены(watched_on):
    """Лот Кедрова за 1000 ₽ при пороге 5 млн — находка, а не шум: порог
    цены на особый контроль не смотрит. Чужой такой же лот — отбрасывается."""
    settings = {"min_price": 5_000_000, "max_price": 0, "include_no_price": True}
    ours = {"name": "Реагенты для цеха", "company": 'АО "ЗРК "Кедр"', "sum": 1000}
    theirs = {"name": "Реагенты для цеха", "company": "ООО Ромашка", "sum": 1000}

    kept = apply_price_filter([ours, theirs], settings)

    assert kept == [ours], "чужой лот ниже порога обязан отсеяться"


# ── Мягкий keyword-фильтр ────────────────────────────────────────────────────


def test_лот_особого_контроля_проходит_keyword_фильтр(watched_on):
    """Лот найден по фразе, но названием не совпал и организатора нет в
    client_labels (равенство полных названий не сработает) — всё равно
    остаётся: заказчик важнее формулировки. Чужой такой же — отсекается."""
    ours = {
        "name": "Организация питания вахтового поселка",
        "company": 'АО "ЗРК "Кедр"',
        "search_key": "Герметик",
        "sum": None,
        "platform": "Росэлторг",
    }
    theirs = dict(ours, company="ООО Ромашка")

    kept = apply_keyword_filter([ours, theirs], ["Герметик"])

    assert kept == [ours], "нефильтрованная лента чужих обязана отсеяться"


# ── Корзины ленты ────────────────────────────────────────────────────────────


def _row(**over):
    """Строка лота в том виде, в каком её видит _bucket_of."""
    row = {
        "company": "ООО Ромашка",
        "date_deadline": "20.09.2026",
        "off_industry": 0,
        "off_profile": 0,
        "core_profile": 0,
        "client_level": "market",
        "below_threshold": 0,
        "needs_review": 0,
        "tags_list": ["reagents"],
    }
    row.update(over)
    return row


def test_лот_особого_контроля_не_прячется_в_чужой_профиль(watched_on):
    """Профиль отсёк лот Соснаа (off_profile=1), но корзина «Не наш
    профиль» его не получит: по этому заказчику работа идёт адресно."""
    row = _row(company="ООО «Сосна»", off_profile=1)
    assert _bucket_of(row, TODAY, NOW) == "client"
    assert _bucket_of(_row(off_profile=1), TODAY, NOW) == "foreign"


def test_горящий_лот_особого_контроля_попадает_в_горит(watched_on):
    """Особый контроль считается интересным: дедлайн ≤ 3 дней — верх
    разбора, а не «client»."""
    row = _row(company="ООО «Сосна»", off_profile=1, date_deadline="18.09.2026")
    assert _bucket_of(row, TODAY, NOW) == "urgent"


def test_истёкший_лот_особого_контроля_остаётся_истёкшим(watched_on):
    """Единственное исключение из правила (решение владельца 16.09.2026):
    просроченный лот не нужен даже по заказчику особого контроля —
    подавать заявку некуда."""
    row = _row(company="ООО «Сосна»", off_profile=1, date_deadline="01.09.2026")
    assert _bucket_of(row, TODAY, NOW) == "expired"


def test_лот_особого_контроля_минует_корзину_ниже_порога(watched_on):
    """«Ниже порога» — тоже отбор по цене, а правило 16.09.2026 снимает
    ценовые пороги с этих заказчиков целиком: мелкая закупка Кедрова
    ждёт в «Закреплённых клиентах», а не в корзине мелочи."""
    row = _row(company='АО "ЗРК "Кедр"', below_threshold=1)
    assert _bucket_of(row, TODAY, NOW) == "client"
    assert _bucket_of(_row(below_threshold=1), TODAY, NOW) == "below"
