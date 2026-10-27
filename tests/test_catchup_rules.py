"""Кого догонять в 07:00 (ревью 16.10.2026, В4).

Раньше догон знал одну причину — обрыв связи. Прогон, упавший ошибкой или
остановленный, не догонялся вовсе, а «подозрительный ноль» и аномалия —
тоже. Теперь: ошибка/остановка — все включённые площадки без полного
отчёта; обрыв связи, ноль, аномалия, таймаут — повтор один раз; формат,
антибот, застывшая лента — НЕ повтор (тот же код даст то же), а явная
строка «нужна правка программы или проверка площадки».
"""

from datetime import datetime, timedelta

import pytest

from src import run_journal
from src.core.catchup import platforms_need_human, platforms_to_retry
from src.parsers import completeness as c

НОЧЬ = datetime(2026, 10, 16, 3, 0, 1)
УТРО = НОЧЬ + timedelta(hours=4)

ВКЛЮЧЕНЫ = {
    "parse_tender_pro": True,
    "parse_bidzaar": True,
    "parse_tektorg": True,
    "parse_roseltorg": False,
}


def _ok(platform):
    return c.build_unit_report(
        "phrase", "ф", platform=platform, expected_pages=1, collected_pages=1
    )


def _record(reports, outcome="ok"):
    return run_journal.build_record(
        started_at=НОЧЬ,
        finished_at=НОЧЬ + timedelta(minutes=30),
        app_version="test",
        outcome=outcome,
        settings=ВКЛЮЧЕНЫ,
        unit_reports=reports,
        lots_found=1,
        lots_new=0,
    )


@pytest.mark.parametrize("outcome", ["error", "stopped"])
def test_упавший_прогон_догоняет_все_площадки_без_полного_отчёта(outcome):
    rec = _record([_ok("Tender.pro")], outcome=outcome)
    assert platforms_to_retry(rec, now=УТРО) == ["parse_bidzaar", "parse_tektorg"]


def test_упавший_прогон_без_отчётов_догоняет_все_включённые():
    rec = _record(None, outcome="error")
    assert platforms_to_retry(rec, now=УТРО) == [
        "parse_bidzaar",
        "parse_tektorg",
        "parse_tender_pro",
    ]


@pytest.mark.parametrize(
    "reason",
    [c.REASON_NETWORK, c.REASON_EMPTY_PLATFORM, c.REASON_ANOMALY, c.REASON_TIMEOUT],
)
def test_повторяемые_причины_догоняются(reason):
    rec = _record([_ok("Tender.pro"), c.platform_unit("Bidzaar", reason)])
    assert platforms_to_retry(rec, now=УТРО) == ["parse_bidzaar"]


@pytest.mark.parametrize("reason", sorted(c.REASONS_NEED_HUMAN))
def test_нужна_правка_не_догоняется(reason):
    rec = _record([_ok("Tender.pro"), c.platform_unit("ТЭК-Торг", reason)])
    assert platforms_to_retry(rec, now=УТРО) == []
    assert [name for name, _text in platforms_need_human(rec)] == ["ТЭК-Торг"]


def test_нужна_правка_важнее_обрыва_связи():
    rec = _record(
        [
            c.failed_unit("phrase", "ф", "ТЭК-Торг", c.REASON_NETWORK),
            c.platform_unit("ТЭК-Торг", c.REASON_FORMAT_CHANGED),
        ]
    )
    assert platforms_to_retry(rec, now=УТРО) == []


def test_упавший_прогон_не_догоняет_площадку_с_правкой():
    rec = _record([c.platform_unit("ТЭК-Торг", c.REASON_BLOCKED)], outcome="error")
    assert platforms_to_retry(rec, now=УТРО) == ["parse_bidzaar", "parse_tender_pro"]


def test_потолок_страниц_не_догоняется():
    bad = c.build_unit_report(
        "phrase",
        "ф",
        platform="Bidzaar",
        expected_pages=9,
        collected_pages=5,
        reason=c.REASON_PAGE_CAP,
    )
    assert platforms_to_retry(_record([bad]), now=УТРО) == []


def test_догон_после_догона_и_старый_прогон_не_догоняются():
    rec = _record([c.platform_unit("Bidzaar", c.REASON_ANOMALY)])
    assert platforms_to_retry(rec, now=НОЧЬ + timedelta(hours=20)) == []
    rec["trigger"] = "catchup"
    assert platforms_to_retry(rec, now=УТРО) == []


def test_итог_прогона_говорит_что_нужна_правка():
    rec = _record(
        [_ok("Tender.pro"), c.platform_unit("ТЭК-Торг", c.REASON_FORMAT_CHANGED)]
    )
    assert "нужна правка программы или проверка площадки: ТЭК-Торг" in rec["summary"]
    assert "нужна правка" not in _record([_ok("Tender.pro")])["summary"]
