"""Межпрогонная проверка площадок (ревью 16.10.2026, В3).

Сторож внутри прогона не видит «тихих» поломок: 14–16.10 Bidzaar отдавал
старую ленту (новых почти ноль, свежайшая публикация 13.10), 16.10
ТЭК-Торг принёс 12 лотов вместо обычной сотни — и оба раза журнал писал
«собрано целиком». Проверка сравнивает площадку с медианой последних
ночей и добавляет отчёт «площадка целиком» с причиной.
"""

from datetime import date, datetime, timedelta

import pytest

from src.core import run_health as rh
from src.db import connect, ensure_schema
from src.db.runs import record_run
from src.parsers import completeness as c

# Пятница 16.10.2026, ночной прогон 03:00 — покрывает четверг 15.10.
СЕЙЧАС = datetime(2026, 10, 16, 3, 0, 1)


def _night(
    day: date, lots, new, newest=None, complete=True, trigger=None, outcome="ok"
):
    return {
        "started_at": datetime(day.year, day.month, day.day, 3, 0, 1).isoformat(),
        "lots": lots,
        "lots_new": new,
        "newest_published": newest,
        "complete": complete,
        "trigger": trigger,
        "outcome": outcome,
    }


def _history(values, start=date(2026, 10, 15)):
    """Ночи от вчерашней назад: values — [(lots, new), ...]."""
    out = []
    day = start
    for lots, new in values:
        out.append(_night(day, lots, new))
        day -= timedelta(days=1)
    return out


# Будничная история: 7 ночей с 15.10 назад (11.10 и 12.10 покрывают выходные).
ОБЫЧНО = _history(
    [(470, 100), (480, 110), (460, 95), (450, 5), (455, 3), (465, 105), (475, 120)]
)


def _check(lots, new, newest="2026-10-15", history=ОБЫЧНО, now=СЕЙЧАС):
    return rh.check_platform("Bidzaar", lots, new, newest, now, history)


# ── Календарь ────────────────────────────────────────────────────────────────


def test_ночь_покрывает_предыдущий_день():
    assert rh.covered_day(datetime(2026, 10, 16, 3, 0)) == date(2026, 10, 15)
    assert rh.covered_day(datetime(2026, 10, 16, 7, 0)) == date(2026, 10, 15)


def test_рабочие_дни_между_датами():
    # после пятницы 09.10 до вторника 13.10: пн 12, вт 13
    assert rh.workdays_between(date(2026, 10, 9), date(2026, 10, 13)) == 2
    assert rh.workdays_between(date(2026, 10, 13), date(2026, 10, 13)) == 0


# ── Правила ──────────────────────────────────────────────────────────────────


def test_здоровая_площадка_молчит():
    assert _check(468, 98) is None


def test_ноль_лотов_при_обычной_сотне():
    v = _check(0, 0)
    assert v.reason == c.REASON_EMPTY_PLATFORM
    assert "вчера 470, сегодня 0" in v.note


def test_лотов_меньше_трети_медианы_это_аномалия():
    v = _check(12, 0)
    assert v.reason == c.REASON_ANOMALY
    assert "вчера 470, сегодня 12" in v.note and "обычно 470" in v.note


def test_два_будних_нуля_новых_подряд_лента_стоит():
    history = [_night(date(2026, 10, 15), 470, 0)] + ОБЫЧНО[1:]
    v = _check(460, 0, history=history)
    assert v.reason == c.REASON_STALE
    assert "новых вчера 0, сегодня 0" in v.note


def test_один_ноль_новых_ещё_не_тревога():
    # Один ноль при обычной сотне — уже обвал (правило «новых < 10%»),
    # но не «лента стоит»: стоп-сигнал только на две ночи подряд.
    v = _check(460, 0)
    assert v.reason == c.REASON_ANOMALY


def test_выходные_не_тревожат():
    # Понедельник 12.10 03:00 покрывает воскресенье: новых ноль — нормально.
    now = datetime(2026, 10, 12, 3, 0, 1)
    history = _history(
        [(450, 3), (465, 105), (475, 120), (470, 100)], start=date(2026, 10, 11)
    )
    assert rh.check_platform("Bidzaar", 455, 0, "2026-10-09", now, history) is None


def test_обвал_новых_в_будни_тревожит():
    """Bidzaar 14.10: новых 2 при обычной сотне — лента почти стоит."""
    v = _check(345, 2)
    assert v.reason == c.REASON_ANOMALY
    assert "новых вчера 100, сегодня 2" in v.note


def test_свежайшая_публикация_старше_двух_рабочих_дней():
    # покрываемый день — чт 15.10; после пн 12.10: вт, ср, чт = 3 рабочих дня
    v = _check(460, 50, newest="2026-10-12")
    assert v.reason == c.REASON_STALE
    assert "12.10" in v.note


def test_свежайшая_публикация_малой_площадки_не_тревожит():
    # Росэлторг: новых обычно 0–2 — старая публикация здесь норма
    history = _history([(7, 0), (7, 1), (6, 0), (7, 0), (7, 0), (8, 2), (7, 0)])
    assert rh.check_platform("Росэлторг", 7, 0, "2026-10-07", СЕЙЧАС, history) is None


def test_без_истории_проверка_молчит():
    assert _check(0, 0, history=ОБЫЧНО[:2]) is None


