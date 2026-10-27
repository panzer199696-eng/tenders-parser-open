"""
Страж формата срока подачи (16.10.2026).

Инцидент: миграция из старой базы записала срок в ISO
(«2026-08-28 06:54»), а лента, Excel, профиль и склейка дублей понимали только
«ДД.ММ.ГГГГ». 1320 лотов стали бессрочными, и тендеры, закончившиеся в
августе, полтора месяца висели среди рабочих — в ленте и в Excel.

Три барьера, которые здесь проверяются:
1. все разборщики срока понимают одни и те же форматы;
2. в базу срок пишется в едином виде, а старые ISO-строки приводятся при старте;
3. срок, который программа не поняла, не проходит молча — он в сводке записи.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, save_lots
from src.db.dedup import _deadline_day
from src.dates import deadline_unreadable, normalize_deadline
from src.dates import parse_deadline as _deadline_at  # лента, Excel, реестр
from src.profile.profile import Profile
from src.utils.classifier import _parse_deadline

# Форматы, которые встречались в базе: точки (парсеры) и ISO (миграция).
FORMATS = [
    "28.08.2026 06:54",
    "28.08.2026",
    "2026-08-28 06:54",
    "2026-08-28T06:54:00",
    "2026-08-28T06:54:00+03:00",
    "2026-08-28",
]


@pytest.mark.parametrize("value", FORMATS)
def test_все_разборщики_срока_понимают_формат(value):
    day = date(2026, 8, 28)
    assert _deadline_at(value).date() == day, "лента и Excel"
    assert Profile.deadline_at(value).date() == day, "профиль"
    assert _parse_deadline(value) == day, "классификатор"
    assert _deadline_day(value) == "2026-08-28", "склейка дублей"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-28 06:54", "28.08.2026 06:54"),
        ("2026-08-28T06:54:00+03:00", "28.08.2026 06:54"),
        ("2026-08-28", "28.08.2026"),
        ("28.08.2026 06:54", "28.08.2026 06:54"),
        ("Не указана", "Не указана"),
        ("до конца месяца", "до конца месяца"),  # непонятое не теряем
        (None, None),
    ],
)
def test_срок_приводится_к_единому_виду(value, expected):
    assert normalize_deadline(value) == expected


def test_срок_без_времени_действует_до_конца_дня(db):
    # 16.10.2026: лента считала «16.10.2026» истёкшим с полуночи, и лоты
    # ТЭК-Торга уходили в «Срок истёк» утром своего последнего дня.
    from src.db import load_feed

    assert _deadline_at("16.10.2026") == datetime(2026, 10, 16, 23, 59)
    assert _deadline_at("2026-10-16") == datetime(2026, 10, 16, 23, 59)
    db.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, name, date_deadline,"
        " client_level, core_profile)"
        " VALUES ('T|1', 'ТЭК-Торг', '1', 'Компаунд', '16.10.2026', 'full', 1)"
    )
    db.commit()
    feed = load_feed(db, today=date(2026, 10, 16), now=datetime(2026, 10, 16, 11, 0))
    assert feed["counts"]["expired"] == 0
    assert feed["counts"]["urgent"] == 1


def test_непонятый_срок_распознаётся():
    assert deadline_unreadable("до конца месяца")
    assert not deadline_unreadable("2026-08-28 06:54")
    assert not deadline_unreadable("Не указана")
    assert not deadline_unreadable("")


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _tender(lot_id, deadline, platform="Росэлторг"):
    return {
        "platform": platform,
        "lot_id": lot_id,
        "name": "Компаунд заливочный",
        "company": "АО Рудник",
        "date_deadline": deadline,
    }


def test_срок_в_iso_пишется_в_базу_с_точками(db):
    save_lots(db, [_tender("1", "2026-08-28 06:54")])
    row = db.execute("SELECT date_deadline FROM lots").fetchone()
    assert row[0] == "28.08.2026 06:54"


def test_непонятый_срок_попадает_в_сводку(db):
    summary = save_lots(
        db,
        [
            _tender("1", "28.08.2026"),
            _tender("2", "завтра к обеду", platform="Новая площадка"),
            _tender("3", "послезавтра", platform="Новая площадка"),
        ],
    )
    assert summary["bad_deadline"] == 2
    info = summary["bad_deadline_by_platform"]["Новая площадка"]
    assert info == {"count": 2, "example": "завтра к обеду"}
    # Исходный текст не потерян — его увидит человек в карточке.
    row = db.execute("SELECT date_deadline FROM lots WHERE lot_id = '2'").fetchone()
    assert row[0] == "завтра к обеду"


def test_старые_iso_строки_приводятся_при_старте(db):
    db.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, date_deadline)"
        " VALUES ('ROS|1', 'Росэлторг', '1', '2026-08-28 06:54'),"
        "        ('ROS|2', 'Росэлторг', '2', 'мусор-2026')"
    )
    db.commit()
    ensure_schema(db)
    ensure_schema(db)  # повторный старт ничего не ломает
    rows = dict(db.execute("SELECT lot_id, date_deadline FROM lots").fetchall())
    assert rows == {"1": "28.08.2026 06:54", "2": "мусор-2026"}


def test_истёкший_iso_лот_не_висит_среди_рабочих(db):
    from src.db import load_feed

    db.execute(
        "INSERT INTO lots (lot_key, platform, lot_id, name, date_deadline,"
        " client_level, core_profile)"
        " VALUES ('ROS|1', 'Росэлторг', '1', 'Компаунд', '2026-08-28 06:54', 'full', 1)"
    )
    db.commit()
    feed = load_feed(db, today=date(2026, 10, 16), now=datetime(2026, 10, 16, 3, 32))
    assert feed["counts"]["expired"] == 1
    assert sum(v for k, v in feed["counts"].items() if k != "expired") == 0


# ── src/dates.py: дата внутри строки и московское время (16.10.2026) ─────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12.10.2026 10:00 (МСК)", datetime(2026, 10, 12, 10, 0)),
        ("до 12.10.2026 10:00", datetime(2026, 10, 12, 10, 0)),
        ("12.10.2026 10:00:00", datetime(2026, 10, 12, 10, 0)),
        ("04.09.2026 в 14:00", datetime(2026, 9, 4, 14, 0)),
        ("12.10.2026", datetime(2026, 10, 12, 23, 59)),
        # пояс переводится в Москву, а не отбрасывается
        ("2026-10-22T20:00:00+03:00", datetime(2026, 10, 22, 20, 0)),
        ("2026-10-22T07:00:00Z", datetime(2026, 10, 22, 10, 0)),
        ("2026-10-22T14:00:00+07:00", datetime(2026, 10, 22, 10, 0)),
        ("мусор", None),
    ],
)
def test_разбор_срока_одинаков_во_всех_местах(value, expected):
    from src.dates import parse_deadline

    assert parse_deadline(value) == expected
    assert _deadline_at(value) == expected, "лента и Excel"
    assert Profile.deadline_at(value) == expected, "профиль"
    assert _parse_deadline(value) == (expected.date() if expected else None)
    assert deadline_unreadable(value) is (expected is None)


def test_сейчас_по_москве_а_не_по_часам_пк():
    from datetime import timezone

    from src.dates import now_msk

    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now_msk() - utc
    assert abs(delta.total_seconds() - 3 * 3600) < 5
