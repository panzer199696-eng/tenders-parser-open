"""
Дедупликация одного тендера между площадками (ТЗ-7).

С приходом агрегатора Bidzaar одна и та же закупка приезжает дважды:
напрямую от площадки и через агрегатор. Человек видит два одинаковых
лота одного заказчика и разбирает их дважды.

Признак дубля — отпечаток (`fingerprint`): номер процедуры, а при его
отсутствии — связка «название + заказчик + день дедлайна». Номер надёжнее
названия: площадки копируют его из документации как есть, а название
переформулируют. Порядок убывания надёжности и есть порядок проверки.

Здесь же — `merge_duplicates`: одноразовая миграция живой базы, в которой
дубли уже накопились до появления дедупликации в `save_lots`.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Организационные формы заказчика, которые вырезаются перед сравнением:
# одна площадка пишет «ООО "РОМАШКА-СЕВЕР"», другая — «Ромашка-Север»,
# и это один заказчик. Длинные расшифровки идут раньше аббревиатур, иначе
# «публичное акционерное общество» после вырезания «ао» превратилось бы
# в «публичное общество» и перестало совпадать со своим же «ПАО».
_ORG_FORMS = (
    "общество с ограниченной ответственностью",
    "непубличное акционерное общество",
    "публичное акционерное общество",
    "акционерное общество",
    "ооо",
    "пао",
    "нао",
    "зао",
    "ао",
)


def _normalize(text) -> str:
    """Текст к каноническому виду: регистр, ё→е, только буквы и цифры.

    Пробелы, тире, кавычки и прочая пунктуация выбрасываются целиком:
    площадки оформляют один и тот же номер и как «0332200/0», и как
    «0332200-0», и сравнение «как есть» эти варианты не склеило бы.
    """
    lowered = str(text or "").strip().lower().replace("ё", "е")
    return "".join(ch for ch in lowered if ch.isalnum())


def _normalize_company(text) -> str:
    """Заказчик без организационной формы — см. `_ORG_FORMS` выше."""
    lowered = str(text or "").strip().lower().replace("ё", "е")
    for form in _ORG_FORMS:
        lowered = _remove_word(lowered, form)
    return "".join(ch for ch in lowered if ch.isalnum())


def _remove_word(text: str, word: str) -> str:
    """Вырезать слово целиком, а не как подстроку.

    Голая замена «ао» → «» превратила бы «Рао ЕЭС» в «Р ЕЭС» и задвоила
    бы сравнение; граница слова оставляет нетронутыми буквы внутри слов.
    """
    start = 0
    while True:
        idx = text.find(word, start)
        if idx < 0:
            return text
        before_ok = idx == 0 or not text[idx - 1].isalnum()
        after = idx + len(word)
        after_ok = after >= len(text) or not text[after].isalnum()
        if before_ok and after_ok:
            text = text[:idx] + " " + text[after:]
            start = idx  # строка стала короче — дальше той же позиции
        else:
            start = idx + 1


def _deadline_day(value) -> str:
    """День дедлайна «ГГГГ-ММ-ДД» без времени; пустая строка — не разобран.

    Время отбрасывается сознательно: площадки показывают одно и то же
    окончание приёма в своих часовых поясах, и минуты у них расходятся.
    Склеивать по минутам значило бы терять дубли; разные дни — это
    разные закупки, и они останутся разными.
    """
    from ..dates import parse_deadline  # один разбор срока на всю программу

    at = parse_deadline(str(value or "").strip())
    return at.date().isoformat() if at else ""


def fingerprint(tender: dict) -> str | None:
    """Отпечаток тендера: один и тот же — значит, одна закупка.

    1. Номер процедуры `no:` — самый точный признак: на живой базе
       03.09.2026 он давал 283 группы дублей против 215 у названия.
    2. Название + заказчик + день дедлайна `nm:` — запасной признак,
       когда площадки не отдали номер.
    3. Нет ни номера, ни названия → None: нечего сравнивать, лот
       в схлопывании не участвует.
    """
    number = _normalize(tender.get("tender_no"))
    if number:
        return f"no:{number}"

    name = _normalize(tender.get("name"))
    if name:
        company = _normalize_company(tender.get("company"))
        return f"nm:{name}|{company}|{_deadline_day(tender.get('date_deadline'))}"

    return None


# Поля, которые миграция может дополнить из схлопнутого дубля. Оценка
# профиля и признаки разбора сюда не входят: они посчитаны по основному
# лоту, и заменять их данными второй публикации значило бы смешивать
# два разных расчёта.
_FILLABLE = (
    "name",
    "company",
    "inn",
    "href",
    "sum",
    "date_published",
    "date_deadline",
    "date_to_display",
    "tender_no",
    "tender_type",
    "organizer_href",
)

_EMPTY = (None, "", "Не указана", 0)


def _database_path(conn: sqlite3.Connection) -> Path | None:
    """Файл основной базы из самого соединения; None для памяти."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2] and row[2] != ":memory:":
            return Path(row[2])
    return None


