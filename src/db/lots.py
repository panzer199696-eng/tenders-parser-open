"""
Запись собранных лотов в единую базу (задачи T013, T014, T015).

Что здесь принципиально нового по сравнению со старой `Database.save_tenders`:

1. **Оценка считается при сборе, а не при показе.** Раньше скоринг жил в
   интерфейсе второй программы: лот попадал в базу «голым», а объяснение
   «почему он в списке» собиралось заново при каждом открытии страницы —
   и в Excel, например, не попадало вовсе. Теперь оценка и её расшифровка
   ложатся в базу вместе с лотом (FR-014).

2. **Лот, встреченный снова, не задваивается и не теряет историю.** Повторная
   встреча двигает `last_seen_at` и дополняет пустые поля, но не затирает
   заполненное: площадка во второй раз может отдать меньше данных, чем в
   первый.

3. **Лот ниже профильного порога не выбрасывается, а помечается** (FR-014,
   T015). Порог у каждой группы номенклатуры свой: закупка расходников на
   300 тыс. ₽ — наша, а тот же порог для оборудования означал бы мелочь.
   Молча выброшенный лот — это лот, о котором никто не узнает.
"""

import json
import logging
from datetime import datetime

from ..dates import deadline_unreadable, normalize_deadline
from ..platforms import source_rank
from ..prepayment import detect as detect_prepayment
from .dedup import fingerprint
from .schema import make_lot_key

logger = logging.getLogger(__name__)


def _as_int(value) -> int:
    return 1 if value else 0


def _tender_row(tender: dict, verdict=None) -> dict:
    """Лот парсера + разбор профиля → строка таблицы `lots`."""
    platform = str(tender.get("platform") or "").strip()
    lot_id = str(tender.get("lot_id") or "").strip()
    now = datetime.now().isoformat(timespec="seconds")

    row = {
        "lot_key": make_lot_key(platform, lot_id),
        "platform": platform,
        "lot_id": lot_id,
        "name": tender.get("name"),
        "company": tender.get("company"),
        "inn": tender.get("inn") or "",
        "href": tender.get("href"),
        "sum": tender.get("sum"),
        "date_published": tender.get("date_published") or "Не указана",
        # Срок — в едином формате: его читают лента, Excel, профиль и склейка
        # дублей, и формат, понятый не всеми, делает лот вечным (16.10.2026).
        "date_deadline": normalize_deadline(tender.get("date_deadline"))
        or "Не указана",
        "date_to_display": tender.get("date_to_display"),
        "search_key": tender.get("search_key"),
        "tender_no": tender.get("tender_no"),
        "tender_type": tender.get("tender_type"),
        "organizer_match": _as_int(tender.get("organizer_match")),
        "organizer_href": tender.get("organizer_href"),
        "first_seen_at": now,
        "last_seen_at": now,
        "created_at": now,
        "updated_at": now,
    }

    # Условия оплаты — из текста лота (FR-012). Во вложения пока не смотрим,
    # поэтому «не найдено» остаётся честным третьим состоянием, а не «нет».
    state, quote = detect_prepayment(
        tender.get("name"), tender.get("description"), tender.get("extra_info")
    )
    row["prepayment"] = state
    row["prepayment_note"] = quote or None

    if verdict is not None:
        row["score"] = int(verdict.score or 0)
        # Объяснение словами — то, ради чего всё затевалось: «Насосы +30 ·
        # закреплённый клиент +40 · совпало: „насос центробежный“».
        row["score_explain"] = verdict.explain()
        row["tags"] = json.dumps(verdict.groups or [], ensure_ascii=False)
        row["client_id"] = getattr(verdict, "client_id", None)
        row["client_level"] = getattr(verdict, "client_level", None)
        # «Перспективный» — свойство самого лота: наш профиль или закреплённый
        # заказчик. Срочность («горит») сюда НЕ пишем: она зависит от того,
        # какое сегодня число, и сохранённая протухла бы к следующему утру.
        # Её считает лента при показе — см. src/db/feed.py.
        row["promising"] = _as_int(
            getattr(verdict, "core_profile", False)
            or getattr(verdict, "client_level", None) in ("full", "partial")
        )
        row["core_profile"] = _as_int(getattr(verdict, "core_profile", False))
        row["below_threshold"] = _as_int(getattr(verdict, "below_threshold", False))
        # Признаки разбора (ТЗ-5): лента и отчёты делят лоты по ним на
        # «горит / профиль / клиенты / на проверку / не наш профиль». Без
        # записи в базу они жили бы до конца прогона — и каждый вечер
        # «слабые» лоты снова становились бы «прочими».
        row["match_strength"] = str(getattr(verdict, "strength", "none") or "none")
        row["industry"] = str(getattr(verdict, "industry", "unknown") or "unknown")
        row["needs_review"] = _as_int(getattr(verdict, "needs_review", False))
        row["off_industry"] = _as_int(getattr(verdict, "off_industry", False))
        row["off_profile"] = _as_int(getattr(verdict, "off_profile", False))
        # matched_terms — заготовка для обучения на решениях пользователя:
        # сериализуем сразу, чтобы база хранила ровно то, что вернул профиль.
        matched = getattr(verdict, "matched_terms", None) or []
        row["matched_terms"] = (
            json.dumps(matched, ensure_ascii=False) if matched else None
        )
    else:
        row["score"] = int(tender.get("score") or 0)
        row["tags"] = json.dumps(tender.get("tags") or [], ensure_ascii=False)
        row["promising"] = _as_int(tender.get("promising"))

    return row


