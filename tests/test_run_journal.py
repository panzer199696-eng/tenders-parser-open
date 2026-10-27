"""Журнал прогонов: полнота сбора должна пережить сам прогон.

Программа считает полноту (``summarize_units``), но раньше писала её только в
лог. Соседние программы показывают цифры по базе и без журнала выдают
неполный сбор за полный — руководитель видит «найдено 1546» и считает, что
это весь рынок. Здесь проверяется, что итоги записываются, читаются обратно и
объясняются человеческим языком.

Сеть не трогается: файлы пишутся в tmp_path через TENDERSPARSER_DATA_DIR.
"""

import json
from datetime import datetime, timedelta

import pytest

from src import run_journal
from src.parsers import completeness as comp


@pytest.fixture
def каталог(tmp_path, monkeypatch):
    """Журнал уводим в tmp_path — рабочие файлы пользователя не трогаем."""
    monkeypatch.setenv("TENDERSPARSER_DATA_DIR", str(tmp_path))
    return tmp_path / "data"


СТАРТ = datetime(2026, 8, 27, 3, 0, 0)
ФИНИШ = datetime(2026, 8, 27, 3, 10, 0)

НАСТРОЙКИ = {
    "parse_tektorg": True,
    "parse_tender_pro": True,
    "parse_zakupki": False,
    "parse_gpb": False,
}


def полный_объект(label='АО "Ромашка"'):
    return comp.build_unit_report(
        "firm", label, expected_pages=6, collected_pages=6, collected_lots=51
    )


def урезанный_объект(label="Насосы"):
    """Объект, обойдённый частично из-за прав аккаунта — наш живой случай."""
    r = comp.build_unit_report(
        "phrase", label, expected_pages=27, collected_pages=1, collected_lots=20
    )
    r["reason"] = comp.REASON_LIMITED_ACCOUNT
    return r


def запись(**поля):
    базовые = dict(
        started_at=СТАРТ,
        finished_at=ФИНИШ,
        app_version="2.1.0",
        outcome=run_journal.OUTCOME_OK,
        settings=НАСТРОЙКИ,
        unit_reports=[полный_объект()],
        lots_found=1546,
        lots_new=24,
    )
    базовые.update(поля)
    return run_journal.build_record(**базовые)


# ── Запись и чтение ──────────────────────────────────────────────────────────


class TestЗаписьЧтение:
    def test_запись_читается_обратно(self, каталог):
        """Без этого журнала нет вовсе: пишем — значит должны прочитать."""
        run_journal.write_run(запись())
        прочитано = run_journal.read_last_run()
        assert прочитано is not None
        assert прочитано["lots_found"] == 1546
        assert прочитано["data_complete"] is True

    def test_второй_прогон_дописывает_историю_а_не_затирает(self, каталог):
        """История нужна для ответа «а вчера как было» — затирать нельзя."""
        run_journal.write_run(запись(lots_found=100))
        run_journal.write_run(запись(lots_found=200))
        история = run_journal.read_runs()
        assert [r["lots_found"] for r in история] == [200, 100]  # свежие первыми
        assert run_journal.read_last_run()["lots_found"] == 200

    def test_битый_файл_даёт_none_а_не_исключение(self, каталог):
        """Оборванная запись не должна ронять экран, который её читает."""
        run_journal.write_run(запись())
        run_journal.last_run_path().write_text("{это не json", encoding="utf-8")
        assert run_journal.read_last_run() is None

    def test_журнала_нет_вовсе(self, каталог):
        assert run_journal.read_last_run() is None
        assert run_journal.read_runs() == []

    def test_битая_строка_истории_пропускается(self, каталог):
        """Одна испорченная строка не должна прятать остальные прогоны."""
        run_journal.write_run(запись(lots_found=100))
        with run_journal.history_path().open("a", encoding="utf-8") as f:
            f.write("{оборвано\n")
        run_journal.write_run(запись(lots_found=300))
        assert [r["lots_found"] for r in run_journal.read_runs()] == [300, 100]

    def test_секретов_в_файле_нет(self, каталог):
        """Файл читают другие программы и он переживает прогон."""
        run_journal.write_run(запись())
        текст = run_journal.last_run_path().read_text(encoding="utf-8").lower()
        for слово in ("password", "cookie", "phpsessid", "token", "openid"):
            assert слово not in текст


# ── Фраза для человека ───────────────────────────────────────────────────────


class TestОписание:
    def test_полный_сбор_говорит_что_данные_полные(self):
        текст = run_journal.describe(запись())
        assert "данные полные" in текст
        assert "27.08" in текст

    def test_неполный_сбор_называет_причину_словами_а_не_кодом(self):
        """`limited_account` ничего не значит ни менеджеру, ни руководителю."""
        r = запись(unit_reports=[полный_объект(), урезанный_объект()])
        текст = run_journal.describe(r)
        assert "не полностью" in текст
        assert "1 объектов из 2" in текст
        assert comp.REASON_LIMITED_ACCOUNT not in текст
        assert comp.reason_text(comp.REASON_LIMITED_ACCOUNT) in текст

    def test_неизвестное_число_новых_не_выдаётся_за_ноль(self):
        """Ноль соврёт «ничего нового не появилось» — это разные вещи."""
        текст = run_journal.describe(запись(lots_new=None))
        assert "новых: неизвестно" in текст
        assert "новых 0" not in текст

    def test_прерванный_ошибкой_прогон_честно_называется_неполным(self):
        текст = run_journal.describe(
            запись(outcome=run_journal.OUTCOME_ERROR, error="браузер не запустился")
        )
        assert "заведомо неполные" in текст
        assert "браузер не запустился" in текст

    def test_остановленный_вручную_прогон_тоже_неполный(self):
        текст = run_journal.describe(запись(outcome=run_journal.OUTCOME_STOPPED))
        assert "остановлен вручную" in текст
        assert "неполные" in текст

    def test_журнала_нет_так_и_сказано(self):
        assert "не запускался" in run_journal.describe(None)


