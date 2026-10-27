"""
Миграция трёх старых баз в единую `tenderwork.db` (задачи T002, T003).

Источники и что из них берём:

  • `tenders.db` (любая из двух копий) — таблица `tenders`: собранные лоты.
  • `registry.db` — таблица `applications`: 1546 записей, из которых 1543 в
    статусе FOUND. **FOUND — это не заявка, а найденный лот**, поэтому такие
    записи едут в `lots`, а в `applications` попадают только те, по которым
    действительно началась работа (взят, отклонён, оценён, подан, итог).
  • `registry.db` — таблица `history`: переход `None → FOUND` означает «лот
    впервые увиден», и он становится `lots.first_seen_at`, а не записью в
    истории заявки. Настоящие переходы статусов переносятся как есть.

Свойства миграции:

  • **Старые базы открываются только на чтение** — файлы не меняются, откат
    состоит в удалении новой базы.
  • **Идемпотентна**: повторный запуск не создаёт дублей. Лот, встреченный
    второй раз, обновляет `last_seen_at` и недостающие поля, но не затирает
    заполненное пустым — данные из разных копий базы дополняют друг друга.
  • **Отчёт словами**: что перенесено, что пропущено и почему. Молчащая
    миграция на живых данных недопустима (NFR-002).
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..dates import normalize_deadline
from .schema import connect, ensure_schema, make_lot_key

# Статус «найден» в старом реестре: такие записи — лоты, а не заявки.
STATUS_FOUND = "FOUND"


@dataclass
class MigrationReport:
    """Что сделала миграция — для показа человеку и для теста."""

    lots_added: int = 0
    lots_updated: int = 0
    lots_seen_again: int = 0
    applications_added: int = 0
    history_added: int = 0
    sources_added: int = 0
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        seen = self.lots_added + self.lots_updated + self.lots_seen_again
        lines = [
            f"Прочитано записей: {seen}",
            f"Лотов в единой базе: {self.lots_added}",
            f"Повторных встреч:  {self.lots_updated + self.lots_seen_again}"
            f" (из них дополнили данными: {self.lots_updated})",
            f"Заявок в работе:  {self.applications_added}",
            f"Записей истории:  {self.history_added}",
            f"Мест публикации:  {self.sources_added}",
        ]
        lines += [f"— {n}" for n in self.notes]
        if self.skipped:
            lines.append(f"Пропущено записей: {len(self.skipped)}")
            lines += [f"  · {s}" for s in self.skipped[:10]]
        return "\n".join(lines)


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    """Открыть старую базу только на чтение; нет файла — вернуть None."""
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _row_get(row: sqlite3.Row, name: str, default=None):
    """Значение поля, если оно есть в этой версии таблицы."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _upsert_lot(conn: sqlite3.Connection, lot: dict, report: MigrationReport) -> None:
    """Добавить лот или дополнить существующий.

    Дополнить, а не перезаписать: две копии базы содержат разные куски
    (в одной свежее время, в другой заполнен скоринг), и затирать
    заполненное пустым значило бы терять данные.
    """
    key = lot["lot_key"]
    # Одна из старых баз хранила срок в ISO («2026-08-28 06:54»): без
    # приведения 1320 лотов 27.08.2026 стали бессрочными и не истекали.
    if "date_deadline" in lot:
        lot["date_deadline"] = normalize_deadline(lot["date_deadline"])
    existing = conn.execute("SELECT * FROM lots WHERE lot_key = ?", (key,)).fetchone()
    if existing is None:
        cols = ", ".join(lot.keys())
        marks = ", ".join("?" for _ in lot)
        conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(lot.values()))
        report.lots_added += 1
        return

    updates = {}
    for col, value in lot.items():
        if col in ("lot_key", "platform", "lot_id", "created_at", "first_seen_at"):
            continue
        if value in (None, "", 0) and existing[col] not in (None, ""):
            continue
        if existing[col] in (None, "", 0) and value not in (None, ""):
            updates[col] = value
    # Позже увиденный лот двигает last_seen_at вперёд.
    seen = lot.get("last_seen_at")
    if seen and (not existing["last_seen_at"] or seen > existing["last_seen_at"]):
        updates["last_seen_at"] = seen
    if updates:
        sets = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(
            f"UPDATE lots SET {sets} WHERE lot_key = ?", [*updates.values(), key]
        )
        report.lots_updated += 1
    else:
        # Лот встречен второй раз, но нового в нём ничего нет. Это не ошибка
        # и не обновление — но в отчёте цифры обязаны сходиться, иначе
        # «прочитали 2243, перенесли 2043» выглядит как потеря данных.
        report.lots_seen_again += 1


