"""Тесты слоя путей: папка данных пользователя, результаты, подготовка файлов.

Проверяем главное обещание v2.1: рабочие файлы живут в профиле пользователя,
рядом с exe их нет, а при обновлении со старой версии они туда переезжают
вместе с настройками и сессией.

Ни один тест не пишет в реальный %LOCALAPPDATA% — только в tmp_path.
"""

import sys

import pytest

from src import runtime_paths as rp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Сбрасываем переопределение папки данных между тестами."""
    monkeypatch.delenv(rp.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    yield


# ── user_data_dir ────────────────────────────────────────────────────────────


def test_env_override_wins(tmp_path, monkeypatch):
    target = tmp_path / "своя-папка"
    monkeypatch.setenv(rp.DATA_DIR_ENV, str(target))
    assert rp.user_data_dir() == target
    assert target.is_dir()  # создаётся сразу


def test_frozen_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "TendersParser.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert rp.user_data_dir() == tmp_path / "AppData" / "Local" / rp.APP_DIR_NAME


def test_dev_run_uses_project_root(monkeypatch):
    assert rp.user_data_dir() == rp._PROJECT_ROOT


def test_unwritable_target_falls_back_to_app_dir(tmp_path, monkeypatch):
    """Сборка без переопределения: папку в профиле создать нельзя — работаем
    рядом с программой (там нет чужих живых данных)."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "TendersParser.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "нельзя"))

    def _boom(*a, **k):
        raise OSError("нет прав")

    monkeypatch.setattr(rp.Path, "mkdir", _boom)
    assert rp.user_data_dir() == rp.app_base_dir()


def test_недоступная_папка_из_переменной_даёт_ошибку_а_не_живые_данные(
    tmp_path, monkeypatch
):
    """С TENDERSPARSER_DATA_DIR откат на папку программы в dev — это корень
    проекта с живыми базами: тесты и опыты писали бы в боевые данные."""
    monkeypatch.setenv(rp.DATA_DIR_ENV, str(tmp_path / "нельзя"))

    def _boom(*a, **k):
        raise OSError("нет прав")

    monkeypatch.setattr(rp.Path, "mkdir", _boom)
    with pytest.raises(rp.DataDirError, match="нельзя"):
        rp.user_data_dir()


# ── results_dir ──────────────────────────────────────────────────────────────


