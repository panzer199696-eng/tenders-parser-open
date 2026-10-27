"""Общая обвязка тестов: изоляция от живых данных и от сети.

1. Папка данных — временная. В dev-запуске user_data_dir() — корень проекта,
   где лежат ЖИВЫЕ базы, .env и logs/parser.log. main_ui при импорте вешает
   файловый лог на <папка данных>/logs/parser.log, и тестовые «HTTP Error
   503: boom» сыпались бы в боевой журнал. Переменная ставится ДО любого
   импорта src.

   ⚠️ 12.10.2026 первая версия этой обвязки снесла живые данные: при
   заданной переменной ensure_user_files ПЕРЕНОСИЛ файлы из корня проекта
   во временную папку, а atexit её удалял. Перенос в dev теперь запрещён
   (runtime_paths._migration_allowed), а здесь — страж: живые файлы
   переписываются до прогона и сверяются после; если что-то пропало,
   временная папка НЕ удаляется (там последняя надежда) и прогон падает.

2. ТЭК-Торг в тестах в сеть не ходит: проба прямого пути подменена ответом
   «прямой путь есть», окно браузера — заглушкой «антибот не пройден».
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Живые файлы dev-запуска: всё, что не в git и без чего ночной сбор встанет.
_LIVE_NAMES = (
    "settings.json",
    "companies.json",
    "keywords.json",
    "client_inns.json",
    ".env",
)


def _live_snapshot(root: Path = _PROJECT_ROOT) -> dict[str, tuple[int, int]]:
    """Живые файлы: имя → (размер, время изменения).

    Одного наличия мало (ревью 16.10.2026): тест, переписавший живую базу
    на месте, страж пропускал — файл ведь не пропал.
    """
    paths = [root / n for n in _LIVE_NAMES]
    for sub in ("data", "logs"):
        d = root / sub
        if d.is_dir():
            paths += [p for p in d.iterdir()]
    found = {}
    for p in paths:
        try:
            if p.is_file():
                st = p.stat()
                found[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return found


def _compare_live(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """(пропавшие, изменённые) живые файлы."""
    lost = sorted(set(before) - set(after))
    changed = sorted(n for n in before if n in after and before[n] != after[n])
    return lost, changed


_LIVE_BEFORE = _live_snapshot()
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="tp-tests-")
os.environ["TENDERSPARSER_DATA_DIR"] = _TEST_DATA_DIR

# Заполнить изолированную папку поставочными defaults/ ДО импорта config:
# он читает companies.json при импорте, и без этого тесты видели пустой
# список (раньше — живой список из корня проекта).
from src.runtime_paths import ensure_user_files

ensure_user_files()


def pytest_sessionfinish(session, exitstatus):
    lost, changed = _compare_live(_LIVE_BEFORE, _live_snapshot())
    # Журнал parser.log пишет и открытое окно программы, если оно запущено
    # рядом с тестами, — его изменение не повод валить прогон, но сказать надо.
    noisy = [n for n in changed if n.startswith("logs/")]
    changed = [n for n in changed if n not in noisy]
    if noisy:
        print("\n\n!!! Во время тестов изменились логи: " + ", ".join(noisy))
    if lost or changed:
        session.exitstatus = 1
        print(
            "\n\n!!! ТЕСТЫ ТРОНУЛИ ЖИВЫЕ ДАННЫЕ — пропали: "
            + (", ".join(lost) or "—")
            + "; изменены: "
            + (", ".join(changed) or "—")
            + f"\n!!! Временная папка сохранена: {_TEST_DATA_DIR}\n"
        )
        return
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


class _OfflineTektorgTransport:
    instances: list = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append(self)

    async def start(self) -> bool:
        return False

    async def close(self) -> None:
        return None

    async def fetch(self, payload):  # pragma: no cover — не должен вызываться
        raise AssertionError("тест не должен ходить в ТЭК-Торг")


@pytest.fixture(autouse=True)
def _offline_tektorg_transport(monkeypatch):
    monkeypatch.setattr(
        "src.core.parser_runner.probe_direct", lambda fetch: "ok", raising=False
    )
    monkeypatch.setattr(
        "src.core.parser_runner.TektorgBrowserTransport",
        _OfflineTektorgTransport,
        raising=False,
    )
    yield


@pytest.fixture(autouse=True)
def _release_collect_lock():
    """Замок сбора не переживает тест: тесты с подменённым потоком занимают
    запуск, но поток не идёт, и снять замок некому."""
    yield
    collect = sys.modules.get("src.core.collect")
    if collect is not None:
        collect._release_collect_lock()


@pytest.fixture(autouse=True)
def _offline_net_preflight(monkeypatch):
    """Проверка связи перед сбором (net_preflight) в тестах не ходит в сеть:
    все площадки «на связи», пауз нет. Тесты самой проверки берут исходную
    функцию probe через импорт по имени до подмены."""
    monkeypatch.setattr(
        "src.core.net_preflight.probe", lambda url, **kw: True, raising=False
    )
    yield
