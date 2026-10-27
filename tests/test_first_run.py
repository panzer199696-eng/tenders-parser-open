# -*- coding: utf-8 -*-
"""
Тесты первого запуска и обновления программы (T039, T040).

Два сценария, в которых пользователь легко теряет данные или упирается
в непонятную ошибку:

1. **Обновление.** Лоты и реестр лежат в прежних базах. Если их не забрать,
   человек откроет новую версию и увидит пустую ленту — для него это
   выглядит как потеря всей работы.

2. **Чистая установка.** Справочников ещё нет. Без них профиль не собирается,
   и программа падала при первой же оценке лота — на это уже наступили.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, migrate  # noqa: E402
from src.runtime_paths import USER_FILES, _seed_source  # noqa: E402

OLD_TENDERS = """
CREATE TABLE tenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, name TEXT, company TEXT,
    href TEXT UNIQUE, sum REAL, date_published TEXT, date_deadline TEXT,
    date_to_display TEXT, created_at TEXT, search_key TEXT, tender_no TEXT,
    lot_id TEXT, tender_type TEXT, organizer_match INTEGER, organizer_href TEXT,
    construction INTEGER, tags TEXT, score INTEGER, promising INTEGER
);
"""


def test_справочники_входят_в_посев():
    """Без них чистая установка падает при первой оценке лота."""
    for name in ("data/nomenclature.json", "data/clients.json", "data/rules.json"):
        assert name in USER_FILES, f"{name} не сеется при первом запуске"


def test_справочник_находится_в_поставке(tmp_path):
    """Файл из подпапки лежит в плоском defaults/ — посев должен его найти."""
    bundle = tmp_path / "bundle"
    (bundle / "defaults").mkdir(parents=True)
    (bundle / "defaults" / "clients.json").write_text("{}", encoding="utf-8")

    source, move_it = _seed_source("data/clients.json", tmp_path / "base", bundle)
    assert source is not None, "справочник не найден в поставке"
    assert source.name == "clients.json"
    assert move_it is False, "файл поставки копируется, а не переносится"


def test_обновление_забирает_лоты_из_прежней_базы(tmp_path):
    """Иначе пользователь увидит пустую ленту и решит, что потерял работу."""
    old = tmp_path / "tenders.db"
    conn = sqlite3.connect(old)
    conn.executescript(OLD_TENDERS)
    conn.execute(
        "INSERT INTO tenders (platform, name, company, href, lot_id, created_at)"
        " VALUES ('Росэлторг','Компаунд заливочный','АО Кедр','https://example.invalid/1','1','2026-08-01 10:00')"
    )
    conn.commit()
    conn.close()

    target = tmp_path / "tenderwork.db"
    report = migrate([old], target)
    assert report.lots_added == 1

    conn = connect(target)
    ensure_schema(conn)
    assert conn.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_перенос_не_трогает_прежнюю_базу(tmp_path):
    """Откат обновления — это удаление новой базы, старая должна быть цела."""
    old = tmp_path / "tenders.db"
    conn = sqlite3.connect(old)
    conn.executescript(OLD_TENDERS)
    conn.execute(
        "INSERT INTO tenders (platform, name, href, lot_id) VALUES ('ROS','Лот','h','1')"
    )
    conn.commit()
    conn.close()
    before = old.read_bytes()

    migrate([old], tmp_path / "new.db")
    assert old.read_bytes() == before


def test_повторное_обновление_ничего_не_дублирует(tmp_path):
    """Программу запускают каждый день — перенос обязан быть идемпотентным."""
    old = tmp_path / "tenders.db"
    conn = sqlite3.connect(old)
    conn.executescript(OLD_TENDERS)
    conn.execute(
        "INSERT INTO tenders (platform, name, href, lot_id) VALUES ('ROS','Лот','h','1')"
    )
    conn.commit()
    conn.close()

    target = tmp_path / "new.db"
    migrate([old], target)
    migrate([old], target)

    conn = connect(target)
    assert conn.execute("SELECT count(*) FROM lots").fetchone()[0] == 1


def test_отсутствие_прежних_баз_не_ошибка(tmp_path):
    """Чистая установка — обычный случай, а не сбой."""
    report = migrate([tmp_path / "нет.db"], tmp_path / "new.db")
    assert report.lots_added == 0
    assert any("не найден" in note for note in report.notes)
