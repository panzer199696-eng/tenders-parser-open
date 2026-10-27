# -*- coding: utf-8 -*-
"""
Тесты src.settings_store:
  - defaults при отсутствии файла;
  - битый JSON → defaults;
  - save → load круговой;
  - миграция из env (monkeypatch os.environ + tmp_path);
  - неизвестные ключи игнорируются.

Запуск:
    pytest tests/test_settings_store.py -v
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import settings_store  # noqa: E402
from src.settings_store import DEFAULTS  # noqa: E402


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Перенаправить SETTINGS_FILE во временную директорию."""
    target = tmp_path / "settings.json"
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", target)
    return target


# ── defaults при отсутствии файла ────────────────────────────────────────────


def test_defaults_when_no_file(tmp_settings, monkeypatch):
    # Если .env пуст — миграция пишется из DEFAULTS.
    for var in ("DAYS_BACK", "MIN_PRICE", "MAX_PRICE", "BROWSER_HEADLESS",
                "PARSE_GPB", "PARSE_TENDER_PRO", "PARSE_ROSELTORG"):
        monkeypatch.delenv(var, raising=False)

    assert not tmp_settings.exists()
    result = settings_store.load_settings()
    assert result == DEFAULTS
    # миграция должна была создать settings.json
    assert tmp_settings.exists()


# ── битый JSON → defaults (и файл НЕ перезаписывается) ───────────────────────


def test_broken_json_returns_defaults(tmp_settings):
    tmp_settings.write_text("{ это не json", encoding="utf-8")
    result = settings_store.load_settings()
    assert result == DEFAULTS
    # файл остался как был (не затёрт DEFAULTS)
    assert "{" in tmp_settings.read_text(encoding="utf-8")


def test_non_object_json_returns_defaults(tmp_settings):
    tmp_settings.write_text("[1, 2, 3]", encoding="utf-8")
    assert settings_store.load_settings() == DEFAULTS


# ── save → load круговой ─────────────────────────────────────────────────────


def test_save_load_roundtrip(tmp_settings):
    data = {
        "days_back": 7,
        "min_price": 1000,
        "max_price": 5000,
        "include_no_price": False,
        "parse_roseltorg": False,
        "parse_tender_pro": False,
        "parse_gpb": True,
        "browser_headless": False,
        "keyword_search_enabled": True,
        "keyword_filter_enabled": True,
    }
    settings_store.save_settings(data)
    loaded = settings_store.load_settings()
    for k, v in data.items():
        assert loaded[k] == v, f"круговой тест провален по ключу {k!r}"


def test_save_atomic_creates_file(tmp_settings):
    settings_store.save_settings(dict(DEFAULTS))
    assert tmp_settings.exists()
    raw = tmp_settings.read_text(encoding="utf-8")
    # JSON-объект на верхнем уровне + ensure_ascii=False + indent=2
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)


# ── миграция из env ──────────────────────────────────────────────────────────


def test_migrate_from_env(tmp_settings, monkeypatch):
    monkeypatch.setenv("DAYS_BACK", "5")
    monkeypatch.setenv("MIN_PRICE", "1000000")
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setenv("PARSE_GPB", "true")
    monkeypatch.setenv("PARSE_TENDER_PRO", "false")
    monkeypatch.setenv("PARSE_ROSELTORG", "false")

    result = settings_store.load_settings()
    assert result["days_back"] == 5
    assert result["min_price"] == 1000000
    assert result["browser_headless"] is False
    assert result["parse_gpb"] is True
    assert result["parse_tender_pro"] is False
    assert result["parse_roseltorg"] is False
    # defaults сохраняются для незатронутых ключей
    assert result["include_no_price"] == DEFAULTS["include_no_price"]
    # файл сохранён при первом запуске
    assert tmp_settings.exists()


def test_migrate_env_defaults_when_empty(tmp_settings, monkeypatch):
    for var in ("DAYS_BACK", "MIN_PRICE", "MAX_PRICE", "BROWSER_HEADLESS",
                "PARSE_GPB", "PARSE_TENDER_PRO", "PARSE_ROSELTORG"):
        monkeypatch.delenv(var, raising=False)

    result = settings_store.load_settings()
    assert result == DEFAULTS


def test_migrate_invalid_env_value_falls_back(tmp_settings, monkeypatch):
    # DAYS_BACK=не-число → сохраняем DEFAULTS
    monkeypatch.setenv("DAYS_BACK", "abc")
    monkeypatch.setenv("MIN_PRICE", "не-число")
    result = settings_store.load_settings()
    assert result["days_back"] == DEFAULTS["days_back"]
    assert result["min_price"] == DEFAULTS["min_price"]


# ── неизвестные ключи игнорируются ───────────────────────────────────────────


def test_unknown_keys_ignored(tmp_settings):
    tmp_settings.write_text(
        json.dumps({"days_back": 9, "unknown_key": "trash", "another": 123}),
        encoding="utf-8",
    )
    result = settings_store.load_settings()
    assert result["days_back"] == 9
    assert "unknown_key" not in result
    assert "another" not in result


def test_partial_settings_filled_from_defaults(tmp_settings):
    # Только один ключ задан — остальные из DEFAULTS
    tmp_settings.write_text(json.dumps({"days_back": 3}), encoding="utf-8")
    result = settings_store.load_settings()
    assert result["days_back"] == 3
    assert result["max_price"] == DEFAULTS["max_price"]
    assert result["include_no_price"] == DEFAULTS["include_no_price"]


# ── типобезопасность ─────────────────────────────────────────────────────────


def test_wrong_type_replaced_by_default(tmp_settings):
    # Строка вместо int → берём DEFAULTS
    tmp_settings.write_text(json.dumps({"days_back": "семь"}), encoding="utf-8")
    result = settings_store.load_settings()
    assert result["days_back"] == DEFAULTS["days_back"]


def test_bool_not_treated_as_int(tmp_settings):
    # bool не должен засчитываться как int (True это 1 в Python)
    tmp_settings.write_text(json.dumps({"days_back": True}), encoding="utf-8")
    result = settings_store.load_settings()
    assert result["days_back"] == DEFAULTS["days_back"]


def test_save_filters_wrong_types(tmp_settings):
    # save_settings также валидирует и не пишет бред
    settings_store.save_settings({"days_back": "не число", "min_price": "хм"})
    loaded = settings_store.load_settings()
    assert loaded["days_back"] == DEFAULTS["days_back"]
    assert loaded["min_price"] == DEFAULTS["min_price"]


# ── runner без pytest ───────────────────────────────────────────────────────


def _run_all():
    fns = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    passed = failed = 0
    for fn in fns:
        try:
            import inspect
            # пропускаем тесты с фикстурами — только pytest
            sig = inspect.signature(fn)
            if any(p.name != "tmp_settings" and p.name != "monkeypatch"
                   for p in sig.parameters.values()):
                continue
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
