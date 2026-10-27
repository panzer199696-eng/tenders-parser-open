"""
HTTP-тесты воронки реестра (ТЗ-9): шаги от «взял» до «подался» и итога.

Зачем отдельным файлом. Правила воронки покрыты юнит-тестами реестра
(`test_registry`), но стык «маршрут Flask ↔ реестр ↔ JSON, который читает
app.js» не проверял никто. Поломка тут выглядит для человека как «кнопка
не работает» или, хуже, молчаливый успех без смены статуса — по зелёным
юнит-тестам это не видно.

Все файлы изолированы в tmp_path: рабочая база не трогается.
"""

import sys
from datetime import date, timedelta
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

    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    main_ui.app.config.update(TESTING=True)
    return main_ui.app.test_client()


def _add(client, lot_id="1", deadline=None):
    """Положить лот прямо в базу, которую читают маршруты.

    Дедлайн по умолчанию далёкий: красная зона не должна мелькать в тестах,
    которые про неё не говорят.
    """
    row = {
        "lot_key": f"ROS|{lot_id}",
        "platform": "Росэлторг",
        "lot_id": lot_id,
        "name": "Компаунд заливочный 20 т",
        "company": 'АО ПК "ЮЖНАЯ"',
        "href": f"https://example.test/tender-{lot_id}/",
        "date_deadline": deadline or "01.12.2026",
        "score": 70,
        "score_explain": "Материалы цеха +30 · закреплённый клиент +40",
        "tags": '["reagents_zif"]',
        "client_level": "full",
        "core_profile": 1,
    }
    conn = connect(ctx.UNIFIED_DB_PATH)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()
    conn.close()
    return row["lot_key"]


def _step(client, key, action, **body):
    """POST шага воронки. Тело всегда JSON — как шлёт app.js."""
    return client.post(f"/api/lot/{key}/{action}", json=body)


def _walk_to(client, key, status):
    """Провести лот по воронке до нужного статуса (включая его)."""
    from src.registry.registry import (
        ASSESSED,
        DECIDED,
        PRICE_APPROVED,
        PRICED,
        SELECTED,
        SUBMITTED,
    )

    seq = {
        SELECTED: [],
        ASSESSED: [("assess", dict(who="Иван"))],
        DECIDED: [
            ("assess", dict(who="Иван")),
            ("decide", dict(by="Руководитель", participate=True, comment="наш профиль")),
        ],
        PRICED: [
            ("assess", dict(who="Иван")),
            ("decide", dict(by="Руководитель", participate=True, comment="наш профиль")),
            ("price", dict(who="Иван", supplier="Завод", cost=80)),
        ],
        PRICE_APPROVED: [
            ("assess", dict(who="Иван")),
            ("decide", dict(by="Руководитель", participate=True, comment="наш профиль")),
            ("price", dict(who="Иван", supplier="Завод", cost=80, bid_price=100)),
            ("approve-price", dict(by="Руководитель", bid_price=100)),
        ],
        SUBMITTED: [
            ("assess", dict(who="Иван")),
            ("decide", dict(by="Руководитель", participate=True, comment="наш профиль")),
            ("price", dict(who="Иван", supplier="Завод", cost=80, bid_price=100)),
            ("approve-price", dict(by="Руководитель", bid_price=100)),
            ("submit", dict(who="Иван", submission_no="№1")),
        ],
    }
    r = _step(client, key, "take", manager="Иван")
    assert r.status_code == 200, r.get_data(as_text=True)
    for action, body in seq[status]:
        r = _step(client, key, action, **body)
        assert r.status_code == 200, f"{action}: {r.get_data(as_text=True)}"


# ── 1. Полный путь ───────────────────────────────────────────────────────────


def test_полный_путь_лота_от_взятия_до_итога(client):
    """Каждый шаг отвечает 200 и меняет статус — «податься» можно из UI."""
    key = _add(client)
    steps = [
        ("assess", dict(who="Иван", volume_t=20, packaging="биг-бэги", hazard=True)),
        ("decide", dict(by="Руководитель", participate=True, comment="наш профиль")),
        ("price", dict(who="Иван", supplier="Завод", purchase_price=70, logistics=10, cost=80, markup=25)),
        ("approve-price", dict(by="Руководитель", bid_price=100)),
        ("submit", dict(who="Иван", submission_no="№17")),
        ("result", dict(who="Иван", result="won", winner_price=100)),
    ]
    expected = ["ASSESSED", "DECIDED", "PRICED", "PRICE_APPROVED", "SUBMITTED", "RESULT"]
    r = _step(client, key, "take", manager="Иван")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["status"] == "SELECTED"
    for (action, body), status in zip(steps, expected):
        r = _step(client, key, action, **body)
        assert r.status_code == 200, f"{action}: {r.get_data(as_text=True)}"
        assert r.get_json()["status"] == status
        # Ответ — обновлённая заявка тем же видом, что /api/registry:
        # без этого интерфейс не перерисует строку без второго запроса.
        assert r.get_json()["lot_key"] == key


def test_после_итога_доступен_followup(client):
    """Воронка не обрывается на итоге: за итогом идёт follow-up."""
    key = _add(client)
    _walk_to(client, key, "SUBMITTED")
    _step(client, key, "result", who="Иван", result="lost", winner_price=90, loss_reason="дорого")
    r = _step(client, key, "followup", who="Иван", contact="Иван, ГРК", next_touch="2026-10-01")
    assert r.status_code == 200, r.get_data(as_text=True)
    row = r.get_json()
    assert row["status"] == "FOLLOWUP"
    assert row["contact"] == "Иван, ГРК"