def test_results_dir_created_and_writable(tmp_path, monkeypatch):
    monkeypatch.setenv(rp.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    (tmp_path / "profile" / "Documents").mkdir(parents=True)
    target = rp.results_dir()
    assert target == tmp_path / "profile" / "Documents" / rp.APP_DIR_NAME
    (target / "проба.txt").write_text("ок", encoding="utf-8")


def test_results_dir_without_documents_uses_profile(tmp_path, monkeypatch):
    monkeypatch.setenv(rp.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    (tmp_path / "profile").mkdir()
    assert rp.results_dir() == tmp_path / "profile" / rp.APP_DIR_NAME


def test_results_dir_dev_run(monkeypatch):
    assert rp.results_dir() == rp._PROJECT_ROOT / "results"


# ── ensure_user_files ────────────────────────────────────────────────────────


@pytest.fixture
def frozen_layout(tmp_path, monkeypatch):
    """Имитация установленной программы: папка exe, сборка, профиль."""
    app = tmp_path / "Program" / "TendersParser"
    bundle = app / "_internal"
    data = tmp_path / "AppData" / "Local" / rp.APP_DIR_NAME
    (bundle / "defaults").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "TendersParser.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    # conftest задаёт папку данных на весь прогон; установленная программа
    # её не задаёт — иначе это уже не «обновление exe».
    monkeypatch.delenv(rp.DATA_DIR_ENV, raising=False)
    return {"app": app, "bundle": bundle, "data": data}


def test_creates_subdirs(frozen_layout):
    rp.ensure_user_files()
    for sub in rp.USER_SUBDIRS:
        assert (frozen_layout["data"] / sub).is_dir()


def test_migrates_old_install_files(frozen_layout):
    """Файлы прежней версии, лежавшие рядом с exe, переезжают в профиль."""
    old = frozen_layout["app"] / "settings.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('{"days_back": 7}', encoding="utf-8")

    rp.ensure_user_files()

    moved = frozen_layout["data"] / "settings.json"
    assert moved.read_text(encoding="utf-8") == '{"days_back": 7}'
    assert not old.exists()  # рядом с exe пользовательских файлов не остаётся


def test_seeds_missing_files_from_defaults(frozen_layout):
    (frozen_layout["bundle"] / "defaults" / "companies.json").write_text(
        '{"companies": ["Тест"]}', encoding="utf-8"
    )
    rp.ensure_user_files()
    seeded = frozen_layout["data"] / "companies.json"
    assert '"Тест"' in seeded.read_text(encoding="utf-8")


def test_does_not_overwrite_existing(frozen_layout):
    data = frozen_layout["data"]
    data.mkdir(parents=True, exist_ok=True)
    (data / "companies.json").write_text('{"companies": ["Моё"]}', encoding="utf-8")
    (frozen_layout["bundle"] / "defaults" / "companies.json").write_text(
        '{"companies": ["Из поставки"]}', encoding="utf-8"
    )

    rp.ensure_user_files()
    rp.ensure_user_files()  # идемпотентность

    assert "Моё" in (data / "companies.json").read_text(encoding="utf-8")


def test_migrates_old_logs_and_db(frozen_layout):
    app = frozen_layout["app"]
    (app / "logs").mkdir(parents=True, exist_ok=True)
    (app / "logs" / "parser.log").write_text("старый лог", encoding="utf-8")
    (app / "data").mkdir(parents=True, exist_ok=True)
    (app / "data" / "tenders.db").write_bytes(b"sqlite")

    rp.ensure_user_files()

    assert (frozen_layout["data"] / "logs" / "parser.log").read_text(
        encoding="utf-8"
    ) == "старый лог"
    assert (frozen_layout["data"] / "data" / "tenders.db").read_bytes() == b"sqlite"


def test_survives_broken_environment(frozen_layout, monkeypatch):
    """Подготовка не должна ронять запуск даже при отказе файловых операций."""

    def _boom(*a, **k):
        raise OSError("диск недоступен")

    monkeypatch.setattr(rp.shutil, "move", _boom)
    monkeypatch.setattr(rp.shutil, "copy2", _boom)
    (frozen_layout["bundle"] / "defaults" / "keywords.json").write_text(
        "{}", encoding="utf-8"
    )
    rp.ensure_user_files()  # не бросает


# ── Живые файлы dev-запуска не переносятся (12.10.2026) ──────────────────────


@pytest.fixture
def dev_layout(tmp_path, monkeypatch):
    """dev-запуск: корень проекта с живыми файлами + TENDERSPARSER_DATA_DIR."""
    root = tmp_path / "project"
    (root / "defaults").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "logs").mkdir()
    (root / "settings.json").write_text('{"live": true}', encoding="utf-8")
    (root / ".env").write_text("SECRET=x", encoding="utf-8")
    (root / "data" / "tenderwork.db").write_bytes(b"live-db")
    (root / "logs" / "parser.log").write_text("live log", encoding="utf-8")
    (root / "defaults" / "settings.json").write_text(
        '{"default": true}', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(rp, "_PROJECT_ROOT", root)
    data = tmp_path / "isolated"
    monkeypatch.setenv(rp.DATA_DIR_ENV, str(data))
    return {"root": root, "data": data}


def test_dev_с_переопределением_не_уносит_живые_файлы(dev_layout):
    rp.ensure_user_files()
    root, data = dev_layout["root"], dev_layout["data"]
    # Живое осталось на месте.
    assert (root / "settings.json").read_text(encoding="utf-8") == '{"live": true}'
    assert (root / ".env").is_file()
    assert (root / "data" / "tenderwork.db").read_bytes() == b"live-db"
    assert (root / "logs" / "parser.log").is_file()
    # Изолированная папка получила только поставочное, не копию живого.
    assert (data / "settings.json").read_text(encoding="utf-8") == '{"default": true}'
    assert not (data / ".env").exists()
    assert not (data / "data" / "tenderwork.db").exists()
    assert not (data / "logs" / "parser.log").exists()


def test_миграция_разрешена_только_в_сборке_без_переопределения(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv(rp.DATA_DIR_ENV, raising=False)
    assert rp._migration_allowed()
    monkeypatch.setenv(rp.DATA_DIR_ENV, "X")
    assert not rp._migration_allowed()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv(rp.DATA_DIR_ENV, raising=False)
    assert not rp._migration_allowed()
