"""Эксплуатационные предохранители (ревью 16.10.2026, Д3 и А4).

* страж живых файлов в conftest замечает не только пропажу файла, но и
  его подмену (размер, время изменения): тест, переписавший живую базу,
  раньше проходил молча — файл ведь на месте;
* соединение с единой базой ждёт занятую базу, а не падает сразу
  «database is locked», когда ночной сбор и окно программы пишут вместе.
"""

import os
import sqlite3

import conftest


def _make(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_страж_замечает_пропажу_и_подмену_живого_файла(tmp_path):
    _make(tmp_path, "settings.json", "{}")
    db = _make(tmp_path, "data/tenderwork.db", "старая база")
    gone = _make(tmp_path, "data/runs.jsonl", "{}\n")
    before = conftest._live_snapshot(tmp_path)

    db.write_text("тест переписал живую базу", encoding="utf-8")
    gone.unlink()
    lost, changed = conftest._compare_live(before, conftest._live_snapshot(tmp_path))

    assert lost == ["data/runs.jsonl"]
    assert changed == ["data/tenderwork.db"]


def test_страж_замечает_подмену_того_же_размера(tmp_path):
    path = _make(tmp_path, "data/tenderwork.db", "abc")
    before = conftest._live_snapshot(tmp_path)
    path.write_text("xyz", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    _lost, changed = conftest._compare_live(before, conftest._live_snapshot(tmp_path))
    assert changed == ["data/tenderwork.db"]


def test_страж_не_тревожится_без_изменений(tmp_path):
    _make(tmp_path, "settings.json", "{}")
    _make(tmp_path, "data/tenderwork.db", "база")
    before = conftest._live_snapshot(tmp_path)
    assert conftest._compare_live(before, conftest._live_snapshot(tmp_path)) == ([], [])


def test_соединение_с_базой_ждёт_занятую_базу(monkeypatch, tmp_path):
    from src.db import schema

    seen = {}
    real = sqlite3.connect

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(schema.sqlite3, "connect", spy)
    conn = schema.connect(tmp_path / "t.db")
    conn.close()
    assert seen.get("timeout") == 30
