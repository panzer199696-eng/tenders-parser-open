"""
Разбор цены: одна функция `_common.parse_price` вместо шести копий (16.10.2026).

Копии расходились (характеризующая таблица — коммит 4c034f7):

    строка                          парсер А / ЕИС    Росэлторг   ГПБ
    «1 234 567,89 руб.»             123456789.0 ×100  1234567.89  123456789.0
    «1 234 567,89 руб. (с НДС 20%)» 123456789.2       1234567.89  12345678920.0
    «1.234.567,89»                  1234567.89        1.234       123456789.0
    «2467180.00»                    2467180.0         2467180.0   246718000.0

ТЭК-Торг пока со своей копией: его файлы ведёт отдельный поток, переведёт
ведущий после слияния.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers._common import parse_price

from src.parsers.roseltorg_parser import RoseltorgParser
from src.parsers.zakupki_parser import ZakupkiParser

EXPECTED = [
    (None, None),
    ("", None),
    ("Без указания цены", None),
    ("НМЦ не указано", None),
    (True, None),
    (1500, 1500.0),
    (173981.72, 173981.72),
    ("2467180.00", 2467180.0),
    ("1 234 567,89", 1234567.89),
    ("1 234 567,89 руб.", 1234567.89),
    ("1 234 567,89 (с НДС 20%)", 1234567.89),
    ("1 234 567,89 руб. (с НДС 20%)", 1234567.89),
    ("1.234.567,89", 1234567.89),
    ("1,234,567.89", 1234567.89),
    ("1.234.567", 1234567.0),
    ("173981.72 ₽", 173981.72),
    ("НМЦ 500 000 ₽", 500000.0),
    ("1\xa0234\xa0567,89\xa0₽", 1234567.89),
    ("1 234,5", 1234.5),
    ("1 234 567,89", 1234567.89),
    ("1 234 567 руб.", 1234567.0),
    ("500.", 500.0),
]


@pytest.mark.parametrize("text,value", EXPECTED, ids=[repr(c[0]) for c in EXPECTED])
def test_parse_price(text, value):
    got = parse_price(text)
    if value is None:
        assert got is None
    else:
        assert got == pytest.approx(value)


def test_росэлторг_точки_разрядов():
    assert RoseltorgParser._parse_sum("НМЦ 1.234.567,89 ₽") == pytest.approx(1234567.89)
    assert RoseltorgParser._parse_sum("НМЦ не указано") is None


def test_еис_через_общую_функцию():
    assert ZakupkiParser._parse_price("1 234 567,89 руб.") == pytest.approx(1234567.89)
