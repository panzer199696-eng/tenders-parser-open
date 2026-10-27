"""
Запись прогонов в единую базу (ТЗ-5, задача 5).

До сих пор журнал жил только в `data/runs.jsonl`: его читают соседние
программы, но базе он не принадлежал — и лист отчёта «когда собрано»
приходилось собирать из файла, который может быть перенесён, удалён или
просто не добраться до него с другой машины. Таблицы `runs` и
`run_platforms` существовали в схеме с самого начала (фича 002), но
оставались пустыми, потому что писать их было некому.

Файловый журнал НЕ отменяется: старые сборки и внешние читатели продолжают
видеть `runs.jsonl`. База становится источником истины для самой программы.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Красивое имя площадки для ключа настройки parse_*: строки с отчётами
# приходят под этими же именами, и вторая строка с «tektorg» рядом с
# «ТЭК-Торг» выглядела бы двумя разными площадками.
from ..platforms import PLATFORMS as _PLATFORMS

_PARSE_KEY_TO_PLATFORM = {p.key: p.name for p in _PLATFORMS}


def record_run(conn, record: dict) -> int | None:
    """Записать прогон в `runs` (+ строки по включённым площадкам).

    `record` — словарь формата `src.run_journal.build_record`. Возвращает id
    записи или None, если записать не удалось: журнал — важный, но не
    первичный механизм, его сбой не должен ронять завершение прогона
    (вызывающая сторона решает, что делать с исключением).
    """
    try:
        cur = conn.execute(
            "INSERT INTO runs (started_at, finished_at, outcome, trigger, app_version,"
            " lots_found, lots_new, lots_merged, data_complete, summary, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("started_at"),
                record.get("finished_at"),
                record.get("outcome"),
                record.get("trigger"),
                record.get("app_version"),
                record.get("lots_found"),
                record.get("lots_new"),
                record.get("lots_merged"),
                # SQLite хранит булево как 0/1; None оставляем NULL —
                # «неизвестно» и «ложь» в журнале полноты не одно и то же.
                None
                if record.get("data_complete") is None
                else int(record["data_complete"]),
                record.get("summary"),
                record.get("error"),
            ),
        )
        run_id = cur.lastrowid

        # Построчная полнота по площадкам (ТЗ-8): таблица заведена именно под
        # это. by_platform — сводка из run_journal.build_record: сколько
        # объектов обхода было у площадки и собран ли каждый целиком.
        seen_platforms: set[str] = set()
        for row in record.get("by_platform") or []:
            name = str(row.get("platform") or "").strip()
            if not name:
                continue
            seen_platforms.add(name.casefold())
            conn.execute(
                "INSERT OR IGNORE INTO run_platforms"
                " (run_id, platform, collected, complete, reason,"
                " lots, lots_new, newest_published, reason_code)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    name,
                    row.get("units"),
                    # SQLite хранит булево как 0/1; None оставляем NULL —
                    # «неизвестно» и «ложь» в журнале полноты не одно и то же.
                    None
                    if row.get("data_complete") is None
                    else int(row["data_complete"]),
                    # Причина — словами, не кодом: таблицу читает отчёт.
                    # Для полной площадки кладём вердикт («собрано целиком»,
                    # «площадка не сообщает объём»), чтобы отчёт не выдумывал
                    # формулировку сам и не путал «полно» с «известно».
                    str(row.get("reason") or row.get("verdict") or "").strip() or None,
                    # Живость площадки (В1): по ним прогон сравнивается
                    # с прошлыми ночами (src/core/run_health.py).
                    row.get("lots"),
                    row.get("lots_new"),
                    row.get("newest_published"),
                    row.get("reason_code"),
                ),
            )
        # Включённые площадки без отчётов (например, ГПБ — механизм полноты
        # к ней ещё не подключён) тоже получают строку: молчание площадки в
        # журнале выглядело бы «площадка не участвовала».
        for key, enabled in sorted((record.get("platforms") or {}).items()):
            if not enabled:
                continue
            # Площадка уже отчиталась — вторая строка не нужна.
            if _PARSE_KEY_TO_PLATFORM.get(key, "").casefold() in seen_platforms:
                continue
            platform = _PARSE_KEY_TO_PLATFORM.get(key) or key.removeprefix("parse_")
            if platform.casefold() in seen_platforms:
                continue
            # Площадка была включена, но о полноте не отчиталась. Ставить ей
            # общий вердикт прогона нельзя: «целиком» здесь означало бы, что
            # мы проверили её обход, а мы его не проверяли. NULL — честное
            # «неизвестно», и в отчёте оно так и читается (найдено 04.09.2026:
            # в живой базе у пяти площадок стояло «целиком» без единой цифры).
            conn.execute(
                "INSERT OR IGNORE INTO run_platforms"
                " (run_id, platform, collected, complete, reason)"
                " VALUES (?, ?, NULL, NULL, ?)",
                (run_id, platform, "площадка не отчиталась о полноте"),
            )
        conn.commit()
        return run_id
    except Exception:
        logger.exception("Не удалось записать прогон в таблицы runs/run_platforms")
        return None


def new_lots_by_platform(conn, started_at) -> dict[str, int]:
    """Сколько лотов каждой площадки впервые увидены начиная со `started_at`.

    save_lots знает только общее «новых N»; по площадке считаем по
    first_seen_at — его ставит та же save_lots (локальное время ПК, как и
    начало прогона). Параллельный прогон исключён замком сбора.
    """
    since = (
        started_at.isoformat(timespec="seconds")
        if hasattr(started_at, "isoformat")
        else str(started_at)
    )
    rows = conn.execute(
        "SELECT platform, count(*) FROM lots WHERE first_seen_at >= ? GROUP BY platform",
        (since,),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def new_lots_today(conn, now_msk: datetime | None = None) -> int:
    """Сколько лотов впервые увидено сегодня — с полуночи по Москве.

    «Сегодня» в программе московское (сроки площадок по Москве), а
    first_seen_at пишется по часам ПК (UTC+7): полночь МСК переводим в
    местное время и сравниваем строки ISO.
    """
    from ..dates import MSK
    from ..dates import now_msk as _now_msk

    day = (now_msk or _now_msk()).date()
    start = datetime(day.year, day.month, day.day, tzinfo=MSK)
    since = start.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT count(*) FROM lots WHERE first_seen_at >= ?", (since,)
    ).fetchone()
    return int(row[0])


def last_run(conn, *, skip_catchup: bool = False) -> dict | None:
    """Последний прогон из базы (для строки «когда собрано») или None.

    Читается без исключений: отсутствие таблицы runs — не ошибка, а признак
    старой базы, из которой просто ещё ничего не записано.

    ``skip_catchup`` — пропустить догоняющие прогоны: отсчёт «что принесла
    ночь» ведётся от ночного сбора, а не от догона в 07:00.
    """
    where = " WHERE COALESCE(trigger, '') != 'catchup'" if skip_catchup else ""
    try:
        row = conn.execute(
            "SELECT started_at, finished_at, outcome, summary, data_complete"
            f" FROM runs{where} ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def run_by_id(conn, run_id: int | None = None) -> dict | None:
    """Прогон по id (None — последний) или None. Читается без исключений."""
    try:
        if run_id is None:
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def _platform_rows(conn, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT platform, collected, complete, reason, lots, lots_new,"
        " newest_published, reason_code FROM run_platforms WHERE run_id = ?"
        " ORDER BY platform",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _hh_mm(raw) -> str:
    try:
        return f"{datetime.fromisoformat(str(raw)):%H:%M}"
    except ValueError:
        return "?"


def run_platforms_for(conn, run_id: int | None = None) -> list[dict]:
    """Полнота прогона по площадкам (None — последний) для отчёта.

    Догоняющий прогон в 07:00 обходит только упавшие ночью площадки, и его
    собственные строки — это две-три площадки из шести. Книге после догона
    нужна вся картина: ночные строки, где догнанные площадки заменены
    строками догона с пометкой «догнано в 07:06» (ревью 16.10.2026, В2).

    Читается мягко: у старой базы таблицы может не быть вовсе, и это не повод
    ронять сборку книги — отчёт просто не покажет блок.
    """
    try:
        run = run_by_id(conn, run_id)
        if run is None:
            return []
        rows = _platform_rows(conn, run["id"])
        if run.get("trigger") != "catchup":
            return rows
        night = conn.execute(
            "SELECT id FROM runs WHERE id < ? AND COALESCE(trigger, '') != 'catchup'"
            " ORDER BY id DESC LIMIT 1",
            (run["id"],),
        ).fetchone()
        if night is None:
            return rows
        mark = f"догнано в {_hh_mm(run.get('finished_at') or run.get('started_at'))}"
        caught = {r["platform"]: r for r in rows}
        merged = []
        for row in _platform_rows(conn, night[0]):
            again = caught.pop(row["platform"], None)
            if again is None:
                merged.append(row)
                continue
            again["reason"] = f"{again['reason']} ({mark})" if again["reason"] else mark
            merged.append(again)
        merged.extend(caught.values())
        return sorted(merged, key=lambda r: r["platform"])
    except Exception:
        logger.exception("Не удалось прочитать полноту прогона по площадкам")
        return []


def last_run_platforms(conn) -> list[dict]:
    """Полнота последнего прогона по площадкам (для отчёта) или пустой список."""
    return run_platforms_for(conn)
