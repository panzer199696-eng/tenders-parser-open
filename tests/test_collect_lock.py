"""Межпроцессная блокировка сбора (ревью 16.10.2026, А4).

Два сбора одновременно — ночной `--collect` и кнопка «Запустить» в окне,
или догон 07:00 поверх затянувшейся ночи — пишут в одну базу и журнал,
и итоги одного затирают итоги другого. Внутрипроцессный флаг `running`
второй процесс не видит, поэтому нужна блокировка файла в папке данных.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src import collect_lock

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "data" / "collect.lock"


def test_второй_захват_в_том_же_процессе_не_проходит(lock_path):
    first = collect_lock.acquire(lock_path)
    assert first is not None
    try:
        with pytest.raises(collect_lock.CollectBusy) as err:
            collect_lock.acquire(lock_path)
        assert err.value.pid == __import__("os").getpid()
    finally:
        first.release()


def test_после_освобождения_захват_снова_проходит(lock_path):
    collect_lock.acquire(lock_path).release()
    again = collect_lock.acquire(lock_path)
    again.release()


def test_другой_процесс_видит_занятый_замок(lock_path):
    held = collect_lock.acquire(lock_path)
    try:
        code = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(_ROOT)!r})
            from src import collect_lock
            try:
                collect_lock.acquire({str(lock_path)!r})
            except collect_lock.CollectBusy as e:
                print("busy", e.pid)
                sys.exit(3)
            sys.exit(0)
            """
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 3, done.stderr
        assert done.stdout.split() == ["busy", str(__import__("os").getpid())]
    finally:
        held.release()


def test_замок_умершего_процесса_не_держит(lock_path):
    """Процесс убит — ОС снимает блокировку, файл с PID остаётся, но не мешает."""
    code = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(_ROOT)!r})
        from src import collect_lock
        collect_lock.acquire({str(lock_path)!r})
        os._exit(0)
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
    assert lock_path.exists()
    collect_lock.acquire(lock_path).release()
