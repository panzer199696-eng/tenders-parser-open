"""
Реестр заявок: воронка работы с лотом (задача T019).

Перенесён из отдельной программы реестра и переписан под единую базу.
Главное отличие от прежней версии — **заявка больше не дублирует лот**.
Раньше `applications` хранила и название, и заказчика, и цену: в реестре
лежало 1546 записей, из которых 1543 были просто найденными лотами, а
реальной работы — три. Теперь лот живёт в `lots`, а заявка появляется
только тогда, когда с лотом начали работать, и ссылается на него по ключу.

Воронка линейная, отказ возможен с любого шага: без этого «мимо» и его
причина теряются, а именно причины отказов и показывают, где мы промахиваемся.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

# Один разбор срока на всю программу: свои копии расходились в форматах,
# и лоты становились «вечными» (16.10.2026).
from ..dates import parse_deadline, resolve_now

logger = logging.getLogger(__name__)

SELECTED = "SELECTED"  # взят в работу
ASSESSED = "ASSESSED"  # оценён
DECIDED = "DECIDED"  # решение руководителя об участии
PRICED = "PRICED"  # цена собрана
PRICE_APPROVED = "PRICE_APPROVED"  # цена утверждена
SUBMITTED = "SUBMITTED"  # заявка подана
RESULT = "RESULT"  # итог участия
FOLLOWUP = "FOLLOWUP"  # follow-up после итога
REJECTED = "REJECTED"  # отказ с причиной

STATUS_TITLES = {
    SELECTED: "взят в работу",
    ASSESSED: "оценён",
    DECIDED: "решение принято",
    PRICED: "цена собрана",
    PRICE_APPROVED: "цена утверждена",
    SUBMITTED: "подан",
    RESULT: "итог",
    FOLLOWUP: "follow-up",
    REJECTED: "отказ",
}

# Порядок шагов регламента. Отказ — из любого статуса.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    None: {SELECTED, REJECTED},  # лот ещё не в реестре
    SELECTED: {ASSESSED, REJECTED},
    ASSESSED: {DECIDED, REJECTED},
    DECIDED: {PRICED, REJECTED},
    PRICED: {PRICE_APPROVED, REJECTED},
    PRICE_APPROVED: {SUBMITTED, REJECTED},
    SUBMITTED: {RESULT, REJECTED},
    RESULT: {FOLLOWUP, REJECTED},
    FOLLOWUP: {REJECTED},
    REJECTED: set(),
}

# Почему переход невозможен — текст показывается человеку, а не код ошибки.
STEP_HINTS = {
    SELECTED: "лот должен сначала появиться в сборе",
    ASSESSED: "сначала возьмите лот в работу",
    DECIDED: "сначала заполните оценку лота",
    PRICED: "нужно решение руководителя об участии",
    PRICE_APPROVED: "сначала соберите цену — поставщик и себестоимость",
    SUBMITTED: "нельзя подать заявку, пока цена не утверждена",
    RESULT: "заявка ещё не подана",
    FOLLOWUP: "сначала подведите итог участия",
}

# Красная зона сроков считается только по лотам, ВЗЯТЫМ в работу. Найденный и
# никем не взятый лот с прошедшим дедлайном — не аврал, а обычное течение дел:
# на живых данных 27.08.2026 таких было 100 из 1546, и они превращали красную
# зону в бесполезную простыню.
IN_WORK_BEFORE_SUBMIT = (SELECTED, ASSESSED, DECIDED, PRICED, PRICE_APPROVED)

# Молчание дольше этого срока по взятому лоту — повод напомнить.
SILENCE_DAYS = 2
URGENT_DAYS = 3

# Отдельный случай красной зоны: лот взят, до подачи осталось два дня и
# меньше, а заявка так и не отправлена. Это то, что реально теряет деньги:
# лот уже выбран (значит, решили участвовать), но заявка не ушла. В отличие
# от «горит», где срок просто близок, здесь названа и причина тревоги:
# неподача.
NOT_SUBMITTED_DAYS = 2

# Целевой статус шага → (маршрут API, надпись кнопки). Карта живёт рядом с
# ALLOWED_TRANSITIONS, а не в интерфейсе: кнопки обязаны соглашаться с
# регламентом переходов, иначе интерфейс начнёт предлагать перескок через
# шаг, который реестр всё равно не примёт.
STEP_ROUTES = {
    ASSESSED: ("assess", "Оценить"),
    DECIDED: ("decide", "Решение"),
    PRICED: ("price", "Цена"),
    PRICE_APPROVED: ("approve-price", "Утвердить цену"),
    SUBMITTED: ("submit", "Подал"),
    RESULT: ("result", "Итог"),
    FOLLOWUP: ("followup", "Follow-up"),
}


def next_step_map() -> dict[str, dict]:
    """Кнопка следующего шага для каждого статуса: ровно одна.

    Строится из ALLOWED_TRANSITIONS, поэтому перескок через шаг здесь
    невозможен в принципе: у каждого статуса не-отказных продолжений
    либо одно (линейная воронка), либо ноль (FOLLOWUP, REJECTED —
    терминальные, у них только «Мимо» или ничего).
    """
    result: dict[str, dict] = {}
    for status, allowed in ALLOWED_TRANSITIONS.items():
        if status is None:
            continue  # «взять в работу» — кнопка ленты, а не реестра
        forward = [s for s in allowed if s != REJECTED]
        if len(forward) == 1:
            route, title = STEP_ROUTES[forward[0]]
            result[status] = {"route": route, "title": title}
    return result


def _not_submitted_text(days_left: int) -> str:
    """Формулировка случая «взят, а заявка не отправлена» — словами.

    «подача через 2 дня, заявка не отправлена» читается человеком сразу;
    «days_left=2» — нет. Поэтому текст собирается здесь, у источника
    решения, а не в интерфейсе.
    """
    if days_left <= 0:
        return "подача сегодня, заявка не отправлена"
    if days_left == 1:
        return "подача завтра, заявка не отправлена"
    return f"подача через {days_left} дня, заявка не отправлена"


class TransitionError(Exception):
    """Переход нарушает воронку. Сообщение написано для человека."""


def _now() -> str:
    # Метки — в том же ISO с «T», что и у лотов (Г3, 16.10.2026): их
    # сравнивают строками, а «2026-08-27 06:37» и «…T06:37:00» сортируются
    # неправильно.
    return datetime.now().isoformat(timespec="seconds")


class Registry:
    """Операции над заявками в единой базе."""

    def __init__(self, conn):
        self.conn = conn

    # ── чтение ────────────────────────────────────────────────────────────────

    def status_of(self, lot_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM applications WHERE lot_key = ?", (lot_key,)
        ).fetchone()
        return row["status"] if row else None

    def get(self, lot_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT a.*, l.name, l.company, l.sum, l.date_deadline, l.platform, l.href,"
            " l.score, l.score_explain"
            " FROM applications a JOIN lots l USING (lot_key) WHERE a.lot_key = ?",
            (lot_key,),
        ).fetchone()
        return dict(row) if row else None

    def list(self, status: str | None = None, manager: str | None = None) -> list[dict]:
        """Заявки с фильтрами; ближе дедлайн — выше."""
        sql = (
            "SELECT a.*, l.name, l.company, l.sum, l.date_deadline, l.platform, l.href,"
            " l.score, l.score_explain"
            " FROM applications a JOIN lots l USING (lot_key) WHERE 1=1"
        )
        params: list = []
        if status:
            sql += " AND a.status = ?"
            params.append(status)
        if manager:
            sql += " AND a.manager = ?"
            params.append(manager)
        # Порядок — по разобранному сроку, а не строкой «ДД.ММ.ГГГГ»: строкой
        # «05.11» вставало раньше «20.10» (Г3, 16.10.2026). Без срока — в конец.
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        rows.sort(key=lambda r: parse_deadline(r.get("date_deadline")) or datetime.max)
        return rows

    def history(self, lot_key: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM history WHERE lot_key = ? ORDER BY id", (lot_key,)
            )
        ]

    # ── переходы ──────────────────────────────────────────────────────────────

    def _move(
        self, lot_key: str, to_status: str, fields: dict, who: str, comment: str = ""
    ) -> dict:
        lot = self.conn.execute(
            "SELECT lot_key FROM lots WHERE lot_key = ?", (lot_key,)
        ).fetchone()
        if lot is None:
            raise TransitionError("такого лота нет в базе — возможно, он ещё не собран")

        current = self.status_of(lot_key)
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if to_status not in allowed:
            hint = STEP_HINTS.get(to_status, "шаг недоступен из текущего состояния")
            now_title = STATUS_TITLES.get(current, "не в работе")
            raise TransitionError(
                f"нельзя перейти к «{STATUS_TITLES.get(to_status, to_status)}»: "
                f"лот сейчас «{now_title}» — {hint}"
            )

        stamp = _now()
        if current is None:
            columns = {
                "lot_key": lot_key,
                "status": to_status,
                "created_at": stamp,
                "updated_at": stamp,
                **fields,
            }
            cols = ", ".join(columns)
            marks = ", ".join("?" for _ in columns)
            self.conn.execute(
                f"INSERT INTO applications ({cols}) VALUES ({marks})",
                list(columns.values()),
            )
        else:
            updates = {"status": to_status, "updated_at": stamp, **fields}
            sets = ", ".join(f"{c} = ?" for c in updates)
            self.conn.execute(
                f"UPDATE applications SET {sets} WHERE lot_key = ?",
                [*updates.values(), lot_key],
            )

        self.conn.execute(
            "INSERT OR IGNORE INTO history (lot_key, from_status, to_status, at, who, comment)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (lot_key, current, to_status, stamp, who or None, comment or None),
        )
        self.conn.commit()
        return self.get(lot_key)

    def take(self, lot_key: str, manager: str) -> dict:
        """Взять лот в работу. Без менеджера непонятно, с кого спрашивать."""
        if not (manager or "").strip():
            raise ValueError("укажите, кто берёт лот")
        return self._move(
            lot_key, SELECTED, {"manager": manager.strip()}, manager, "взят в работу"
        )

    def skip(self, lot_key: str, manager: str, reason: str) -> dict:
        """«Мимо» с причиной. Без причины не учимся на отказах."""
        if not (reason or "").strip():
            raise ValueError("без причины не учимся — укажите, почему лот мимо")
        return self._move(
            lot_key,
            REJECTED,
            {
                "manager": (manager or "").strip() or None,
                "reject_reason": reason.strip(),
            },
            manager,
            reason.strip(),
        )

    def assess(self, lot_key: str, who: str = "", **fields) -> dict:
        """Оценка лота: объём, тара, опасный груз, недостающие документы."""
        allowed = {
            "assessment",
            "volume_t",
            "packaging",
            "hazard",
            "docs_needed",
            "note",
            "contact",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        return self._move(lot_key, ASSESSED, payload, who, "оценён")

    def decide(
        self, lot_key: str, participate: bool, by: str, comment: str = ""
    ) -> dict:
        """Решение руководителя об участии — точка согласования регламента."""
        if not (by or "").strip():
            raise ValueError("решение должен принять человек — укажите, кто")
        if not participate:
            return self.skip(
                lot_key, by, comment or "решение руководителя: не участвуем"
            )
        return self._move(
            lot_key,
            DECIDED,
            {"decision": "участвуем", "decision_by": by.strip(), "decision_at": _now()},
            by,
            comment or "решение: участвуем",
        )

    def set_price(self, lot_key: str, who: str = "", **fields) -> dict:
        allowed = {
            "supplier",
            "purchase_price",
            "logistics",
            "cost",
            "markup",
            "bid_price",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        return self._move(lot_key, PRICED, payload, who, "цена собрана")

    def approve_price(
        self, lot_key: str, by: str, bid_price: float | None = None
    ) -> dict:
        """Утверждение итоговой цены — вторая точка согласования регламента."""
        if not (by or "").strip():
            raise ValueError("цену утверждает человек — укажите, кто")
        payload = {"price_approved_by": by.strip(), "price_approved_at": _now()}
        if bid_price is not None:
            payload["bid_price"] = bid_price
        return self._move(lot_key, PRICE_APPROVED, payload, by, "цена утверждена")

    def submit(self, lot_key: str, who: str, submission_no: str = "") -> dict:
        """Отметка о подаче. Саму заявку подаёт человек — программа не подаёт.

        Это граница регламента: автоматическая подача означала бы, что
        программа принимает юридически значимое решение без человека.
        Здесь только фиксация свершившегося факта и номер, который дала
        площадка, — по нему заявку потом находят в протоколах.
        """
        return self._move(
            lot_key,
            SUBMITTED,
            {"submitted_at": _now(), "submission_no": submission_no or None},
            who,
            "заявка подана",
        )

    def followup(self, lot_key: str, who: str = "", **fields) -> dict:
        """Follow-up после итога: с кем говорить и когда следующее касание.

        Проигранный тендер — не конец отношений: заказчик ещё будет
        закупать, и контакт, заведённый сразу, дешевле нового поиска.
        """
        allowed = {"contact", "next_touch", "note"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        return self._move(lot_key, FOLLOWUP, payload, who, "follow-up назначен")

    def set_result(
        self,
        lot_key: str,
        result: str,
        who: str = "",
        winner_price: float | None = None,
        loss_reason: str = "",
    ) -> dict:
        payload = {"result": result, "loss_reason": loss_reason or None}
        current = self.get(lot_key) or {}
        bid = current.get("bid_price")
        if winner_price is not None:
            payload["winner_price"] = winner_price
            # Отклонение считаем только когда есть с чем сравнивать.
            if bid:
                payload["deviation"] = round(
                    (bid - winner_price) / winner_price * 100, 1
                )
        return self._move(lot_key, RESULT, payload, who, f"итог: {result}")

    # ── контроль ──────────────────────────────────────────────────────────────

    def red_zone(self, today: date | None = None) -> dict:
        """Что требует вмешательства сегодня: молчание, аврал, просрочка.

        «Заявка не отправлена» — самый дорогой случай: лот взят, дедлайн
        через два дня и меньше, а статуса «подан» всё нет. Пустая группа
        так же информативна, как заполненная: она говорит, что всё
        выбранное либо подано, либо честно отклонено.
        """
        # Сроки — московские, и сравниваются с московским «сейчас»; отметки
        # истории пишутся по часам ПК, и молчание меряется по ним же.
        ref, now = resolve_now(today)
        horizon = datetime.combine(ref, datetime.now().time()) - timedelta(
            days=SILENCE_DAYS
        )

        zone: dict[str, list[dict]] = {
            "заявка не отправлена": [],
            "без движения": [],
            "горит": [],
            "просрочено": [],
        }
        for row in self.list():
            status = row["status"]
            if status == SELECTED:
                last = self.conn.execute(
                    "SELECT at FROM history WHERE lot_key = ? ORDER BY id DESC LIMIT 1",
                    (row["lot_key"],),
                ).fetchone()
                at = parse_deadline(last["at"]) if last else None
                if at and at < horizon:
                    zone["без движения"].append(row)
            if status in IN_WORK_BEFORE_SUBMIT:
                deadline = parse_deadline(row.get("date_deadline"))
                if deadline is None:
                    continue
                days = (deadline.date() - ref).days
                if deadline < now:
                    zone["просрочено"].append(row)
                    continue
                # Сначала точный диагноз (не отправлена при ≤2 дней), затем
                # общий («горит» при ≤3): лот с подачей через два дня
                # попадает в обе группы — это осознанно, у групп разные
                # вопросы: «срок близко» и «деньги теряются прямо сейчас».
                if days <= NOT_SUBMITTED_DAYS:
                    zone["заявка не отправлена"].append(
                        {**row, "why": _not_submitted_text(days)}
                    )
                if days <= URGENT_DAYS:
                    zone["горит"].append(row)
        return zone

    def stats(self) -> dict:
        """Сводка по воронке: сколько на каком шаге."""
        rows = self.conn.execute(
            "SELECT status, count(*) n FROM applications GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        return {
            "by_status": by_status,
            "titles": STATUS_TITLES,
            "in_work": sum(n for s, n in by_status.items() if s != REJECTED),
            "rejected": by_status.get(REJECTED, 0),
        }
