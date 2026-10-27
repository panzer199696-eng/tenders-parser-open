# -*- coding: utf-8 -*-
"""Валидатор справочника номенклатуры (ТЗ-4) и схема живых файлов.

Валидатор стоит на входе сохранения из интерфейса, поэтому его задача —
поймать опечатку человека ДО того, как она молча выкинет находки из
поиска: дубль id перезапишет группу, общее слово в strong вернёт вал из
тысяч нерелевантных лотов (замер ТЗ-4: подстрока «запасн част» ловила
запчасти легкового транспорта).

Тесты читают файлы репозитория (defaults/ и, если есть, рабочий data/) —
сети не трогают и ничего не пишут.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from src.store.derive import validate_nomenclature  # noqa: E402

DATA_FILE = ROOT / "data" / "nomenclature.json"
DEFAULTS_FILE = ROOT / "defaults" / "nomenclature.json"


def test_валидатор_ловит_дубль_id_короткий_strong_и_weak_без_уточнений():
    data = {
        "groups": [
            # «нас» — 3 символа без пробела и цифр: классический возврат
            # общего слова в сильные, который и должен отсекаться
            {"id": "a", "title": "Группа А", "weight": 10, "match": {"strong": ["нас"]}},
            {"id": "a", "title": "Группа А2", "weight": 5, "match": {"strong": ["насос шламов"]}},
            # weak без qualifier бессмыслен: общее слово нельзя усилить
            {
                "id": "c",
                "title": "Группа С",
                "weight": 5,
                "match": {"strong": ["кабел силов"], "weak": ["кабел"]},
            },
        ]
    }
    problems = validate_nomenclature(data)
    assert any("повторяющийся id" in p for p in problems)
    assert any("слишком короткий strong-термин" in p for p in problems)
    assert any("weak-термины" in p and "qualifier" in p for p in problems)


def test_термин_в_strong_двух_групп_неоднозначен():
    """Движок молча выбрал бы группу с большим весом — человек должен
    узнать о неоднозначности при сохранении, а не по пропавшим лотам."""
    data = {
        "groups": [
            {"id": "a", "title": "А", "weight": 10, "match": {"strong": ["насос шламов"]}},
            # регистр не спасает: «Насос шламов» — тот же термин
            {"id": "b", "title": "Б", "weight": 10, "match": {"strong": ["Насос шламов"]}},
        ]
    }
    problems = validate_nomenclature(data)
    assert any("в strong двух групп" in p for p in problems)
    assert any("«b»" in p and "«a»" in p for p in problems)


def test_группа_без_названия_или_веса_отлавливается():
    data = {
        "groups": [
            {"id": "a", "weight": 10, "match": ["насос"]},
            {"id": "b", "title": "Б", "match": ["кабел"]},
        ]
    }
    problems = validate_nomenclature(data)
    assert any("без названия" in p and "«a»" in p for p in problems)
    assert any("без веса" in p and "«b»" in p for p in problems)


def test_старый_формат_match_проходит_валидатор():
    """Старые списки — выверенные данные: «мшц» и «мшр» — точные марки
    мельниц длиной 3 символа, перепроверять их кодом нельзя, иначе
    валидатор забраковал бы живой справочник."""
    data = {
        "groups": [
            {"id": "mills", "title": "Мельницы", "weight": 30, "match": ["мшц", "мшр", "мельниц"]}
        ]
    }
    assert validate_nomenclature(data) == []


def test_рабочий_справочник_проходит_валидатор():
    """Свой справочник пользователя (data/ не в git) — если он заведён."""
    if not DATA_FILE.exists():
        pytest.skip("рабочего data/nomenclature.json нет — проверять нечего")
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    assert validate_nomenclature(data) == []


def test_дефолтный_справочник_проходит_валидатор():
    data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    assert validate_nomenclature(data) == []


def test_аббревиатура_без_гласных_допустима_в_сильных():
    """«МШЦ», «НКУ», «ПДМ» — точные марки и обозначения, а не общие слова.

    Короткое слово сравнивается только на точное равенство, поэтому ложных
    срабатываний оно не даёт: «мшц» совпадёт со словом «МШЦ» и ни с чем ещё.
    Запрещать их — значит выкинуть из справочника марки мельниц и щитового
    оборудования, которыми лоты и называются (найдено 01.09.2026).
    """
    from src.store.derive import validate_nomenclature

    data = {
        "groups": [
            {
                "id": "mills",
                "title": "Мельницы",
                "weight": 30,
                "match": {"strong": ["МШЦ", "МШР", "НКУ"], "weak": [], "qualifiers": []},
            }
        ]
    }
    assert validate_nomenclature(data) == []


def test_короткое_обычное_слово_в_сильных_по_прежнему_запрещено():
    from src.store.derive import validate_nomenclature

    data = {
        "groups": [
            {
                "id": "pumps",
                "title": "Насосы",
                "weight": 25,
                "match": {"strong": ["бак", "лом"], "weak": [], "qualifiers": []},
            }
        ]
    }
    problems = validate_nomenclature(data)
    assert len(problems) == 2, problems
