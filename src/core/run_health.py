"""
Межпрогонная проверка площадок: сегодня против обычных ночей (ревью 16.10.2026, В3).

Сторож внутри прогона (completeness.guard_platforms) ловит только явное:
площадка упала, не отчиталась, дала ровно ноль. «Тихие» поломки он не
видит: 14–16.10.2026 Bidzaar отдавал старую ленту (новых почти ноль,
свежайшая публикация 13.10), 16.10 ТЭК-Торг принёс 12 лотов вместо
обычной сотни — и оба раза журнал писал «собрано целиком».

Здесь площадка сравнивается с медианой последних ночей (run_platforms:
lots, lots_new, newest_published — пишутся с В1). Найденное становится
отчётом «площадка целиком» (completeness.platform_unit) ДО записи
журнала: прогон честно неполный, а итог говорит «вчера N, сегодня M».

Календарь. Ночной сбор в 03:00 по часам ПК (UTC+7) — это 23:00 МСК
предыдущего дня: ночь собирает публикации ВЧЕРАШНЕГО дня (covered_day).
Новых по выходным почти нет, поэтому правила про новые лоты смотрят только
ночи, покрывающие будни. Праздники не учитываются.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from statistics import median

from ..parsers.completeness import (
    REASON_ANOMALY,
    REASON_EMPTY_PLATFORM,
    REASON_STALE,
    UNIT_PARTIAL,
    UNIT_SKIPPED,
    platform_unit,
)

# Ночи, когда площадку пометила САМА эта проверка, в эталон входят: иначе
# площадка, законно сменившая уровень (меньше фраз, сезон), тревожила бы
# вечно — эталон застыл бы на старых ночах (бэктест по журналу). Ночи,
# неполные по другой причине (обрыв связи, формат), в эталон не входят:
# их низкие цифры объяснены и не должны тянуть медиану вниз.
_OWN_REASONS = frozenset({REASON_ANOMALY, REASON_STALE, REASON_EMPTY_PLATFORM})

logger = logging.getLogger(__name__)

# Эталон — последние 7 ночей того же рода (будни/выходные); меньше 3 —
# проверка молчит (истории lots ещё нет: колонка появилась 16.10.2026).
HISTORY_NIGHTS = 7
MIN_HISTORY = 3
# Лоты до фильтров: ноль — при обычных хотя бы 5; меньше 30% обычного —
# при обычных хотя бы 20. У малых площадок (Tender.pro, Росэлторг:
# 3–20 лотов) «втрое меньше» — шум: бэктест 25.09–16.10 дал на Tender.pro
# две ложные тревоги в воскресенье при пороге 5.
MIN_MEDIAN_LOTS = 5
MIN_MEDIAN_ANOMALY = 20
ANOMALY_SHARE = 0.3
# Новые лоты (медиана по будним ночам): ноль две будние ночи подряд — лента
# стоит; свежайшая публикация старше 2 рабочих дней — тоже. Только для
# площадок, где новых обычно ≥ 10: у Росэлторга 0–2 новых в день — норма.
MIN_MEDIAN_NEW = 10
STALE_WORKDAYS = 2
# Обвал новых: ≤ 10% обычного в будний день при обычных ≥ 20. Без этого
# правила Bidzaar 14.10 (новых 2 при обычной сотне) прошёл бы молча:
# «ноль две ночи подряд» срабатывает только 16.10 (бэктест 16.10.2026).
COLLAPSE_SHARE = 0.1
MIN_MEDIAN_NEW_COLLAPSE = 20


class Verdict:
    def __init__(self, platform: str, reason: str, note: str):
        self.platform = platform
        self.reason = reason
        self.note = note

    def __repr__(self) -> str:  # для сообщений pytest
        return f"Verdict({self.platform!r}, {self.reason!r}, {self.note!r})"


def covered_day(started: datetime) -> date:
    """День публикаций, который собирает прогон: ночь — за вчера."""
    return (started - timedelta(hours=12)).date()


def is_workday(day: date) -> bool:
    return day.weekday() < 5


def workdays_between(after: date, until: date) -> int:
    """Рабочих дней в промежутке (after, until]."""
    n = 0
    day = after + timedelta(days=1)
    while day <= until:
        n += is_workday(day)
        day += timedelta(days=1)
    return n


def _started(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("started_at")))
    except ValueError:
        return None


def _ddmm(iso: str) -> str:
    try:
        return f"{date.fromisoformat(iso):%d.%m}"
    except ValueError:
        return iso


def night_history(conn, platform: str, limit: int = 30) -> list[dict]:
    """Ночные успешные прогоны площадки с известными lots, свежие первыми.

    Догоняющие прогоны не в счёт: они обходят только упавшие площадки и
    своим «новых 0» сбивали бы эталон. `complete` — была ли площадка в ту
    ночь полной: медианы считаются только по здоровым ночам.
    """
    try:
        rows = conn.execute(
            "SELECT r.started_at, p.lots, p.lots_new, p.newest_published,"
            " p.complete, p.reason_code"
            " FROM runs r JOIN run_platforms p ON p.run_id = r.id"
            " WHERE p.platform = ? AND r.outcome = 'ok'"
            " AND COALESCE(r.trigger, '') != 'catchup' AND p.lots IS NOT NULL"
            " ORDER BY r.id DESC LIMIT ?",
            (platform, limit),
        ).fetchall()
    except Exception:
        logger.exception("История прогонов площадки %s не прочиталась", platform)
        return []
    return [dict(r) for r in rows]


def check_platform(
    platform: str,
    lots: int | None,
    lots_new: int | None,
    newest: str | None,
    now: datetime,
    history: list[dict],
) -> Verdict | None:
    """Вердикт по одной площадке или None — площадка выглядит как обычно."""
    day = covered_day(now)

    def workday_night(h: dict) -> bool:
        at = _started(h)
        return at is not None and is_workday(covered_day(at))

    # complete: 0/1/NULL в базе; 0 (и False) — ночь, когда площадка недобрала.
    healthy = [
        h
        for h in history
        if h.get("complete") != 0 or h.get("reason_code") in _OWN_REASONS
    ]

    # ── Лоты до фильтров ────────────────────────────────────────────────────
    # Эталон — ночи того же рода (будни/выходные): у Tender.pro в выдаче почти
    # одни свежие публикации, и в воскресенье лотов втрое меньше — со сплошной
    # медианой бэктест дал 4 ложные тревоги на выходных.
    hist_lots = [
        int(h["lots"])
        for h in healthy
        if h.get("lots") is not None and workday_night(h) == is_workday(day)
    ][:HISTORY_NIGHTS]
    if lots is not None and len(hist_lots) >= MIN_HISTORY:
        usual = median(hist_lots)
        yesterday = history[0].get("lots") if history else None
        note = f"{platform}: лотов вчера {yesterday}, сегодня {lots} (обычно {round(usual)})"
        if lots == 0 and usual >= MIN_MEDIAN_LOTS:
            return Verdict(platform, REASON_EMPTY_PLATFORM, note)
        if usual >= MIN_MEDIAN_ANOMALY and lots < ANOMALY_SHARE * usual:
            return Verdict(platform, REASON_ANOMALY, note)

    # ── Новые лоты и свежайшая публикация — по будним ночам ─────────────────
    all_wd = [h for h in history if h.get("lots_new") is not None and workday_night(h)]
    wd_new = [
        int(h["lots_new"])
        for h in healthy
        if h.get("lots_new") is not None and workday_night(h)
    ][:HISTORY_NIGHTS]
    if len(wd_new) < MIN_HISTORY:
        return None
    usual_new = median(wd_new)

    if is_workday(day) and lots_new is not None:
        prev = all_wd[0]["lots_new"] if all_wd else None
        note = (
            f"{platform}: новых вчера {prev}, сегодня {lots_new} "
            f"(обычно {round(usual_new)})"
        )
        if lots_new == 0 and prev == 0 and usual_new >= MIN_MEDIAN_NEW:
            return Verdict(platform, REASON_STALE, note)
        if (
            usual_new >= MIN_MEDIAN_NEW_COLLAPSE
            and lots_new <= COLLAPSE_SHARE * usual_new
        ):
            return Verdict(platform, REASON_ANOMALY, note)

    if newest and usual_new >= MIN_MEDIAN_NEW:
        try:
            newest_day = date.fromisoformat(newest)
        except ValueError:
            newest_day = None
        if newest_day and workdays_between(newest_day, day) > STALE_WORKDAYS:
            return Verdict(
                platform,
                REASON_STALE,
                f"{platform}: свежайшая публикация {_ddmm(newest)} — "
                f"старше {STALE_WORKDAYS} рабочих дней",
            )
    return None


def check_run(
    conn,
    now: datetime,
    reports: list[dict],
    *,
    platform_lots: dict[str, int] | None,
    platform_new: dict[str, int] | None,
    platform_newest: dict[str, str] | None,
) -> list[str]:
    """Проверить площадки прогона; вердикты дописываются в `reports`.

    Площадку, уже неполную в этом прогоне (обрыв связи, формат, сторож),
    не проверяем: причина известна, а лишний «stale» поверх обрыва связи
    запретил бы её догон (REASONS_NEED_HUMAN). Возвращает строки для итога.
    """
    notes: list[str] = []
    for name, lots in (platform_lots or {}).items():
        if any(
            r.get("platform") == name
            and r.get("status") in (UNIT_PARTIAL, UNIT_SKIPPED)
            for r in reports
        ):
            continue
        verdict = check_platform(
            name,
            lots,
            None if platform_new is None else int(platform_new.get(name, 0)),
            (platform_newest or {}).get(name),
            now,
            night_history(conn, name),
        )
        if verdict is None:
            continue
        reports.append(platform_unit(name, verdict.reason))
        notes.append(verdict.note)
        logger.warning("Проверка по прошлым ночам: %s", verdict.note)
    return notes
