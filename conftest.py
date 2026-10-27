"""
Запрет сети в тестах — на весь прогон `pytest`.

Конституция проекта: тесты не ходят в сеть. 16.10.2026 тест сторожа формата
ТЭК-Торга после правки парсера переключился на новый путь, загрузчик которого
не был подменён, и ~5 минут без пауз долбил живой api.tektorg.ru — сотни
запросов к площадке, которая режет частые обращения антиботом. Подмены в
отдельных тестах такое не ловят: новый сетевой путь всегда появляется раньше,
чем о нём узнают тесты. Поэтому запрет общий — на уровне сокета.

Разрешён только локальный адрес (тесты интерфейса и single-instance).
"""

import socket

import pytest

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_real_connect = socket.socket.connect


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def _guarded_connect(self, address):
    host = _host_of(address)
    if self.family in (socket.AF_INET, socket.AF_INET6) and host not in _ALLOWED_HOSTS:
        raise RuntimeError(
            f"Тест пытается выйти в сеть ({host}) — подмените загрузчик. "
            "Тесты не ходят на площадки (conftest.py в корне проекта)."
        )
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    yield
