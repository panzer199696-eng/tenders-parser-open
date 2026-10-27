"""
Тесты расписания сбора (T024–T026).

Программа работает на ПК пользователя, а не на сервере, поэтому главный
сценарий здесь — не «таймер сработал», а «ПК был выключен». Планировщик
Windows пропущенный слот не повторяет, и без догоняющего запуска день
выпал бы молча.

Планировщик в тестах не вызывается: проверяется команда, которую программа
собирается ему отдать.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.schedule import (
    build_command,
    describe,
    missed_run,
    normalize,
    previous_slot,
)

WORKDAYS = {
    "enabled": True,
    "time": "03:00",
    "days": ["mon", "tue", "wed", "thu", "fri"],
}
MONDAY_10 = datetime(2026, 8, 31, 10, 0)  # понедельник


# ── Нормализация ─────────────────────────────────────────────────────────────


def test_время_приводится_к_единому_виду():
    assert normalize({"time": "3:0"})["time"] == "03:00"


def test_кривое_время_не_отключает_расписание():
    """Ошибка ввода не должна молча превращаться в «сбора не будет»."""
    s = normalize({"enabled": True, "time": "четверть третьего"})
    assert s["enabled"] is True
    assert s["time"] == "03:00"


@pytest.mark.parametrize("bad", ["24:00", "12:60", "-1:00", ""])
def test_недопустимое_время_заменяется_умолчанием(bad):
    assert normalize({"time": bad})["time"] == "03:00"


def test_пустой_список_дней_означает_ошибку_а_не_никогда():
    """Включённое расписание без единого дня — это опечатка, а не намерение."""
    assert len(normalize({"enabled": True, "days": []})["days"]) == 7


def test_мусорные_дни_отбрасываются():
    s = normalize({"days": ["mon", "вторник", "FRI", 42]})
    assert s["days"] == ["mon", "fri"]


def test_дни_идут_в_порядке_недели():
    assert normalize({"days": ["fri", "mon", "wed"]})["days"] == ["mon", "wed", "fri"]


# ── Описание словами ─────────────────────────────────────────────────────────


def test_описание_выключенного():
    assert describe({"enabled": False}) == "сбор по расписанию выключен"


def test_описание_каждый_день():
    assert describe({"enabled": True, "time": "03:00"}) == "каждый день в 03:00"


def test_описание_будней():
    assert describe(WORKDAYS) == "по будням в 03:00"


def test_описание_произвольных_дней():
    s = {"enabled": True, "time": "07:30", "days": ["mon", "thu"]}
    assert describe(s) == "пн, чт в 07:30"


# ── Слоты и пропуски ─────────────────────────────────────────────────────────


def test_последний_слот_сегодня_если_время_прошло():
    assert previous_slot(WORKDAYS, MONDAY_10) == datetime(2026, 8, 31, 3, 0)


def test_последний_слот_вчера_если_время_ещё_не_пришло():
    early = datetime(2026, 8, 31, 1, 0)  # понедельник до 03:00
    assert previous_slot(WORKDAYS, early) == datetime(2026, 8, 28, 3, 0)  # пятница


def test_выходные_пропускаются_при_расписании_по_будням():
    sunday = datetime(2026, 8, 30, 12, 0)
    assert previous_slot(WORKDAYS, sunday) == datetime(2026, 8, 28, 3, 0)


def test_у_выключенного_расписания_слотов_нет():
    assert previous_slot({"enabled": False}) is None


def test_пропущенный_слот_находится():
    """ПК был выключен ночью — прогон нужно догнать."""
    assert missed_run(WORKDAYS, datetime(2026, 8, 30, 12, 0), MONDAY_10) == datetime(
        2026, 8, 31, 3, 0
    )


def test_состоявшийся_прогон_догонять_не_нужно():
    assert missed_run(WORKDAYS, datetime(2026, 8, 31, 3, 5), MONDAY_10) is None


def test_без_единого_прогона_слот_считается_пропущенным():
    assert missed_run(WORKDAYS, None, MONDAY_10) is not None


def test_догон_можно_отключить():
    schedule = {**WORKDAYS, "catch_up": False}
    assert missed_run(schedule, None, MONDAY_10) is None


def test_выключенное_расписание_ничего_не_догоняет():
    assert missed_run({"enabled": False}, None, MONDAY_10) is None


# ── Команда планировщику ─────────────────────────────────────────────────────


def test_команда_содержит_дни_и_время():
    cmd = build_command(WORKDAYS, "C:/app/TendersParser.exe --collect")
    assert "MON,TUE,WED,THU,FRI" in cmd
    assert "03:00" in cmd
    assert "/SC" in cmd and "WEEKLY" in cmd


def test_команда_перезаписывает_прежнюю_задачу():
    """Иначе после смены времени в планировщике осталась бы старая задача."""
    assert "/F" in build_command(WORKDAYS, "app.exe")


def test_в_команду_попадает_то_что_запускать():
    cmd = build_command(WORKDAYS, "C:/app/TendersParser.exe --collect")
    assert "C:/app/TendersParser.exe --collect" in cmd
