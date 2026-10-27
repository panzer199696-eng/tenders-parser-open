"""Открытая редакция: только площадки без входа и никаких внешних каналов.

Стережём, чтобы при доработках не вернулись площадки с авторизацией,
автообновление и отправка диагностики наружу, а поставочные справочники
оставались примером без реальных заказчиков.
"""

import io
import json
import zipfile

import pytest

import main_ui
from src.core import context as ctx
from src.platforms import KEYS, PLATFORMS
from src.settings_store import DEFAULTS


@pytest.fixture
def client():
    main_ui.app.config.update(TESTING=True)
    return main_ui.app.test_client()


def test_площадки_только_открытые():
    assert set(KEYS) == {
        "parse_tektorg",
        "parse_roseltorg",
        "parse_tender_pro",
        "parse_bidzaar",
        "parse_gpb",
        "parse_zakupki",
    }
    assert all("b2b" not in p.probe_url and "metal-it" not in p.probe_url for p in PLATFORMS)


def test_настройки_по_умолчанию_без_личного():
    assert DEFAULTS["watch_clients"] == []
    assert DEFAULTS["manager_name"] == ""
    assert DEFAULTS["tektorg_skip_phrases"] == []
    for removed in ("parse_b2bcenter", "parse_metalit", "fetch_positions", "fetch_lot_details"):
        assert removed not in DEFAULTS


@pytest.mark.parametrize(
    "method, url",
    [
        ("get", "/api/credentials"),
        ("post", "/api/login"),
        ("get", "/api/update/check"),
        ("post", "/api/update/apply"),
        ("post", "/api/diagnostics/send"),
    ],
)
def test_удалённые_маршруты_не_отвечают(client, method, url):
    assert getattr(client, method)(url).status_code in (404, 405)


def test_статус_без_сессии_и_входа(client):
    data = client.get("/api/status").get_json()
    assert "session" not in data and "login" not in data


def test_диагностика_локальная_и_без_секретов(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ctx, "BASE_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"days_back": 1, "smtp_password": "секрет", "api_token": "x"}),
        encoding="utf-8",
    )
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        settings = json.loads(zf.read("settings.json").decode("utf-8"))
    assert settings["days_back"] == 1
    assert settings["smtp_password"] == "***"
    assert settings["api_token"] == "***"


def test_поставочные_заказчики_без_ИНН():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    clients = json.loads((root / "defaults" / "clients.json").read_text(encoding="utf-8"))
    assert all(not c.get("inns") for c in clients["clients"])
    for name in ("client_inns.json", "defaults/client_inns.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        assert data["targets"] == []
