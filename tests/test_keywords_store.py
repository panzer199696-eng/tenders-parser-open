# -*- coding: utf-8 -*-
"""
Тесты src.keywords_store:
  - пусто/битый файл → [];
  - save → load круговой: strip пустых, дедуп case-insensitive;
  - enabled_phrases.

Запуск:
    pytest tests/test_keywords_store.py -v
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import keywords_store  # noqa: E402


@pytest.fixture
def tmp_keywords(tmp_path, monkeypatch):
    target = tmp_path / "keywords.json"
    monkeypatch.setattr(keywords_store, "KEYWORDS_FILE", target)
    return target


# ── нет/битый файл ───────────────────────────────────────────────────────────


def test_no_file_returns_empty(tmp_keywords):
    assert not tmp_keywords.exists()
    assert keywords_store.load_keywords() == []


def test_broken_json_returns_empty(tmp_keywords):
    tmp_keywords.write_text("{ это не json", encoding="utf-8")
    assert keywords_store.load_keywords() == []


def test_non_object_json_returns_empty(tmp_keywords):
    tmp_keywords.write_text("[1,2,3]", encoding="utf-8")
    assert keywords_store.load_keywords() == []


def test_no_keywords_field_returns_empty(tmp_keywords):
    tmp_keywords.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert keywords_store.load_keywords() == []


# ── save/load круговой ───────────────────────────────────────────────────────


def test_save_load_roundtrip(tmp_keywords):
    data = [
        {"phrase": "Строительство моста", "enabled": True},
        {"phrase": "СМР", "enabled": False},
    ]
    keywords_store.save_keywords(data)
    loaded = keywords_store.load_keywords()
    assert loaded == data


def test_save_strips_phrases(tmp_keywords):
    keywords_store.save_keywords([
        {"phrase": "  Поставка щебня  ", "enabled": True},
    ])
    loaded = keywords_store.load_keywords()
    assert loaded[0]["phrase"] == "Поставка щебня"


def test_save_ignores_empty_phrases(tmp_keywords):
    keywords_store.save_keywords([
        {"phrase": "  ", "enabled": True},
        {"phrase": "", "enabled": True},
        {"phrase": "Монтаж", "enabled": True},
    ])
    loaded = keywords_store.load_keywords()
    assert len(loaded) == 1
    assert loaded[0]["phrase"] == "Монтаж"


def test_save_dedup_case_insensitive(tmp_keywords):
    keywords_store.save_keywords([
        {"phrase": "Строительство", "enabled": True},
        {"phrase": "строительство", "enabled": False},   # дубликат
        {"phrase": "СТРОИТЕЛЬСТВО", "enabled": True},     # дубликат
    ])
    loaded = keywords_store.load_keywords()
    assert len(loaded) == 1
    # сохраняется ПЕРВАЯ (с оригинальным регистром)
    assert loaded[0]["phrase"] == "Строительство"


def test_save_atomic_creates_file(tmp_keywords):
    keywords_store.save_keywords([{"phrase": "тест", "enabled": True}])
    assert tmp_keywords.exists()
    raw = tmp_keywords.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert "keywords" in parsed
    assert isinstance(parsed["keywords"], list)


def test_save_skips_non_dict_entries(tmp_keywords):
    keywords_store.save_keywords([
        "not a dict",
        42,
        None,
        {"phrase": "ОК", "enabled": True},
    ])
    loaded = keywords_store.load_keywords()
    assert len(loaded) == 1
    assert loaded[0]["phrase"] == "ОК"


def test_save_default_enabled_false(tmp_keywords):
    keywords_store.save_keywords([{"phrase": "без флага"}])
    loaded = keywords_store.load_keywords()
    assert loaded[0]["enabled"] is False


# ── enabled_phrases ──────────────────────────────────────────────────────────


def test_enabled_phrases_filters_and_orders(tmp_keywords):
    keywords_store.save_keywords([
        {"phrase": "Один", "enabled": True},
        {"phrase": "Два", "enabled": False},
        {"phrase": "Три", "enabled": True},
        {"phrase": "Четыре", "enabled": False},
    ])
    assert keywords_store.enabled_phrases() == ["Один", "Три"]


def test_enabled_phrases_empty_when_all_disabled(tmp_keywords):
    keywords_store.save_keywords([
        {"phrase": "Один", "enabled": False},
        {"phrase": "Два", "enabled": False},
    ])
    assert keywords_store.enabled_phrases() == []


def test_enabled_phrases_empty_when_no_file(tmp_keywords):
    assert keywords_store.enabled_phrases() == []


# ── поставляемый keywords.json (профиль: промхимия и цеха, с 26.08.2026) ─────


def test_shipped_keywords_file_valid():
    """Реальный keywords.json в корне проекта: валиден, без дублей, без пустых."""
    shipped = ROOT / "keywords.json"
    assert shipped.exists(), "keywords.json отсутствует в корне проекта"

    data = json.loads(shipped.read_text(encoding="utf-8"))
    items = data["keywords"]
    assert isinstance(items, list) and items

    phrases = [it["phrase"] for it in items]
    assert all(isinstance(p, str) and p.strip() == p and p for p in phrases)
    lowered = [p.casefold() for p in phrases]
    assert len(lowered) == len(set(lowered)), "дубли фраз (case-insensitive)"
    assert all(isinstance(it["enabled"], bool) for it in items)


def test_shipped_keywords_profile_core_present(monkeypatch):
    """Фразы ежедневной группы примера (tier = daily) в поставке включены."""
    monkeypatch.setattr(keywords_store, "KEYWORDS_FILE", ROOT / "keywords.json")
    enabled = set(keywords_store.enabled_phrases())
    core = {"Насос центробежный", "Насосный агрегат", "Запорная арматура", "Задвижка"}
    missing = core - enabled
    assert not missing, f"нет включённых фраз профиля: {missing}"


def test_shipped_keywords_survive_store_roundtrip(monkeypatch):
    """Поставляемый список без потерь проходит через load: ничего не отсеивается."""
    monkeypatch.setattr(keywords_store, "KEYWORDS_FILE", ROOT / "keywords.json")
    raw = json.loads((ROOT / "keywords.json").read_text(encoding="utf-8"))["keywords"]
    loaded = keywords_store.load_keywords()
    assert len(loaded) == len(raw)