def _add_source(conn: sqlite3.Connection, lot: dict, report: MigrationReport) -> None:
    """Запомнить место публикации лота (FR-003)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO lot_sources (lot_key, platform, href, lot_id, seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            lot["lot_key"],
            lot["platform"],
            lot.get("href"),
            lot.get("lot_id"),
            lot.get("last_seen_at"),
        ),
    )
    report.sources_added += max(0, cur.rowcount)


def _migrate_tenders(
    src: sqlite3.Connection, dst: sqlite3.Connection, report: MigrationReport
) -> None:
    """Лоты из `tenders.db`."""
    if "tenders" not in _tables(src):
        return
    count = 0
    for row in src.execute("SELECT * FROM tenders"):
        platform = _row_get(row, "platform", "")
        lot_id = str(_row_get(row, "lot_id", "") or "").strip()
        if not platform or not lot_id:
            report.skipped.append(
                f"лот без площадки или номера: {str(_row_get(row, 'name', ''))[:40]}"
            )
            continue
        created = _row_get(row, "created_at")
        lot = {
            "lot_key": make_lot_key(platform, lot_id),
            "platform": platform,
            "lot_id": lot_id,
            "name": _row_get(row, "name"),
            "company": _row_get(row, "company"),
            "href": _row_get(row, "href"),
            "sum": _row_get(row, "sum"),
            "date_published": _row_get(row, "date_published"),
            "date_deadline": _row_get(row, "date_deadline"),
            "date_to_display": _row_get(row, "date_to_display"),
            "search_key": _row_get(row, "search_key"),
            "tender_no": _row_get(row, "tender_no"),
            "tender_type": _row_get(row, "tender_type"),
            "organizer_match": _row_get(row, "organizer_match", 0),
            "organizer_href": _row_get(row, "organizer_href"),
            "tags": _row_get(row, "tags"),
            "score": _row_get(row, "score", 0),
            "promising": _row_get(row, "promising", 0),
            "first_seen_at": created,
            "last_seen_at": created,
            "created_at": created,
            "updated_at": created,
        }
        _upsert_lot(dst, lot, report)
        _add_source(dst, lot, report)
        count += 1
    report.notes.append(f"из таблицы лотов прочитано записей: {count}")


def _migrate_registry(
    src: sqlite3.Connection, dst: sqlite3.Connection, report: MigrationReport
) -> None:
    """Реестр: FOUND → лоты, остальное → заявки; история переходов."""
    tables = _tables(src)
    if "applications" not in tables:
        return

    first_seen: dict[str, str] = {}
    if "history" in tables:
        for row in src.execute(
            "SELECT lot_id, MIN(at) first_at FROM history"
            " WHERE from_status IS NULL GROUP BY lot_id"
        ):
            first_seen[str(row["lot_id"])] = row["first_at"]

    found = in_work = 0
    for row in src.execute("SELECT * FROM applications"):
        platform = _row_get(row, "platform", "")
        lot_id = str(_row_get(row, "lot_id", "") or "").strip()
        if not platform or not lot_id:
            report.skipped.append(f"заявка без площадки/номера: {lot_id or '—'}")
            continue
        key = make_lot_key(platform, lot_id)
        seen = first_seen.get(lot_id) or _row_get(row, "created_at")

        lot = {
            "lot_key": key,
            "platform": platform,
            "lot_id": lot_id,
            "name": _row_get(row, "name"),
            "company": _row_get(row, "company"),
            "href": _row_get(row, "href"),
            "sum": _row_get(row, "sum"),
            "date_deadline": _row_get(row, "deadline"),
            "score": _row_get(row, "score", 0),
            "tags": _row_get(row, "groups"),
            "first_seen_at": seen,
            "last_seen_at": _row_get(row, "updated_at") or seen,
            "created_at": _row_get(row, "created_at"),
            "updated_at": _row_get(row, "updated_at"),
        }
        _upsert_lot(dst, lot, report)
        _add_source(dst, lot, report)

        status = _row_get(row, "status", "")
        if status == STATUS_FOUND:
            found += 1
            continue

        # По лоту началась работа — это уже заявка.
        app = {
            "lot_key": key,
            "client_id": _row_get(row, "client_id"),
            "client_level": _row_get(row, "client_level"),
            "manager": _row_get(row, "manager"),
            "status": status,
            "reject_reason": _row_get(row, "reject_reason"),
            "decision": _row_get(row, "decision"),
            "decision_at": _row_get(row, "decision_at"),
            "decision_by": _row_get(row, "decision_by"),
            "assessment": _row_get(row, "assessment"),
            "volume_t": _row_get(row, "volume_t"),
            "packaging": _row_get(row, "packaging"),
            "hazard": _row_get(row, "hazard"),
            "docs_needed": _row_get(row, "docs_needed"),
            "supplier": _row_get(row, "supplier"),
            "purchase_price": _row_get(row, "purchase_price"),
            "logistics": _row_get(row, "logistics"),
            "cost": _row_get(row, "cost"),
            "markup": _row_get(row, "markup"),
            "bid_price": _row_get(row, "bid_price"),
            "price_approved_by": _row_get(row, "price_approved_by"),
            "price_approved_at": _row_get(row, "price_approved_at"),
            "submitted_at": _row_get(row, "submitted_at"),
            "submission_no": _row_get(row, "submission_no"),
            "result": _row_get(row, "result"),
            "winner_price": _row_get(row, "winner_price"),
            "deviation": _row_get(row, "deviation"),
            "loss_reason": _row_get(row, "loss_reason"),
            "contact": _row_get(row, "contact"),
            "next_touch": _row_get(row, "next_touch"),
            "note": _row_get(row, "note"),
            "created_at": _row_get(row, "created_at"),
            "updated_at": _row_get(row, "updated_at"),
        }
        cols = ", ".join(app.keys())
        marks = ", ".join("?" for _ in app)
        cur = dst.execute(
            f"INSERT OR IGNORE INTO applications ({cols}) VALUES ({marks})",
            list(app.values()),
        )
        if cur.rowcount:
            report.applications_added += 1
            in_work += 1

    report.notes.append(
        f"из реестра: {found} найденных лотов (статус FOUND) переехали в ленту, "
        f"{in_work} — реальные заявки в работе"
    )

    if "history" in tables:
        platform_by_lot = {
            str(r["lot_id"]): r["platform"]
            for r in src.execute("SELECT lot_id, platform FROM applications")
        }
        for row in src.execute(
            "SELECT * FROM history WHERE from_status IS NOT NULL ORDER BY id"
        ):
            lot_id = str(_row_get(row, "lot_id", ""))
            platform = platform_by_lot.get(lot_id)
            if not platform:
                report.skipped.append(f"история без лота: {lot_id}")
                continue
            cur = dst.execute(
                "INSERT OR IGNORE INTO history"
                " (lot_key, from_status, to_status, at, who, comment)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    make_lot_key(platform, lot_id),
                    _row_get(row, "from_status"),
                    _row_get(row, "to_status"),
                    _row_get(row, "at"),
                    _row_get(row, "who"),
                    _row_get(row, "comment"),
                ),
            )
            report.history_added += max(0, cur.rowcount)
        report.notes.append(
            "переходы «ничего → найден» не считаются историей заявки: "
            "они стали временем первой встречи лота"
        )


def migrate(sources: list[Path], target: Path) -> MigrationReport:
    """Собрать единую базу из старых. Старые файлы не изменяются."""
    report = MigrationReport()
    dst = connect(target)
    ensure_schema(dst)

    for path in sources:
        src = _open_readonly(Path(path))
        if src is None:
            report.notes.append(f"источник не найден, пропущен: {path}")
            continue
        try:
            _migrate_tenders(src, dst, report)
            _migrate_registry(src, dst, report)
        finally:
            src.close()

    dst.execute(
        "INSERT INTO meta (key, value) VALUES ('migrated_at', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    dst.commit()
    dst.close()
    return report
