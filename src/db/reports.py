"""
Отчёты из единой базы (задача T022).

Три вопроса, на которые отвечает раздел «Отчёты», и почему именно они:

1. **Откуда приходят лоты.** Пока не видно вклада каждой площадки, нельзя
   решить, стоит ли держать дорогую в поддержке (браузерный парсер) ради
   десятка лотов. Здесь же видно, какая площадка вообще перестала отдавать
   данные — молчащая площадка выглядит как «просто ничего не нашлось».

2. **Что происходит с нашими лотами.** Воронка по шагам регламента: сколько
   взято, оценено, подано, чем закончилось. Отказы считаются отдельно и с
   причинами — по ним видно, где мы промахиваемся.

3. **Что мы вообще ищем.** Какие группы номенклатуры дают лоты, а какие
   молчат: группа без единого попадания за месяц — повод пересмотреть фразы,
   а не считать, что рынок пуст.

Отчёты считаются по всей базе, а не по последнему прогону: лот, найденный
неделю назад и до сих пор актуальный, — такая же наша работа, как сегодняшний.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ..dates import resolve_now
from ..watch_clients import watch_names
from .feed import classify_lots, is_ours


def by_platform(conn, days: int = 30) -> list[dict]:
    """Вклад каждой площадки: сколько лотов, сколько профильных, когда последний.

    `days` ограничивает окно «свежести» для отметки о молчании: площадка,
    не давшая ничего за это время, помечается явно.
    """
    rows = conn.execute(
        "SELECT platform,"
        " count(*) AS total,"
        " sum(CASE WHEN core_profile = 1 THEN 1 ELSE 0 END) AS core,"
        " sum(CASE WHEN client_level IN ('full','partial') THEN 1 ELSE 0 END) AS clients,"
        " sum(CASE WHEN prepayment = 'yes' THEN 1 ELSE 0 END) AS prepaid,"
        " max(last_seen_at) AS last_seen"
        " FROM lots GROUP BY platform ORDER BY total DESC"
    ).fetchall()

    horizon = datetime.now() - timedelta(days=days)
    out = []
    for row in rows:
        item = dict(row)
        last_seen = item.get("last_seen")
        silent = True
        if last_seen:
            try:
                silent = datetime.fromisoformat(last_seen) < horizon
            except ValueError:
                silent = False
        item["silent"] = silent
        out.append(item)
    return out


def by_group(conn) -> list[dict]:
    """Сколько лотов дала каждая группа номенклатуры.

    Группа, не давшая ничего, — не обязательно пустой рынок: возможно, у неё
    неудачные фразы поиска. Поэтому показываем и нули.
    """
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT tags FROM lots WHERE tags IS NOT NULL AND tags != ''"
    ):
        try:
            for group in json.loads(row["tags"]) or []:
                counts[group] = counts.get(group, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue
    # Человеку показываем название группы из справочника, а не внутренний
    # идентификатор: «gmo_spares» ни о чём не говорит тому, кто с лотами работает.
    titles: dict[str, str] = {}
    try:
        from ..store import load_nomenclature

        for group in load_nomenclature().get("groups", []):
            gid = str(group.get("id", ""))
            if gid:
                titles[gid] = str(group.get("title") or gid)
    except Exception:
        pass

    return [
        {"group": titles.get(name, name), "id": name, "lots": number}
        for name, number in sorted(counts.items(), key=lambda x: -x[1])
    ]


def funnel(conn) -> dict:
    """Воронка заявок по шагам регламента плюс причины отказов."""
    steps = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, count(*) n FROM applications GROUP BY status"
        )
    }
    reasons = [
        {"reason": row["reject_reason"], "count": row["n"]}
        for row in conn.execute(
            "SELECT reject_reason, count(*) n FROM applications"
            " WHERE status = 'REJECTED' AND reject_reason IS NOT NULL"
            " GROUP BY reject_reason ORDER BY n DESC LIMIT 10"
        )
    ]
    return {"steps": steps, "reject_reasons": reasons}


def deadlines(conn, today: date | None = None, now: datetime | None = None) -> dict:
    """Что горит, что на неделе, что истекло и что пропало — по ленте.

    Раскладка та же, что у ленты и Excel (feed.classify_lots): «горит» в
    отчёте и в ленте — одно число (Г4, 16.10.2026). Считаются неразобранные
    лоты: взятые в работу видны в красной зоне реестра. «Истёк» и «на
    неделе» — только по нашему (feed.is_ours): чужой истёкший лот — не
    упущенная возможность.

    «Сейчас» — текущий московский момент, а не полночь: с полуночью лот,
    истёкший сегодня утром, весь день числился горящим (Г2, 16.10.2026).
    """
    today, now = resolve_now(today, now)
    watched = watch_names()

    urgent = expired = week = missing = 0
    for lot in classify_lots(conn, now, today, watched):
        bucket = lot["bucket"]
        if bucket == "urgent":
            urgent += 1
        elif bucket == "missing":
            missing += 1
        elif not is_ours(lot, watched):
            continue
        elif bucket == "expired":
            expired += 1
        elif lot["days_left"] is not None and lot["days_left"] <= 7:
            week += 1
    return {
        "urgent": urgent,
        "this_week": week,
        "expired": expired,
        "missing": missing,
        "as_of": str(today),
    }


def summary(conn, days: int = 30) -> dict:
    """Всё вместе — один запрос для раздела «Отчёты»."""
    total = conn.execute("SELECT count(*) FROM lots").fetchone()[0]
    prepaid = conn.execute(
        "SELECT count(*) FROM lots WHERE prepayment = 'yes'"
    ).fetchone()[0]
    unknown_pay = conn.execute(
        "SELECT count(*) FROM lots WHERE prepayment = 'unknown'"
    ).fetchone()[0]
    return {
        "total_lots": total,
        "prepaid": prepaid,
        # Честно называем долю неизвестного: без этого «предоплаты почти нет»
        # читалось бы как вывод о рынке, а это вывод о полноте наших данных.
        "payment_unknown": unknown_pay,
        "platforms": by_platform(conn, days),
        "groups": by_group(conn),
        "funnel": funnel(conn),
        "deadlines": deadlines(conn),
    }
