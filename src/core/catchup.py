"""
Догоняющий прогон после неполной ночи (12.10.2026).

Ночной сбор в 03:00 мог потерять площадку, и до следующей ночи её лотов
не было. В 07:00 планировщик запускает ``main_ui.py --collect
--retry-incomplete``: программа читает журнал ночи и повторно обходит
площадки, которым повтор может помочь (ревью 16.10.2026, В4):

  • прогон упал ошибкой или остановлен — все включённые площадки, у которых
    нет полного отчёта (до них ночь могла просто не дойти);
  • обрыв связи, «подозрительный ноль», аномалия, таймаут — один раз:
    догон после догона не запускается.

Причины из REASONS_NEED_HUMAN (формат ответа, антибот, застывшая лента) не
догоняются: тот же код даст тот же результат, нужна правка программы или
проверка площадки — об этом говорит итог прогона. Потолок страниц и
протухший вход повтор тоже не вылечит.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..parsers.completeness import (
    REASON_ANOMALY,
    REASON_EMPTY_PLATFORM,
    REASON_NETWORK,
    REASON_REQUEST_CAP,
    REASON_TIMEOUT,
    REASONS_NEED_HUMAN,
    reason_text,
)
from ..platforms import BY_KEY, key_of

TRIGGER_CATCHUP = "catchup"

# Догоняем только свежую ночь: вчерашний неполный прогон к обеду уже не
# актуален, а пропущенную ночь целиком догоняет запуск программы (FR-006).
MAX_AGE = timedelta(hours=12)

# Причины, при которых повторный обход может помочь.
REASONS_RETRY = frozenset(
    {
        REASON_NETWORK,
        REASON_EMPTY_PLATFORM,
        REASON_ANOMALY,
        REASON_TIMEOUT,
        # Ночной лимит запросов ТЭК-Торга: утренний заход добирает хвост
        # (состояние обхода хранит, на чём остановились).
        REASON_REQUEST_CAP,
    }
)


def _reasons_by_platform(record: dict) -> dict[str, list[str]]:
    """Коды причин неполноты по площадкам — из списка неполных объектов
    и из строк by_platform (там с В1 лежат reason_codes)."""
    out: dict[str, list[str]] = {}
    for u in record.get("incomplete_units") or []:
        name = u.get("platform")
        if name and u.get("reason"):
            out.setdefault(name, []).append(u["reason"])
    for row in record.get("by_platform") or []:
        name = row.get("platform")
        for code in row.get("reason_codes") or []:
            out.setdefault(name, []).append(code)
    return out


def platforms_need_human(record: dict | None) -> list[tuple[str, str]]:
    """Площадки, которым повтор не поможет: (имя, причина словами)."""
    if not record:
        return []
    out = []
    for name, codes in _reasons_by_platform(record).items():
        human = [code for code in codes if code in REASONS_NEED_HUMAN]
        if human:
            out.append((name, reason_text(human[0])))
    return out


def platforms_to_retry(record: dict | None, now: datetime | None = None) -> list[str]:
    """Ключи настроек parse_* для повторного обхода (пусто — догонять нечего)."""
    if not record or record.get("data_complete"):
        return []
    # Догон после догона не запускаем: если площадка не ответила и в 07:00,
    # третья попытка подряд — это уже не сбой связи, а повод смотреть сеть.
    if record.get("trigger") == TRIGGER_CATCHUP:
        return []
    try:
        started = datetime.fromisoformat(str(record.get("started_at")))
    except ValueError:
        return []
    if (now or datetime.now()) - started > MAX_AGE:
        return []

    reasons = _reasons_by_platform(record)
    human = {name for name, _text in platforms_need_human(record)}
    names: set[str] = set()
    if record.get("outcome") in ("error", "stopped"):
        # Прогон не дошёл до конца: догоняем всё включённое, кроме площадок
        # с полным отчётом (их лоты уже в базе).
        done = {
            row.get("platform")
            for row in record.get("by_platform") or []
            if row.get("data_complete")
        }
        for key, enabled in (record.get("platforms") or {}).items():
            name = _name_of(key)
            if enabled and name and name not in done:
                names.add(name)
    for name, codes in reasons.items():
        if any(code in REASONS_RETRY for code in codes):
            names.add(name)
    keys = {key_of(name) for name in names - human}
    return sorted(k for k in keys if k)


def _name_of(key: str) -> str | None:
    platform = BY_KEY.get(key)
    return platform.name if platform else None
