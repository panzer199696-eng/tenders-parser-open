"""Движок «наше / не наше» (ТЗ-4): сила совпадения, отрасль, anti-группы.

Проверяется профиль целиком — от справочников до итоговой оценки. Все
справочники подкладываются фикстурой в tmp_path: тесты не ходят в сеть и
не трогают папку пользователя (как tests/test_store_catalog.py).

Ключевые примеры, ради которых движок переписывался:
«Запасные части легкового транспорта» ловилось подстрокой «запасн част»,
масло для котельной ТЭЦ лежало в теме наравне с закупками заводов, а общее
слово («насос») нельзя было завести в справочник вообще — оно сразу давало
полный вес группы.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.profile import Profile

# Справочник собран специально под случаи ТЗ-4: есть группа в старом формате
# (список строк = strong), группы нового формата с weak/qualifiers/exclude,
# отраслевая секция и anti-группа формы закупки.
NOMENCLATURE = {
    "groups": [
        {
            "id": "valves",
            "title": "Запорная арматура",
            "weight": 30,
            "enabled": True,
            "tier": "daily",
            # старый формат: читается как strong (обратная совместимость)
            "match": ["задвижк", "задвижек", "клинов затвор"],
        },
        {
            "id": "oils",
            "title": "Масла индустриальные",
            "weight": 30,
            "enabled": True,
            "tier": "daily",
            # масла покупают и ТЭЦ, и водоканалы — группа отраслевая
            "industry_sensitive": True,
            "match": {
                "strong": ["масло индустриальн"],
                "weak": [],
                "qualifiers": [],
                "exclude": [],
            },
        },
        {
            "id": "own_seals",
            "title": "Уплотнения — своё производство",
            "weight": 40,
            "enabled": True,
            "tier": "daily",
            # флаг справочника, а не id в коде: бонус своего производства
            "own_production": True,
            # узкая позиция — отрасль не режет вес
            "match": ["сальников набивк", "нш 1-15", "нш 2-12"],
        },
        {
            "id": "equipment",
            "title": "Оборудование",
            "weight": 30,
            "enabled": True,
            "tier": "daily",
            "match": ["фильтр-пресс", "компрессор"],
        },
        {
            "id": "pumps",
            "title": "Насосы",
            "weight": 25,
            "enabled": True,
            "tier": "weekly",
            "industry_sensitive": True,
            "match": {
                "strong": ["насос шламов", "насос грунтов", "warman"],
                "weak": ["насос", "насосн"],
                "qualifiers": ["шламов", "пульп", "дренажн", "промышленн"],
                "exclude": ["пожаротушен", "теплов насос", "насос гидроусилител"],
            },
        },
        {
            "id": "electric",
            "title": "Электрика и кабель",
            "weight": 10,
            "enabled": True,
            "tier": "weekly",
            "match": {
                "strong": ["электродвигател"],
                "weak": ["кабел"],
                "qualifiers": ["силов", "бронирован"],
                "exclude": [],
            },
        },
        {
            "id": "drill_spares",
            "title": "Запчасти к буровому оборудованию",
            "weight": 15,
            "enabled": True,
            "tier": "weekly",
            "match": {
                "strong": ["пневмоударник", "буров коронк"],
                "weak": ["запасн"],
                "qualifiers": ["буров", "долот"],
                "exclude": ["легков"],
            },
        },
    ],
    "industry": {
        "ours": ["гок", "завод", "рудник", "комбинат", "обогатительн"],
        "foreign": ["ТЭЦ", "грэс", "водоканал", "теплосет", "жкх", "птицефабрик"],
    },
    "anti_groups": [
        {
            "id": "works",
            "title": "Работы и подряд",
            "match": ["выполнение работ", "строительств", "капитальн ремонт", "монтаж"],
            "unless": ["поставка", "изготовлен"],
            "reason": "это подряд, а не поставка",
        }
    ],
    "stop_words": [],
}

CLIENTS = {
    "priority_weights": {"full": 40, "partial": 25, "market": 5},
    "clients": [
        {
            "id": "severny",
            "name": "Северный рудник",
            "level": "full",
            "aliases": ["Северный рудник", "СР Сервис"],
        }
    ],
}

RULES = {"min_price": {"default": 0}}


def _write_catalog(tmp_path, nomenclature=None):
    """Разложить справочники в tmp_path и собрать по ним Profile."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "nomenclature.json").write_text(
        json.dumps(nomenclature or NOMENCLATURE, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "clients.json").write_text(
        json.dumps(CLIENTS, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "rules.json").write_text(
        json.dumps(RULES, ensure_ascii=False), encoding="utf-8"
    )
    return Profile(data_dir=tmp_path)


@pytest.fixture
def profile(tmp_path):
    return _write_catalog(tmp_path)


def _lot(name, company="", deadline=""):
    return {"name": name, "company": company, "date_deadline": deadline, "sum": None}


# ── Совпадение ───────────────────────────────────────────────────────────────


def test_задвижка_даёт_сильное_совпадение_и_попадает_в_matched_terms(profile):
    """«Задвижка стальная 20 шт» — арматура, strength strong.

    Совпадают обе формы справочника: «задвижк» и «задвижек», основы у них
    общие.
    """
    v = profile.evaluate(_lot("Задвижка стальная 20 шт"))
    assert v.groups == ["valves"]
    assert v.strength == "strong"
    assert v.matched_terms == [
        {"term": "задвижк", "group": "valves", "strength": "strong"},
        {"term": "задвижек", "group": "valves", "strength": "strong"},
    ]


def test_порядок_слов_не_важен(profile):
    """«Затворы клиновые, поворотные» ↔ «клинов затвор».

    Площадка переворачивает слова и склоняет их — сравнение по основе с
    перестановкой обязано находить.
    """
    v = profile.evaluate(_lot("Затворы клиновые, поворотные"))
    assert v.groups == ["valves"]
    assert v.strength == "strong"


def test_позиция_своего_производства_с_бонусом(profile):
    v = profile.evaluate(_lot("Набивка сальниковая НШ 1-15"))
    assert "own_seals" in v.groups
    assert v.score == 40 + 10  # вес группы + своё производство


def test_нш_1_16_не_совпадает_с_нш_1_15(profile):
    """Марка значащая: «1-15» и «1-16» — разные позиции."""
    v = profile.evaluate(_lot("Набивка НШ 1-16"))
    assert v.groups == []
    assert v.score == 0


def test_фильтр_пресс_да_фильтр_воздушный_нет(profile):
    v = profile.evaluate(_lot("Фильтр-пресс FP-800"))
    assert v.groups == ["equipment"]
    v = profile.evaluate(_lot("Фильтр воздушный КамАЗ"))
    assert v.groups == []


def test_насос_шламовый_сильный_насосное_оборудование_слабое(profile):
    v = profile.evaluate(_lot("Насос шламовый Warman 6/4"))
    assert v.strength == "strong"
    v = profile.evaluate(_lot("Насосное оборудование"))
    assert v.groups == ["pumps"]
    assert v.strength == "weak"


def test_насос_пожаротушения_вето_группы(profile):
    """Exclude гасит группу целиком, вето запоминается с причиной."""
    v = profile.evaluate(_lot("Насос для системы пожаротушения"))
    assert "pumps" not in v.groups
    assert ("pumps", "пожаротушен") in v.vetoed


def test_кабель_с_уточнением_становится_сильным(profile):
    """weak + qualifier = strong: «Кабель КГ 3х25 силовой»."""
    v = profile.evaluate(_lot("Кабель КГ 3х25 силовой"))
    assert v.groups == ["electric"]
    assert v.strength == "strong"


def test_запчасти_легкового_транспорта_вето(profile):
    """Живой случай из замера ТЗ-4: подстрока «запасн част» ловила его
    в тему запчастей. Теперь общее слово + вето по «легков»."""
    v = profile.evaluate(_lot("Запасные части легкового транспорта"))
    assert "drill_spares" not in v.groups
    assert ("drill_spares", "легков") in v.vetoed


def test_пустое_и_нестроковое_имя_не_роняет_движок(profile):
    assert profile.classify("") == []
    assert profile.classify(None) == []
    assert profile.classify(123) == []
    for tender in ({"name": None}, {"name": 123}, {}, {"name": ""}):
        v = profile.evaluate(tender)
        assert v.groups == []
        assert v.score == 0


def test_старый_формат_match_читается_как_strong(profile):
    """Группа valves — список строк, и он обязан работать как strong."""
    assert "match" in NOMENCLATURE["groups"][0]
    v = profile.evaluate(_lot("Задвижка стальная 20 шт"))
    assert v.strength == "strong"
    assert v.matched == ["задвижек", "задвижк"]


# ── Отраслевой контекст ──────────────────────────────────────────────────────


def test_масло_для_тэц_чужая_отрасль_и_нулевой_вес(profile):
    v = profile.evaluate(_lot("Масло индустриальное И-40 для городской ТЭЦ"))
    assert v.off_industry is True
    assert v.score == 0
    assert v.explain().startswith("не наш профиль:")
    assert "чужой отрасли" in v.explain()


def test_масло_для_завода_полный_вес_плюс_бонус_отрасли(profile):
    v = profile.evaluate(_lot("Масло индустриальное для завода Восток"))
    assert v.industry == "ours"
    assert v.off_industry is False
    assert v.needs_review is False
    assert v.score == 30 + 10  # полный вес группы + наша отрасль без клиента


def test_неизвестная_отрасль_режет_вес_и_просит_проверить(profile):
    v = profile.evaluate(_lot("Масло индустриальное"))
    assert v.needs_review is True
    assert v.score == round(30 * 0.6)


def test_клиент_из_справочника_сильнее_чужого_маркера_в_названии(profile):
    v = profile.evaluate(_lot("Масло индустриальное для ТЭЦ", company="Северный рудник"))
    assert v.industry == "client"
    assert v.off_industry is False
    assert v.score == 30 + 40  # полный вес + закреплённый клиент


def test_неотраслевая_группа_не_режется_контекстом(profile):
    """Узкую позицию покупает только профильный заказчик — вес полный всегда."""
    v = profile.evaluate(_lot("Набивка сальниковая НШ 1-15"))
    assert v.needs_review is False
    assert v.score == 40 + 10


# ── Оценка ───────────────────────────────────────────────────────────────────


def test_сильное_совпадение_выше_слабого_при_одном_клиенте(profile):
    strong = profile.evaluate(_lot("Задвижка стальная", company="Северный рудник"))
    weak = profile.evaluate(
        _lot("Насосное оборудование", company="Северный рудник")
    )
    weak_alone = profile.evaluate(_lot("Насосное оборудование"))
    assert strong.score > weak.score > weak_alone.score


def test_истёкший_лот_не_получает_бонус_срочности(profile):
    from datetime import date, datetime

    today, now = date(2026, 9, 1), datetime(2026, 9, 1, 12, 0)
    burning = profile.evaluate(
        _lot("Задвижка стальная", deadline="03.09.2026"), today=today, now=now
    )
    expired = profile.evaluate(
        _lot("Задвижка стальная", deadline="31.08.2026"), today=today, now=now
    )
    assert burning.urgent is True and burning.score == 30 + 15
    assert expired.expired is True and expired.urgent is False
    assert expired.score == 30


def test_движок_детерминирован_и_не_зависит_от_порядка_групп(tmp_path):
    p1 = _write_catalog(tmp_path)
    v1 = p1.evaluate(_lot("Задвижка стальная 20 шт"))
    v2 = p1.evaluate(_lot("Задвижка стальная 20 шт"))
    assert (v1.score, v1.groups, v1.reasons) == (v2.score, v2.groups, v2.reasons)

    shuffled = dict(NOMENCLATURE)
    shuffled["groups"] = list(reversed(NOMENCLATURE["groups"]))
    p2 = _write_catalog(tmp_path / "shuffled", shuffled)
    v3 = p2.evaluate(_lot("Задвижка стальная 20 шт"))
    assert (v3.score, v3.groups, v3.matched) == (v1.score, v1.groups, v1.matched)


# ── Anti-группы ──────────────────────────────────────────────────────────────


def test_подряд_помечается_как_не_наш_профиль_с_причиной(profile):
    v = profile.evaluate(_lot("Выполнение строительно-монтажных работ"))
    assert v.off_profile is True
    assert v.explain() == "не наш профиль: это подряд, а не поставка"


def test_поставка_и_монтаж_не_подряд_сработал_unless(profile):
    """«Поставка и монтаж компрессора» — наша поставка, слово из unless
    гасит anti-группу, оборудование при этом считается."""
    v = profile.evaluate(_lot("Поставка и монтаж компрессора"))
    assert v.off_profile is False
    assert v.groups == ["equipment"]


# ── Отраслевые маркеры ─────────────────────────────────────────────


def test_маркер_отрасли_ищется_с_начала_слова(profile):
    """Фонд капремонта Москвы считался «нашей отраслью» и получал +10:
    короткий маркер «гок» находился внутри «мноГОКвартирных»."""
    v = profile.evaluate(
        _lot(
            "Выполнение работ по капитальному ремонту многоквартирных домов",
            company="ФОНД КАПИТАЛЬНОГО РЕМОНТА МНОГОКВАРТИРНЫХ ДОМОВ",
        )
    )
    assert v.industry == "unknown"


def test_маркер_отрасли_по_прежнему_держит_падежи(profile):
    """Начало слова, а не целое слово: «Теплосети», «ГОКа», «обогатительной
    фабрики» узнаются, как и раньше."""
    assert profile.industry_of("АО Теплосети", "Масло индустриальное") == "foreign"
    assert profile.industry_of("", "Кабель для Северного ГОКа") == "ours"
    assert profile.industry_of("", "Насос для обогатительной фабрики") == "ours"
    assert profile.industry_of("", "Масло индустриальное для ТЭЦ-22") == "foreign"


def test_сильное_совпадение_главнее_слабого_даже_при_меньшем_весе(profile):
    """Позиция распознана точно, но сверху вставало общее слово другой
    группы большего веса — лот уходил «на проверку» со слабой темой.
    Уверенная находка должна быть главной."""
    v = profile.evaluate(_lot("Насос с электродвигателем"))
    assert v.groups[0] == "electric"
    assert v.strength == "strong"
    assert v.needs_review is False
    assert v.score == 10


# ── Срочность только по понятным лотам ──────────────────────────────────────



def _urgent(profile, name, company=""):
    from datetime import date, datetime

    return profile.evaluate(
        _lot(name, company=company, deadline="03.09.2026"),
        today=date(2026, 9, 1),
        now=datetime(2026, 9, 1, 12, 0),
    )


def test_срочность_не_поднимает_лот_неизвестного_заказчика(profile):
    """Масло от непонятной конторы с дедлайном через 2 дня обгоняло лоты
    закреплённых клиентов за счёт +15. Срок показываем, бонус и 🔥 — нет."""
    v = _urgent(profile, "Масло индустриальное")
    assert v.needs_review is True
    assert v.urgent is False
    assert v.score == round(30 * 0.6)
    assert any("дедлайн через 2 дн." in r for r in v.reasons)


def test_срочность_не_даётся_слабой_теме(profile):
    v = _urgent(profile, "Насосное оборудование")
    assert v.urgent is False


def test_срочность_не_даётся_подряду(profile):
    v = _urgent(profile, "Выполнение строительно-монтажных работ")
    assert v.urgent is False


def test_срочность_не_даётся_лоту_без_темы_и_без_заказчика(profile):
    v = _urgent(profile, "Диваны офисные")
    assert v.urgent is False
    assert v.score == 0


def test_срочность_остаётся_у_закреплённого_клиента_даже_без_темы(profile):
    v = _urgent(profile, "Диваны офисные", company="Северный рудник")
    assert v.urgent is True
    assert v.score == 40 + 15


def test_срочность_остаётся_у_уверенной_темы_нашей_отрасли(profile):
    v = _urgent(profile, "Масло индустриальное для завода Восток")
    assert v.urgent is True
    assert v.score == 30 + 10 + 15
