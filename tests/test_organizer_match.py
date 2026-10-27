"""
Сверка организатора лота с искомым заказчиком — по ЦЕЛЫМ СЛОВАМ (16.10.2026).

Подстрока в нескольких копиях парсеров нарушала правило проекта
(src/watch_clients.py): она приписывала заказчику «Ромашка» лоты
«…ИМЕНИ РОМАШКИНА» и не узнавала «Север — Сталь» в «ООО "СЕВЕР - СТАЛЬ"»
(длинное тире против дефиса).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers._common import organizer_match  # noqa: E402
from src.parsers.tender_pro_parser import TenderProParser  # noqa: E402


def test_часть_слова_не_совпадение():
    assert not organizer_match(
        "Ромашка", 'ГКУЗ "ДЕТСКИЙ САНАТОРИЙ ИМЕНИ РОМАШКИНА"'
    )
    assert organizer_match("Ромашка", 'АО "ПК "Ромашка"')


def test_тире_и_дефис_одно_и_то_же():
    assert organizer_match("Север — Сталь", 'ООО "СЕВЕР - СТАЛЬ"')


def test_форма_собственности_не_слово_имени():
    assert organizer_match("Ромашка", 'АО "Ромашка"')
    assert organizer_match('ООО "ТД ПРИМЕР-ЭНЕРГО"', "Пример-Энерго")
    assert not organizer_match("АО", 'АО "Ромашка"')


def test_пусто_не_совпадает():
    assert not organizer_match("", "АО Ромашка")
    assert not organizer_match("Ромашка", "")
    assert not organizer_match(None, None)


def test_парсер_сверяет_так_же():
    company = 'ГКУЗ "САНАТОРИЙ ИМЕНИ РОМАШКИНА"'
    assert TenderProParser._organizer_match("Ромашка", company) is False
    assert TenderProParser._organizer_match("Ромашка", 'АО "Ромашка"') is True