# ── 2–3. Регламент: перескок и имя ───────────────────────────────────────────


def test_перескок_через_шаг_отклоняется(client):
    """take → submit — нельзя: кнопки шагов не должны уметь того же."""
    key = _add(client)
    _walk_to(client, key, "SELECTED")
    r = _step(client, key, "submit", who="Иван", submission_no="№1")
    assert r.status_code == 400
    # Текст словами: человек должен понять, чего не хватает, без консоли.
    assert "нельзя" in r.get_json()["error"]
    # Статус не изменился — молчаливый успех здесь хуже отказа.
    items = client.get("/api/registry").get_json()["items"]
    assert items[0]["status"] == "SELECTED"


def test_шаг_без_имени_человека_отклоняется(client):
    """Решение без автора неотличимо от «никто ничего не решал»."""
    key = _add(client)
    _walk_to(client, key, "SELECTED")
    r = _step(client, key, "assess", who="")
    assert r.status_code == 400
    assert "укажите, кто" in r.get_json()["error"]


def test_кнопки_следующего_шага_не_предлагают_перескока(client):
    """next_steps строится из ALLOWED_TRANSITIONS: у каждого статуса одна
    кнопка «вперёд», у терминальных её нет."""
    from src.registry import ALLOWED_TRANSITIONS, next_step_map

    m = next_step_map()
    for status, allowed in ALLOWED_TRANSITIONS.items():
        if status is None:
            continue
        forward = [s for s in allowed if s != "REJECTED"]
        if len(forward) == 1:
            assert m[status]["route"], f"у {status} должна быть кнопка шага"
        else:
            assert status not in m, f"у {status} не должно быть кнопки «вперёд»"
    # Сервер отдаёт карту реестру — интерфейс её не придумывает сам.
    key = _add(client)
    _walk_to(client, key, "SELECTED")
    assert client.get("/api/registry").get_json()["next_steps"]["SELECTED"]["route"] == "assess"


# ── 4. Отказ через решение ───────────────────────────────────────────────────


def test_решение_не_участвуем_уводит_в_отказ_с_причиной(client):
    key = _add(client)
    _walk_to(client, key, "ASSESSED")
    r = _step(
        client, key, "decide",
        by="Руководитель", participate=False, comment="дорогая логистика",
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    row = r.get_json()
    assert row["status"] == "REJECTED"
    assert "дорогая логистика" in row["reject_reason"]


def test_решение_без_слова_руководителя_не_принимается(client):
    """Отсутствующее решение не должно превращаться в «не участвуем»."""
    key = _add(client)
    _walk_to(client, key, "ASSESSED")
    r = _step(client, key, "decide", by="Руководитель")
    assert r.status_code == 400
    assert "участвуем" in r.get_json()["error"]


# ── 5–6. Подача и итог ───────────────────────────────────────────────────────


def test_submit_сохраняет_номер_заявки(client):
    key = _add(client)
    _walk_to(client, key, "PRICE_APPROVED")
    r = _step(client, key, "submit", who="Иван", submission_no="№777")
    assert r.status_code == 200, r.get_data(as_text=True)
    items = client.get("/api/registry").get_json()["items"]
    assert items[0]["submission_no"] == "№777"
    assert items[0]["submitted_at"], "отметка о подаче фиксирует и момент"


def test_итог_с_ценой_победителя_считает_отклонение(client):
    key = _add(client)
    _walk_to(client, key, "SUBMITTED")  # bid_price = 100
    r = _step(
        client, key, "result",
        who="Иван", result="lost", winner_price=80, loss_reason="дорого",
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["deviation"] == pytest.approx(25.0)
    assert r.get_json()["loss_reason"] == "дорого"


# ── 7. «Мимо» из середины воронки ────────────────────────────────────────────


def test_мимо_доступно_из_середины_воронки(client):
    """Отказ разрешён с любого шага: иначе его причина теряется."""
    key = _add(client)
    _walk_to(client, key, "PRICED")
    r = _step(client, key, "skip", manager="Иван", reason="передумали")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["status"] == "REJECTED"
    assert client.get("/api/registry").get_json()["stats"]["rejected"] == 1


# ── 8. Красная зона: взят, а заявка не отправлена ───────────────────────────


def test_взятый_и_не_поданный_с_подачей_через_два_дня_горит(client):
    """Лот выбран, но заявка не ушла — это то, что реально теряет деньги."""
    deadline = (date.today() + timedelta(days=2)).strftime("%d.%m.%Y")
    key = _add(client, deadline=deadline)
    _walk_to(client, key, "SELECTED")
    zone = client.get("/api/registry").get_json()["red_zone"]
    rows = zone["заявка не отправлена"]
    assert [r["lot_key"] for r in rows] == [key]
    assert "заявка не отправлена" in rows[0]["why"], "формулировка — словами"


def test_поданный_лот_из_случая_не_отправлена_исчезает(client):
    """После отметки о подаче тревога снимается — дальше ждём только итог."""
    deadline = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")
    key = _add(client, deadline=deadline)
    _walk_to(client, key, "SUBMITTED")
    zone = client.get("/api/registry").get_json()["red_zone"]
    assert zone["заявка не отправлена"] == []
