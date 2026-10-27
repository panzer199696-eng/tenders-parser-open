"""
Лента лотов из единой базы (задача T017).

Раскладка по корзинам перенесена из прежнего рабочего стола, но с одним
принципиальным изменением: **корзины считаются при показе, а оценка берётся
из базы**. Причина простая — они отвечают на разные вопросы:

  • «Насколько лот наш» (группа номенклатуры, уровень заказчика, порог цены)
    не зависит от того, какое сегодня число. Это считается один раз при сборе
    и лежит в базе вместе с лотом.
  • «Горит ли лот» и «не истёк ли срок» зависят ровно от сегодняшнего дня.
    Сохранить их в базу значило бы получить назавтра ленту, где вчерашний
    аврал всё ещё горит, а истёкшее предлагается подать.

Порядок корзин — это порядок разбора. Основной профиль отделён от
клиентского вала: иначе запчасти закреплённых заказчиков вытесняют
основное (на живых данных в «наши» падало 280 строк, из которых по
основному профилю были единицы).
"""

import json
import sqlite3
from bisect import bisect_right
from datetime import date, datetime, timedelta

from ..dates import parse_deadline, resolve_now, today_msk
from ..platforms import source_rank
from ..watch_clients import is_watched, watch_names

# Экстренная закупка: дедлайн вплотную — короткие тендеры, на которые
# медленно реагируют, стоит видеть первыми.
URGENT_DAYS = 3

BUCKETS = (
    ("urgent", "🔥 Горит", "наше с дедлайном ≤ 3 дней"),
    ("core", "★ Основной профиль", "группы номенклатуры ежедневного набора (tier = daily)"),
    ("client", "● Закреплённые клиенты", "прочая номенклатура у наших заказчиков"),
    ("below", "Ниже порога", "наше по сути, но мелкое — не теряем"),
    # ТЗ-5: «Прочее» исчезает — его содержимое распределилось между двумя
    # новыми корзинами. Обе свёрнуты по умолчанию (решение владельца от
    # 01.09.2026): нераспознанное прячем из основного окна, но копим.
    ("review", "? На проверку", "тема угадана слабо или заказчик незнаком"),
    ("foreign", "Не наш профиль", "тема совпала, но отрасль чужая или это подряд"),
    (
        "missing",
        "Пропал — проверить",
        "площадка больше не показывает лот — не отменён ли он",
    ),
    ("expired", "Срок истёк", "подавать уже некуда"),
)

# Корзины, свёрнутые при показе: сомнительное, отсеянное и просроченное не
# должно вытеснять рабочий поток из первого экрана, но и терять его нельзя.
# Истёкшие сворачиваются по той же причине: на живой базе их пять сотен, и
# они занимали весь экран ниже разбираемого.
COLLAPSED_BUCKETS = frozenset({"review", "foreign", "missing", "expired"})

# «Пропал — проверить» (16.10.2026): лот, снятый площадкой
# (отмена, перенос под новым номером), висел в «Горит» до своего срока.
# Судить можно только по площадкам, которые отдают ВСЁ активное: у
# Tender.pro окно по дате публикации, и живой лот из него выпадает.
MISSING_PLATFORMS = ("Bidzaar", "Росэлторг", "ТЭК-Торг")
# Сколько полноценных прогонов подряд площадка не показывала лот: одна
# ночь — ещё случайность (лот на минуту скрыли, выдача моргнула).
MISSING_NIGHTS = 2

def preferred_source(sources) -> dict | None:
    """Источник для перехода: первоисточник, а не агрегатор.

    Площадки без приоритета (появятся новые — ГПБ, ЕИС) идут после
    известных, по алфавиту: скорее всего это тоже агрегаторы либо
    непроверенные источники. При равном приоритете предпочитаем запись
    со ссылкой: пустой href ничем помочь не может.
    """
    if not sources:
        return None

    def rank(src: dict):
        platform = str(src.get("platform") or "").strip()
        return (source_rank(platform), platform, 0 if src.get("href") else 1)

    return min(sources, key=rank)


def days_left(deadline: str, today: date | None = None) -> int | None:
    at = parse_deadline(deadline)
    if at is None:
        return None
    return (at.date() - (today or today_msk())).days