# Поля, которые нельзя перезаписывать при повторной встрече лота.
_IMMUTABLE = ("lot_key", "platform", "lot_id", "created_at", "first_seen_at")

# Поля вердикта, которые обновляются даже в пустую: это результат пересчёта,
# а не «площадка во второй раз отдала меньше». Если справочник изменился и
# совпадений больше нет, хранить вчерашний список matched_terms — враньё.
_VERDICT_OVERWRITE_EMPTY = ("matched_terms",)


def _apply_updates(
    conn, existing, row: dict, key: str, fill_only: bool = False
) -> None:
    """Обновить существующий лот данными новой встречи.

    Одна логика для двух случаев: повторная встреча того же лота и
    публикация той же закупки с другой площадки. `fill_only` отличает их:
    площадка не ниже «лица» лота может поправить срок и цену (данные
    актуализировались), а вот публикация агрегатора только дополняет
    пустое — цена агрегатора не перетирает цену первоисточника (ТЗ-7).
    """
    updates = {
        "last_seen_at": row["last_seen_at"],
        "updated_at": row["updated_at"],
    }
    for col, value in row.items():
        if col in _IMMUTABLE or col in updates or col == "dedup_key":
            continue
        # Пустым значением заполненное поле не затираем: во второй раз
        # площадка может отдать меньше, чем в первый. Исключение —
        # поля пересчёта: пустой вердикт честнее устаревшего.
        if value in (None, "", "Не указана") and col not in _VERDICT_OVERWRITE_EMPTY:
            continue
        if fill_only and col not in _VERDICT_OVERWRITE_EMPTY:
            if existing[col] not in (None, "", "Не указана"):
                continue
        if existing[col] != value:
            updates[col] = value
    # Отпечаток — по итоговому лоту, а не по последней публикации: площадка,
    # однажды отдавшая лот без номера процедуры, не должна подменять
    # точный «no:» слабым «nm:» — по нему лот потом не нашёлся бы.
    merged = {**dict(existing), **updates}
    dedup_key = fingerprint(merged)
    if dedup_key and dedup_key != existing["dedup_key"]:
        updates["dedup_key"] = dedup_key
    sets = ", ".join(f"{c} = ?" for c in updates)
    conn.execute(f"UPDATE lots SET {sets} WHERE lot_key = ?", [*updates.values(), key])