def _backup_database(path: Path | None, now: datetime | None = None) -> str | None:
    """Копия базы рядом с файлом: `tenderwork.db.bak-ГГГГММДД-ЧЧММ`.

    Схлопывание меняет базу без возможности отката внутри SQLite, а база
    у пользователя одна. Файловая копия — единственный честный откат.
    """
    if path is None:
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return str(target)


def _fill_from_duplicate(conn, key: str, values: dict, dup) -> int:
    """Дополнить пустые поля основного лота данными второй публикации.

    Заполненное не перетирается: если у первоисточника цена уже стоит,
    цена агрегатора её не заменяет — менять смысл цифры без причины
    нельзя. `values` — текущее состояние полей основного лота (меняется
    на месте, чтобы второй дубль группы сравнивался уже с дополненным).
    """
    updates = {}
    for col in _FILLABLE:
        if values.get(col) in _EMPTY and dup[col] not in _EMPTY:
            updates[col] = dup[col]
            values[col] = dup[col]
    if updates:
        updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(
            f"UPDATE lots SET {sets} WHERE lot_key = ?", [*updates.values(), key]
        )
    return len(updates)


def _move_sources(conn, primary_key: str, dup) -> None:
    """Перенести места публикации дубля на основной лот.

    «Схлопнули» не значит «удалили молча»: площадка и ссылка второй
    публикации переезжают в `lot_sources`, иначе человек потеряет
    переход на агрегатор, где лот мог выглядеть иначе. Строки самого
    дубля и всё, что успело накопиться в его `lot_sources`, едут туда же.
    """
    conn.execute(
        "INSERT OR IGNORE INTO lot_sources (lot_key, platform, href, lot_id, seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (primary_key, dup["platform"], dup["href"], dup["lot_id"], dup["last_seen_at"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO lot_sources (lot_key, platform, href, lot_id, seen_at)"
        " SELECT ?, platform, href, lot_id, seen_at FROM lot_sources WHERE lot_key = ?",
        (primary_key, dup["lot_key"]),
    )


