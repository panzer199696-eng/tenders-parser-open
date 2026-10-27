"""
Пути приложения: где лежит программа, а где — файлы пользователя.

Четыре разных места, которые нельзя путать:

- `bundle_dir()` — РЕСУРСЫ поставки, вшитые в сборку (шаблоны/статика `src/ui`,
  каталог `defaults/`). В dev-запуске — корень проекта; в сборке PyInstaller —
  `sys._MEIPASS` (для onedir это `dist/TendersParser/_internal`).

- `app_base_dir()` — папка САМОЙ ПРОГРАММЫ (рядом с exe в сборке).
  Нужна миграции старых установок и команде планировщика. Рабочих файлов
  пользователя здесь больше нет.

- `user_data_dir()` — файлы ПОЛЬЗОВАТЕЛЯ: `settings.json`, `companies.json`,
  `keywords.json`, `client_inns.json`, `.env`, `data/tenderwork.db`, `logs/`. В сборке — `%LOCALAPPDATA%\\TendersParser`, чтобы пользователь не
  видел (и не ломал) их рядом с exe. В dev-запуске — корень проекта, как было
  исторически.

- `results_dir()` — куда кладём готовые Excel-отчёты: «Документы\\TendersParser».
  Пользователь должен находить результат сам, без вопросов «куда скачалось».

Переопределение для тестов и переноса на другую машину — переменная окружения
`TENDERSPARSER_DATA_DIR`.
"""

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Имя папки в профиле пользователя (и в «Документах» — для результатов).
APP_DIR_NAME = "TendersParser"

# Переменная окружения для принудительного указания папки данных.
DATA_DIR_ENV = "TENDERSPARSER_DATA_DIR"

# Файлы пользователя, которые приложение готовит при первом запуске:
# переносит из старой установки (рядом с exe) либо берёт из defaults/.
USER_FILES = (
    "settings.json",
    "companies.json",
    "keywords.json",
    # ИНН заказчиков для площадок, которые ищут по ИНН (ТЭК-Торг,
    # Росэлторг) — см. src/client_inns_store.py.
    "client_inns.json",
    # Справочники-источники (фича 002, T004): без них профиль не соберётся, и
    # на чистой установке программа падала бы при первой же оценке лота.
    # Лежат в подпапке data/ — путь указывается вместе с ней.
    "data/nomenclature.json",
    "data/clients.json",
    "data/rules.json",
    ".env",
)

