"""
Тесты дедупликации одного тендера между площадками (ТЗ-7).

Ситуация, из которой выросло ТЗ: с приходом агрегатора Bidzaar одна и та
же закупка приезжает дважды — напрямую от площадки и через агрегатор.
На живой базе 03.09.2026 в дублях участвовали 347 лотов. Здесь проверяется
вся механика: отпечаток, схлопывание при сохранении, выбор первоисточника
для перехода и миграция уже накопившейся базы.

Сеть из тестов не нужна (и запрещена ТЗ): всё на временной базе.
"""

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, load_lot, save_lots
from src.db.dedup import fingerprint, merge_duplicates
from src.db.feed import preferred_source
from src.run_journal import describe


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.db"


def _tender(**over):
    base = {
        "platform": "Росэлторг",
        "lot_id": "111",
        "name": "Поставка герметика",
        "company": 'ООО "ВОСТОЧНАЯ МЕДЬ"',
        "inn": "0890123456",
        "href": "https://www.roseltorg.ru/lot/111",
        "sum": 1_000_000.0,
        "date_deadline": "10.09.2026",
        "tender_no": "033220000125200009",
    }
    base.update(over)
    return base


def _platforms(conn) -> set[str]:
    return {
        r["platform"]
        for r in conn.execute("SELECT platform FROM lot_sources")
    }


# ── Отпечаток ────────────────────────────────────────────────────────────────


def test_номер_нормализуется_перед_сравнением():
    """Регистр, ё→е и пунктуация номера не мешают совпадению: площадки
    оформляют один номер и как «ПР-1/25», и как «пр 1 25»."""
    assert fingerprint({"tender_no": " ПР-1/25 ё"}) == "no:пр125е"
    assert fingerprint({"tender_no": "пр 1 25 е"}) == "no:пр125е"


# ── Схлопывание при сохранении (случаи 1–7 ТЗ) ───────────────────────────────


def test_1_одинаковый_номер_на_двух_площадках_это_один_лот(db):
    """Самый точный признак дубля: площадки копируют номер процедуры из
    документации как есть, даже когда переформулируют название."""
    summary = save_lots(
        db,
        [
            _tender(),
            _tender(
                platform="Bidzaar",
                lot_id="222",
                href="https://bidzaar.ru/tender/222",
            ),
        ],
        None,
    )
    assert summary["added"] == 1
    assert summary["merged"] == 1
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1
    # Обе площадки видны как места публикации — ссылка агрегатора не теряется.
    assert _platforms(db) == {"Росэлторг", "Bidzaar"}
    row = db.execute("SELECT dedup_key FROM lots").fetchone()
    assert row["dedup_key"] == "no:033220000125200009"


