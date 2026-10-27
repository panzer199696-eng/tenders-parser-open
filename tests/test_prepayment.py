"""
Тесты определения предоплаты (T034–T036).

Главное правило, ради которого всё написано: **«не знаем» не равно «нет»**.
Программа видит только текст лота — полные условия оплаты лежат во вложениях,
и разбор вложений отложен. Выдать незнание за отсутствие значит заставить
человека зря пропустить хороший лот.

Второе по важности — приоритет отрицаний: «аванс не предусмотрен» содержит
слово «аванс», и без этого приоритета лот получил бы ровно обратную пометку.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, load_feed, save_lots
from src.prepayment import describe, detect, sort_bonus

# ── Три состояния ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Поставка центробежных насосов, аванс 30% в течение 5 дней",
        "Поставка антисептика, 100% предоплата",
        "Поставка с предварительной оплатой 50%",
        "Условия: оплата до отгрузки",
    ],
)
def test_предоплата_распознаётся(text):
    state, quote = detect(text)
    assert state == "yes"
    assert quote, "рядом с пометкой должна быть фраза, по которой решили"


@pytest.mark.parametrize(
    "text",
    [
        "Поставка реагентов. Авансовый платёж не предусмотрен",
        "Поставка герметика, оплата по факту поставки",
        "Отсрочка платежа 60 календарных дней",
        "Поставка без аванса",
        "Постоплата в течение 30 дней",
    ],
)
def test_отсутствие_предоплаты_распознаётся(text):
    assert detect(text)[0] == "no"


@pytest.mark.parametrize(
    "text",
    [
        "Поставка футеровки мельницы МШР 3.2х3.1",
        "Антисептик калия бутиловый (225 т)",
        "",
    ],
)
def test_молчание_текста_даёт_неизвестно(text):
    """Ключевое: не «нет», а «не знаем» (FR-012)."""
    assert detect(text)[0] == "unknown"


def test_отрицание_сильнее_упоминания():
    """«Аванс не предусмотрен» — это отсутствие аванса, а не его наличие."""
    assert detect("Аванс не предусмотрен")[0] == "no"
    assert detect("Предоплата не производится")[0] == "no"


def test_текст_собирается_из_нескольких_частей():
    """Условия могут быть не в названии, а в дополнительной информации."""
    state, _ = detect("Поставка реагентов", "", "Порядок расчётов: аванс 30%")
    assert state == "yes"


def test_описание_состояний_человеческое():
    assert describe("yes") == "предоплата"
    assert describe("no") == "без предоплаты"
    assert describe("unknown") == "условие не найдено"


def test_бонус_только_за_предоплату():
    assert sort_bonus("yes") == 10
    assert sort_bonus("no") == 0
    assert sort_bonus("unknown") == 0


# ── Сохранение и лента ───────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


# Ленту читаем на фиксированную дату: корзина зависит от того, сколько
# осталось до дедлайна, и без явной даты тест начинает падать сам по себе,
# когда календарь доходит до дедлайна фикстуры (07.09.2026).
FEED_TODAY = date(2026, 9, 1)
FEED_NOW = datetime(2026, 9, 1, 10, 0)


def _lot(lot_id, name, **over):
    base = {
        "platform": "Росэлторг",
        "lot_id": lot_id,
        "name": name,
        "company": "АО Рудник",
        "date_deadline": "10.09.2026",
    }
    base.update(over)
    return base


def test_состояние_сохраняется_с_цитатой(db):
    save_lots(db, [_lot("1", "Поставка насосов, аванс 30%")], None)
    row = db.execute("SELECT prepayment, prepayment_note FROM lots").fetchone()
    assert row["prepayment"] == "yes"
    assert "аванс" in row["prepayment_note"].lower()


def test_у_молчаливого_лота_остаётся_unknown(db):
    save_lots(db, [_lot("1", "Поставка футеровки")], None)
    row = db.execute("SELECT prepayment, prepayment_note FROM lots").fetchone()
    assert row["prepayment"] == "unknown"
    assert row["prepayment_note"] is None


def test_лот_с_предоплатой_выше_равного_без_неё(db):
    """FR-013: предоплата поднимает лот среди равных по профилю."""
    save_lots(
        db,
        [
            _lot("1", "Поставка центробежных насосов"),
            _lot("2", "Поставка центробежных насосов, аванс 30%"),
        ],
        None,
    )
    db.execute("UPDATE lots SET score = 50, core_profile = 1, tags = '[\"reagents\"]'")
    db.commit()

    feed = load_feed(db, today=FEED_TODAY, now=FEED_NOW)
    core = feed["buckets"]["core"]
    assert [lot["lot_id"] for lot in core] == ["2", "1"]


def test_предоплата_не_перевешивает_профиль(db):
    """Профильный лот без аванса важнее непрофильного с авансом."""
    save_lots(
        db,
        [
            _lot("1", "Поставка центробежных насосов"),
            _lot("2", "Поставка канцтоваров, аванс 100%"),
        ],
        None,
    )
    db.execute(
        "UPDATE lots SET score = 60, core_profile = 1, tags = '[\"reagents\"]' WHERE lot_id='1'"
    )
    db.execute(
        "UPDATE lots SET score = 5, core_profile = 1, tags = '[\"other\"]' WHERE lot_id='2'"
    )
    db.commit()

    feed = load_feed(db, today=FEED_TODAY, now=FEED_NOW)
    assert [lot["lot_id"] for lot in feed["buckets"]["core"]] == ["1", "2"]


# ── Живые формулировки площадок ──────────────────────────────────────────────
#
# Условия оплаты в лоте почти никогда не называются «постоплатой»: площадка
# пишет их деловым языком. Эти случаи — типичные формулировки
# карточек лотов на площадках.


@pytest.mark.parametrize(
    "text",
    [
        "Покупатель обязуется оплатить Поставщику денежные средства в размере 100% "
        "от стоимости данной партии товаров в срок не позднее 15 рабочих дней "
        "с даты предоставления отгрузочных документов",
        "оплата в течение 30 дней с даты поставки",
        "оплата не позднее 10 рабочих дней после подписания акта",
        "оплата производится в течение 45 календарных дней с момента получения товара",
    ],
)
def test_постоплата_без_слова_постоплата(text):
    """«100% от стоимости» — это не предоплата, если платят после отгрузки."""
    assert detect(text)[0] == "no", text


def test_частичный_аванс_остаётся_предоплатой():
    """Аванс 30% с остатком после отгрузки — предоплата всё равно есть."""
    assert detect("аванс 30%, остаток в течение 15 дней с даты отгрузки")[0] == "yes"
