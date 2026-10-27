"""Типичные лоты против справочника-примера из defaults/.

Справочник в поставке — пример (насосы, подшипники и РТИ, кабель, запорная
арматура, металлопрокат). Тест читает defaults/ — тот же справочник, что
засевает чистую установку, — и не трогает папку пользователя. Если вы
заменили справочник своим, поправьте и кейсы здесь.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.profile import Profile
from src.store.derive import validate_nomenclature


@pytest.fixture(scope="module")
def profile():
    return Profile(data_dir=ROOT / "defaults")


def _top(profile, name, company=""):
    return profile.evaluate(
        {"name": name, "company": company, "date_deadline": "", "sum": None}
    )


def test_справочник_примера_проходит_валидатор(profile):
    assert validate_nomenclature(profile.nomenclature) == []


@pytest.mark.parametrize(
    "name, group",
    [
        ("Поставка насоса центробежного ЦНС 60-198", "pumps"),
        ("Насосный агрегат НД 2,5", "pumps"),
        ("Насос погружной дренажный", "pumps"),
        ("Подшипник роликовый 3626", "bearings_rti"),
        ("Резинотехнические изделия для нужд цеха", "bearings_rti"),
        ("Кабель силовой ВВГнг 3х2,5", "cable_electric"),
        ("Кабель КГ 3х25", "cable_electric"),
        ("Задвижка клиновая 30с41нж DN100", "valves"),
        ("Кран шаровой фланцевый", "valves"),
        ("Лист стальной горячекатаный 10 мм", "metal_rolled"),
        ("Труба стальная электросварная 108х4", "metal_rolled"),
    ],
)
def test_позиция_получает_тему(profile, name, group):
    v = _top(profile, name)
    assert v.groups, f"тема не распознана: {name}"
    assert v.groups[0] == group, f"{name}: {v.groups}"
    assert v.strength == "strong", f"{name}: {v.explain()}"


def test_общее_слово_без_уточнения_слабое(profile):
    v = _top(profile, "Насосы")
    assert v.groups[0] == "pumps"
    assert v.strength == "weak"
    assert v.needs_review is True


@pytest.mark.parametrize(
    "name, group",
    [
        ("Насос для системы пожаротушения", "pumps"),
        ("Кран мостовой 10 т", "valves"),
        ("Труба дымовая котельной", "metal_rolled"),
    ],
)
def test_вето_группы_снимает_тему(profile, name, group):
    assert group not in _top(profile, name).groups


@pytest.mark.parametrize(
    "company, name, industry",
    [
        ("ГБУЗ «Городская больница №1»", "Насос центробежный", "foreign"),
        ('ООО "Металлургический комбинат Восток"', "Задвижка", "ours"),
        ("ООО Ромашка", "Насос", "client"),
        ("", "Канцелярия", "unknown"),
    ],
)
def test_отрасль_заказчика(profile, company, name, industry):
    assert profile.industry_of(company, name) == industry


def test_чужая_отрасль_обнуляет_вес_темы(profile):
    v = _top(profile, "Насос центробежный", "ГБУЗ «Городская больница №1»")
    assert v.off_industry is True
    assert v.score == 0, v.explain()


def test_закреплённый_клиент_получает_вес(profile):
    v = _top(profile, "Насос центробежный", 'ООО "Ромашка"')
    assert v.client_id == "romashka"
    assert any("закреплённый клиент" in r for r in v.reasons)


def test_подряд_помечается_не_нашим(profile):
    v = _top(profile, "Выполнение работ по капитальному ремонту насосной станции")
    assert v.off_profile is True


def test_продажа_имущества_не_наш_профиль(profile):
    v = _top(profile, "Продажа имущества, расположенного по адресу: г. Пример")
    assert v.off_profile is True
