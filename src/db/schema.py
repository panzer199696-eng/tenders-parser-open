"""
Схема единой базы `tenderwork.db` (фича 002, задача T001).

Зачем единая база. До слияния данные жили в трёх местах и расходились:
  • %LOCALAPPDATA%\\TendersParser\\data\\tenders.db — 345 лотов, куда пишет
    установленная версия и откуда читает рабочий стол;
  • <проект>\\data\\tenders.db — 352 лота, куда пишет dev-запуск;
  • registry.db отдельной программы реестра — 1546 «заявок», из которых
    1543 на деле просто найденные лоты.
Пользователь видел «собрал» в одной программе и «ничего не изменилось» в
другой — это выглядело как баг, а было расхождением конструкции.

Что изменено по существу, а не механически перенесено:

1. **Лот живёт в одном месте — `lots`.** Раньше найденный лот лежал и в
   `tenders`, и в `applications` со статусом FOUND. Теперь `applications` —
   только лоты, по которым НАЧАЛАСЬ работа (взят, отклонён, оценён, подан).
   Реестр перестал дублировать ленту.

2. **Ключ лота — `lot_key` = «площадка|номер».** В старом реестре ключом был
   голый номер лота, и совпадение номеров на двух площадках затёрло бы запись.

3. **`lot_sources`** — один и тот же тендер публикуется на нескольких
   площадках. Канонический лот один, а места публикации перечислены здесь:
   так дедупликация между площадками (FR-003) не теряет ссылки.

4. **`runs` и `run_platforms`** — журнал прогонов переезжает из `runs.jsonl`
   в базу: по каждой площадке видно, собрано ли всё и если нет, то почему
   (FR-004, NFR-002). Файловый журнал остаётся для совместимости со старыми
   сборками, но источником истины становится база.

5. **`prepayment`** — три состояния: 'yes', 'no', 'unknown'. Значение по
   умолчанию именно 'unknown', а не 'no': не знать и знать, что предоплаты
   нет — разные вещи, и путать их запрещено спецификацией (FR-012).
"""

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 7

# Состояния предоплаты (FR-012). 'unknown' — значение по умолчанию.
PREPAYMENT_YES = "yes"
PREPAYMENT_NO = "no"
PREPAYMENT_UNKNOWN = "unknown"

