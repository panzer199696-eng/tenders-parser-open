# -*- coding: utf-8 -*-
"""
Тесты новой логики:
  1. Фильтр активности по ДЕДЛАЙНУ (не по публикации).
  2. Отбор ключевых слов для листа «Строительство и подряд».

Запуск (без pytest):
    cd <проект>
    set PYTHONUTF8=1
    python tests/test_filters.py

С pytest (если установлен):
    pytest tests/test_filters.py -v
"""

import sys
from datetime import date
from pathlib import Path

# Путь к корню проекта, чтобы импортировать src.*
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.classifier import (  # noqa: E402
    is_active,
    is_construction,
    classify,
    CONSTRUCTION_KEYWORDS,
)


TODAY = date(2026, 6, 8)


def _t(**kw):
    """Синтетический тендер с дефолтами."""
    base = {
        "platform": "Росэлторг",
        "name": "Тестовый лот",
        "company": "Ромашка",
        "href": "https://example.invalid/lot/1",
        "sum": None,
        "date_published": "01.01.2026",
        "date_deadline": "Не указана",
    }
    base.update(kw)
    return base


# ── 1. is_active: фильтр по дедлайну ────────────────────────────────────────


def test_active_future_deadline():
    # дедлайн в будущем → активен
    t = _t(date_deadline="20.07.2026")
    assert is_active(t, today=TODAY) is True


def test_active_deadline_today():
    # дедлайн == сегодня → ещё активен (>= today)
    t = _t(date_deadline="08.06.2026")
    assert is_active(t, today=TODAY) is True


def test_inactive_past_deadline():
    # дедлайн в прошлом → завершён, режем
    t = _t(date_deadline="01.05.2026")
    assert is_active(t, today=TODAY) is False


def test_active_deadline_with_time():
    # формат с временем
    t = _t(date_deadline="09.10.2027 18:00")
    assert is_active(t, today=TODAY) is True


def test_active_unknown_deadline_kept():
    # «Не указана» → оставляем (не теряем из-за неизвестной даты)
    t = _t(date_deadline="Не указана")
    assert is_active(t, today=TODAY) is True


def test_active_unparseable_deadline_kept():
    # мусор в дате → оставляем
    t = _t(date_deadline="скоро")
    assert is_active(t, today=TODAY) is True


def test_active_empty_deadline_kept():
    t = _t(date_deadline="")
    assert is_active(t, today=TODAY) is True


def test_active_publication_ignored():
    # старая публикация, но дедлайн в будущем → активен
    # (доказывает, что фильтр НЕ по дате публикации)
    t = _t(date_published="01.01.2025", date_deadline="01.08.2026")
    assert is_active(t, today=TODAY) is True


# ── 2. is_construction: отбор по ключевым словам ────────────────────────────


def test_construction_positive_cases():
    names = [
        "Строительство моста через р. Монни",
        "Выполнение СМР по объекту",
        "Монтаж металлоконструкций",
        "Реконструкция подъездной дороги",
        "Капитальный ремонт здания АБК",
        "Выбор подрядчика на устройство фундаментов",
        "Генподряд на возведение склада",
        "Земляные работы и благоустройство площадки",
        "Демонтаж сооружения",
        "Прокладка трубопровода",
    ]
    for n in names:
        assert is_construction(n) is True, f"должно быть строительным: {n!r}"


def test_construction_normalization_yo():
    # ё→е и регистр не должны мешать
    assert is_construction("ЩЁБЕНЬ для дорожного полотна") is True  # 'дорожн'
    assert is_construction("МОНТАЖ оборудования") is True


def test_construction_negative_cases():
    names = [
        "Поставка дизельного топлива",
        "Закупка запасных частей к экскаватору",
        "Поставка щебня",
        "Канцелярские товары",
    ]
    for n in names:
        assert is_construction(n) is False, f"НЕ должно быть строительным: {n!r}"


def test_construction_keywords_editable():
    # список существует и непустой (редактируемое место)
    assert isinstance(CONSTRUCTION_KEYWORDS, (list, tuple))
    assert len(CONSTRUCTION_KEYWORDS) >= 15


def test_classify_sets_construction_flag():
    c = classify(_t(name="Строительство моста"))
    assert c["construction"] is True
    c2 = classify(_t(name="Поставка дизельного топлива"))
    assert c2["construction"] is False


# ── 3. Excel: два главных листа ─────────────────────────────────────────────


# ── 4. Регрессия скоринга дедлайна (баг: bonus никогда не начислялся) ────────


def test_score_deadline_bonus_within_optimum():
    """Дедлайн в окне 7-21 день → +15 к скору (регрессия на сломанный срез fmt).

    score_tender использует datetime.now(), поэтому дедлайн берём относительно
    реальной сегодняшней даты, а не TODAY из теста.
    """
    from datetime import date, timedelta

    today = date.today()
    dl = (today + timedelta(days=14)).strftime("%d.%m.%Y")
    c = classify(_t(name="Строительство моста", date_deadline=dl))
    # СМР(+15) + construction(+20) + deadline(+15) = 50
    assert c["score"] >= 50, f"бонус за дедлайн не начислен: score={c['score']}"


def test_score_deadline_bonus_absent_for_past():
    """Дедлайн в прошлом → бонуса за близость нет.

    Внимание: score_tender использует datetime.now(), а не TODAY из теста —
    поэтому дедлайны берём относительно реальной сегодняшней даты.
    """
    from datetime import date, timedelta

    today = date.today()
    dl_past = (today - timedelta(days=30)).strftime("%d.%m.%Y")
    dl_future = (today + timedelta(days=14)).strftime("%d.%m.%Y")
    c_past = classify(_t(name="Строительство моста", date_deadline=dl_past))
    c_future = classify(_t(name="Строительство моста", date_deadline=dl_future))
    assert c_past["score"] < c_future["score"], (
        f"старый дедлайн не должен давать бонус: "
        f"past={c_past['score']}, future={c_future['score']}"
    )


# ── runner без pytest ───────────────────────────────────────────────────────


def _run_all():
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    passed = 0
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nИтого: {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
