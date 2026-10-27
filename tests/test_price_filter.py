# -*- coding: utf-8 -*-
"""
Тесты фильтрации ParserRunner без браузера.

Матрица по цене: (price None/низкая/в диапазоне/высокая)
                × (include_no_price, min_price, max_price).
Также keyword-фильтр (apply_keyword_filter).

Запуск:
    pytest tests/test_price_filter.py -v
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.parser_runner import (  # noqa: E402
    apply_price_filter,
    apply_keyword_filter,
)


def _t(price=None, name="Лот"):
    return {"name": name, "sum": price}


def _settings(**kw):
    base = {
        "min_price": 0,
        "max_price": 0,
        "include_no_price": True,
    }
    base.update(kw)
    return base


# ── price = None ──────────────────────────────────────────────────────────────


def test_none_price_included_when_flag_on():
    res = apply_price_filter([_t(None)], _settings(include_no_price=True))
    assert len(res) == 1


def test_none_price_dropped_when_flag_off():
    res = apply_price_filter([_t(None)], _settings(include_no_price=False))
    assert res == []


# ── min_price ────────────────────────────────────────────────────────────────


def test_low_price_dropped_by_min():
    res = apply_price_filter(
        [_t(100)], _settings(min_price=500)
    )
    assert res == []


def test_price_equal_min_kept():
    res = apply_price_filter(
        [_t(500)], _settings(min_price=500)
    )
    assert len(res) == 1


def test_price_above_min_kept():
    res = apply_price_filter(
        [_t(1000)], _settings(min_price=500)
    )
    assert len(res) == 1


def test_min_zero_keeps_all_prices():
    # min_price = 0 → нет порога
    res = apply_price_filter(
        [_t(0), _t(1), _t(1_000_000)], _settings(min_price=0)
    )
    assert len(res) == 3


# ── max_price ────────────────────────────────────────────────────────────────


def test_high_price_dropped_by_max():
    res = apply_price_filter(
        [_t(10_000)], _settings(max_price=5000)
    )
    assert res == []


def test_price_equal_max_kept():
    res = apply_price_filter(
        [_t(5000)], _settings(max_price=5000)
    )
    assert len(res) == 1


def test_price_below_max_kept():
    res = apply_price_filter(
        [_t(1000)], _settings(max_price=5000)
    )
    assert len(res) == 1


# ── диапазон min..max ────────────────────────────────────────────────────────


def test_range_filter():
    tenders = [_t(50), _t(200), _t(800), _t(1500), _t(2000)]
    res = apply_price_filter(
        tenders, _settings(min_price=200, max_price=1500)
    )
    prices = [t["sum"] for t in res]
    assert prices == [200, 800, 1500]


def test_range_with_none_price_excluded():
    # В диапазоне, но None при include_no_price=False → отброшен
    tenders = [_t(None), _t(500)]
    res = apply_price_filter(
        tenders, _settings(min_price=100, max_price=1000, include_no_price=False)
    )
    assert len(res) == 1
    assert res[0]["sum"] == 500


# ── матрица (price × flags) ─────────────────────────────────────────────────


def test_matrix_all_cases():
    """Матрица: price None/низкая/в диапазоне/высокая × (include_no_price, min, max)."""
    cases = [
        # (price, settings, expected_kept)
        (None,  dict(min_price=0, max_price=0, include_no_price=True),  True),
        (None,  dict(min_price=0, max_price=0, include_no_price=False), False),
        (50,    dict(min_price=100, max_price=0, include_no_price=True), False),
        (150,   dict(min_price=100, max_price=0, include_no_price=True), True),
        (5000,  dict(min_price=0, max_price=1000, include_no_price=True), False),
        (500,   dict(min_price=100, max_price=1000, include_no_price=True), True),
    ]
    for price, st, expected in cases:
        res = apply_price_filter([_t(price)], _settings(**st))
        assert (len(res) == 1) == expected, (
            f"price={price}, settings={st}: "
            f"ожидалось kept={expected}, got {len(res)} tenders"
        )


# ── исходные данные не мутируются ────────────────────────────────────────────


def test_does_not_mutate_input():
    tenders = [_t(10), _t(20), _t(30)]
    original = [dict(t) for t in tenders]
    apply_price_filter(tenders, _settings(min_price=25))
    assert tenders == original


# ── keyword-фильтр (мягкий режим: проверяются только лоты,
#    найденные по фразе, т.е. search_key = фраза) ─────────────────────────────


def test_keyword_filter_keeps_matching():
    # лоты найдены поиском по фразе «строительство»
    tenders = [
        {"name": "Строительство моста через реку", "search_key": "строительство"},
        {"name": "Поставка дизельного топлива", "search_key": "строительство"},
        {"name": "Монтаж металлоконструкций", "search_key": "строительство"},
    ]
    res = apply_keyword_filter(tenders, ["строительство"])
    assert len(res) == 1
    assert "моста" in res[0]["name"]


def test_keyword_filter_case_insensitive():
    tenders = [
        {"name": "СТРОИТЕЛЬСТВО МОСТА", "search_key": "строительство"},
        {"name": "Запчасти", "search_key": "строительство"},
    ]
    res = apply_keyword_filter(tenders, ["строительство"])
    assert len(res) == 1


def test_keyword_filter_multiple_phrases_any_match():
    tenders = [
        {"name": "Топливо дизельное", "search_key": "топливо"},
        {"name": "Монтаж конструкций", "search_key": "топливо"},
        {"name": "Канцелярия", "search_key": "топливо"},
    ]
    res = apply_keyword_filter(tenders, ["топливо", "монтаж"])
    assert len(res) == 2


def test_keyword_filter_empty_phrases_returns_input():
    # если фраз нет — фильтр не применяется (warning)
    tenders = [{"name": "Что угодно"}]
    res = apply_keyword_filter(tenders, [])
    assert res == tenders


def test_keyword_filter_no_name_kept_out():
    # у тендера нет name — ни одна фраза не сматчится
    tenders = [
        {"name": "", "search_key": "стройка"},
        {"name": None, "search_key": "стройка"},
        {"name": "Стройка", "search_key": "стройка"},
    ]
    res = apply_keyword_filter(tenders, ["стройка"])
    assert len(res) == 1


def test_keyword_filter_does_not_mutate_input():
    tenders = [{"name": "Строительство"}, {"name": "Запчасти"}]
    original = [dict(t) for t in tenders]
    apply_keyword_filter(tenders, ["строительство"])
    assert tenders == original


# ── runner без pytest ───────────────────────────────────────────────────────


def _run_all():
    fns = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    passed = failed = 0
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