def _better_face(new_platform: str, current_platform: str) -> bool:
    """Должна ли новая публикация стать «лицом» лота.

    Правило владельца от 04.09.2026: при дублях первоисточники по порядку
    приоритета (src/platforms.py), агрегатор — последним. Причина простая: на
    первоисточнике полная документация и там же подаётся заявка, а агрегатор
    показывает лишь витрину.

    Раньше лицом становилась та площадка, которая пришла первой, — а порядок
    случаен: Bidzaar обходится за пять минут, другая площадка за полчаса, и в другой день
    тот же лот числился бы за агрегатором.

    Меняются только отображаемые поля (площадка, ссылка, номер процедуры).
    `lot_key` остаётся прежним всегда: на него ссылаются заявки и история.
    """
    return source_rank(new_platform) < source_rank(current_platform)


def _find_existing(conn, row: dict):
    """Лот, к которому относится публикация, и признак «это чужой лот».

    Порядок — от точного к приблизительному:
      1. `lot_key` — лот, заведённый этой же публикацией;
      2. (площадка, номер лота) — публикация стала лицом чужого лота
         (UNIQUE-индекс: вставка такой строки роняла бы запись);
      3. место публикации в `lot_sources` — публикация уже склеена с лотом,
         даже если сегодня площадка не отдала номер процедуры;
      4. отпечаток `dedup_key` — та же закупка с другой площадки.
    """
    found = conn.execute(
        "SELECT * FROM lots WHERE lot_key = ?", (row["lot_key"],)
    ).fetchone()
    if found is not None:
        return found, False
    found = conn.execute(
        "SELECT * FROM lots WHERE platform = ? AND lot_id = ?",
        (row["platform"], row["lot_id"]),
    ).fetchone()
    if found is not None:
        return found, False
    found = conn.execute(
        "SELECT l.* FROM lot_sources s JOIN lots l ON l.lot_key = s.lot_key"
        " WHERE s.platform = ? AND s.lot_id = ? LIMIT 1",
        (row["platform"], row["lot_id"]),
    ).fetchone()
    if found is not None:
        return found, True
    if row["dedup_key"]:
        found = conn.execute(
            "SELECT * FROM lots WHERE dedup_key = ? LIMIT 1", (row["dedup_key"],)
        ).fetchone()
        if found is not None:
            return found, True
    return None, False


def _save_one(conn, row: dict) -> tuple[bool, bool]:
    """Записать одну публикацию. Возвращает (новый лот, склеен с чужим)."""
    platform = row["platform"]
    existing, is_merged = _find_existing(conn, row)

    if existing is None:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))
        target = row["lot_key"]
    else:
        # `lot_key` основного лота не меняется никогда — на него
        # ссылаются applications и history.
        target = existing["lot_key"]
        face = str(existing["platform"] or "")
        # Права обновления — по приоритету площадки, а не по совпадению
        # ключа: когда лицом лота стал первоисточник, его продлённый срок и новая
        # цена обязаны дойти до лота, даже если ключ остался от агрегатора,
        # а вчерашние данные агрегатора — не откатывать их назад. Полные
        # права — у самого лица и у более приоритетной площадки; вторая
        # публикация той же площадки под другим номером лишь дополняет.
        better = _better_face(platform, face)
        is_face = platform == face and row["lot_id"] == str(existing["lot_id"] or "")
        _apply_updates(conn, existing, row, target, fill_only=not (better or is_face))
        # Публикация с более приоритетной площадки становится лицом лота:
        # человек должен идти на первоисточник, а не на агрегатор (см. _better_face).
        # Конфликта с UNIQUE (platform, lot_id) здесь нет: лот с таким лицом
        # нашёлся бы в _find_existing вторым шагом.
        if better:
            conn.execute(
                "UPDATE lots SET platform = ?, href = COALESCE(NULLIF(?, ''), href),"
                " lot_id = ?, tender_no = COALESCE(NULLIF(?, ''), tender_no)"
                " WHERE lot_key = ?",
                (
                    platform,
                    row.get("href"),
                    row["lot_id"],
                    row.get("tender_no"),
                    target,
                ),
            )

    # Место публикации пишется под ключ ОСНОВНОГО лота: карточка показывает
    # «где ещё публикуется» именно по этой таблице. seen_at двигается при
    # каждой встрече — по нему видно, что площадка лот больше не показывает.
    conn.execute(
        "INSERT INTO lot_sources (lot_key, platform, href, lot_id, seen_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (lot_key, platform) DO UPDATE SET"
        " seen_at = excluded.seen_at,"
        " href = COALESCE(NULLIF(excluded.href, ''), lot_sources.href),"
        " lot_id = excluded.lot_id",
        (target, platform, row.get("href"), row["lot_id"], row["last_seen_at"]),
    )
    return existing is None, is_merged


