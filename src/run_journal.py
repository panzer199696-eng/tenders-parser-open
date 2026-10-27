"""
Журнал прогонов: что и насколько полно собрано в прошлый раз.

Программа уже умеет считать полноту сбора (``summarize_units`` в
``parsers/completeness.py``): по каждому объекту обхода видно, обойден он
целиком, частично или пропущен, и почему. Но живёт это знание ровно до конца
прогона — дальше остаётся только строка в логе, которую никто не читает.

Из-за этого соседние программы (утренний список, рабочий стол менеджера,
пульт руководителя, вечерняя сводка) показывают «найдено 1546» так, будто это
весь рынок. На деле у рабочего аккаунта поиск по фразе обрезан площадкой, и
часть лотов просто не собрана. Руководитель принимает решения по числу,
которое ему никто не оговорил, — это и есть нечестность, которую убирает
журнал.

Что здесь есть:
  * ``write_run`` — записать итоги прогона (последний + история);
  * ``read_last_run`` / ``read_runs`` — прочитать;
  * ``describe`` — одна фраза для человека, словами, без кодов причин.

Файлы лежат рядом с базой лотов, в пользовательском каталоге:
``<user_data_dir>/data/last_run.json`` и ``<user_data_dir>/data/runs.jsonl``.
Пути вычисляются функциями, а не константами модуля: впереди переезд на
сервер, где каталог другой, и жёстко зашитый путь пришлось бы выкорчёвывать.

Секретам здесь не место: ни cookie, ни логинов, ни токенов — файл читают
другие программы, и он переживает прогон.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .runtime_paths import user_data_dir

logger = logging.getLogger(__name__)

LAST_RUN_NAME = "last_run.json"
HISTORY_NAME = "runs.jsonl"

# Больше строк в истории держать незачем: она нужна для ответа «а вчера как
# было», а не для аналитики. Файл читается целиком, поэтому пусть будет мал.
HISTORY_LIMIT = 400
# Неполных объектов в строке истории — не больше этого.
HISTORY_UNITS_LIMIT = 50

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_STOPPED = "stopped"

# Сутки — граница «свежести»: показанные цифры старше суток надо оговаривать.
_DAY = timedelta(days=1)


def journal_dir() -> Path:
    """Каталог журнала — там же, где база лотов."""
    return user_data_dir() / "data"


def last_run_path() -> Path:
    return journal_dir() / LAST_RUN_NAME


def history_path() -> Path:
    return journal_dir() / HISTORY_NAME


def _write_atomic(path: Path, text: str) -> None:
    """Запись через временный файл: оборванный прогон не оставит битый JSON.

    Читающая сторона (соседние программы) не должна падать оттого, что
    компьютер выключили посреди записи.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _trim_history(path: Path) -> None:
    """Оставить последние HISTORY_LIMIT строк."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= HISTORY_LIMIT:
        return
    _write_atomic(path, "\n".join(lines[-HISTORY_LIMIT:]) + "\n")


def write_run(record: dict) -> Path:
    """Записать итоги прогона: последний — целиком, история — строкой.

    Возвращает путь к last_run.json. Исключения не глотает: вызывающая
    сторона сама решает, что делать (в прогоне — залогировать и жить дальше,
    журнал не повод ронять сбор).
    """
    directory = journal_dir()
    directory.mkdir(parents=True, exist_ok=True)

    text = json.dumps(record, ensure_ascii=False, indent=2)
    path = directory / LAST_RUN_NAME
    _write_atomic(path, text)

    # В истории список неполных объектов обрезан: ночь с обрывом связи
    # давала сотни объектов на строку, и runs.jsonl, который читается
    # целиком, рос на мегабайты. Полный список остаётся в last_run.json.
    bad = record.get("incomplete_units") or []
    if len(bad) > HISTORY_UNITS_LIMIT:
        record = {
            **record,
            "incomplete_units": bad[:HISTORY_UNITS_LIMIT],
            "incomplete_units_total": len(bad),
        }
    line = json.dumps(record, ensure_ascii=False)
    hist = directory / HISTORY_NAME
    with hist.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _trim_history(hist)
    return path


def read_last_run(path: Path | None = None) -> dict | None:
    """Последний прогон или None.

    None означает «неизвестно»: файла нет, он битый или это не словарь.
    Молча возвращаем None, потому что отсутствие журнала — обычное дело
    (первый запуск), а не ошибка, из-за которой стоит ронять экран.
    """
    p = Path(path) if path else last_run_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_runs(limit: int = 30, path: Path | None = None) -> list[dict]:
    """Последние прогоны, свежие первыми. Битые строки пропускаются."""
    p = Path(path) if path else history_path()
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
        if len(out) >= limit:
            break
    return out


def _when(record: dict) -> str:
    """«27.08 в 03:10» — момент прогона человеческим языком."""
    raw = record.get("finished_at") or record.get("started_at") or ""
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return "неизвестно когда"
    return f"{dt:%d.%m} в {dt:%H:%M}"


def _lots_phrase(record: dict) -> str:
    found = record.get("lots_found")
    new = record.get("lots_new")
    part = f"{found} лотов" if isinstance(found, int) else "лоты не посчитаны"
    # Ноль соврёт («ничего нового»), поэтому неизвестное так и называем.
    if new is None:
        part += ", новых: неизвестно"
    elif isinstance(new, int):
        part += f", новых {new}"
    # Схлопнувшиеся дубли — часть ответа «что принёс прогон»: если дедуп
    # однажды начнёт склеивать разное, это должно быть заметно по цифре,
    # а не по жалобе человека, разобравшего один тендер дважды (ТЗ-7).
    merged = record.get("lots_merged")
    if isinstance(merged, int) and merged > 0:
        part += f", объединено дублей {merged}"
    return part


# Сколько площадок должно молчать целиком, чтобы винить связь, а не их.
NETWORK_NIGHT_MIN_PLATFORMS = 3
NETWORK_NIGHT_HINT = (
    " Похоже, этой ночью не было связи с площадками (интернет, прокси или VPN) — "
    "проверьте подключение машины."
)


def _network_night_hint(record: dict) -> str:
    """Подсказка «дело в связи», если несколько площадок молчали целиком.

    27–30.09 и 10–11.10.2026 разом отвалились Tender.pro, Росэлторг и
    Bidzaar, прогон шёл 3 часа, а итог перечислял площадки по одной
    («обрыв связи с площадкой») — будто сломались все. Одна причина
    на всех — связь этой машины, о ней и надо сказать первой.
    """
    dead = 0
    for row in record.get("by_platform") or []:
        units = row.get("units") or 0
        reason = str(row.get("reason") or "").lower()
        if units and row.get("skipped", 0) >= units and "связ" in reason:
            dead += 1
    return NETWORK_NIGHT_HINT if dead >= NETWORK_NIGHT_MIN_PLATFORMS else ""


def describe(record: dict | None) -> str:
    """Одна фраза про последний прогон — та, что показывается человеку.

    Причины неполноты называются словами («у аккаунта урезанный поиск
    площадки»), а не кодами: код `limited_account` не значит ничего для
    менеджера и тем более для руководителя. С ТЗ-8 сводка ещё и называет
    виновника по имени площадки: «Bidzaar — 2 объектов из 106» вместо
    безликого «часть объектов не собрана».
    """
    if not record:
        return "Сбор ещё не запускался — данных о полноте нет."

    outcome = record.get("outcome")
    when = _when(record)
    lots = _lots_phrase(record)

    if outcome == OUTCOME_STOPPED:
        return (
            f"Сбор {when} остановлен вручную: {lots}. "
            "Данные заведомо неполные — прогон не доведён до конца."
        )
    if outcome == OUTCOME_ERROR:
        error = str(record.get("error") or "").strip()
        tail = f" Причина: {error}." if error else ""
        return f"Сбор {when} прерван ошибкой: {lots}. Данные заведомо неполные.{tail}"

    units = record.get("units") or {}
    total = units.get("total")
    complete = units.get("complete")

    if record.get("data_complete"):
        if isinstance(total, int) and total:
            # «Целиком» и «объём неизвестен» — разные новости: площадка, не
            # сообщающая объём, могла отдать не всё (16.10.2026 журнал писал
            # «434 объектов обойдены целиком», а у 75 объём был неизвестен).
            unknown = units.get("unknown") or 0
            whole = complete if isinstance(complete, int) else total - unknown
            parts = []
            if whole:
                parts.append(f"{whole} объектов обойдены целиком")
            if unknown:
                parts.append(f"у {unknown} объём неизвестен (площадка не сообщает)")
            done = ", ".join(parts) or f"{total} объектов"
            return f"Сбор {when}: {lots}, {done} — данные полные."
        return f"Сбор {when}: {lots} — данные полные."

    # Виновники неполноты — по площадкам: «Bidzaar: 2 объектов из 106,
    # упёрлись в потолок страниц». Одна недобравшая площадка не должна
    # выглядеть обвинением всех сразу.
    culprits = []
    for row in record.get("by_platform") or []:
        if row.get("data_complete"):
            continue
        reason = str(row.get("reason") or "").strip()
        culprits.append(
            f"{row.get('platform')}: {row.get('partial', 0) + row.get('skipped', 0)} "
            f"объектов из {row.get('units', 0)}" + (f", {reason}" if reason else "")
        )
    culprit_part = f" — данные неполные: {'; '.join(culprits[:3])}." if culprits else ""
    culprit_part += _network_night_hint(record)

    bad = record.get("incomplete_units") or []
    reasons = []
    for u in bad:
        text = (u.get("reason_text") or "").strip()
        if text and text not in reasons:
            reasons.append(text)
    reason_part = f" ({'; '.join(reasons[:3])})" if reasons else ""
    if isinstance(total, int) and total:
        count = len(bad) or (total - (complete or 0))
        if culprit_part:
            return (
                f"Сбор {when}: {lots}, но {count} объектов из {total} обойдены "
                f"не полностью{culprit_part}"
            )
        return (
            f"Сбор {when}: {lots}, но {count} объектов из {total} обойдены "
            f"не полностью{reason_part} — часть лотов не собрана."
        )
    return f"Сбор {when}: {lots}, собрано не полностью{reason_part}{culprit_part}"


def is_stale(record: dict | None, now: datetime | None = None) -> bool:
    """Прогон старше суток: показанные цифры — вчерашние или старше."""
    if not record:
        return True
    raw = record.get("finished_at") or record.get("started_at") or ""
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return (now or datetime.now()) - dt > _DAY


def newest_by_platform(tenders) -> dict[str, str]:
    """Самая свежая дата публикации по площадкам (ISO ГГГГ-ММ-ДД, по Москве).

    Дата публикации у площадок в разных видах («15.10.2026 10:00»,
    «14.10.2026», ISO с поясом) — разбирается общим src.dates. Лоты без
    понятной даты пропускаются; площадка без единой даты в ответ не попадает.
    """
    from .dates import deadline_date

    newest: dict[str, str] = {}
    for tender in tenders or []:
        day = deadline_date(tender.get("date_published"))
        if day is None:
            continue
        name = str(tender.get("platform") or "")
        iso = day.isoformat()
        if iso > newest.get(name, ""):
            newest[name] = iso
    return newest


def _reason_codes(reports: list[dict], platform: str) -> list[str]:
    """Коды причин неполноты площадки без повторов, в порядке появления."""
    from .parsers.completeness import PLATFORM_UNKNOWN, UNIT_PARTIAL, UNIT_SKIPPED

    codes: list[str] = []
    for r in reports:
        if (str(r.get("platform") or "").strip() or PLATFORM_UNKNOWN) != platform:
            continue
        if r.get("status") not in (UNIT_PARTIAL, UNIT_SKIPPED):
            continue
        code = str(r.get("reason") or "")
        if code and code not in codes:
            codes.append(code)
    return codes


def _main_reason(reports: list[dict], platform: str, codes: list[str]) -> str | None:
    """Главная причина площадки: та, что требует человека, затем причина
    уровня «площадка целиком», затем первая по порядку."""
    from .parsers.completeness import KIND_PLATFORM, REASONS_NEED_HUMAN

    if not codes:
        return None
    for code in codes:
        if code in REASONS_NEED_HUMAN:
            return code
    for r in reports:
        if r.get("platform") == platform and r.get("kind") == KIND_PLATFORM:
            if r.get("reason") in codes:
                return r["reason"]
    return codes[0]


def build_record(
    *,
    started_at: datetime,
    finished_at: datetime,
    app_version: str,
    outcome: str,
    settings: dict,
    unit_reports: list[dict] | None,
    lots_found: int | None,
    lots_new: int | None,
    lots_after_filters: int | None = None,
    lots_merged: int | None = None,
    items_collected: int | None = None,
    error: str = "",
    session_warnings: list[str] | None = None,
    trigger: str | None = None,
    network_note: str = "",
    platform_lots: dict[str, int] | None = None,
    platform_new: dict[str, int] | None = None,
    platform_newest: dict[str, str] | None = None,
    health_notes: list[str] | None = None,
) -> dict:
    """Собрать запись прогона из того, что известно вызывающей стороне.

    Здесь же считается полнота: механизм живёт в parsers/completeness.py,
    но журналу нужен только итог, а не устройство отчётов. С ТЗ-8 отчёты
    приходят со всех площадок, и запись получает разбивку по ним —
    `by_platform` читают таблица `run_platforms` и лист Excel.

    `platform_lots` — лоты площадок до фильтров (ParserRunner.platform_lots),
    `platform_new` — из них впервые увиденные, `platform_newest` — ISO-дата
    самой свежей публикации (newest_by_platform).
    """
    from .parsers.completeness import (
        platform_verdict,
        reason_text,
        summarize_by_platform,
        summarize_units,
    )

    units = {"total": 0, "complete": 0, "partial": 0, "skipped": 0, "unknown": 0}
    incomplete: list[dict[str, Any]] = []
    by_platform: list[dict[str, Any]] = []
    # Пока объектов обхода нет вовсе (например, работал только keyword-режим
    # другой площадки), утверждать «полно» нельзя — но и пугать нечем:
    # считаем полным, а отсутствие проверки видно по units.total == 0.
    data_complete = True

    if unit_reports:
        s = summarize_units(unit_reports)
        units = {
            "total": len(unit_reports),
            "complete": s["complete"],
            "partial": s["partial"],
            "skipped": s["skipped"],
            "unknown": s["unknown"],
        }
        data_complete = bool(s["data_complete"])
        incomplete = [
            {
                "label": u.get("label"),
                "kind": u.get("kind"),
                "platform": u.get("platform"),
                "reason": u.get("reason"),
                "reason_text": reason_text(u.get("reason")),
                "collected": u.get("collected_lots"),
                "expected": u.get("expected_lots"),
            }
            for u in s["incomplete_units"]
        ]
        by_platform = summarize_by_platform(unit_reports)
        # Вердикт словами кладём рядом с цифрами: журнал читают люди,
        # и «полных 0 из 73» без пояснения выглядит поломкой.
        for row in by_platform:
            row["verdict"] = platform_verdict(row)
            codes = _reason_codes(unit_reports, row["platform"])
            row["reason_codes"] = codes
            row["reason_code"] = _main_reason(unit_reports, row["platform"], codes)
            # Живость площадки (В1): лоты до фильтров, впервые увиденные,
            # свежайшая публикация. None — «не знаем», не ноль: прогон мог
            # упасть до подсчёта.
            name = row["platform"]
            row["lots"] = None
            row["lots_new"] = None
            row["newest_published"] = (platform_newest or {}).get(name)
            if platform_lots is not None and name in platform_lots:
                row["lots"] = int(platform_lots[name])
                if platform_new is not None:
                    row["lots_new"] = int(platform_new.get(name, 0))

    from .platforms import KEYS as _PLATFORM_KEYS

    # Все площадки реестра: раньше список был свой, и Bidzaar в журнал
    # не попадал вовсе (найдено ревью 16.10.2026).
    platforms = {key: bool(settings.get(key)) for key in _PLATFORM_KEYS}

    # Прогон, оборванный ошибкой или остановленный человеком, не может дать
    # полные данные — что бы ни показали объекты обхода: до остальных площадок
    # он просто не дошёл. Без этой проверки в журнале стояло
    # «data_complete: true» рядом с текстом «данные заведомо неполные».
    if outcome != "ok":
        data_complete = False

    record = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "app_version": app_version,
        "outcome": outcome,
        "error": error,
        "platforms": platforms,
        "lots_found": lots_found,
        "lots_new": lots_new,
        "lots_after_filters": lots_after_filters,
        "lots_merged": lots_merged,
        # Состав закупки (ТЗ-10): сколько лотов посмотрели. None — этап не
        # дошёл до сбора состава; в таблицу runs не ездит, живёт только
        # в JSON-записи.
        "items_collected": items_collected,
        "units": units,
        "data_complete": data_complete,
        "incomplete_units": incomplete,
        "by_platform": by_platform,
        "session_warnings": list(session_warnings or []),
        # schedule | manual | catchup: догоняющий прогон в 07:00 не должен
        # сбивать отсчёт «что принесла ночь» и запускать догон сам себя.
        "trigger": trigger,
        # Площадки, не ответившие перед сбором (net_preflight): строка
        # с подсказкой про сеть едет в итог прогона.
        "network_note": network_note,
        # Межпрогонная проверка (src/core/run_health.py): «вчера N, сегодня M».
        "health_notes": list(health_notes or []),
    }
    # Готовую фразу кладём в саму запись: её читает соседняя программа
    # тендерной работы, и формулировка должна быть одна на всех, а не
    # переписана заново в каждом месте показа.
    record["summary"] = describe(record)
    if health_notes:
        record["summary"] += f" Сравнение с прошлыми ночами: {'; '.join(health_notes)}."
    # Повтор тем же кодом не поможет (формат, антибот, застывшая лента) —
    # догон их не трогает, и сказать об этом надо прямо (В4).
    from .core.catchup import platforms_need_human

    human = platforms_need_human(record)
    if human:
        names = ", ".join(f"{name} ({text})" for name, text in human)
        record["summary"] += (
            f" Повтор не поможет — нужна правка программы или проверка площадки: {names}."
        )
    if network_note:
        record["summary"] = f"{record['summary']} {network_note}".strip()
    return record


def write_run_safe(record: dict) -> Path | None:
    """Записать журнал, не роняя прогон: сбор важнее записи о сборе."""
    try:
        return write_run(record)
    except Exception:
        logger.exception("Не удалось записать журнал прогона")
        return None