class TestСвежесть:
    def test_вчерашний_прогон_считается_устаревшим(self):
        """Цифры суточной давности надо оговаривать, а не показывать молча."""
        r = запись()
        assert run_journal.is_stale(r, now=ФИНИШ + timedelta(days=2)) is True

    def test_сегодняшний_прогон_свежий(self):
        r = запись()
        assert run_journal.is_stale(r, now=ФИНИШ + timedelta(hours=3)) is False

    def test_без_журнала_считаем_устаревшим(self):
        assert run_journal.is_stale(None) is True


# ── Состав записи ────────────────────────────────────────────────────────────


class TestСоставЗаписи:
    def test_площадки_записаны_как_были_в_настройках(self):
        r = запись()
        assert r["platforms"]["parse_tektorg"] is True
        assert r["platforms"]["parse_zakupki"] is False

    def test_неполные_объекты_перечислены_с_причиной(self):
        r = запись(unit_reports=[урезанный_объект("Задвижки")])
        assert r["data_complete"] is False
        [плохой] = r["incomplete_units"]
        assert плохой["label"] == "Задвижки"
        assert плохой["reason"] == comp.REASON_LIMITED_ACCOUNT
        assert плохой["reason_text"]

    def test_без_объектов_обхода_полнота_не_утверждается_числом(self):
        """Работал только keyword-режим чужой площадки — проверять нечего."""
        r = запись(unit_reports=[])
        assert r["units"]["total"] == 0
        assert "данные полные" in run_journal.describe(r)

    def test_запись_сериализуется_в_json(self):
        """Иначе write_run упадёт уже на живом прогоне."""
        json.dumps(запись(), ensure_ascii=False)


# ── Устойчивость ─────────────────────────────────────────────────────────────


def test_сбой_записи_не_роняет_вызывающего(каталог, monkeypatch):
    """Журнал — не повод потерять час сбора."""

    def падает(_record):
        raise OSError("диск переполнен")

    monkeypatch.setattr(run_journal, "write_run", падает)
    assert run_journal.write_run_safe(запись()) is None


def test_готовая_фраза_лежит_в_самой_записи():
    """Формулировка должна быть одна на всех: соседняя программа читает поле,
    а не пишет свой вариант описания."""
    r = запись(unit_reports=[урезанный_объект()])
    assert r["summary"] == run_journal.describe(r)
    assert "не полностью" in r["summary"]


def test_остановленный_прогон_не_считается_полным():
    """В журнале стояло «data_complete: true» рядом с «данные неполные».

    Прогон, оборванный человеком или ошибкой, до остальных площадок не дошёл —
    полным он быть не может, что бы ни показали пройденные объекты обхода.
    """
    from datetime import datetime

    from src.run_journal import build_record

    for outcome in ("stopped", "error"):
        record = build_record(
            started_at=datetime(2026, 8, 31, 3, 0),
            finished_at=datetime(2026, 8, 31, 3, 5),
            app_version="2.2.0",
            outcome=outcome,
            error="" if outcome == "stopped" else "сеть недоступна",
            settings={},
            unit_reports=[],
            lots_found=None,
            lots_new=None,
        )
        assert record["data_complete"] is False, outcome


def test_успешный_прогон_без_объектов_обхода_считается_полным():
    """Обратный случай: keyword-режим объектов обхода не создаёт."""
    from datetime import datetime

    from src.run_journal import build_record

    record = build_record(
        started_at=datetime(2026, 8, 31, 3, 0),
        finished_at=datetime(2026, 8, 31, 3, 5),
        app_version="2.2.0",
        outcome="ok",
        error="",
        settings={},
        unit_reports=[],
        lots_found=10,
        lots_new=3,
    )
    assert record["data_complete"] is True


# ── Сетевая ночь (12.10.2026) ────────────────────────────────────────────────


def _night(dead_platforms: int) -> dict:
    rows = []
    for i in range(dead_platforms):
        rows.append({"platform": f"P{i}", "units": 5, "complete": 0, "partial": 0,
                     "skipped": 5, "data_complete": False,
                     "reason": "обрыв связи с площадкой"})
    rows.append({"platform": "ТЭК-Торг", "units": 115, "complete": 115, "partial": 0,
                 "skipped": 0, "data_complete": True, "reason": ""})
    return {"outcome": "ok", "started_at": "2026-10-10T03:00:00",
            "finished_at": "2026-10-10T06:05:00", "lots_found": 436, "lots_new": 71,
            "units": {"total": 115 + 5 * dead_platforms, "complete": 115},
            "data_complete": False, "by_platform": rows, "incomplete_units": []}


def test_три_молчащие_площадки_это_связь_а_не_площадки():
    from src import run_journal
    assert "не было связи с площадками" in run_journal.describe(_night(3))


def test_одна_молчащая_площадка_без_подсказки_про_связь():
    from src import run_journal
    assert "не было связи" not in run_journal.describe(_night(1))