def save_lots(conn, tenders: list[dict], profile=None) -> dict:
    """Сохранить лоты, посчитав оценку профилем. Возвращает сводку.

    `profile` — объект с методом `evaluate(tender) -> Verdict`. Если он не
    передан, лоты сохраняются без оценки (так работает импорт из старых баз).

    Одна закупка с двух площадок сохраняется одним лотом (ТЗ-7): агрегаторы
    дублируют то, что мы и так собираем напрямую, а человек не должен
    разбирать один лот дважды. Вторая публикация становится строкой
    в `lot_sources`, её площадка и ссылка не теряются.
    """
    added = updated = scored = skipped = merged = failed = 0
    # Сроки, которые программа не поняла, — по площадкам, с примером.
    # Лот с таким сроком не истекает никогда; молчать об этом нельзя.
    bad_deadlines: dict[str, list[str]] = {}

    # Вся пачка — одна транзакция (быстро и атомарно для прогона), строки
    # внутри — точки отката. Без явного BEGIN первая же RELEASE закрывала
    # бы транзакцию, и каждая строка писалась бы на диск отдельно.
    if not conn.in_transaction:
        conn.execute("BEGIN")

    for tender in tenders:
        platform = str(tender.get("platform") or "").strip()
        lot_id = str(tender.get("lot_id") or "").strip()
        if not platform or not lot_id:
            skipped += 1
            continue
        if deadline_unreadable(tender.get("date_deadline")):
            bad_deadlines.setdefault(platform, []).append(
                str(tender.get("date_deadline"))
            )

        verdict = None
        if profile is not None:
            try:
                verdict = profile.evaluate(tender)
                scored += 1
            except Exception as e:
                # Ошибка оценки не должна стоить нам лота: сохраняем без неё.
                logger.warning(
                    "Профиль не смог оценить лот %s: %s",
                    str(tender.get("name", "?"))[:50],
                    e,
                )

        # Каждая строка — в своей точке отката: строка, которую база не
        # приняла, откатывается целиком (лот и место публикации), а пачка
        # пишется дальше. Раньше одно IntegrityError роняло запись всей
        # ночи (найдено ревью данных 16.10.2026).
        conn.execute("SAVEPOINT save_lot")
        try:
            row = _tender_row(tender, verdict)
            row["dedup_key"] = fingerprint(tender)
            is_new, is_merged = _save_one(conn, row)
        except Exception as e:
            conn.execute("ROLLBACK TO SAVEPOINT save_lot")
            conn.execute("RELEASE SAVEPOINT save_lot")
            failed += 1
            logger.warning("Лот %s|%s не записан в базу: %s", platform, lot_id, e)
            continue
        conn.execute("RELEASE SAVEPOINT save_lot")
        if is_new:
            added += 1
        else:
            updated += 1
        if is_merged:
            merged += 1

    conn.commit()
    summary = {
        "added": added,
        "updated": updated,
        "scored": scored,
        "skipped": skipped,
        "failed": failed,
        "merged": merged,
        "bad_deadline": sum(len(v) for v in bad_deadlines.values()),
        "bad_deadline_by_platform": {
            p: {"count": len(v), "example": v[0]} for p, v in bad_deadlines.items()
        },
    }
    for platform_name, values in bad_deadlines.items():
        logger.warning(
            "%s: срок не разобран у %d лотов (пример: %r) — такие лоты не истекут",
            platform_name,
            len(values),
            values[0],
        )
    logger.info(
        "База: новых лотов %d, обновлено %d, оценено профилем %d%s%s%s",
        added,
        updated,
        scored,
        f", объединено дублей {merged}" if merged else "",
        f", пропущено без номера {skipped}" if skipped else "",
        f", НЕ ЗАПИСАНО {failed}" if failed else "",
    )
    return summary


