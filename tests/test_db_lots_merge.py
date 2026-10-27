"""
Склейка публикаций и права обновления в save_lots (ревью данных 16.10.2026, Г1).

Сценарии взяты из воспроизведения ревьюера: лот пришёл сначала с агрегатора,
потом с первоисточника (Росэлторг). Что было сломано:
  • Первоисточник продлил срок и поднял цену — лот этого не видел: лицо лота сменилось
    на первоисточник, но ключ остался «Bidzaar|…», и первоисточник считался «чужой» публикацией
    с правом только дополнять пустое;
  • Первоисточник отдал тот же лот без номера процедуры — отпечаток стал другим, лот
    не нашёлся, вставка упёрлась в UNIQUE (platform, lot_id), и исключение
    роняло запись всей ночи.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, save_lots


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    return conn


def _lot(platform, lot_id, no, deadline, price=100.0, href="h", **over):
    base = {
        "platform": platform,
        "lot_id": lot_id,
        "tender_no": no,
        "name": "Герметик",
        "company": "АО Тест",
        "date_deadline": deadline,
        "sum": price,
        "href": href,
    }
    base.update(over)
    return base


def _lots(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT lot_key, platform, lot_id, date_deadline, sum FROM lots"
        )
    ]


@pytest.fixture
def glued(db):
    """Лот с агрегатора, склеенный с первоисточником: лицо — первоисточник, ключ — агрегатора."""
    save_lots(db, [_lot("Bidzaar", "bz-1", "4570001", "20.10.2026 10:00")])
    save_lots(db, [_lot("Росэлторг", "4570001", "4570001", "20.10.2026 10:00")])
    return db


def test_склейка_лицо_первоисточника_ключ_прежний(glued):
    assert _lots(glued) == [
        ("Bidzaar|bz-1", "Росэлторг", "4570001", "20.10.2026 10:00", 100.0)
    ]


def test_первоисточник_продлевает_срок_и_меняет_цену(glued):
    save_lots(
        glued, [_lot("Росэлторг", "4570001", "4570001", "27.10.2026 10:00", 120)]
    )
    assert _lots(glued) == [
        ("Bidzaar|bz-1", "Росэлторг", "4570001", "27.10.2026 10:00", 120.0)
    ]


def test_агрегатор_со_старыми_данными_не_откатывает_первоисточник(glued):
    save_lots(
        glued, [_lot("Росэлторг", "4570001", "4570001", "27.10.2026 10:00", 120)]
    )
    save_lots(glued, [_lot("Bidzaar", "bz-1", "4570001", "20.10.2026 10:00", 100)])
    assert _lots(glued) == [
        ("Bidzaar|bz-1", "Росэлторг", "4570001", "27.10.2026 10:00", 120.0)
    ]


def test_агрегатор_дополняет_пустое(glued):
    save_lots(
        glued, [_lot("Bidzaar", "bz-1", "4570001", "20.10.2026 10:00", inn="7700")]
    )
    assert glued.execute("SELECT inn FROM lots").fetchone()[0] == "7700"


def test_лицо_без_номера_процедуры_находится_по_площадке_и_номеру_лота(glued):
    """Отпечаток сменился (no: → nm:), но (площадка, номер лота) — тот же:
    это та же публикация, а не новый лот и не IntegrityError."""
    summary = save_lots(
        glued, [_lot("Росэлторг", "4570001", "", "27.10.2026 10:00", 120)]
    )
    assert summary["failed"] == 0
    assert _lots(glued) == [
        ("Bidzaar|bz-1", "Росэлторг", "4570001", "27.10.2026 10:00", 120.0)
    ]
    # Сильный отпечаток по номеру не подменяется слабым по названию.
    assert glued.execute("SELECT dedup_key FROM lots").fetchone()[0] == "no:4570001"


def test_агрегатор_без_номера_находится_по_месту_публикации(db):
    """Публикация агрегатора уже записана в lot_sources другого лота —
    без номера процедуры её всё равно не заводят новым лотом."""
    save_lots(db, [_lot("Росэлторг", "4570001", "4570001", "20.10.2026 10:00")])
    save_lots(db, [_lot("Bidzaar", "bz-1", "4570001", "20.10.2026 10:00")])
    save_lots(
        db,
        [_lot("Bidzaar", "bz-1", "", "21.10.2026 10:00", name="Герметик анионный")],
    )
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1
    assert _lots(db)[0][3] == "20.10.2026 10:00", "агрегатор не меняет срок лица"


def test_ключ_основного_лота_не_меняется(glued):
    """На lot_key ссылаются applications и history."""
    glued.execute(
        "INSERT INTO applications (lot_key, status) VALUES ('Bidzaar|bz-1', 'SELECTED')"
    )
    glued.commit()
    save_lots(glued, [_lot("Росэлторг", "4570001", "", "27.10.2026 10:00", 120)])
    save_lots(glued, [_lot("Bidzaar", "bz-1", "4570001", "20.10.2026 10:00")])
    keys = [r[0] for r in glued.execute("SELECT lot_key FROM lots")]
    assert keys == ["Bidzaar|bz-1"]


def test_ошибка_одной_строки_не_роняет_остальные(db):
    """Строка, которую база не приняла, — предупреждение и счётчик, а не
    потерянная ночь: остальные лоты пачки сохраняются."""
    bad = _lot("Росэлторг", "2", "2", "20.10.2026", price={"не": "число"})
    summary = save_lots(
        db,
        [
            _lot("Росэлторг", "1", "1", "20.10.2026"),
            bad,
            _lot("Росэлторг", "3", "3", "20.10.2026"),
        ],
    )
    assert summary["failed"] == 1
    assert summary["added"] == 2
    ids = sorted(r[0] for r in db.execute("SELECT lot_id FROM lots"))
    assert ids == ["1", "3"]
    # Недописанная строка откатилась целиком — и в местах публикации её нет.
    assert (
        db.execute("SELECT count(*) FROM lot_sources WHERE lot_id = '2'").fetchone()[0]
        == 0
    )


def test_время_встречи_места_публикации_обновляется(db):
    """seen_at — когда площадка показывала лот в последний раз: по нему
    корзина «Пропал — проверить» отличает живой лот от снятого."""
    save_lots(db, [_lot("Росэлторг", "1", "1", "20.10.2026")])
    db.execute("UPDATE lot_sources SET seen_at = '2026-10-01T03:00:00'")
    db.commit()
    save_lots(db, [_lot("Росэлторг", "1", "1", "20.10.2026")])
    seen = db.execute("SELECT seen_at FROM lot_sources").fetchone()[0]
    assert seen > "2026-10-01T03:00:00"


def test_повторная_запись_пачки_не_плодит_лотов(db):
    batch = [
        _lot("Bidzaar", "bz-1", "4570001", "20.10.2026 10:00"),
        _lot("Росэлторг", "4570001", "4570001", "20.10.2026 10:00"),
        _lot("Росэлторг", "4570002", "", "21.10.2026 10:00", name="Известь"),
        _lot("ТЭК-Торг", "t-9", "", "21.10.2026 10:00", name="Известь"),
    ]
    save_lots(db, batch)
    before = _lots(db)
    summary = save_lots(db, batch)
    assert summary["added"] == 0
    assert _lots(db) == before
