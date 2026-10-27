# -*- coding: utf-8 -*-
"""
Тесты отчётов (T022).

Отчёт должен помогать принимать решения, а не показывать красивые числа.
Отсюда проверяемые здесь свойства:
  • молчащая площадка помечается явно — иначе она выглядит как «ничего
    не нашлось», и дорогой в поддержке парсер тихо перестаёт работать;
  • доля неизвестных условий оплаты называется прямо: «предоплаты почти нет» —
    это вывод о полноте наших данных, а не о рынке;
  • группа номенклатуры без попаданий показывается, а не скрывается.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema  # noqa: E402
from src.db.reports import by_group, by_platform, deadlines, funnel, summary  # noqa: E402


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _add(conn, lot_id, **over):
    row = {
        "lot_key": f"{over.get('platform', 'Росэлторг')}|{lot_id}",
        "platform": over.get("platform", "Росэлторг"),
        "lot_id": lot_id,
        "name": "Насос центробежный",
        "core_profile": 1,
        "client_level": "market",
        "tags": '["pumps"]',
        "prepayment": "unknown",
        "date_deadline": "10.09.2026",
        "last_seen_at": datetime.now().isoformat(timespec="seconds"),
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()


def test_вклад_площадок_считается(db):
    _add(db, "1", platform="Росэлторг")
    _add(db, "2", platform="ТЭК-Торг")
    _add(db, "3", platform="ТЭК-Торг")
    rows = {r["platform"]: r["total"] for r in by_platform(db)}
    assert rows == {"Росэлторг": 1, "ТЭК-Торг": 2}


def test_молчащая_площадка_помечается(db):
    """Иначе переставший работать парсер выглядит как пустой рынок."""
    old = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    _add(db, "1", platform="Старая площадка", last_seen_at=old)
    _add(db, "2", platform="Росэлторг")
    rows = {r["platform"]: r["silent"] for r in by_platform(db, days=30)}
    assert rows["Старая площадка"] is True
    assert rows["Росэлторг"] is False


def test_профильные_и_клиентские_лоты_считаются_отдельно(db):
    _add(db, "1", core_profile=1, client_level="market")
    _add(db, "2", core_profile=0, client_level="full")
    row = by_platform(db)[0]
    assert row["core"] == 1
    assert row["clients"] == 1


def test_группы_номенклатуры_считаются(db):
    _add(db, "1", tags='["pumps"]')
    _add(db, "2", tags='["pumps", "valves"]')
    rows = {g["id"]: g["lots"] for g in by_group(db)}
    assert rows == {"pumps": 2, "valves": 1}


def test_группы_показываются_названием_а_не_кодом(db):
    """«valves» ни о чём не говорит тому, кто работает с лотами."""
    _add(db, "1", tags='["pumps"]')
    row = by_group(db)[0]
    assert row["id"] == "pumps"
    assert row["group"] != "pumps", "должно подставиться название из справочника"


def test_битые_теги_не_ломают_отчёт(db):
    _add(db, "1", tags="не json")
    _add(db, "2", tags='["pumps"]')
    rows = by_group(db)
    assert len(rows) == 1 and rows[0]["id"] == "pumps" and rows[0]["lots"] == 1


def test_сроки_считаются_на_дату(db):
    _add(db, "1", date_deadline="02.09.2026")   # горит
    _add(db, "2", date_deadline="06.09.2026")   # на неделе
    _add(db, "3", date_deadline="20.08.2026")   # истёк
    row = deadlines(db, today=date(2026, 9, 1))
    assert row["urgent"] == 1
    assert row["this_week"] == 1
    assert row["expired"] == 1


def test_чужие_лоты_в_сроки_не_попадают(db):
    """Считаем сроки по нашему, а не по всей ленте площадки."""
    _add(db, "1", core_profile=0, client_level="market", date_deadline="02.09.2026")
    assert deadlines(db, today=date(2026, 9, 1))["urgent"] == 0


def test_воронка_и_причины_отказов(db):
    _add(db, "1")
    _add(db, "2")
    db.execute("INSERT INTO applications (lot_key, status) VALUES ('Росэлторг|1','SELECTED')")
    db.execute(
        "INSERT INTO applications (lot_key, status, reject_reason)"
        " VALUES ('Росэлторг|2','REJECTED','не наш профиль')"
    )
    db.commit()
    data = funnel(db)
    assert data["steps"] == {"SELECTED": 1, "REJECTED": 1}
    assert data["reject_reasons"][0]["reason"] == "не наш профиль"


def test_сводка_называет_долю_неизвестной_оплаты(db):
    """«Предоплаты почти нет» — вывод о наших данных, а не о рынке."""
    _add(db, "1", prepayment="unknown")
    _add(db, "2", prepayment="yes")
    data = summary(db)
    assert data["total_lots"] == 2
    assert data["prepaid"] == 1
    assert data["payment_unknown"] == 1


def test_пустая_база_не_роняет_отчёт(db):
    data = summary(db)
    assert data["total_lots"] == 0
    assert data["platforms"] == []
