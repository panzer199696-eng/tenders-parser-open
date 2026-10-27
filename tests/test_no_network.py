"""Тесты не ходят в сеть: запрет в conftest.py корня проекта (16.10.2026)."""

import urllib.request

import pytest


def test_выход_в_сеть_из_теста_запрещён():
    with pytest.raises(Exception, match="выйти в сеть"):
        urllib.request.urlopen("https://api.tektorg.ru/open-api/lots", timeout=5)