DDL = """
CREATE TABLE IF NOT EXISTS lots (
    lot_key         TEXT PRIMARY KEY,   -- «площадка|номер лота»
    platform        TEXT NOT NULL,
    lot_id          TEXT NOT NULL,
    name            TEXT,
    company         TEXT,
    inn             TEXT,               -- ИНН организатора (даёт ТЭК-Торг)
    href            TEXT,
    sum             REAL,
    date_published  TEXT,
    date_deadline   TEXT,
    date_to_display TEXT,
    search_key      TEXT,               -- фраза или заказчик, по которым нашли
    tender_no       TEXT,
    tender_type     TEXT,
    organizer_match INTEGER DEFAULT 0,  -- лот пришёл адресным обходом
    organizer_href  TEXT,
    tags            TEXT,
    score           INTEGER DEFAULT 0,
    score_explain   TEXT,               -- объяснение словами (FR-014)
    promising       INTEGER DEFAULT 0,
    client_id       TEXT,               -- заказчик из справочника, если узнан
    client_level    TEXT,               -- full | partial | market
    core_profile    INTEGER DEFAULT 0,  -- попал в основной профиль (tier daily)
    below_threshold INTEGER DEFAULT 0,  -- ниже профильного порога цены (T015)
    -- Признаки разбора профиля (Verdict из ТЗ-4, ТЗ-5). Раньше они жили
    -- только в памяти до конца прогона: лента не могла отделить «тема
    -- угадана слабо» и «чужая отрасль» от основного потока, и всё это
    -- сваливалось в «Прочее». Колонки nullable/DEFAULT, чтобы миграция
    -- старых баз прошла сама, без пересоздания.
    match_strength  TEXT,               -- strong | weak | none
    industry        TEXT,               -- client | ours | unknown | foreign
    needs_review    INTEGER DEFAULT 0,  -- тема угадана слабо или заказчик незнаком
    off_industry    INTEGER DEFAULT 0,  -- тема совпала, но отрасль заказчика чужая
    off_profile     INTEGER DEFAULT 0,  -- стоп-слово или подряд: лот отсеян
    matched_terms   TEXT,               -- JSON: [{"term","group","strength"}] — что сработало
    prepayment      TEXT DEFAULT 'unknown',
    prepayment_note TEXT,               -- цитата, по которой решили
    -- Отпечаток тендера для дедупликации между площадками (ТЗ-7): «no:…»
    -- по номеру процедуры или «nm:…» по названию+заказчику+дню дедлайна.
    -- NULL — лот без отпечатка, в схлопывании не участвует.
    dedup_key       TEXT,
    -- Когда пытались собрать состав закупки (ТЗ-10). Без этой отметки
    -- программа ходила бы за составом к одному и тому же лоту вечно:
    -- у большинства лотов состава на странице нет, и «пусто» — это
    -- тоже результат, который не надо перепроверять каждый прогон.
    items_fetched_at TEXT,
    first_seen_at   TEXT,
    last_seen_at    TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lots_platform_lot ON lots (platform, lot_id);
CREATE INDEX IF NOT EXISTS idx_lots_deadline  ON lots (date_deadline);
CREATE INDEX IF NOT EXISTS idx_lots_company   ON lots (company);
CREATE INDEX IF NOT EXISTS idx_lots_score     ON lots (score DESC);
CREATE INDEX IF NOT EXISTS idx_lots_prepay    ON lots (prepayment);
CREATE INDEX IF NOT EXISTS idx_lots_dedup     ON lots (dedup_key);

-- Где ещё встречен тот же тендер (FR-003): канонический лот один,
-- ссылок может быть несколько.
CREATE TABLE IF NOT EXISTS lot_sources (
    lot_key   TEXT NOT NULL,
    platform  TEXT NOT NULL,
    href      TEXT,
    lot_id    TEXT,
    seen_at   TEXT,
    PRIMARY KEY (lot_key, platform),
    FOREIGN KEY (lot_key) REFERENCES lots (lot_key) ON DELETE CASCADE
);

-- Только лоты, по которым началась работа. Найденный и никем не взятый лот
-- сюда НЕ попадает — он живёт в lots.
CREATE TABLE IF NOT EXISTS applications (
    lot_key           TEXT PRIMARY KEY,
    client_id         TEXT,
    client_level      TEXT,
    manager           TEXT,
    status            TEXT NOT NULL,
    reject_reason     TEXT,
    decision          TEXT,
    decision_at       TEXT,
    decision_by       TEXT,
    assessment        TEXT,
    volume_t          REAL,
    packaging         TEXT,
    hazard            INTEGER,
    docs_needed       TEXT,
    supplier          TEXT,
    purchase_price    REAL,
    logistics         REAL,
    cost              REAL,
    markup            REAL,
    bid_price         REAL,
    price_approved_by TEXT,
    price_approved_at TEXT,
    submitted_at      TEXT,
    submission_no     TEXT,
    result            TEXT,
    winner_price      REAL,
    deviation         REAL,
    loss_reason       TEXT,
    contact           TEXT,
    next_touch        TEXT,
    note              TEXT,
    created_at        TEXT,
    updated_at        TEXT,
    FOREIGN KEY (lot_key) REFERENCES lots (lot_key)
);

CREATE INDEX IF NOT EXISTS idx_app_status  ON applications (status);
CREATE INDEX IF NOT EXISTS idx_app_manager ON applications (manager);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_key     TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    at          TEXT,
    who         TEXT,
    comment     TEXT
);

-- Состав закупки (ТЗ-10): позиции внутри лота. Половина профильных
-- находок — сборные лоты («Расходники, реактивы, посуда»), и по названию
-- не понять, наша ли там позиция. Площадка публикует состав на странице
-- позиций, а цену — почти никогда (172 лота из 4627), поэтому price
-- nullable почти всегда.
CREATE TABLE IF NOT EXISTS lot_items (
    lot_key   TEXT NOT NULL,
    position  INTEGER NOT NULL,     -- номер строки в составе
    name      TEXT NOT NULL,        -- наименование позиции
    quantity  REAL,                 -- количество, если указано
    unit      TEXT,                 -- единица измерения
    price     REAL,                 -- максимальная цена за позицию, если указана
    PRIMARY KEY (lot_key, position),
    FOREIGN KEY (lot_key) REFERENCES lots (lot_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_lot ON history (lot_key, id DESC);

-- Один и тот же переход не может произойти дважды в одну минуту у одного
-- лота. Индекс делает повторную миграцию безопасной: без него второй прогон
-- удваивал историю.
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_unique
    ON history (lot_key, at, from_status, to_status);

-- Журнал прогонов: NFR-002 «ни один прогон не завершается молча».
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    outcome       TEXT,          -- ok | stopped | error
    trigger       TEXT,          -- schedule | manual | catchup (FR-006)
    app_version   TEXT,
    lots_found    INTEGER,
    lots_new      INTEGER,
    lots_merged   INTEGER,       -- сколько публикаций схлопнулось в дубли (ТЗ-7)
    data_complete INTEGER,       -- 0/1: собрано ли всё
    summary       TEXT,          -- человеческим языком
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);

-- Полнота по каждой площадке отдельно (FR-004).
CREATE TABLE IF NOT EXISTS run_platforms (
    run_id     INTEGER NOT NULL,
    platform   TEXT NOT NULL,
    collected  INTEGER DEFAULT 0,
    complete   INTEGER DEFAULT 0,   -- 0/1
    reason     TEXT,                -- почему неполно — словами, не кодом
    -- Живость площадки (16.10.2026): collected — это число ОБЪЕКТОВ обхода,
    -- и ноль лотов за ним был не виден. Эти три колонки — лоты площадки до
    -- фильтров отбора, из них впервые увиденных, и самая свежая дата
    -- публикации (ISO ГГГГ-ММ-ДД) — по ним прогон сравнивается с прошлыми.
    lots            INTEGER,
    lots_new        INTEGER,
    newest_published TEXT,
    reason_code     TEXT,           -- причина кодом (format_changed, stale…)
    PRIMARY KEY (run_id, platform),
    FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def make_lot_key(platform: str, lot_id: str) -> str:
    """Ключ лота: «площадка|номер».

    Голый номер лота ключом быть не может: номера уникальны внутри площадки,
    а не между ними, и совпадение затёрло бы чужую запись.
    """
    return f"{str(platform or '').strip()}|{str(lot_id or '').strip()}"


def connect(path: Path | str) -> sqlite3.Connection:
    """Соединение с включёнными внешними ключами и построчным доступом.

    timeout=30: ночной сбор и открытое окно программы пишут в одну базу,
    и без ожидания вторая запись сразу падала «database is locked».
    """
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Колонки таблиц вычисляются из самого DDL, а не ведутся списком руками:
# список неизбежно отстаёт от схемы, а база у пользователя одна, и падение
# при обновлении означает потерю доступа к своим данным.
_COLUMN_RE = re.compile(
    r"^\s*([a-z_]+)\s+(TEXT|INTEGER|REAL)", re.IGNORECASE | re.MULTILINE
)


def _declared_columns(table: str) -> dict[str, str]:
    """Колонки таблицы из DDL: имя → тип с умолчанием."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", DDL, re.DOTALL
    )
    if not match:
        return {}
    columns: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        if not line or line.upper().startswith(
            ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE")
        ):
            continue
        found = _COLUMN_RE.match(line)
        if not found:
            continue
        name = found.group(1)
        definition = line[len(name) :].strip()
        # PRIMARY KEY нельзя добавить через ALTER — такие пропускаем: они
        # есть в любой версии таблицы, иначе это не наша база.
        if "PRIMARY KEY" in definition.upper():
            continue
        columns[name] = definition
    return columns


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Дополнить существующие таблицы колонками, появившимися позже."""
    for table in (
        "lots",
        "applications",
        "history",
        "runs",
        "run_platforms",
        "lot_sources",
        "lot_items",
    ):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # таблицы ещё нет — её создаст DDL
        for name, definition in _declared_columns(table).items():
            if name in existing:
                continue
            # SQLite не даёт добавить NOT NULL без умолчания в непустую
            # таблицу. Такие колонки — часть исходной схемы: если их нет,
            # это не наша база, и молча портить её нельзя.
            upper = definition.upper()
            if "NOT NULL" in upper and "DEFAULT" not in upper:
                logger.warning(
                    "Таблица %s без обязательной колонки %s — пропускаю", table, name
                )
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _normalize_deadlines(conn: sqlite3.Connection) -> None:
    """Привести сроки, записанные в ISO, к «ДД.ММ.ГГГГ ЧЧ:ММ».

    Так в базу попали лоты миграции 27.08.2026, и часть программы считала
    их бессрочными. Запись теперь нормализуется на входе, а здесь
    добиваются старые строки. Идемпотентно: после первого раза выборка
    пуста. Непонятые значения не трогаются — исходный текст не теряем.
    """
    from ..dates import normalize_deadline

    rows = conn.execute(
        "SELECT lot_key, date_deadline FROM lots WHERE date_deadline LIKE '____-__-__%'"
    ).fetchall()
    changed = 0
    for row in rows:
        value = normalize_deadline(row[1])
        if value != row[1]:
            conn.execute(
                "UPDATE lots SET date_deadline = ? WHERE lot_key = ?", (value, row[0])
            )
            changed += 1
    if changed:
        logger.info("Сроки в ISO приведены к ДД.ММ.ГГГГ: %d лотов", changed)


# Метки времени (не сроки!) — в одном виде «ГГГГ-ММ-ДДTЧЧ:ММ:СС». В живой
# базе 16.10.2026 их было три: «2026-08-27 06:30» из миграции 27.08, ISO
# с секундами и ISO с микросекундами. Метки сравниваются строками («новое
# с начала прогона», «площадка не видела лот после…»), а пробел меньше «T».
_TIMESTAMP_COLUMNS = {
    "lots": (
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
        "items_fetched_at",
    ),
    "lot_sources": ("seen_at",),
    "history": ("at",),
    "applications": (
        "created_at",
        "updated_at",
        "decision_at",
        "price_approved_at",
        "submitted_at",
    ),
}
_ISO_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]"
)


def _normalize_timestamps(conn: sqlite3.Connection) -> None:
    """Разово привести метки времени к ISO с «T» (отметка `timestamps_iso`).

    Идемпотентна и без отметки: выборка берёт только строки не в
    каноническом виде. Непонятое и значения с поясом не трогаются.
    Совпадение с уже записанной строкой истории (тот же переход в ту же
    минуту в другом формате) пропускается: уникальный индекс истории
    уронил бы запуск программы.
    """
    if conn.execute("SELECT 1 FROM meta WHERE key = 'timestamps_iso'").fetchone():
        return
    from datetime import datetime

    changed = skipped = 0
    for table, columns in _TIMESTAMP_COLUMNS.items():
        for col in columns:
            rows = conn.execute(
                f"SELECT rowid, {col} FROM {table}"
                f" WHERE {col} IS NOT NULL AND {col} != ''"
                f" AND {col} NOT GLOB '{_ISO_GLOB}'"
            ).fetchall()
            for rowid, value in rows:
                try:
                    at = datetime.fromisoformat(str(value).strip())
                except ValueError:
                    continue
                if at.tzinfo is not None:
                    continue
                fixed = at.replace(microsecond=0).isoformat(timespec="seconds")
                try:
                    conn.execute(
                        f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (fixed, rowid)
                    )
                    changed += 1
                except sqlite3.IntegrityError:
                    skipped += 1
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('timestamps_iso', ?)",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    if changed or skipped:
        logger.info(
            "Метки времени приведены к ISO: %d, пропущено совпавших в истории %d",
            changed,
            skipped,
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Создать недостающие таблицы и проставить версию схемы.

    Идемпотентна: повторный вызов на готовой базе ничего не ломает — это
    важно, потому что вызывается при каждом запуске программы.
    """
    # Порядок важен: сначала таблицы, потом недостающие колонки, и только
    # потом индексы. Индекс по колонке, которой в старой базе ещё нет, роняет
    # обновление программы с «no such column» — а база у пользователя одна.
    statements = [x.strip() for x in DDL.split(";") if x.strip()]
    tables = [
        x
        for x in statements
        if "CREATE INDEX" not in x.upper() and "CREATE UNIQUE INDEX" not in x.upper()
    ]
    indexes = [x for x in statements if x not in tables]

    for statement in tables:
        conn.execute(statement)
    _add_missing_columns(conn)
    for statement in indexes:
        conn.execute(statement)
    _normalize_deadlines(conn)
    _normalize_timestamps(conn)

    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    """Версия схемы в базе; 0 — схемы ещё нет."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["value"]) if row else 0
