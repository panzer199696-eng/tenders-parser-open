# -*- coding: utf-8 -*-
"""
Тесты дат парсеров (аудит 2026-07-13):

D2 — ГПБ: раньше первая попавшаяся дата карточки писалась в date_deadline,
хотя часто это была дата публикации. Теперь даты извлекаются по контексту
(«актуально до…» → дедлайн, «опубликован…» → публикация), а без маркеров
дедлайн — самая поздняя из дат.

D1 — Tender.pro: days_back принимался конструктором, но нигде не применялся.
Теперь parse() отбрасывает тендеры старше периода по date_published;
тендеры без распознанной даты сохраняются.

Запуск:
    pytest tests/test_parser_dates.py -v
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers.gpb_parser import GPBParser  # noqa: E402
from src.parsers.tender_pro_parser import TenderProParser  # noqa: E402


# ── D2: ГПБ — контекстное извлечение дат ─────────────────────────────────────


def _card(html: str):
    return BeautifulSoup(f"<div>{html}</div>", "lxml").div


def test_gpb_deadline_by_marker():
    """«Актуально до» → дедлайн, «Опубликовано» → публикация."""
    card = _card(
        "<span>Опубликовано: 01.07.2026</span><span>Актуально до: 20.07.2026</span>"
    )
    published, deadline = GPBParser._extract_dates(card)
    assert published == "01.07.2026"
    assert deadline == "20.07.2026"


def test_gpb_published_date_not_taken_as_deadline():
    """Первая дата — публикация с маркером: в дедлайн НЕ попадает."""
    card = _card(
        "<span>Дата публикации 05.07.2026</span>"
        "<span>Подача заявок до 25.07.2026</span>"
    )
    published, deadline = GPBParser._extract_dates(card)
    assert deadline == "25.07.2026"
    assert published == "05.07.2026"


def test_gpb_no_markers_latest_is_deadline():
    """Без маркеров: дедлайн — самая поздняя дата, публикация — самая ранняя."""
    card = _card("<span>03.07.2026</span><span>18.07.2026</span>")
    published, deadline = GPBParser._extract_dates(card)
    assert deadline == "18.07.2026"
    assert published == "03.07.2026"


def test_gpb_single_date_no_markers():
    """Единственная дата без маркеров → дедлайн, публикация не выдумывается."""
    card = _card("<span>10.07.2026</span>")
    published, deadline = GPBParser._extract_dates(card)
    assert deadline == "10.07.2026"
    assert published == ""


def test_gpb_no_dates():
    card = _card("<span>Запрос предложений</span>")
    published, deadline = GPBParser._extract_dates(card)
    assert published == "" and deadline == ""


def test_gpb_until_word_boundary():
    """«до 15.08.2026» — маркер; «договор» ложно не срабатывает."""
    card = _card(
        "<span>договор поставки от 01.07.2026</span><span>до 15.08.2026</span>"
    )
    _, deadline = GPBParser._extract_dates(card)
    assert deadline == "15.08.2026"


# ── D1: Tender.pro — применение days_back ────────────────────────────────────


def _tp(days_back: int) -> TenderProParser:
    return TenderProParser(browser_manager=None, companies=[], days_back=days_back)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def test_tp_fresh_within_period():
    p = _tp(3)
    assert p._within_days_back(_fmt(datetime.now())) is True
    assert p._within_days_back(_fmt(datetime.now() - timedelta(days=2))) is True


def test_tp_old_filtered_out():
    p = _tp(3)
    assert p._within_days_back(_fmt(datetime.now() - timedelta(days=10))) is False


def test_tp_boundary_day_kept():
    """Дата ровно на границе периода (cutoff в полночь) — сохраняется."""
    p = _tp(5)
    assert p._within_days_back(_fmt(datetime.now() - timedelta(days=5))) is True


def test_tp_no_date_kept():
    """«Не указана»/пусто/мусор — тендер не отбрасывается."""
    p = _tp(1)
    assert p._within_days_back("Не указана") is True
    assert p._within_days_back("") is True
    assert p._within_days_back("99.99.9999") is True


def test_tp_with_time_suffix():
    """Формат «дд.мм.гггг чч:мм» после _fmt_date тоже распознаётся."""
    p = _tp(3)
    fresh = _fmt(datetime.now()) + " 10:30"
    old = _fmt(datetime.now() - timedelta(days=30)) + " 10:30"
    assert p._within_days_back(fresh) is True
    assert p._within_days_back(old) is False
