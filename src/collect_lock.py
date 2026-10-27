"""
Межпроцессная блокировка сбора: в один момент идёт только один прогон.

Флаг `running` в main_ui живёт внутри процесса, а сборов бывает несколько
процессов: ночной `--collect` из планировщика, догон в 07:00, окно программы
с кнопкой «Запустить». Затянувшаяся ночь плюс догон или ручной запуск поверх
ночного писали в одну базу и журнал одновременно, и итоги одного прогона
затирали итоги другого (ревью 16.10.2026, А4).

Блокировка — участок файла `data/collect.lock` в папке данных (msvcrt на
Windows, flock в остальных системах). Её держит ОС, поэтому убитый процесс
замок не оставляет. В начале файла — PID владельца, чтобы второй запуск мог
сказать, кто занят. Байт блокировки лежит далеко за PID: на Windows
заблокированный участок не читается другими процессами.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_NAME = "collect.lock"
_LOCK_OFFSET = 1 << 16


class CollectBusy(RuntimeError):
    """Сбор уже идёт в другом процессе (или в этом же через другой вход)."""

    def __init__(self, pid: int | None):
        self.pid = pid
        who = f" (процесс {pid})" if pid else ""
        super().__init__(f"Сбор уже идёт{who} — второй запуск отменён")


def lock_path() -> Path:
    from .runtime_paths import user_data_dir

    return user_data_dir() / "data" / LOCK_NAME


def _read_pid(fh) -> int | None:
    try:
        fh.seek(0)
        text = fh.read(32).decode("ascii", "ignore").strip()
        return int(text.split()[0]) if text else None
    except (OSError, ValueError, IndexError):
        return None


def _try_lock(fh) -> bool:
    fh.seek(_LOCK_OFFSET)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


class CollectLock:
    """Захваченный замок; release() снимает его (повторный вызов безвреден)."""

    def __init__(self, fh):
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(_LOCK_OFFSET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


def acquire(path: Path | str | None = None) -> CollectLock:
    """Захватить замок сбора или бросить CollectBusy с PID владельца."""
    target = Path(path) if path else lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # «a+b»: файл создаётся, если его нет, и не обрезается при открытии —
    # PID владельца не стирается вторым, неудачливым запуском.
    fh = open(target, "a+b")
    if not _try_lock(fh):
        pid = _read_pid(fh)
        fh.close()
        raise CollectBusy(pid)
    fh.seek(0)
    fh.truncate(0)
    fh.write(f"{os.getpid()}\n".encode("ascii"))
    fh.flush()
    return CollectLock(fh)