def merge_duplicates(conn, db_path=None) -> dict:
    """Схлопнуть дубли в существующей базе (одноразовая миграция ТЗ-7).

    Правила безопасности:

    • Лот, по которому есть запись в `applications`, остаётся основным:
      реестр ссылается на его `lot_key`. Если таких в группе два — группу
      не трогаем вовсе и пишем предупреждение: человек взял в работу оба,
      и решать, какой из них «лишний», может только он.
    • `lot_key` оставшихся лотов не меняется никогда — на него ссылается
      реестр и история.
    • Идемпотентна: повторный запуск не находит дублей и не меняет строк.

    Возвращает отчёт словарём: сколько групп схлопнуто, сколько лотов
    удалено, какие группы пропущены и куда лёг бэкап.
    """
    report = {
        "groups": 0,
        "merged": 0,
        "filled": 0,
        "skipped_groups": [],
        "keys_written": 0,
        "backup": None,
    }

    rows = conn.execute("SELECT rowid AS _rid, * FROM lots ORDER BY rowid").fetchall()
    if not rows:
        return report

    in_registry = {str(r[0]) for r in conn.execute("SELECT lot_key FROM applications")}

    # Проставляем отпечатки всем лотам заранее: будущие прогоны будут
    # искать дубли по колонке, а не пересчитывать базу каждый раз.
    keys_to_write = []
    groups: dict[str, list] = {}
    for row in rows:
        fp = fingerprint(dict(row))
        if fp is None:
            continue
        if row["dedup_key"] != fp:
            keys_to_write.append((fp, row["lot_key"]))
        groups.setdefault(fp, []).append(row)

    # Группы с реальным схлопыванием: больше одного лота с отпечатком.
    plans = []
    for fp, members in groups.items():
        if len(members) < 2:
            continue
        with_app = [m for m in members if m["lot_key"] in in_registry]
        if len(with_app) >= 2:
            # Оба лота в работе: выбирать «главного» за человека нельзя.
            report["skipped_groups"].append([m["lot_key"] for m in members])
            logger.warning(
                "Группа дублей не тронута: %d лотов уже в работе (%s). "
                "Решить, какой из них основной, может только человек.",
                len(with_app),
                ", ".join(m["lot_key"] for m in with_app),
            )
            continue
        # Основной — лот из реестра (на него ссылки), иначе первый увиденный.
        primary = with_app[0] if with_app else members[0]
        dups = [m for m in members if m["lot_key"] != primary["lot_key"]]
        plans.append((fp, primary, dups))

    if not plans and not keys_to_write:
        # Нечего делать — база уже приведена. Второй запуск приходит сюда
        # и не создаёт ни бэкапа, ни изменений: в этом смысл идемпотентности.
        return report

    if plans:
        path = Path(db_path) if db_path else _database_path(conn)
        try:
            report["backup"] = _backup_database(path)
        except OSError:
            logger.exception("Бэкап базы перед схлопыванием не создан")
            report["skipped_groups"].extend(
                [m["lot_key"] for _, _, d in plans for m in d]
            )
            # Без бэкапа схлопывать нельзя: это единственный путь отката.
            conn.rollback()
            return report

    for fp, key in keys_to_write:
        conn.execute("UPDATE lots SET dedup_key = ? WHERE lot_key = ?", (fp, key))
        report["keys_written"] += 1

    for fp, primary, dups in plans:
        key = primary["lot_key"]
        # Собственная площадка основного лота тоже должна быть в источниках:
        # старые базы писали lot_sources не всегда, а карточка показывает
        # «где публикуется» именно по этой таблице.
        conn.execute(
            "INSERT OR IGNORE INTO lot_sources (lot_key, platform, href, lot_id, seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                key,
                primary["platform"],
                primary["href"],
                primary["lot_id"],
                primary["last_seen_at"],
            ),
        )
        values = {col: primary[col] for col in _FILLABLE}
        for dup in dups:
            _move_sources(conn, key, dup)
            report["filled"] += _fill_from_duplicate(conn, key, values, dup)
            # Удаляем только теперь: источники уже переехали, пустые поля
            # дополнены, и запись о лоте не исчезает бесследно.
            conn.execute("DELETE FROM lots WHERE lot_key = ?", (dup["lot_key"],))
            report["merged"] += 1
        report["groups"] += 1

    conn.commit()
    if report["merged"]:
        logger.info(
            "Схлопнуто дублей: %d (групп %d), дополнено полей %d, бэкап: %s",
            report["merged"],
            report["groups"],
            report["filled"],
            report["backup"] or "—",
        )
    return report


def promote_primary_sources(conn) -> int:
    """Выправить «лицо» у лотов, схлопнутых до правила приоритета площадок.

    Лот, у которого среди публикаций есть более приоритетная площадка,
    должен числиться за ней (правило владельца от 04.09.2026: первоисточник
    главнее агрегатора). `lot_key` при этом не трогаем.
    """
    from ..platforms import source_rank as rank

    rows = conn.execute(
        "SELECT l.lot_key, l.platform FROM lots l"
        " WHERE (SELECT count(*) FROM lot_sources s WHERE s.lot_key = l.lot_key) > 1"
    ).fetchall()

    fixed = 0
    for row in rows:
        sources = conn.execute(
            "SELECT platform, href, lot_id FROM lot_sources WHERE lot_key = ?",
            (row["lot_key"],),
        ).fetchall()
        best = min(sources, key=lambda s: rank(str(s["platform"] or "")))
        if rank(str(best["platform"])) < rank(str(row["platform"] or "")):
            conn.execute(
                "UPDATE lots SET platform = ?, href = COALESCE(?, href),"
                " lot_id = COALESCE(?, lot_id) WHERE lot_key = ?",
                (best["platform"], best["href"], best["lot_id"], row["lot_key"]),
            )
            fixed += 1

    if fixed:
        conn.commit()
        logger.info("Лицо лота выправлено по приоритету площадок: %d", fixed)
    return fixed
