# -*- coding: utf-8 -*-
"""
Порога в 5 млн ₽ больше нет нигде по умолчанию.

Единый порог отсекал ровно профильные лоты: небольшие партии и запчасти
стоят сотни тысяч. Порог теперь профильный, по группе номенклатуры, и живёт
в справочнике (data/rules.json); лот ниже порога помечается, а не
выбрасывается.

Сеть не используется.
"""


def test_дефолт_настроек_не_отсекает_по_цене():
    from src.settings_store import DEFAULTS

    assert DEFAULTS["min_price"] == 0
    assert DEFAULTS["max_price"] == 0
    assert DEFAULTS["include_no_price"] is True


def test_legacy_константа_больше_не_пятимиллионная():
    from src import config

    assert config.MIN_PRICE == 0


def test_фильтр_цены_при_нулевом_пороге_ничего_не_режет():
    from src.core.parser_runner import apply_price_filter

    лоты = [
        {"name": "Компаунд заливочный, 3 т", "sum": 900_000},
        {"name": "Запчасти к дозатору НШ 1-15", "sum": 150_000},
        {"name": "Колонны сорбционные", "sum": 12_000_000},
        {"name": "Известь", "sum": None},
    ]
    kept = apply_price_filter(лоты, {"min_price": 0, "max_price": 0, "include_no_price": True})
    assert len(kept) == 4


def test_профильные_пороги_описаны_в_справочнике():
    """Значения порогов — данные, а не код: их правят без релиза."""
    import json
    from pathlib import Path

    rules = Path(__file__).resolve().parent.parent / "defaults" / "rules.json"
    data = json.loads(rules.read_text(encoding="utf-8"))["min_price"]
    assert data["default"] < 5_000_000
    assert all(v < 5_000_000 for v in data["by_group"].values())