def test_2_разный_номер_но_название_заказчик_и_день_совпали(db):
    """Запасной признак: номер площадки не отдала, но связка «название +
    заказчик + день дедлайна» однозначно указывает на ту же закупку."""
    save_lots(
        db,
        [
            _tender(tender_no=None),
            _tender(
                platform="Bidzaar",
                lot_id="222",
                tender_no=None,
                href="https://bidzaar.ru/tender/222",
            ),
        ],
        None,
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_3_одинаковое_название_но_разные_заказчики_не_склеиваются(db):
    """Одноимённые закупки у разных заказчиков — это разные тендеры:
    заказчик входит в отпечаток именно поэтому."""
    save_lots(
        db,
        [
            _tender(tender_no=None, company="Восточная медь"),
            _tender(
                platform="Bidzaar",
                lot_id="222",
                tender_no=None,
                company="Промметалл",
            ),
        ],
        None,
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 2


def test_4_разные_дни_дедлайна_это_разные_закупки(db):
    """Частый случай у «Ромашки»: одна и та же позиция закупается
    двумя партиями с дедлайнами в разные дни."""
    save_lots(
        db,
        [
            _tender(tender_no=None, date_deadline="10.09.2026"),
            _tender(
                platform="Bidzaar",
                lot_id="222",
                tender_no=None,
                date_deadline="11.09.2026",
            ),
        ],
        None,
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 2


def test_минуты_дедлайна_не_мешают_схлопыванию(db):
    """Площадки показывают одно и то же окончание приёма в своих часовых
    поясах — поэтому в отпечаток идёт только день, без времени."""
    save_lots(
        db,
        [
            _tender(tender_no=None, date_deadline="10.09.2026 09:00"),
            _tender(
                platform="Bidzaar",
                lot_id="222",
                tender_no=None,
                date_deadline="10.09.2026 18:30",
            ),
        ],
        None,
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_5_организационная_форма_заказчика_не_мешает(db):
    """Одна площадка пишет «ООО "ВОСТОЧНАЯ МЕДЬ"», другая — «Восточная
    медь»: форма вырезается, остаётся сам заказчик."""
    save_lots(
        db,
        [
            _tender(tender_no=None, company='ООО "ВОСТОЧНАЯ МЕДЬ"'),
            _tender(
                platform="Bidzaar",
                lot_id="222",
                tender_no=None,
                company="Восточная медь",
            ),
        ],
        None,
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_6_без_номера_и_названия_лот_не_участвует_в_схлопывании(db):
    """Нет отпечатка — нечего сравнивать: такие лоты просто сохраняются
    каждый сам по себе, «на всякий случай» склеивать нельзя."""
    summary = save_lots(
        db,
        [
            _tender(name="", tender_no=None),
            _tender(name="", tender_no=None, platform="Bidzaar", lot_id="222"),
        ],
        None,
    )
    assert summary["merged"] == 0
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 2


def test_7_пустые_поля_дополняются_заполненные_не_трогаются(db):
    """У агрегатора есть цена — она переезжает в основной лот; когда цена
    уже стоит, чужая публикация её не перетирает."""
    save_lots(db, [_tender(sum=None, inn="")], None)  # первоисточник без цены
    save_lots(
        db,
        [
            _tender(
                platform="Bidzaar",
                lot_id="222",
                sum=2_000_000.0,
                inn="0890123456",
            )
        ],
        None,
    )
    row = db.execute("SELECT platform, sum, inn FROM lots").fetchone()
    assert row["platform"] == "Росэлторг", "основной — первый увиденный лот"
    assert row["sum"] == 2_000_000.0, "цена агрегатора переехала в основной лот"
    assert row["inn"] == "0890123456"

    # Третья публикация с другой ценой: заполненное поле не перетирается.
    save_lots(
        db,
        [_tender(platform="Tender.pro", lot_id="333", sum=999.0)],
        None,
    )
    assert db.execute("SELECT sum FROM lots").fetchone()[0] == 2_000_000.0


# ── Выбор первоисточника для перехода (случай 8 ТЗ) ──────────────────────────


def test_8_первоисточник_предпочтительнее_агрегатора(db):
    """Ссылку для человека ведём на прямую площадку: там полная
    документация и подача заявки, агрегатор — только выжимка."""
    sources = [
        {"platform": "Bidzaar", "href": "https://bidzaar.ru/t/222"},
        {"platform": "Росэлторг", "href": "https://www.roseltorg.ru/lot/111"},
    ]
    assert preferred_source(sources)["platform"] == "Росэлторг"
    only_aggregator = [{"platform": "Bidzaar", "href": "https://bidzaar.ru/t/222"}]
    assert preferred_source(only_aggregator)["platform"] == "Bidzaar"
    assert preferred_source([]) is None


def test_8_карточка_показывает_обе_площадки_и_ведёт_на_первоисточник(db):
    """Критерий приёмки ТЗ-7: у лота с двумя публикациями в карточке видны
    обе площадки, а переход — по ссылке первоисточника."""
    save_lots(
        db,
        [
            _tender(),
            _tender(platform="Bidzaar", lot_id="222",
                    href="https://bidzaar.ru/t/222"),
        ],
        None,
    )
    lot = load_lot(db, "Росэлторг|111")
    assert {s["platform"] for s in lot["sources"]} == {"Росэлторг", "Bidzaar"}
    assert lot["main_href"] == "https://www.roseltorg.ru/lot/111"
    assert lot["main_platform"] == "Росэлторг"


# ── Отчёт прогона ────────────────────────────────────────────────────────────


def test_сводка_прогона_упоминает_объединённые_дубли():
    """Если дедуп однажды начнёт склеивать разное, это должно быть заметно
    по цифре в сводке, а не по жалобе человека (ТЗ-7, раздел 5)."""
    record = {
        "outcome": "ok",
        "started_at": "2026-09-03T03:00:00",
        "finished_at": "2026-09-03T03:40:00",
        "lots_found": 120,
        "lots_new": 12,
        "lots_merged": 7,
    }
    assert "объединено дублей 7" in describe(record)
    # Ноль и «неизвестно» молчат: сводка не должна шуметь попусту.
    quiet = dict(record, lots_merged=0)
    assert "дублей" not in describe(quiet)
    quiet.pop("lots_merged")
    assert "дублей" not in describe(quiet)


# ── Миграция существующей базы (случаи 9–11 ТЗ) ──────────────────────────────


def _old_lot(conn, key, platform, lot_id, **fields):
    """Строка «старой» базы: до ТЗ-7 колонки dedup_key не существовало."""
    row = {
        "lot_key": key,
        "platform": platform,
        "lot_id": lot_id,
        "name": fields.get("name"),
        "company": fields.get("company"),
        "href": fields.get("href"),
        "sum": fields.get("sum"),
        "date_deadline": fields.get("date_deadline"),
        "tender_no": fields.get("tender_no"),
        "first_seen_at": "2026-09-01T00:00:00",
        "last_seen_at": "2026-09-02T00:00:00",
        "created_at": "2026-09-01T00:00:00",
        "updated_at": "2026-09-01T00:00:00",
    }
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))


def _fill_db_with_duplicates(conn):
    """Живая база до миграции: две группы дублей и один уникальный лот."""
    _old_lot(
        conn, "Росэлторг|111", "Росэлторг", "111",
        name="Поставка герметика", company='ООО "ВОСТОЧНАЯ МЕДЬ"',
        href="https://www.roseltorg.ru/lot/111", tender_no="N-100",
    )
    _old_lot(
        conn, "Bidzaar|222", "Bidzaar", "222",
        name="Поставка герметика (агрегатор)", company='ООО "ВОСТОЧНАЯ МЕДЬ"',
        href="https://bidzaar.ru/t/222", tender_no="N-100", sum=500_000.0,
    )
    _old_lot(
        conn, "ТЭК-Торг|333", "ТЭК-Торг", "333",
        name="Флотационный реагент", company="Промметалл",
        href="https://tektorg.ru/lot/333", date_deadline="12.09.2026",
    )
    _old_lot(
        conn, "Tender.pro|444", "Tender.pro", "444",
        name="Флотационный реагент", company="Промметалл",
        href="https://tender.pro/lot/444", date_deadline="12.09.2026 14:00",
    )
    _old_lot(
        conn, "ГПБ|555", "ГПБ", "555",
        name="Запчасти к дробилке", company="Уралмаш",
        href="https://etpgpb.ru/lot/555", tender_no="N-300",
    )
    conn.commit()


def test_9_миграция_схлопывает_накопившиеся_дубли(db, db_path):
    _fill_db_with_duplicates(db)
    report = merge_duplicates(db, db_path=db_path)

    assert report["merged"] == 2
    # Число лотов уменьшилось ровно на число дублей: 5 − 2 = 3.
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 3

    # Основной лот группы дополнен ценой агрегатора — пустое не потерялось.
    row = db.execute(
        "SELECT sum FROM lots WHERE lot_key = 'Росэлторг|111'"
    ).fetchone()
    assert row["sum"] == 500_000.0

    # Площадки схлопнутых публикаций переехали в lot_sources, а не исчезли.
    platforms = {
        r["platform"]
        for r in db.execute(
            "SELECT platform FROM lot_sources WHERE lot_key = 'Росэлторг|111'"
        )
    }
    assert platforms == {"Росэлторг", "Bidzaar"}
    by_name = {
        r["platform"]
        for r in db.execute(
            "SELECT platform FROM lot_sources WHERE lot_key = 'ТЭК-Торг|333'"
        )
    }
    assert by_name == {"ТЭК-Торг", "Tender.pro"}

    # Перед схлопыванием рядом с базой лежит бэкап — это единственный откат.
    assert list(db_path.parent.glob("t.db.bak-*")), "бэкап базы не создан"


def test_10_миграция_не_трогает_группу_где_оба_лота_в_работе(
    db, db_path, caplog
):
    """Человек взял в работу оба «дубля» — выбирать за него, какой лишний,
    нельзя: группа остаётся как есть, предупреждение пишется в лог."""
    _old_lot(
        db, "Росэлторг|111", "Росэлторг", "111",
        name="Поставка герметика", company='ООО "ВОСТОЧНАЯ МЕДЬ"',
        tender_no="N-100",
    )
    _old_lot(
        db, "Bidzaar|222", "Bidzaar", "222",
        name="Поставка герметика", company='ООО "ВОСТОЧНАЯ МЕДЬ"',
        tender_no="N-100",
    )
    for key in ("Росэлторг|111", "Bidzaar|222"):
        db.execute(
            "INSERT INTO applications (lot_key, status) VALUES (?, 'SELECTED')",
            (key,),
        )
    db.commit()

    with caplog.at_level(logging.WARNING, logger="src.db.dedup"):
        report = merge_duplicates(db, db_path=db_path)

    assert report["merged"] == 0
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 2
    assert any("в работе" in r.getMessage() for r in caplog.records)


def test_11_миграция_идемпотентна(db, db_path):
    """Повторный запуск не меняет ни одной строки — иначе каждый старт
    программы пересчитывал бы базу и плодил бэкапы."""

    def snapshot():
        lots = [dict(r) for r in db.execute(
            "SELECT * FROM lots ORDER BY lot_key"
        )]
        sources = [dict(r) for r in db.execute(
            "SELECT * FROM lot_sources ORDER BY lot_key, platform"
        )]
        return lots, sources

    _fill_db_with_duplicates(db)
    first = merge_duplicates(db, db_path=db_path)
    assert first["merged"] == 2
    state = snapshot()

    second = merge_duplicates(db, db_path=db_path)
    assert second["merged"] == 0
    assert snapshot() == state
    # Бэкап плодить незачем: менять нечего.
    assert len(list(db_path.parent.glob("t.db.bak-*"))) == 1


def test_первоисточник_становится_лицом_лота_даже_если_пришёл_вторым(db):
    """Правило владельца 04.09.2026: при дублях первоисточник главнее агрегатора.

    Порядок прихода случаен — Bidzaar обходится за пять минут, площадка за
    полчаса. Без правила один и тот же лот в разные дни числился бы то за
    первоисточником, то за агрегатором, и человек уходил бы на витрину
    агрегатора вместо площадки, где подаётся заявка.
    """
    from src.db.lots import save_lots

    общий = {
        "name": "Герметик Пример 853",
        "company": 'АО "РОМАШКА"',
        "tender_no": "ЦБ-1236722",
        "date_deadline": "20.09.2026",
    }
    save_lots(db, [{**общий, "platform": "Bidzaar", "lot_id": "bid-1",
                    "href": "https://bidzaar.com/app/requests/external/bid-1"}])
    save_lots(db, [{**общий, "platform": "Росэлторг", "lot_id": "4581648",
                    "href": "https://www.roseltorg.ru/procedure/4581648/"}])

    rows = db.execute("SELECT lot_key, platform, href FROM lots").fetchall()
    assert len(rows) == 1, "дубль не схлопнулся"
    assert rows[0]["platform"] == "Росэлторг", "лицом лота должен стать первоисточник"
    assert "roseltorg" in rows[0]["href"]
    assert rows[0]["lot_key"].startswith("Bidzaar"), (
        "ключ лота менять нельзя — на него ссылаются заявки"
    )

    sources = {r["platform"] for r in db.execute(
        "SELECT platform FROM lot_sources WHERE lot_key = ?", (rows[0]["lot_key"],)
    )}
    assert sources == {"Bidzaar", "Росэлторг"}, "обе публикации должны остаться"