def rescore_lots(conn, profile, only_missing: bool = True) -> dict:
    """Пересчитать оценку профилем для лотов, у которых её нет.

    Нужно после миграции: лоты, собранные до появления профиля в конвейере,
    приехали без оценки и без объяснения — в ленте они все падали в «Прочее».
    Пересчёт делает их видимыми там, где им место.

    `only_missing=False` пересчитывает всё — это нужно, когда изменился
    справочник номенклатуры и прежние оценки устарели.
    """
    query = "SELECT * FROM lots"
    if only_missing:
        query += " WHERE score_explain IS NULL OR score_explain = ''"

    updated = failed = 0
    for row in conn.execute(query).fetchall():
        tender = {
            "name": row["name"],
            "company": row["company"],
            "sum": row["sum"],
            "date_deadline": row["date_deadline"],
        }
        try:
            verdict = profile.evaluate(tender)
        except Exception as e:
            failed += 1
            logger.warning("Пересчёт: лот %s не оценён (%s)", row["lot_key"], e)
            continue
        # Заодно перечитываем условия оплаты: лоты, собранные до появления
        # детектора, приехали с «unknown» просто потому, что их никто не смотрел.
        state, quote = detect_prepayment(row["name"])

        conn.execute(
            "UPDATE lots SET score = ?, score_explain = ?, tags = ?, client_id = ?,"
            " client_level = ?, core_profile = ?, below_threshold = ?, promising = ?,"
            " match_strength = ?, industry = ?, needs_review = ?, off_industry = ?,"
            " off_profile = ?, matched_terms = ?,"
            " prepayment = ?, prepayment_note = ?,"
            " updated_at = ? WHERE lot_key = ?",
            (
                int(verdict.score or 0),
                verdict.explain(),
                json.dumps(verdict.groups or [], ensure_ascii=False),
                getattr(verdict, "client_id", None),
                getattr(verdict, "client_level", None),
                _as_int(getattr(verdict, "core_profile", False)),
                _as_int(getattr(verdict, "below_threshold", False)),
                _as_int(
                    getattr(verdict, "core_profile", False)
                    or getattr(verdict, "client_level", None) in ("full", "partial")
                ),
                str(getattr(verdict, "strength", "none") or "none"),
                str(getattr(verdict, "industry", "unknown") or "unknown"),
                _as_int(getattr(verdict, "needs_review", False)),
                _as_int(getattr(verdict, "off_industry", False)),
                _as_int(getattr(verdict, "off_profile", False)),
                # Пустой список пишем как NULL: «совпадений нет» и «никто
                # не смотрел» — разные вещи.
                json.dumps(verdict.matched_terms, ensure_ascii=False)
                if getattr(verdict, "matched_terms", None)
                else None,
                state,
                quote or None,
                datetime.now().isoformat(timespec="seconds"),
                row["lot_key"],
            ),
        )
        updated += 1

    conn.commit()
    logger.info("Пересчёт оценок: обновлено %d, не удалось %d", updated, failed)
    return {"updated": updated, "failed": failed}