def is_ours(row: dict, watched: list[str]) -> bool:
    """Наш лот: основной профиль, закреплённый клиент или особый контроль.

    Только такой лот «горит» — и в ленте, и в Excel, и в отчёте «Сроки».
    """
    return (
        is_watched(row.get("company"), watched)
        or bool(row.get("core_profile"))
        or row.get("client_level") in ("full", "partial")
    )


def classify_row(
    row: dict,
    now: datetime,
    watched: list[str] | None = None,
    today: date | None = None,
) -> str:
    """Корзина лота — единственное правило раскладки на всю программу.

    По нему строятся лента, листы Excel («Горит», «Закреплённые клиенты»,
    «Не наш профиль»…) и отчёт «Сроки». Раньше у каждого было своё правило,
    и одна база давала три разных ответа на «что горит» (Г4, 16.10.2026).
    Оценка — из базы, сроки — на момент `now` (московское время).
    `row["missing"]` — признак «площадки больше не показывают лот»
    (см. missing_lot_keys); `row["tags_list"]` — темы лота списком.

    ``watched`` — имена заказчиков особого контроля; None значит взять
    из настроек (в load_feed список читают один раз на всю ленту и
    передают сюда — настройки это файл на диске, читать его на каждую
    из тысяч строк нельзя).

    Порядок проверок — по цене ошибки, а не по порядку показа:

    1. Истёкший лот остаётся в `expired` всегда — особый контроль НЕ
       исключение: просроченный лот не нужен
       даже по заказчику особого контроля, подавать заявку некуда.
    2. Заказчик особого контроля не уходит в `foreign`, даже если профиль
       отсёк лот: по этим заказчикам работа идёт адресно, и прятать их
       закупки в «Не наш профиль» — терять их. Вердикт профиля
       (off_industry/off_profile) остаётся в базе честным — меняется
       только раскладка по корзинам. Такой лот считается интересным:
       горящий дедлайн — `urgent`, иначе — `client`.
    3. Отсеянное профилем (`off_industry`/`off_profile`) — в `foreign` ДО
       проверки срочности: «кислота для ТЭЦ» с горящим дедлайном не должна
       стоять в «Горит» и отвлекать от настоящих находок.
    4. Горящее наше — верх разбора (приоритетная ниша по совещанию 26.08).
    5. Слабое совпадение или неопознанная тема — `review`: сомнительное
       копим, но в основной поток не пускаем.

    «Пропал — проверить» стоит сразу после истёкших: снятый площадкой лот
    не должен гореть и стоять в профиле, а истёкший остаётся истёкшим.
    """
    if watched is None:
        watched = watch_names()
    today = today or now.date()
    at = parse_deadline(row.get("date_deadline"))
    expired = at is not None and at < now
    left = None if at is None else (at.date() - today).days

    if expired:
        return "expired"
    if row.get("missing"):
        return "missing"
    watched_lot = is_watched(row.get("company"), watched)
    if not watched_lot and (row.get("off_industry") or row.get("off_profile")):
        return "foreign"

    interesting = is_ours(row, watched)
    if interesting and left is not None and 0 <= left <= URGENT_DAYS:
        return "urgent"
    # Особый контроль минует и «Ниже порога»: это тоже отбор по цене, а
    # правило 16.09.2026 снимает с этих заказчиков ценовые пороги целиком —
    # их лот за 1000 ₽ ждут в «Закреплённых клиентах», а не в мелочи.
    if row.get("below_threshold") and not watched_lot:
        return "below"
    if row.get("core_profile"):
        return "core"
    # Закреплённый клиент идёт в свою корзину раньше проверки на слабость:
    # за заказчиком, с которым мы работаем, смотрим весь поток целиком, даже
    # если тему лота справочник не распознал. Наша позиция часто спрятана
    # внутри сборного лота («Расходники, реактивы, посуда»), где ни одна
    # фраза не сработает, — прятать такое в «На проверку» значит терять.
    if interesting:
        return "client"
    if row.get("needs_review") or not row.get("tags_list"):
        return "review"
    # Распознано уверенно, но не профиль и не клиент («второй эшелон» у
    # рынка) — раньше падало в «Прочее». Показывать его среди рабочего
    # потока незачем, прятать совсем — потеря: копим в «На проверку».
    return "review"


def _bucket_of(
    row: dict, today: date, now: datetime, watched: list[str] | None = None
) -> str:
    """Прежнее имя classify_row — для старых вызовов."""
    return classify_row(row, now, watched, today)