def test_медиана_только_по_здоровым_ночам():
    # три ночи с обрывом связи (complete=0) не тянут медиану вниз
    bad = [
        _night(date(2026, 10, 15) - timedelta(days=i), 20, 0, complete=False)
        for i in range(3)
    ]
    v = _check(100, 90, history=bad + ОБЫЧНО)
    assert v.reason == c.REASON_ANOMALY


# ── История из базы и вердикт в отчётах ─────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "t.db")
    ensure_schema(db)
    yield db
    db.close()


def _run(conn, day, rows, trigger=None, outcome="ok"):
    started = datetime(day.year, day.month, day.day, 3, 0, 1)
    return record_run(
        conn,
        {
            "started_at": started.isoformat(),
            "finished_at": started.isoformat(),
            "outcome": outcome,
            "trigger": trigger,
            "platforms": {},
            "by_platform": [
                {
                    "platform": p,
                    "units": 1,
                    "data_complete": True,
                    "lots": lots,
                    "lots_new": new,
                    "newest_published": None,
                }
                for p, lots, new in rows
            ],
        },
    )


def test_история_только_ночные_успешные_с_лотами(conn):
    _run(conn, date(2026, 10, 13), [("Bidzaar", 400, 90)])
    _run(conn, date(2026, 10, 14), [("Bidzaar", 410, 95)])
    _run(conn, date(2026, 10, 14), [("Bidzaar", 3, 0)], trigger="catchup")
    _run(conn, date(2026, 10, 15), [("Bidzaar", 5, 0)], outcome="error")
    _run(conn, date(2026, 10, 15), [("Bidzaar", None, None)])
    got = rh.night_history(conn, "Bidzaar")
    assert [h["lots"] for h in got] == [410, 400]


def test_проверка_прогона_добавляет_отчёт_площадки(conn):
    for i, (lots, new) in enumerate([(120, 10), (130, 12), (115, 15), (125, 9)]):
        _run(conn, date(2026, 10, 12) + timedelta(days=i), [("ТЭК-Торг", lots, new)])
    reports = [
        c.build_unit_report(
            "phrase", "ф", platform="ТЭК-Торг", expected_pages=1, collected_pages=1
        )
    ]
    notes = rh.check_run(
        conn,
        СЕЙЧАС,
        reports,
        platform_lots={"ТЭК-Торг": 12},
        platform_new={"ТЭК-Торг": 0},
        platform_newest={"ТЭК-Торг": "2026-10-09"},
    )
    added = [r for r in reports if r["kind"] == c.KIND_PLATFORM]
    assert [(r["platform"], r["reason"]) for r in added] == [
        ("ТЭК-Торг", c.REASON_ANOMALY)
    ]
    assert c.summarize_units(reports)["data_complete"] is False
    assert notes == ["ТЭК-Торг: лотов вчера 125, сегодня 12 (обычно 125)"]


def test_уже_неполная_площадка_не_проверяется(conn):
    for i in range(4):
        _run(conn, date(2026, 10, 12) + timedelta(days=i), [("ТЭК-Торг", 120, 10)])
    reports = [c.failed_unit("phrase", "ф", "ТЭК-Торг", c.REASON_NETWORK)]
    notes = rh.check_run(
        conn,
        СЕЙЧАС,
        reports,
        platform_lots={"ТЭК-Торг": 0},
        platform_new={},
        platform_newest={},
    )
    assert notes == [] and len(reports) == 1


def test_лоты_в_выходную_ночь_сравниваются_с_выходными():
    """Tender.pro: в выдаче почти одни свежие публикации — в воскресенье
    лотов втрое меньше, и это не поломка (бэктест: 4 ложные тревоги)."""
    weekdays = [_night(date(2026, 10, d), 20, 18) for d in (15, 14, 13, 10, 9, 8, 7)]
    weekends = [_night(date(2026, 10, d), 3, 0) for d in (12, 11, 5, 4)]
    history = sorted(weekdays + weekends, key=lambda h: h["started_at"], reverse=True)
    sunday = datetime(2026, 10, 19, 3, 0, 1)  # покрывает вс 18.10
    assert rh.check_platform("Tender.pro", 3, 0, None, sunday, history) is None
    friday = datetime(2026, 10, 16, 3, 0, 1)  # покрывает чт 15.10
    v = rh.check_platform("Tender.pro", 3, 3, None, friday, history)
    assert v.reason == c.REASON_ANOMALY


def test_эталон_привыкает_к_новому_уровню_после_своих_тревог():
    """Ночи, помеченные самой проверкой, входят в эталон: площадка, законно
    сменившая уровень, тревожит несколько ночей, а не вечно (бэктест по журналу)."""
    flagged = [
        {**_night(date(2026, 10, 15) - timedelta(days=i), 120, 50, complete=False),
         "reason_code": c.REASON_ANOMALY}
        for i in (0, 1, 2, 5, 6)
    ]
    history = sorted(flagged + ОБЫЧНО[3:5], key=lambda h: h["started_at"], reverse=True)
    assert _check(118, 48, history=history) is None


def test_обрыв_связи_в_эталон_не_входит():
    bad = [
        {**_night(date(2026, 10, 15) - timedelta(days=i), 120, 50, complete=False),
         "reason_code": c.REASON_NETWORK}
        for i in (0, 1, 2, 5, 6)
    ]
    history = sorted(bad + ОБЫЧНО, key=lambda h: h["started_at"], reverse=True)
    assert _check(118, 48, history=history).reason == c.REASON_ANOMALY
