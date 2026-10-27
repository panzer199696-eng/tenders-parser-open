# -*- coding: utf-8 -*-
"""
Тесты на обработку некорректного JSON в POST-эндпоинтах (Задача 3).

Раньше ``request.get_json(silent=True) or {}`` тихо подменял битый JSON
на пустой ``{}`` и эндпоинт возвращал ``{"ok": True}``, словно данные
сохранились. Теперь при ``None`` от ``get_json`` (синтаксическая ошибка
или отсутствие тела) возвращается ``400`` с понятным сообщением. Валидный
пустой объект ``{}`` остаётся легитимным телом.

Запуск:
    pytest tests/test_ui_invalid_json.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main_ui  # noqa: E402
from src.core import context as ctx  # noqa: E402
import src.settings_store as settings_store  # noqa: E402
import src.keywords_store as keywords_store  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(ctx, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(ctx, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(keywords_store, "KEYWORDS_FILE", tmp_path / "keywords.json")
    main_ui.app.config.update(TESTING=True)
    return main_ui.app.test_client()


# Все 4 эндпоинта, которые проверяют тело как JSON.
ENDPOINTS = [
    "/api/settings",
    "/api/companies",
    "/api/keywords",
]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_invalid_json_returns_400(endpoint, client):
    """POST с синтаксически битым JSON → 400 + error, а не 200 ok:true.

    Используем ``data=`` + ``content_type='application/json'``: Flask пытается
    распарсить тело как JSON и падает → ``get_json(silent=True)`` возвращает
    None. Это и есть тестируемая ветка.
    """
    r = client.post(
        endpoint,
        data="not-json{{{",
        content_type="application/json",
    )
    assert r.status_code == 400, (
        f"{endpoint}: ожидался 400 для битого JSON, получил {r.status_code}"
    )
    body = r.get_json()
    assert body is not None and "error" in body, (
        f"{endpoint}: в ответе нет поля error (body={body!r})"
    )


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_empty_body_returns_400(endpoint, client):
    """Пустое тело запроса → тоже 400 (get_json(silent=True) вернёт None)."""
    r = client.post(endpoint, content_type="application/json")
    assert r.status_code == 400
    assert "error" in r.get_json()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_valid_empty_object_is_ok(endpoint, client):
    """Валидный пустой JSON-объект ``{}`` НЕ должен считаться некорректным JSON.

    Разница с битым JSON: ``get_json(silent=True)`` вернёт ``{}`` (не None)
    для валидного пустого объекта. Для некоторых эндпоинтов ``{}`` — реально
    no-op (settings, keywords → 200), для companies —
    законный 400 от валидатора обязательных полей, но с сообщением,
    отличным от «Некорректный JSON».
    """
    r = client.post(endpoint, json={})
    body = r.get_json()
    if endpoint == "/api/companies":
        # Законный 400 от валидатора полей — НЕ от JSON-парсера.
        assert r.status_code == 400
        assert "error" in body
        msg = body["error"].lower()
        # Сообщение НЕ должно быть про «некорректный JSON».
        assert "json" not in msg, (
            f"{endpoint}: для валидного {{}} не должно быть ошибки про JSON"
        )
    else:
        assert r.status_code == 200, (
            f"{endpoint}: валидный {{}} должен возвращать 200, "
            f"получили {r.status_code}"
        )
        assert body.get("ok") is True


# ── runner без pytest ────────────────────────────────────────────────────────


def _run_all():
    fns = [
        (k, v)
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    passed = failed = skipped = 0
    for name, fn in fns:
        try:
            sig_params = list(fn.__code__.co_varnames[: fn.__code__.co_argcount])
        except Exception:
            sig_params = []
        if sig_params:
            print(f"  SKIP  {name} (нужна pytest-фикстура)")
            skipped += 1
            continue
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nИтого: {passed} passed, {failed} failed, {skipped} skipped")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
