"""Хранение состава закупки (ТЗ-10).

Позиции — отдельный цикл жизни от лотов: пишутся отдельно от обхода и с
отметкой попытки items_fetched_at. Без неё программа
ходила бы к одному и тому же лоту вечно: у большинства лотов состава на
странице нет, и «пусто» — тоже результат.

База только временная (tmp_path + ensure_schema), как у соседей.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, load_lot
from src.db.items import (
    load_lot_items,
    lot_items_by_key,
    mark_items_fetched,
    save_lot_items,
)

@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _add_lot(conn, lot_id, **over):
    """Лот прямым INSERT-ом: тестам хранения оценка профиля не нужна."""
    row = {
        "lot_key": f"ТЭК|{lot_id}",
        "platform": "ТЭК-Торг",
        "lot_id": lot_id,
        "name": "Расходники, реактивы, посуда",
        "href": f"https://www.tektorg.ru/procedures/{lot_id}",
        "date_deadline": "30.09.2026",
        "core_profile": 1,
        "match_strength": "none",
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()


def _items(*names):
    return [
        {
            "position": i + 1,
            "name": n,
            "quantity": float(i + 1),
            "unit": "шт",
            "price": None,
        }
        for i, n in enumerate(names)
    ]


# ── Запись и чтение состава ──────────────────────────────────────────────────


def test_состав_сохраняется_и_читается_по_порядку(db):
    """Порядок position — это порядок строк на странице: карточка и Excel
    показывают состав так, как его видел заказчик, а не как сортировка
    захотела."""
    _add_lot(db, "1")
    save_lot_items(db, "ТЭК|1", _items("Перчатки", "Бутыли", "Стаканы"))
    loaded = load_lot_items(db, "ТЭК|1")
    assert [it["name"] for it in loaded] == ["Перчатки", "Бутыли", "Стаканы"]
    assert [it["position"] for it in loaded] == [1, 2, 3]
    assert loaded[0]["quantity"] == 1.0 and loaded[0]["unit"] == "шт"


def test_повторное_сохранение_того_же_состава_не_плодит_дублей(db):
    """Было 3 позиции — после перезаписи их снова 3, а не 6."""
    _add_lot(db, "1")
    for _ in range(2):
        save_lot_items(db, "ТЭК|1", _items("Перчатки", "Бутыли", "Стаканы"))
    assert len(load_lot_items(db, "ТЭК|1")) == 3


def test_короткий_состав_заменяет_старый_целиком(db):
    """Перезапись, а не слияние: состав — снимок страницы, заказчик мог
    поправить перечень, и хвост старого рядом с новым — враньё в базе."""
    _add_lot(db, "1")
    save_lot_items(db, "ТЭК|1", _items("Перчатки", "Бутыли", "Стаканы"))
    save_lot_items(db, "ТЭК|1", _items("Только стаканы"))
    assert [it["name"] for it in load_lot_items(db, "ТЭК|1")] == ["Только стаканы"]


# ── Отметка попытки ──────────────────────────────────────────────────────────


def test_отметка_ставится_даже_при_пустом_составе(db):
    """«Посмотрели — позиций нет» — результат: без отметки к этому лоту
    ходили бы каждый прогон (иначе будем ходить вечно)."""
    _add_lot(db, "1")
    save_lot_items(db, "ТЭК|1", [])
    mark_items_fetched(db, "ТЭК|1")
    assert db.execute(
        "SELECT items_fetched_at FROM lots WHERE lot_key = 'ТЭК|1'"
    ).fetchone()[0]


# ── Карточка лота и отчёты ───────────────────────────────────────────────────


def test_карточка_лота_отдаёт_состав(db):
    """Ключ items в load_lot — главный ответ на «что внутри сборного лота»."""
    _add_lot(db, "1")
    save_lot_items(db, "ТЭК|1", _items("Перчатки", "Бутыли"))
    assert [it["name"] for it in load_lot(db, "ТЭК|1")["items"]] == [
        "Перчатки",
        "Бутыли",
    ]


def test_у_лота_без_состава_в_карточке_пустой_список(db):
    """Пустой список — это «смотрели, позиций нет», а не отсутствие ключа:
    карточка и интерфейс не должны разбираться в устройстве базы."""
    _add_lot(db, "1")
    assert load_lot(db, "ТЭК|1")["items"] == []


def test_состав_по_ключам_одним_запросом(db):
    """lot_items_by_key — для Excel: точечные заходы по каждому лоту
    превращались бы в тысячи обращений к базе на одну книгу."""
    _add_lot(db, "1")
    _add_lot(db, "2")
    save_lot_items(db, "ТЭК|2", _items("Бутыли", "Стаканы"))
    save_lot_items(db, "ТЭК|1", _items("Перчатки", "Пипетки", "Штативы"))
    by_key = lot_items_by_key(db)
    assert [it["name"] for it in by_key["ТЭК|1"]] == ["Перчатки", "Пипетки", "Штативы"]
    assert [it["name"] for it in by_key["ТЭК|2"]] == ["Бутыли", "Стаканы"]
