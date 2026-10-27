# -*- coding: utf-8 -*-
"""
API-тесты нового интерфейса (ТЗ-3) через flask.test_client.

Покрывают round-trip'ы settings/companies/keywords, небоскрёб /api/status
(прогресс+счётчики) и хвост логов. Все реальные файлы изолируются во tmp_path через monkeypatch,
чтобы не трогать боевые settings.json/companies.json/keywords.json/.env.

Запуск:
    pytest tests/test_ui_api.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main_ui  # noqa: E402
from src.core import collect  # noqa: E402
from src.core import context as ctx  # noqa: E402
import src.settings_store as settings_store  # noqa: E402
import src.keywords_store as keywords_store  # noqa: E402


# ── Фикстура: клиент + изоляция всех файлов ──────────────────────────────────


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Перенаправляем все файлы хранилищ во временную директорию.
    monkeypatch.setattr(ctx, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(ctx, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(keywords_store, "KEYWORDS_FILE", tmp_path / "keywords.json")
    main_ui.app.config.update(TESTING=True)
    return main_ui.app.test_client()


# ── Главная страница ─────────────────────────────────────────────────────────


def test_index_serves_html(client):
    """GET / отдаёт index.html и подключает app.css/app.js."""
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "TendersParser" in body
    assert "app.css" in body
    assert "app.js" in body


# ── Settings round-trip ─────────────────────────────────────────────────────


def test_settings_round_trip(client):
    """POST настроек сохраняется и читается обратно."""
    payload = {
        "days_back": 7,
        "min_price": 100000,
        "max_price": 5000000,
        "include_no_price": False,
        "parse_tektorg": True,
        "parse_tender_pro": True,
        "parse_gpb": True,
        "browser_headless": False,
        "keyword_search_enabled": True,
        "keyword_filter_enabled": True,
    }
    r = client.post("/api/settings", json=payload)
    assert r.status_code == 200 and r.get_json()["ok"] is True

    got = client.get("/api/settings").get_json()
    for k, v in payload.items():
        assert got[k] == v, f"{k}: {got[k]!r} != {v!r}"


def test_settings_keeps_unspecified_fields(client):
    """POST с частью полей не обнуляет остальные."""
    client.post("/api/settings", json={"days_back": 3, "parse_gpb": True})
    client.post("/api/settings", json={"min_price": 500})  # не трогаем days_back/gpb
    got = client.get("/api/settings").get_json()
    assert got["days_back"] == 3
    assert got["parse_gpb"] is True
    assert got["min_price"] == 500


# ── Companies round-trip ────────────────────────────────────────────────────


def test_companies_round_trip(client):
    """POST списка компаний → GET возвращает тот же список."""
    r = client.post(
        "/api/companies", json={"companies": ["Восток-Золото", "Ромашка", "АЛЬФА"]}
    )
    assert r.status_code == 200
    got = client.get("/api/companies").get_json()["companies"]
    assert got == ["Восток-Золото", "Ромашка", "АЛЬФА"]


def test_companies_dedup_and_strip(client):
    """POST чистит пустые, обрезает пробелы и схлопывает дубли (case-insensitive)."""
    r = client.post(
        "/api/companies",
        json={"companies": ["  Восток-Золото  ", "", "восток-золото", "АЛЬФА"]},
    )
    saved = r.get_json()["companies"]
    assert saved == ["Восток-Золото", "АЛЬФА"]


def test_companies_empty_initially(client):
    """Без файла companies.json GET возвращает пустой список (не падает)."""
    assert client.get("/api/companies").get_json() == {"companies": []}


def test_companies_rejects_non_list(client):
    """POST без списка → 400 + error."""
    r = client.post("/api/companies", json={"companies": "не список"})
    assert r.status_code == 400
    assert "error" in r.get_json()


# ── Keywords round-trip ─────────────────────────────────────────────────────


def test_keywords_round_trip(client):
    """POST фраз + флагов → GET возвращает их обратно."""
    payload = {
        "keyword_search_enabled": True,
        "keyword_filter_enabled": False,
        "keywords": [
            {"phrase": "строительство моста", "enabled": True},
            {"phrase": "СМР", "enabled": False},
        ],
    }
    r = client.post("/api/keywords", json=payload)
    assert r.status_code == 200
    got = client.get("/api/keywords").get_json()
    assert got["keyword_search_enabled"] is True
    assert got["keyword_filter_enabled"] is False
    phrases = [(k["phrase"], k["enabled"]) for k in got["keywords"]]
    assert ("строительство моста", True) in phrases
    assert ("СМР", False) in phrases


def test_keywords_flags_persist_in_settings(client):
    """Флаги keyword_*_enabled сохраняются именно в settings.json."""
    client.post(
        "/api/keywords",
        json={"keyword_search_enabled": True, "keyword_filter_enabled": True},
    )
    s = client.get("/api/settings").get_json()
    assert s["keyword_search_enabled"] is True
    assert s["keyword_filter_enabled"] is True


def test_keywords_empty_initially(client):
    got = client.get("/api/keywords").get_json()
    assert got["keywords"] == []
    # С 2026-07-07 режим A включён по умолчанию (DEFAULTS)
    assert got["keyword_search_enabled"] is True


# ── Status не падает и содержит нужные блоки ────────────────────────────────


def test_status_shape(client):
    """/api/status содержит прогресс и счётчики."""
    s = client.get("/api/status").get_json()
    for key in (
        "running",
        "progress",
        "current_company",
        "total_companies",
        "error",
        "tenders_count",
        "counters",
    ):
        assert key in s, f"нет ключа {key}"
    assert "companies" in s["counters"]
    assert "keywords_enabled" in s["counters"]
    assert "tenders_today" in s["counters"]
    assert s["running"] is False
    assert s["error"] is None


def test_status_counters_reflect_data(client):
    """Счётчики Companies/keywords считаются от актуальных данных."""
    client.post("/api/companies", json={"companies": ["А", "Б", "В"]})
    client.post(
        "/api/keywords",
        json={
            "keywords": [
                {"phrase": "x", "enabled": True},
                {"phrase": "y", "enabled": False},
                {"phrase": "z", "enabled": True},
            ]
        },
    )
    c = client.get("/api/status").get_json()["counters"]
    assert c["companies"] == 3
    assert c["keywords_enabled"] == 2


# ── Логи ────────────────────────────────────────────────────────────────────


def test_logs_returns_text(client):
    """/api/logs отдаёт {lines: str} (пусть и пустой, если файла нет)."""
    r = client.get("/api/logs?lines=50")
    assert r.status_code == 200
    data = r.get_json()
    assert "lines" in data and isinstance(data["lines"], str)


# ── Excel без данных → 400 ──────────────────────────────────────────────────


def test_excel_latest_without_parsing(client):
    """Без запуска парсинга книга всё равно собирается — из единой базы.

    Прежде здесь ждали 400 «Нет данных для экспорта. Сначала запустите
    парсинг»: отчёт строился из лотов текущей сессии и после перезапуска
    программы исчезал. С ТЗ-5 источник — накопленная база, поэтому выгрузка
    возможна всегда; на пустой базе выходит книга с пустыми листами, а не
    отказ (01.09.2026).
    """
    r = client.get("/api/excel/latest")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    )
    assert r.data[:2] == b"PK", "ответ не похож на .xlsx"


# ── Остановка парсинга (/api/parse/stop, v1.5.1) ─────────────────────────────


class _FakeTask:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1
        return True


class _FakeLoop:
    """Синхронно исполняет call_soon_threadsafe — как настоящий loop, но сразу."""

    def __init__(self):
        self.scheduled = 0

    def call_soon_threadsafe(self, fn, *args):
        self.scheduled += 1
        fn(*args)


def test_parse_stop_not_running(client, monkeypatch):
    """Стоп без идущего парсинга → 409 + понятная ошибка."""
    monkeypatch.setitem(collect._parse_state, "running", False)
    r = client.post("/api/parse/stop")
    assert r.status_code == 409
    assert "не запущен" in r.get_json()["error"]


def test_parse_stop_cancels_task(client, monkeypatch):
    """Стоп при running → task.cancel() уходит через call_soon_threadsafe."""
    loop, task = _FakeLoop(), _FakeTask()
    monkeypatch.setitem(collect._parse_state, "running", True)
    monkeypatch.setitem(collect._parse_state, "stopping", False)
    monkeypatch.setitem(collect._parse_state, "progress", [])
    monkeypatch.setitem(collect._parse_cancel, "loop", loop)
    monkeypatch.setitem(collect._parse_cancel, "task", task)

    r = client.post("/api/parse/stop")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert loop.scheduled == 1
    assert task.cancelled == 1
    assert collect._parse_state["stopping"] is True
    assert any("Останавливаю" in line for line in collect._parse_state["progress"])


def test_parse_stop_idempotent(client, monkeypatch):
    """Повторный стоп при уже запрошенной остановке → ok без второго cancel."""
    loop, task = _FakeLoop(), _FakeTask()
    monkeypatch.setitem(collect._parse_state, "running", True)
    monkeypatch.setitem(collect._parse_state, "stopping", True)
    monkeypatch.setitem(collect._parse_cancel, "loop", loop)
    monkeypatch.setitem(collect._parse_cancel, "task", task)

    r = client.post("/api/parse/stop")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert task.cancelled == 0  # cancel НЕ дёргался повторно


def test_parse_stop_no_task(client, monkeypatch):
    """running=True, но ссылки на loop/task нет → 500, состояние не ломаем."""
    monkeypatch.setitem(collect._parse_state, "running", True)
    monkeypatch.setitem(collect._parse_state, "stopping", False)
    monkeypatch.setitem(collect._parse_cancel, "loop", None)
    monkeypatch.setitem(collect._parse_cancel, "task", None)
    r = client.post("/api/parse/stop")
    assert r.status_code == 500
    assert collect._parse_state["stopping"] is False


def test_status_exposes_stop_fields(client, monkeypatch):
    """/api/status отдаёт stopping/stopped для UI."""
    monkeypatch.setitem(collect._parse_state, "stopping", True)
    monkeypatch.setitem(collect._parse_state, "stopped", False)
    d = client.get("/api/status").get_json()
    assert d["stopping"] is True
    assert d["stopped"] is False


def test_do_parse_handles_cancellation(monkeypatch, tmp_path):
    """Отмена сбора: worker закрыл браузер и освободил состояние запуска."""
    import asyncio

    closed = []

    class _FakeBrowser:
        def __init__(self, headless=True):
            pass

        async def start(self):
            pass

        async def close(self):
            closed.append(1)

    class _FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run_all(self):
            raise asyncio.CancelledError()

    import src.utils.browser_manager as bm
    import src.core.parser_runner as pr
    import src.settings_store as ss

    monkeypatch.setattr(bm, "BrowserManager", _FakeBrowser)
    monkeypatch.setattr(pr, "ParserRunner", _FakeRunner)
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "settings.json")
    # Единая база и журнал — тоже во временную папку. Без этого тест писал
    # «прогон остановлен» в РАБОЧИЕ данные пользователя: 01.09.2026 в живой
    # базе накопилось восемь фиктивных прогонов, и утренняя плашка «что
    # нового» считала свежесть от последнего из них, а не от ночного сбора.
    import src.run_journal as run_journal

    monkeypatch.setattr(ctx, "UNIFIED_DB_PATH", tmp_path / "tenderwork.db")
    monkeypatch.setattr(run_journal, "journal_dir", lambda: tmp_path / "journal")

    monkeypatch.setitem(collect._parse_state, "running", True)
    monkeypatch.setitem(collect._parse_state, "stopped", False)
    monkeypatch.setitem(collect._parse_state, "progress", [])

    collect._run_parse_async([])

    assert closed == [1]  # finally закрыл браузер
    assert collect._parse_state["running"] is False
    assert collect._parse_state["stopped"] is True
    assert collect._parse_state["error"] is None
    assert any("остановлен пользователем" in line for line in collect._parse_state["progress"])
