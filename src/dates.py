"""
Срок подачи и «сейчас» — одно место на всю программу.

История: срок разбирался в шести местах шестью способами. Лента понимала
только «ДД.ММ.ГГГГ», и 1320 лотов с ISO-сроком из миграции стали вечными;
профиль искал дату внутри строки, а лента нет — «12.10.2026 10:00 (МСК)»
был для одного истёкшим, для другого бессрочным (16.10.2026).

Правила:
  • дата ищется внутри строки: «до 12.10.2026 10:00 (МСК)» — это срок;
  • ISO понимается, с поясом — переводится в московское время;
  • срок без времени действует до конца дня (23:59);
  • сроки площадок записаны по Москве, поэтому и «сейчас» для сравнения —
    московское (`now_msk`), а не часы ПК (ПК живёт в UTC+7: с часами ПК
    лот уходил в «Срок истёк» на 4 часа раньше).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "MSK")

_NOT_SET = ("", "Не указана")

# «12.10.2026», «12.10.2026 10:00», «12.10.2026 в 10:00», «12.10.2026, 10:00:00»
_DOTTED_RE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})"
    r"(?:[\s,]*(?:в\s*)?(\d{1,2}):(\d{2})(?::\d{2})?)?"
)
# ISO внутри строки: «2026-10-12», «2026-10-12 10:00», «2026-10-12T10:00:00»
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::\d{2})?)?")


def now_msk() -> datetime:
    """Текущее московское время (naive) — для сравнения со сроками."""
    return datetime.now(timezone.utc).astimezone(MSK).replace(tzinfo=None)


def today_msk() -> date:
    return now_msk().date()


def resolve_now(
    today: date | None = None, now: datetime | None = None
) -> tuple[date, datetime]:
    """Пара (сегодня, сейчас) по Москве для проверок срока.

    Задан момент — день берётся из него; задан только день (отчёт «на
    дату», тесты) — время текущее московское. Раньше каждое место собирало
    пару само, и при заданном моменте день брался с часов: книга на 01.06
    считала «осталось дней» от сегодняшнего числа.
    """
    if now is None:
        msk = now_msk()
        now = datetime.combine(today, msk.time()) if today else msk
    return today or now.date(), now


def _build(y, mo, d, hh, mm) -> datetime | None:
    try:
        if hh is None:
            return datetime(int(y), int(mo), int(d), 23, 59)
        return datetime(int(y), int(mo), int(d), int(hh), int(mm))
    except ValueError:
        return None


def parse_deadline(value) -> datetime | None:
    """Срок подачи как московское время (naive); None — срока нет или не понят."""
    if value is None:
        return None
    text = str(value).strip()
    if text in _NOT_SET:
        return None
    # Чистый ISO с поясом («2026-10-22T20:00:00+03:00», «…Z») — переводим
    # в Москву, а не отбрасываем пояс: часть площадок отдаёт UTC.
    if re.match(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}", text):
        try:
            at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            at = None
        if at is not None:
            if at.tzinfo is not None:
                at = at.astimezone(MSK).replace(tzinfo=None)
            return at.replace(second=0, microsecond=0)
    m = _DOTTED_RE.search(text)
    if m:
        return _build(m.group(3), m.group(2), m.group(1), m.group(4), m.group(5))
    m = _ISO_RE.search(text)
    if m:
        return _build(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
    return None


def deadline_date(value) -> date | None:
    at = parse_deadline(value)
    return at.date() if at else None


def deadline_unreadable(value) -> bool:
    """Срок указан, но программа его не понимает (лот стал бы вечным)."""
    text = str(value or "").strip()
    return text not in _NOT_SET and parse_deadline(text) is None


def normalize_deadline(value):
    """Срок к единому виду «ДД.ММ.ГГГГ ЧЧ:ММ» (или «ДД.ММ.ГГГГ» без времени).

    Непонятое значение возвращается как есть — исходный текст не теряем.
    """
    if value is None:
        return None
    text = str(value).strip()
    at = parse_deadline(text)
    if at is None:
        return value
    has_time = bool(re.search(r"\d{1,2}:\d{2}", text))
    return at.strftime("%d.%m.%Y %H:%M" if has_time else "%d.%m.%Y")
