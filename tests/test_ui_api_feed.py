"""
HTTP-тесты главного экрана: лента, карточка лота, решения, реестр, отчёты.

Зачем отдельным файлом. Бизнес-логика этих разделов покрыта юнит-тестами
(`test_db_feed`, `test_registry`, `test_reports`), но стык «маршрут Flask ↔
единая база ↔ JSON, который читает app.js» до 01.09.2026 не проверял никто.
Молчаливая поломка тут выглядит как пустой экран у пользователя — ровно то,
что нельзя заметить по зелёным тестам.

Все файлы изолированы в tmp_path: рабочая база и журнал не трогаются.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main_ui
from src.core import context as ctx
from src import run_journal
from src.db import connect, ensure_schema


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "tenderwork.db"
    monkeypatch.setattr(ctx, "UNIFIED_DB_PATH", db_path)
    monkeypatch.setattr(run_journal, "journal_dir", lambda: tmp_path / "journal")

    # Справочники — тоже во временную папку. Тест сохранения номенклатуры
    # проверяет отказ валидатора; если валидатор однажды пропустит мусор,
    # без этой изоляции он ляжет в РАБОЧИЙ справочник пользователя, и профиль
    # перестанет узнавать свою номенклатуру. Ровно так 01.09.2026 тест отмены
    # парсинга записал восемь фиктивных прогонов в живую базу.
    from src.store import catalog as _catalog

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for name, attr in (
        ("nomenclature.json", "NOMENCLATURE_FILE"),
        ("clients.json", "CLIENTS_FILE"),
        ("rules.json", "RULES_FILE"),
    ):
        (data_dir / name).write_text(
            (Path("defaults") / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setattr(_catalog, attr, data_dir / name)
    monkeypatch.setattr(_catalog, "DATA_DIR", data_dir)
    for attr, fname in (
        ("KEYWORDS_FILE", "keywords.json"),
        ("COMPANIES_FILE", "companies.json"),
        ("CLIENT_INNS_FILE", "client_inns.json"),
    ):
        monkeypatch.setattr(_catalog, attr, tmp_path / fname)

    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    main_ui.app.config.update(TESTING=True)
    return main_ui.app.test_client()


def _add(client, lot_id="1", **over):
    """Положить лот прямо в базу, которую читает маршрут."""
    row = {
        "lot_key": f"ROS|{lot_id}",
        "platform": "Росэлторг",
        "lot_id": lot_id,
        "name": "Насос центробежный ЦНС 60-198",
        "company": 'АО "РОМАШКА"',
        "href": f"https://example.test/tender-{lot_id}/",
        "date_deadline": "20.09.2026",
        "score": 70,
        "score_explain": "Насосы и насосное оборудование +30 · закреплённый клиент +40",
        "tags": '["pumps"]',
        "client_level": "full",
        "core_profile": 1,
    }
    row.update(over)
    conn = connect(ctx.UNIFIED_DB_PATH)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()
    conn.close()
    return row["lot_key"]


# ── Лента ────────────────────────────────────────────────────────────────────


def test_лента_отдаёт_корзины_и_свежесть(client):
    _add(client)
    d = client.get("/api/feed").get_json()
    assert d["total"] == 1
    keys = [t["key"] for t in d["titles"]]
    assert "review" in keys and "foreign" in keys
    assert "rest" not in keys, "старая корзина «Прочее» должна была исчезнуть"
    assert "fresh" in d, "плашка «что принесла ночь» читает этот блок"


def test_лента_на_пустой_базе_не_падает(client):
    d = client.get("/api/feed").get_json()
    assert d["total"] == 0
    assert d["last_run"] is None


def test_сомнительные_корзины_приходят_свёрнутыми(client):
    d = client.get("/api/feed").get_json()
    collapsed = {t["key"] for t in d["titles"] if t.get("collapsed")}
    assert {"review", "foreign", "expired"} <= collapsed


# ── Карточка лота ────────────────────────────────────────────────────────────


def test_карточка_лота_отвечает_почему_он_в_списке(client):
    key = _add(client)
    d = client.get(f"/api/lot/{key}").get_json()
    assert d["name"] == "Насос центробежный ЦНС 60-198"
    assert "Насосы и насосное оборудование" in d["score_explain"]


def test_карточка_несуществующего_лота_даёт_404(client):
    assert client.get("/api/lot/ROS%7Cнет-такого").status_code == 404


# ── Решения и реестр ─────────────────────────────────────────────────────────


def test_беру_переводит_лот_в_реестр(client):
    key = _add(client)
    r = client.post(
        f"/api/lot/{key}/take",
        json={"manager": "Иван"},
        content_type="application/json",
    )
    assert r.status_code == 200, r.get_data(as_text=True)

    # Лот ушёл из ленты — иначе его пришлось бы разбирать снова и снова.
    assert client.get("/api/feed").get_json()["total"] == 0
    reg = client.get("/api/registry").get_json()
    assert reg["stats"]["in_work"] == 1


def test_мимо_без_причины_не_принимается(client):
    key = _add(client)
    r = client.post(f"/api/lot/{key}/skip", json={"manager": "Иван", "reason": ""})
    assert r.status_code == 400, "без причины мы не узнаем, где промахиваемся"


def test_мимо_с_причиной_уводит_лот_в_отказ(client):
    key = _add(client)
    r = client.post(
        f"/api/lot/{key}/skip",
        json={"manager": "Иван", "reason": "не наша номенклатура"},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert client.get("/api/registry").get_json()["stats"]["rejected"] == 1


# ── Пересчёт и справочники ───────────────────────────────────────────────────


def test_пересчёт_оценок_проходит_по_базе(client):
    _add(client, score=0, score_explain="", tags="[]", core_profile=0)
    r = client.post("/api/rescore")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["updated"] >= 1


def test_справочник_отдаёт_группы_и_заказчиков(client):
    d = client.get("/api/catalog").get_json()
    assert d["nomenclature"]["groups"], "номенклатура не должна приходить пустой"
    assert d["clients"]["clients"]


def test_справочник_не_принимает_битую_номенклатуру(client):
    """Валидатор стоит до записи: справочник — источник истины профиля."""
    r = client.post(
        "/api/catalog/nomenclature",
        json={
            "groups": [
                {
                    "id": "a",
                    "title": "A",
                    "weight": 10,
                    "match": {"strong": ["бак"], "weak": [], "qualifiers": []},
                },
                {"id": "a", "title": "Дубль", "weight": 10, "match": {"strong": []}},
            ]
        },
    )
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_отчёты_строятся_из_единой_базы(client):
    _add(client)
    d = client.get("/api/reports").get_json()
    assert "platforms" in d or "by_platform" in d or d, "отчёт должен что-то вернуть"


# ── Excel ────────────────────────────────────────────────────────────────────


def test_выгрузка_собирается_из_базы(client):
    _add(client)
    r = client.get("/api/excel/latest")
    assert r.status_code == 200
    assert r.data[:2] == b"PK"
