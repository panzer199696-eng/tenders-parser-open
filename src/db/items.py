"""
Состав закупки: хранение позиций лота (ТЗ-10).

Отдельный модуль, а не ещё одна функция в lots.py: запись лотов и запись
их состава — разные циклы жизни. Лоты пишутся по итогам обхода площадок,
состав — отдельно, если площадка его отдаёт, и с собственной отметкой
попытки items_fetched_at (без неё сборщик ходил бы за составом к одному и
тому же лоту вечно: у большинства лотов состава нет, и «пусто» — тоже
результат). Карточка лота в ленте и книга Excel показывают состав, если он
есть в базе; встроенные парсеры его сейчас не собирают.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def save_lot_items(conn, lot_key: str, items: list[dict]) -> int:
    """Сохранить состав лота. Возвращает число записанных позиций.

    Перезапись, а не слияние: состав — это снимок страницы позиций, и при
    повторном сборе он заменяется целиком (заказчик мог поправить перечень).
    DELETE + INSERT с одним PRIMARY KEY (lot_key, position) не плодит
    дублей при любом числе повторных сохранений.
    """
    conn.execute("DELETE FROM lot_items WHERE lot_key = ?", (lot_key,))
    conn.executemany(
        "INSERT INTO lot_items (lot_key, position, name, quantity, unit, price)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                lot_key,
                int(it.get("position") or (idx + 1)),
                str(it.get("name") or "").strip(),
                it.get("quantity"),
                it.get("unit"),
                it.get("price"),
            )
            for idx, it in enumerate(items or [])
            if str(it.get("name") or "").strip()  # позиция без имени — не позиция
        ],
    )
    conn.commit()
    return len(items or [])


def mark_items_fetched(conn, lot_key: str, at: datetime | None = None) -> None:
    """Отметить попытку сбора состава.

    Проставляется и при пустом составе: «посмотрели — позиций нет» — это
    результат, без отметки к такому лоту ходили бы каждый прогон.
    Не проставлять, если состав посмотреть не удалось: ту попытку честно
    повторить в следующий раз.
    """
    moment = (at or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE lots SET items_fetched_at = ?, updated_at = ? WHERE lot_key = ?",
        (moment, moment, lot_key),
    )
    conn.commit()


def load_lot_items(conn, lot_key: str) -> list[dict]:
    """Позиции лота по порядку — для карточки и отчётов."""
    return [
        {
            "position": r["position"],
            "name": r["name"],
            "quantity": r["quantity"],
            "unit": r["unit"],
            "price": r["price"],
        }
        for r in conn.execute(
            "SELECT position, name, quantity, unit, price FROM lot_items"
            " WHERE lot_key = ? ORDER BY position",
            (lot_key,),
        )
    ]


def lot_items_by_key(conn) -> dict[str, list[dict]]:
    """Весь состав одним запросом, сгруппированный по lot_key.

    Для отчётов, где нужны позиции всех лотов сразу (Excel): N запросов по
    лоту на книгу превращались бы в тысячи точечных заходов в базу.
    """
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT lot_key, position, name, quantity, unit, price FROM lot_items"
        " ORDER BY lot_key, position"
    ):
        out.setdefault(r["lot_key"], []).append(
            {
                "position": r["position"],
                "name": r["name"],
                "quantity": r["quantity"],
                "unit": r["unit"],
                "price": r["price"],
            }
        )
    return out