def fresh_since(conn, now: datetime) -> str | None:
    """Момент, начиная с которого лот считается новым.

    Это начало последнего прогона: человек приходит утром и спрашивает «что
    принесла ночь», а не «что появилось за последние 24 часа». Если журнала
    ещё нет (первая установка, старая база) — берём сутки назад: пусть
    ответ будет приблизительным, но он будет.
    """
    started = None
    try:
        from .runs import last_run as _last_run

        record = _last_run(conn, skip_catchup=True) or {}
        started = record.get("started_at")
    except Exception:
        started = None
    if not started:
        try:
            from .. import run_journal

            started = (run_journal.read_last_run() or {}).get("started_at")
        except Exception:
            started = None
    if started:
        return str(started)
    return (now - timedelta(days=1)).isoformat(timespec="seconds")


def _is_new(row: dict, since: str | None) -> bool:
    """Лот впервые увиден не раньше начала последнего прогона.

    Пустой `first_seen_at` — не повод объявлять лот новым: так помечены
    записи, приехавшие миграцией из старых баз, и утренняя плашка
    показала бы две тысячи «новинок».
    """
    seen = row.get("first_seen_at")
    if not seen or not since:
        return False
    return str(seen) >= str(since)


def _group_titles() -> dict[str, str]:
    """Идентификатор группы → человеческое название из справочника."""
    try:
        from ..store import load_nomenclature

        return {
            str(g.get("id")): str(g.get("title") or g.get("id"))
            for g in load_nomenclature().get("groups", [])
            if g.get("id")
        }
    except Exception:
        return {}