# Подпапки в папке данных, которые должны существовать всегда.
USER_SUBDIRS = ("data", "logs")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_base_dir() -> Path:
    """Папка программы: рядом с exe (сборка) или корень проекта (dev)."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return _PROJECT_ROOT


def bundle_dir() -> Path:
    """Папка ресурсов поставки: `sys._MEIPASS` в сборке, корень проекта в dev."""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_base_dir()))
    return _PROJECT_ROOT


def _local_app_data() -> Path:
    """`%LOCALAPPDATA%` с запасным вариантом, если переменной нет."""
    raw = os.environ.get("LOCALAPPDATA", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


class DataDirError(RuntimeError):
    """Папку данных из TENDERSPARSER_DATA_DIR создать нельзя."""


def user_data_dir() -> Path:
    """Папка рабочих файлов пользователя (создаётся при обращении).

    Приоритет: `TENDERSPARSER_DATA_DIR` → `%LOCALAPPDATA%\\TendersParser`
    (только в сборке) → корень проекта (dev-запуск).

    Если папку создать не удалось (нет прав, диск недоступен) — деградируем
    к папке программы: приложение обязано стартовать в любом случае.
    Кроме явного `TENDERSPARSER_DATA_DIR`: переменную задают ради ОТДЕЛЬНОЙ
    папки (тесты, опыты), а папка программы в dev — корень проекта с живыми
    базами. Откат туда молча пустил бы опыт писать в боевые данные
    (ревью 16.10.2026), поэтому — исключение.
    """
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        target = Path(override)
    elif _is_frozen():
        target = _local_app_data() / APP_DIR_NAME
    else:
        target = _PROJECT_ROOT

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if override:
            raise DataDirError(
                f"Папку данных {target} из {DATA_DIR_ENV} создать нельзя ({e}); "
                "работа с папкой программы вместо неё запрещена"
            ) from e
        logger.warning(
            "Не удалось создать папку данных %s (%s) — работаю рядом с программой",
            target,
            e,
        )
        return app_base_dir()
    return target


def _documents_dir() -> Path:
    """Папка «Документы» текущего пользователя (best-effort)."""
    profile = os.environ.get("USERPROFILE", "").strip()
    home = Path(profile) if profile else Path.home()
    docs = home / "Documents"
    if docs.is_dir():
        return docs
    # Если стандартной папки нет (перенесённый профиль, OneDrive) — кладём
    # прямо в профиль: главное, чтобы путь существовал и был понятен.
    return home


def results_dir() -> Path:
    """Папка готовых Excel-отчётов (создаётся при обращении)."""
    if _is_frozen() or os.environ.get(DATA_DIR_ENV, "").strip():
        target = _documents_dir() / APP_DIR_NAME
    else:
        target = _PROJECT_ROOT / "results"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError as e:
        logger.warning("Не удалось создать папку результатов %s (%s)", target, e)

    fallback = user_data_dir() / "Результаты"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        return user_data_dir()
    return fallback


def _migration_allowed() -> bool:
    """Можно ли ПЕРЕНОСИТЬ рабочие файлы из папки программы в папку данных.

    Перенос придуман для обновления exe: старые версии держали настройки и
    сессию рядом с exe, новые — в %LOCALAPPDATA%. Только там он и уместен.

    В dev-запуске папка программы — корень проекта, где лежат ЖИВЫЕ базы
    и .env. С `TENDERSPARSER_DATA_DIR` на временную папку перенос уносил их
    туда, и вместе с временной папкой они исчезали (тысячи лотов, .env,
    журналы). Поэтому переносим только из сборки и только без ручного
    переопределения папки.
    """
    return _is_frozen() and not os.environ.get(DATA_DIR_ENV, "").strip()


def _seed_source(name: str, base: Path, bundle: Path, allow_legacy: bool = True):
    """Откуда взять файл `name`: (путь, переносить_ли).

    Порядок: старая установка рядом с exe (переносим) → `defaults/` из
    поставки (копируем) → корень поставки (копируем). None — брать неоткуда.
    allow_legacy=False — рабочие файлы рядом с программой не трогаем вовсе
    (ни переносом, ни копией): только поставочные `defaults/`.
    """
    legacy = base / name
    if allow_legacy and legacy.is_file():
        return legacy, True
    packaged = bundle / "defaults" / name
    if packaged.is_file():
        return packaged, False
    # Файл из подпапки (например «data/clients.json») в поставке лежит
    # в плоском defaults/ — иначе пришлось бы держать две копии одного
    # справочника ради структуры каталога.
    if "/" in name:
        flat = bundle / "defaults" / Path(name).name
        if flat.is_file():
            return flat, False
    plain = bundle / name
    if allow_legacy and plain.is_file() and plain != legacy:
        return plain, False
    return None, False


def _migrate_dir_contents(src: Path, dst: Path) -> int:
    """Перенести файлы из `src` в `dst`, не затирая существующие. Вернуть число."""
    if not src.is_dir():
        return 0
    try:
        if src.resolve() == dst.resolve():
            return 0
        entries = list(src.iterdir())
    except OSError:
        return 0
    moved = 0
    for item in entries:
        target = dst / item.name
        if target.exists():
            continue
        try:
            shutil.move(str(item), str(target))
            moved += 1
        except (OSError, shutil.Error):
            continue
    return moved


def ensure_user_files() -> None:
    """Подготовить папку данных пользователя. Идемпотентно, не бросает ошибок.

    - создаёт `data/` и `logs/`;
    - переносит рабочие файлы из старой установки (рядом с exe) — обновление
      с прежних версий не теряет настройки и сессию;
    - недостающие файлы берёт из `defaults/` поставки;
    - существующие файлы НИКОГДА не перезаписывает.
    """
    try:
        data_dir = user_data_dir()
        base = app_base_dir()
        bundle = bundle_dir()
        try:
            same_place = data_dir.resolve() == base.resolve()
        except OSError:
            same_place = data_dir == base
        migrate = _migration_allowed()

        for sub in USER_SUBDIRS:
            try:
                (data_dir / sub).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

        for name in USER_FILES:
            target = data_dir / name
            if target.exists():
                continue
            src, move_it = _seed_source(name, base, bundle, allow_legacy=migrate)
            if src is None:
                continue
            try:
                if src.resolve() == target.resolve():
                    continue
            except OSError:
                pass
            try:
                if move_it and not same_place:
                    shutil.move(str(src), str(target))
                    logger.info("Файл %s перенесён в папку данных", name)
                else:
                    shutil.copy2(str(src), str(target))
                    logger.info("Файл %s создан из поставки", name)
            except (OSError, shutil.Error) as e:
                logger.warning("Не удалось подготовить %s: %s", name, e)

        if migrate and not same_place:
            for sub in USER_SUBDIRS:
                moved = _migrate_dir_contents(base / sub, data_dir / sub)
                if moved:
                    logger.info("Перенесено файлов из %s: %d", sub, moved)
    except Exception as e:  # pragma: no cover — подготовка не должна ронять старт
        logger.warning("Подготовка папки данных не удалась: %s", e)
