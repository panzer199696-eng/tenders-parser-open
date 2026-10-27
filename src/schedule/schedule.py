"""
Расписание сбора (задачи T024–T026).

Программа должна обходить площадки сама: время суток и дни недели задаёт
человек в интерфейсе. Работает это на ПК пользователя, а не на сервере, —
отсюда два следствия, которые и определяют устройство модуля:

1. **Задачу ставим в планировщик Windows**, а не крутим таймер внутри
   программы: программа может быть закрыта, а сбор всё равно должен пойти.

2. **Пропущенный прогон нужно догонять.** Если в назначенное время ПК был
   выключен, планировщик слот пропустит — и без догоняющего запуска день
   выпадет молча. Поэтому при старте программа сверяет, был ли прогон в
   последний запланированный слот, и если нет — запускает его, помечая
   догоняющим.

Расписание живёт в `settings.json` (ключ `schedule`), чтобы переезжать
вместе с остальными настройками пользователя при обновлении программы.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TASK_NAME = "TendersParser — ночной сбор"

# Дни недели: индекс = weekday() Python (понедельник = 0).
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_TITLES = {
    "mon": "пн",
    "tue": "вт",
    "wed": "ср",
    "thu": "чт",
    "fri": "пт",
    "sat": "сб",
    "sun": "вс",
}

DEFAULT_SCHEDULE = {
    "enabled": False,
    # 03:00 — ночью площадки отвечают быстрее, а к утреннему разбору данные
    # уже собраны.
    "time": "03:00",
    "days": list(WEEKDAYS),
    # Догонять пропущенный слот: иначе выключенный на ночь ПК молча съедает день.
    "catch_up": True,
}


def normalize(raw: dict | None) -> dict:
    """Привести расписание к каноническому виду, отбросив мусор."""
    data = dict(DEFAULT_SCHEDULE)
    if not isinstance(raw, dict):
        return data

    data["enabled"] = bool(raw.get("enabled", False))
    data["catch_up"] = bool(raw.get("catch_up", True))

    time_text = str(raw.get("time", DEFAULT_SCHEDULE["time"])).strip()
    try:
        hours, minutes = time_text.split(":")
        hh, mm = int(hours), int(minutes)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        data["time"] = f"{hh:02d}:{mm:02d}"
    except (ValueError, AttributeError):
        # Кривое время — не повод отключать расписание молча: берём значение
        # по умолчанию и пишем об этом в лог.
        logger.warning("Расписание: непонятное время %r — беру 03:00", time_text)
        data["time"] = DEFAULT_SCHEDULE["time"]

    days = raw.get("days")
    if isinstance(days, list):
        clean = [d for d in WEEKDAYS if d in {str(x).lower() for x in days}]
        # Пустой список дней означал бы «никогда» при включённом расписании —
        # это наверняка ошибка ввода, а не намерение.
        data["days"] = clean or list(WEEKDAYS)
    return data


def describe(schedule: dict) -> str:
    """Расписание словами — для интерфейса и журнала."""
    schedule = normalize(schedule)
    if not schedule["enabled"]:
        return "сбор по расписанию выключен"
    days = schedule["days"]
    if len(days) == 7:
        when = "каждый день"
    elif days == ["mon", "tue", "wed", "thu", "fri"]:
        when = "по будням"
    else:
        when = ", ".join(WEEKDAY_TITLES[d] for d in days)
    return f"{when} в {schedule['time']}"


def previous_slot(schedule: dict, now: datetime | None = None) -> datetime | None:
    """Последний запланированный момент сбора до `now` (или None).

    Нужен для догоняющего запуска: сравнив его с временем последнего прогона,
    видно, пропустили ли мы слот.
    """
    schedule = normalize(schedule)
    if not schedule["enabled"] or not schedule["days"]:
        return None

    now = now or datetime.now()
    hh, mm = (int(x) for x in schedule["time"].split(":"))
    allowed = {WEEKDAYS.index(d) for d in schedule["days"]}

    # Ищем назад не больше двух недель: если прогонов не было дольше, догонять
    # уже нечего — данные всё равно устарели, нужен обычный сбор.
    for back in range(15):
        day = (now - timedelta(days=back)).replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        if day.weekday() in allowed and day <= now:
            return day
    return None


def missed_run(
    schedule: dict, last_run_at: datetime | None, now: datetime | None = None
) -> datetime | None:
    """Пропущенный слот, который стоит догнать (или None).

    Слот считается пропущенным, если он был, а последнего прогона либо нет
    вовсе, либо он раньше этого слота.
    """
    schedule = normalize(schedule)
    if not schedule["enabled"] or not schedule["catch_up"]:
        return None
    slot = previous_slot(schedule, now)
    if slot is None:
        return None
    if last_run_at is None or last_run_at < slot:
        return slot
    return None


# ── Планировщик Windows ──────────────────────────────────────────────────────


def _schtasks_days(days: list[str]) -> str:
    """Дни недели в формате schtasks."""
    mapping = {
        "mon": "MON",
        "tue": "TUE",
        "wed": "WED",
        "thu": "THU",
        "fri": "FRI",
        "sat": "SAT",
        "sun": "SUN",
    }
    return ",".join(mapping[d] for d in days)


def build_command(schedule: dict, target: str) -> list[str]:
    """Команда постановки задачи в планировщик Windows."""
    schedule = normalize(schedule)
    return [
        "schtasks",
        "/Create",
        "/F",  # перезаписать существующую задачу
        "/TN",
        TASK_NAME,
        "/TR",
        target,
        "/SC",
        "WEEKLY",
        "/D",
        _schtasks_days(schedule["days"]),
        "/ST",
        schedule["time"],
    ]


def apply(schedule: dict, target: str) -> dict:
    """Поставить или снять задачу в планировщике. Возвращает результат словами."""
    schedule = normalize(schedule)
    try:
        if not schedule["enabled"]:
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"ok": True, "message": "сбор по расписанию выключен, задача снята"}

        result = subprocess.run(
            build_command(schedule, target), capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            return {
                "ok": False,
                "message": f"планировщик Windows не принял задачу: {error}",
            }
        return {"ok": True, "message": f"задача поставлена: {describe(schedule)}"}
    except FileNotFoundError:
        # Не Windows или schtasks недоступен — расписание сохранится, но само
        # не сработает. Молчать об этом нельзя.
        return {
            "ok": False,
            "message": "планировщик Windows недоступен на этой машине — "
            "расписание сохранено, но задача не поставлена",
        }
    except Exception as e:  # pragma: no cover — неожиданные сбои ОС
        logger.exception("Не удалось применить расписание")
        return {"ok": False, "message": str(e)}


def task_exists() -> bool:
    """Стоит ли задача в планировщике сейчас."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False
