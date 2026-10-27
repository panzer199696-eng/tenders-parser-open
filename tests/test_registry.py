"""
Тесты реестра заявок (T019): воронка работы с лотом.

Проверяется не механика SQL, а правила регламента:
  • заявка появляется только когда с лотом начали работать — найденный лот
    заявкой не считается (из-за обратного в старой базе лежало 1546 «заявок»,
    из которых реальной работы было три);
  • шаги нельзя перескакивать, и человеку объясняют почему;
  • отказ возможен с любого шага, но только с причиной — без причин мы не
    узнаем, где промахиваемся;
  • красная зона считается по взятым в работу лотам, а не по всей базе.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, ensure_schema, save_lots
from src.registry import Registry, TransitionError
from src.registry.registry import REJECTED, SELECTED

KEY = "Росэлторг|42"


@pytest.fixture
def reg(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    save_lots(
        conn,
        [
            {
                "platform": "Росэлторг",
                "lot_id": "42",
                "name": "Компаунд заливочный 18 т",
                "company": "АО Кедр",
                "sum": 3_000_000,
                "date_deadline": "05.09.2026",
            }
        ],
        None,
    )
    return Registry(conn)


# ── Появление заявки ─────────────────────────────────────────────────────────


def test_найденный_лот_ещё_не_заявка(reg):
    """Лот лежит в ленте; в реестре его нет, пока никто не взял."""
    assert reg.status_of(KEY) is None
    assert reg.list() == []


def test_взятие_создаёт_заявку(reg):
    row = reg.take(KEY, "Пётр")
    assert row["status"] == SELECTED
    assert row["manager"] == "Пётр"
    assert len(reg.list()) == 1


def test_взять_без_менеджера_нельзя(reg):
    """Иначе непонятно, с кого спрашивать."""
    with pytest.raises(ValueError):
        reg.take(KEY, "")


def test_несуществующий_лот_не_берётся(reg):
    with pytest.raises(TransitionError) as e:
        reg.take("Росэлторг|нет-такого", "Пётр")
    assert "нет в базе" in str(e.value)


# ── Порядок шагов ────────────────────────────────────────────────────────────


def test_шаги_нельзя_перескакивать(reg):
    reg.take(KEY, "Пётр")
    with pytest.raises(TransitionError) as e:
        reg.submit(KEY, "Пётр")
    text = str(e.value)
    assert "нельзя подать заявку, пока цена не утверждена" in text
    assert "взят в работу" in text, "человеку показывают, где лот сейчас"


def test_полная_воронка_проходится(reg):
    reg.take(KEY, "Пётр")
    reg.assess(KEY, who="Пётр", volume_t=18, packaging="биг-бэги")
    reg.decide(KEY, True, by="Руководитель")
    reg.set_price(
        KEY, who="Пётр", supplier="Завод", cost=2_500_000, bid_price=2_900_000
    )
    reg.approve_price(KEY, by="Руководитель")
    row = reg.submit(KEY, "Пётр", submission_no="№17")
    assert row["status"] == "SUBMITTED"
    assert row["submission_no"] == "№17"


def test_каждый_шаг_попадает_в_историю(reg):
    reg.take(KEY, "Пётр")
    reg.assess(KEY, who="Пётр")
    history = reg.history(KEY)
    assert [h["to_status"] for h in history] == ["SELECTED", "ASSESSED"]
    assert history[0]["from_status"] is None


# ── Решения человека ─────────────────────────────────────────────────────────


def test_решение_об_участии_требует_человека(reg):
    """Точка согласования регламента: решает руководитель, не программа."""
    reg.take(KEY, "Пётр")
    reg.assess(KEY, who="Пётр")
    with pytest.raises(ValueError):
        reg.decide(KEY, True, by="")


def test_отказ_руководителя_закрывает_лот(reg):
    reg.take(KEY, "Пётр")
    reg.assess(KEY, who="Пётр")
    row = reg.decide(KEY, False, by="Руководитель", comment="дорогая логистика")
    assert row["status"] == REJECTED
    assert "логистика" in row["reject_reason"]


def test_цену_утверждает_человек(reg):
    reg.take(KEY, "Пётр")
    reg.assess(KEY, who="Пётр")
    reg.decide(KEY, True, by="Руководитель")
    reg.set_price(KEY, who="Пётр", bid_price=2_900_000)
    with pytest.raises(ValueError):
        reg.approve_price(KEY, by="")


# ── Отказы ───────────────────────────────────────────────────────────────────


def test_мимо_без_причины_нельзя(reg):
    """Без причины не учимся на отказах."""
    with pytest.raises(ValueError):
        reg.skip(KEY, "Пётр", "")


def test_мимо_возможно_сразу_из_ленты(reg):
    row = reg.skip(KEY, "Пётр", "не наш профиль")
    assert row["status"] == REJECTED
    assert row["reject_reason"] == "не наш профиль"


def test_после_отказа_дальше_нельзя(reg):
    reg.skip(KEY, "Пётр", "не наш профиль")
    with pytest.raises(TransitionError):
        reg.take(KEY, "Пётр")


# ── Итог участия ─────────────────────────────────────────────────────────────


def _to_submitted(reg):
    reg.take(KEY, "Пётр")
    reg.assess(KEY, who="Пётр")
    reg.decide(KEY, True, by="Руководитель")
    reg.set_price(KEY, who="Пётр", bid_price=2_900_000)
    reg.approve_price(KEY, by="Руководитель")
    reg.submit(KEY, "Пётр")


def test_отклонение_от_цены_победителя(reg):
    _to_submitted(reg)
    row = reg.set_result(KEY, "проигрыш", who="Пётр", winner_price=2_700_000)
    assert row["deviation"] == pytest.approx(7.4, abs=0.1)


def test_без_цены_победителя_отклонение_не_считается(reg):
    _to_submitted(reg)
    row = reg.set_result(KEY, "победа", who="Пётр")
    assert row["deviation"] is None


# ── Красная зона ─────────────────────────────────────────────────────────────


def test_взятый_лот_с_близким_дедлайном_горит(reg):
    reg.take(KEY, "Пётр")
    zone = reg.red_zone(today=date(2026, 9, 3))
    assert [r["lot_key"] for r in zone["горит"]] == [KEY]


def test_взятый_лот_с_прошедшим_дедлайном_просрочен(reg):
    reg.take(KEY, "Пётр")
    zone = reg.red_zone(today=date(2026, 9, 10))
    assert [r["lot_key"] for r in zone["просрочено"]] == [KEY]


def test_никем_не_взятый_лот_в_красную_зону_не_попадает(reg):
    """Иначе она превращается в перечень всех устаревших лотов площадки."""
    zone = reg.red_zone(today=date(2026, 9, 10))
    assert zone["горит"] == [] and zone["просрочено"] == []


def test_поданный_лот_с_контроля_сроков_снимается(reg):
    _to_submitted(reg)
    zone = reg.red_zone(today=date(2026, 9, 10))
    assert zone["просрочено"] == []


# ── Сводка ───────────────────────────────────────────────────────────────────


def test_сводка_по_воронке(reg):
    reg.take(KEY, "Пётр")
    stats = reg.stats()
    assert stats["by_status"] == {"SELECTED": 1}
    assert stats["in_work"] == 1
    assert stats["rejected"] == 0


def test_отказы_не_считаются_работой(reg):
    reg.skip(KEY, "Пётр", "не наш профиль")
    stats = reg.stats()
    assert stats["in_work"] == 0
    assert stats["rejected"] == 1