def _row_to_lot(row, titles: dict[str, str] | None = None) -> dict:
    lot = dict(row)
    try:
        lot["tags_list"] = json.loads(lot.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        lot["tags_list"] = []
    # Тема лота — первая совпавшая группа номенклатуры: по ней лента делится
    # на «насосы», «запчасти» и прочее, чтобы разбирать однородное подряд,
    # а не прыгать между насосами и кабелем.
    titles = titles or {}
    groups = lot["tags_list"]
    lot["topic_id"] = groups[0] if groups else ""
    lot["topic"] = titles.get(lot["topic_id"], "") if groups else "Без темы"
    lot["topics"] = [titles.get(g, g) for g in groups]
    return lot


def _main_link(sources, lot: dict) -> tuple[str | None, str | None]:
    """Куда вести человека: (ссылка, площадка) первоисточника со ссылкой.

    Место публикации без ссылки для перехода бесполезно, даже если площадка
    главнее: первоисточник без href уступает Bidzaar со ссылкой. Ссылок нет ни у
    кого — собственная ссылка лота; нет и её — None, и интерфейс показывает
    номер процедуры «искать на площадке» вместо пустой ссылки (Г3).
    """
    sources = sources or []
    linked = preferred_source([s for s in sources if s.get("href")])
    main = linked or preferred_source(sources) or {}
    href = (linked or {}).get("href") or lot.get("href") or None
    return href, main.get("platform") or lot.get("platform")


def missing_lot_keys(conn) -> set[str]:
    """Лоты, которые площадки перестали показывать («Пропал — проверить»).

    Лот пропал, если у каждой его площадки из MISSING_PLATFORMS после
    последней встречи лота (на любой площадке) прошло не меньше
    MISSING_NIGHTS полноценных прогонов: не догон, итог ok, площадка
    собрана целиком и дала хоть один лот. Ноль лотов — признак сломанной
    площадки, а не пустой: такой прогон не считается. Колонка
    run_platforms.lots появилась 16.10.2026 — до неё истории нет, и
    корзина честно пуста. Взятые в работу лоты не трогаем: они в реестре.
    """
    marks = ", ".join("?" for _ in MISSING_PLATFORMS)
    try:
        good = conn.execute(
            "SELECT p.platform, r.started_at FROM runs r"
            " JOIN run_platforms p ON p.run_id = r.id"
            " WHERE r.outcome = 'ok' AND COALESCE(r.trigger, '') != 'catchup'"
            f" AND p.complete = 1 AND p.lots > 0 AND p.platform IN ({marks})",
            MISSING_PLATFORMS,
        ).fetchall()
    except sqlite3.OperationalError:
        return set()  # база до схемы 7: колонки lots ещё нет
    nights: dict[str, list[str]] = {}
    for platform, started in good:
        if started:
            nights.setdefault(platform, []).append(str(started))
    if not nights:
        return set()
    for starts in nights.values():
        starts.sort()

    seen: dict[str, dict] = {}
    for key, last_seen, platform, seen_at in conn.execute(
        "SELECT l.lot_key, l.last_seen_at, s.platform, s.seen_at"
        " FROM lots l JOIN lot_sources s ON s.lot_key = l.lot_key"
        " LEFT JOIN applications a ON a.lot_key = l.lot_key"
        " WHERE a.lot_key IS NULL"
    ):
        entry = seen.setdefault(key, {"last": str(last_seen or ""), "platforms": set()})
        entry["last"] = max(entry["last"], str(seen_at or ""))
        entry["platforms"].add(platform)

    missing = set()
    for key, entry in seen.items():
        judged = entry["platforms"] & set(MISSING_PLATFORMS)
        if not judged or not entry["last"]:
            continue
        # Последняя встреча — на любой площадке: лот, который ещё видит
        # Tender.pro или вторая площадка, жив.
        if all(
            len(nights.get(p, ())) - bisect_right(nights.get(p, []), entry["last"])
            >= MISSING_NIGHTS
            for p in judged
        ):
            missing.add(key)
    return missing


def classify_lots(
    conn,
    now: datetime,
    today: date | None = None,
    watched: list[str] | None = None,
    titles: dict[str, str] | None = None,
) -> list[dict]:
    """Неразобранные лоты с корзиной — одна раскладка для ленты, Excel и отчётов.

    Неразобранный — лот без решения: взятый в работу или отклонённый
    («Мимо») уходит в реестр. Иначе отклонённый лот остаётся в списке и его
    приходится отклонять снова и снова — ровно та жалоба, с которой начали
    (31.08.2026); с 16.10.2026 так же и в Excel.

    Порядок — как разбирать: оценка с бонусом предоплаты (FR-013: она
    снимает кассовый разрыв, но перевесить профиль не должна — бонус
    сопоставим с одной группой номенклатуры), затем ближний срок. Срок —
    по разобранной дате, а не строкой: «05.11» вставало раньше «20.10» (Г3).
    """
    today = today or now.date()
    if watched is None:
        watched = watch_names()
    rows = conn.execute(
        "SELECT l.*, (l.score + CASE WHEN l.prepayment = 'yes' THEN 10 ELSE 0 END)"
        " AS rank_score"
        " FROM lots l LEFT JOIN applications a ON a.lot_key = l.lot_key"
        " WHERE a.lot_key IS NULL"
    ).fetchall()
    sources: dict[str, list[dict]] = {}
    for key, platform, href in conn.execute(
        "SELECT lot_key, platform, href FROM lot_sources"
    ):
        sources.setdefault(key, []).append({"platform": platform, "href": href})
    missing = missing_lot_keys(conn)

    ordered = []
    for raw in rows:
        lot = _row_to_lot(raw, titles)
        at = parse_deadline(lot.get("date_deadline"))
        lot["days_left"] = None if at is None else (at.date() - today).days
        lot["missing"] = lot["lot_key"] in missing
        lot["main_href"], lot["main_platform"] = _main_link(
            sources.get(lot["lot_key"]), lot
        )
        lot["bucket"] = classify_row(lot, now, watched, today)
        ordered.append(((-(lot.get("rank_score") or 0), at or datetime.max), lot))
    ordered.sort(key=lambda pair: pair[0])
    return [lot for _, lot in ordered]


def load_feed(
    conn,
    today: date | None = None,
    now: datetime | None = None,
    limit_per_bucket: int = 200,
) -> dict:
    """Лента: корзины с лотами и сводка. Читает единую базу.

    `limit_per_bucket` ограничивает выдачу на страницу, но счётчики считаются
    по всем лотам: пользователь должен видеть настоящее число, а не длину
    показанного куска.
    """
    # Сроки площадок — московские, и «сейчас» тоже московское: ПК живёт
    # в UTC+7, и по его часам лот истекал на 4 часа раньше (Г2, 16.10.2026).
    today, now = resolve_now(today, now)

    titles = _group_titles()
    buckets: dict[str, list[dict]] = {key: [] for key, _, _ in BUCKETS}
    counts: dict[str, int] = {key: 0 for key, _, _ in BUCKETS}
    topics: dict[str, dict] = {}

    # Заказчики особого контроля — один запрос к настройкам на всю ленту:
    # это файл на диске, и читать его на каждой из тысяч строк нельзя.
    lots = classify_lots(conn, now, today, watch_names(), titles)
    since = fresh_since(conn, now)
    fresh = {"total": 0, "core": 0, "urgent": 0, "since": since, "excel": None}

    for lot in lots:
        key = lot["bucket"]
        lot["is_new"] = _is_new(lot, since)
        if lot["is_new"]:
            fresh["total"] += 1
            if lot.get("core_profile"):
                fresh["core"] += 1
            # «Горит» в плашке — то же, что в корзине: срочное, но чужое
            # человек в утренней сводке не ищет.
            if key == "urgent":
                fresh["urgent"] += 1
        # Счётчик по темам — по тем же лотам, что показаны в ленте.
        topic_id = lot["topic_id"] or "_none"
        entry = topics.setdefault(
            topic_id,
            {"id": topic_id, "title": lot["topic"] or "Без темы", "lots": 0},
        )
        entry["lots"] += 1
        counts[key] += 1
        if len(buckets[key]) < limit_per_bucket:
            buckets[key].append(lot)
    in_work = conn.execute(
        "SELECT count(*) FROM applications WHERE status NOT IN ('REJECTED')"
    ).fetchone()[0]
    rejected = conn.execute(
        "SELECT count(*) FROM applications WHERE status = 'REJECTED'"
    ).fetchone()[0]
    total_lots = conn.execute("SELECT count(*) FROM lots").fetchone()[0]

    return {
        "buckets": buckets,
        "counts": counts,
        "titles": [
            {
                "key": key,
                "title": title,
                "hint": hint,
                # Свёрнутые корзины: интерфейс рисует их заголовком со
                # счётчиком, содержимое раскрывается кликом.
                "collapsed": key in COLLAPSED_BUCKETS,
            }
            for key, title, hint in BUCKETS
        ],
        # total — сколько ждёт разбора, а не сколько лотов в базе: человек
        # смотрит на ленту, чтобы понять объём работы перед собой.
        # Темы идут по убыванию количества: сверху то, чего больше всего.
        "topics": sorted(topics.values(), key=lambda t: -t["lots"]),
        "total": len(lots),
        "total_lots": total_lots,
        "in_work": in_work,
        "rejected": rejected,
        # Что принёс последний сбор: это первое, что человек читает утром.
        "fresh": fresh,
    }


def load_lot(conn, lot_key: str) -> dict | None:
    """Один лот с местами публикации — для карточки."""
    row = conn.execute("SELECT * FROM lots WHERE lot_key = ?", (lot_key,)).fetchone()
    if row is None:
        return None
    lot = _row_to_lot(row, _group_titles())
    lot["days_left"] = days_left(lot.get("date_deadline"))
    # Состав закупки: главный ответ на вопрос «что внутри сборного лота».
    # Пустой список и отсутствие данных различаются намеренно: пустой значит
    # «смотрели, позиций нет», отсутствие — «ещё не смотрели».
    try:
        from .items import load_lot_items

        lot["items"] = load_lot_items(conn, lot_key)
    except Exception:
        lot["items"] = []

    lot["sources"] = [
        dict(r)
        for r in conn.execute(
            "SELECT platform, href, seen_at FROM lot_sources WHERE lot_key = ?",
            (lot_key,),
        )
    ]
    # Ссылка для перехода — на первоисточник: там полная документация и
    # подача заявки. Если мест публикации нет (старая база до ТЗ-7),
    # остаётся собственная ссылка лота — она всегда про сам тендер.
    lot["main_href"], lot["main_platform"] = _main_link(lot["sources"], lot)
    application = conn.execute(
        "SELECT * FROM applications WHERE lot_key = ?", (lot_key,)
    ).fetchone()
    lot["application"] = dict(application) if application else None
    # История нужна прямо в карточке: «что с лотом делали» — половина ответа
    # на вопрос «что с ним делать дальше».
    lot["history"] = [
        dict(r)
        for r in conn.execute(
            "SELECT from_status, to_status, at, who, comment FROM history"
            " WHERE lot_key = ? ORDER BY id",
            (lot_key,),
        )
    ]
    return lot
