"""
Раздел «Реестр»: лоты в работе и шаги воронки (оценка, решение, цена,
подача, итог, follow-up — ТЗ-9).

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import logging

from flask import Blueprint, jsonify, request

from ...core import context as ctx

logger = logging.getLogger(__name__)

bp = Blueprint("registry", __name__)


@bp.route("/api/registry")
def api_registry():
    """Реестр заявок: воронка, красная зона, сводка.

    `next_steps` — единственная кнопка следующего шага для каждого статуса
    (по ALLOWED_TRANSITIONS): интерфейс не решает сам, куда можно перейти,
    а получает готовую карту — перескок через шаг невозможен даже кнопкой.
    `main_href` у каждой заявки — ссылка на первоисточник: именно там
    человек подаёт заявку, агрегатор дал бы только выжимку.
    """
    from src.registry import STATUS_TITLES, Registry, next_step_map

    try:
        conn = ctx.unified_conn()
        try:
            reg = Registry(conn)
            return jsonify(
                {
                    "items": [_with_main_href(conn, row) for row in reg.list()],
                    "stats": reg.stats(),
                    "red_zone": reg.red_zone(),
                    "titles": STATUS_TITLES,
                    "next_steps": next_step_map(),
                }
            )
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Реестр недоступен")
        return jsonify({"error": str(e)}), 500


# ── Маршруты: шаги воронки (ТЗ-9) ─────────────────────────────────────────────


def _with_main_href(conn, row: dict | None) -> dict | None:
    """Дописать к заявке main_href — ссылку на первоисточник.

    Ссылка на площадку нужна рядом с кнопкой шага: человек отмечает
    «подал» ровно в тот момент, когда возвращается с площадки. Ведём на
    первоисточник (правило выбора — src/db/feed.py), а не на агрегатор:
    только первоисточник даёт полную документацию и приём заявки.
    """
    if not row:
        return row
    from src.db.feed import preferred_source

    sources = [
        dict(r)
        for r in conn.execute(
            "SELECT platform, href FROM lot_sources WHERE lot_key = ?",
            (row["lot_key"],),
        )
    ]
    main = preferred_source(sources)
    row["main_href"] = (main or {}).get("href") or row.get("href")
    return row


def _who_of(payload: dict, what: str) -> str:
    """Имя человека — обязательный реквизит каждого шага воронки.

    Решение без автора неотличимо от «никто ничего не решал»: по имени
    потом спрашивается результат. Реестр проверяет это сам только там,
    где шаг заведомо человековый (решение, утверждение); остальные шаги
    проверяются здесь, чтобы правило было единым для всех маршрутов.
    """
    who = str(payload.get("who") or "").strip()
    if not who:
        raise ValueError(f"укажите, кто {what}")
    return who


def _str_of(payload: dict, name: str) -> str | None:
    value = payload.get(name)
    text = str(value).strip() if value is not None else ""
    return text or None


def _num_of(payload: dict, name: str, title: str) -> float | None:
    """Число из тела запроса или None. Пустое поле — «не заполнено», не ноль."""
    value = payload.get(name)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"«{title}» не похоже на число — уточните значение")


def _run_lot_step(lot_key: str, step):
    """Выполнить шаг воронки по маршруту.

    Ошибки регламента — ValueError/TransitionError из реестра (не указан
    человек, отказ без причины, недопустимый переход) — это не сбой
    программы, а нарушение порядка работы: 400 и текст словами, как уже
    сделано для /skip. Ответ при успехе — обновлённая заявка в том же
    виде, что отдаёт /api/registry, чтобы интерфейс перерисовал строку
    без второго запроса.
    """
    from src.registry import Registry, TransitionError

    payload = request.get_json(silent=True) or {}
    try:
        conn = ctx.unified_conn()
        try:
            row = step(Registry(conn), payload)
            return jsonify(_with_main_href(conn, row))
        finally:
            conn.close()
    except (TransitionError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Шаг воронки не удался: %s", lot_key)
        return jsonify({"error": str(e)}), 500


@bp.route("/api/lot/<path:lot_key>/assess", methods=["POST"])
def api_lot_assess(lot_key):
    """Шаг «Оценить»: объём, тара, опасный груз, недостающие документы.

    Оценка — первый шаг после взятия: она отвечает, стоит ли вообще
    звать руководителя. Поля необязательны, кроме автора: оценка может
    быть и устной приговоркой «обычная наша позиция».
    """

    def step(reg, p):
        # Опасный груз — три состояния: не отмечали (None), да (1), нет (0).
        raw_hazard = p.get("hazard")
        return reg.assess(
            lot_key,
            who=_who_of(p, "оценил лот"),
            assessment=_str_of(p, "assessment"),
            volume_t=_num_of(p, "volume_t", "объём"),
            packaging=_str_of(p, "packaging"),
            hazard=None if raw_hazard is None else (1 if raw_hazard else 0),
            docs_needed=_str_of(p, "docs_needed"),
        )

    return _run_lot_step(lot_key, step)


@bp.route("/api/lot/<path:lot_key>/decide", methods=["POST"])
def api_lot_decide(lot_key):
    """Шаг «Решение»: участвуем или нет. Точка согласования регламента."""

    def step(reg, p):
        participate = p.get("participate")
        # Отсутствующее решение опаснее отрицательного: молчание превратилось
        # бы в «не участвуем» и лот молча выпал бы из работы.
        if not isinstance(participate, bool):
            raise ValueError("укажите решение: участвуем или нет")
        comment = str(p.get("comment") or "").strip()
        if not participate and not comment:
            raise ValueError(
                "отказ без причины не принимается — напишите, почему не участвуем"
            )
        return reg.decide(lot_key, participate, str(p.get("by") or "").strip(), comment)

    return _run_lot_step(lot_key, step)


@bp.route("/api/lot/<path:lot_key>/price", methods=["POST"])
def api_lot_price(lot_key):
    """Шаг «Цена»: поставщик, закупочная цена, логистика, себестоимость, наценка.

    Числа необязательны: незаполненное поле — «пока не знаю», а не ноль.
    Поставщик обязателен: цена без источника нельзя ни проверить, ни
    воспроизвести в следующей закупке.
    """

    def step(reg, p):
        supplier = _str_of(p, "supplier")
        if not supplier:
            raise ValueError("укажите поставщика — у цены должен быть источник")
        return reg.set_price(
            lot_key,
            who=_who_of(p, "собрал цену"),
            supplier=supplier,
            purchase_price=_num_of(p, "purchase_price", "закупочная цена"),
            logistics=_num_of(p, "logistics", "логистика"),
            cost=_num_of(p, "cost", "себестоимость"),
            markup=_num_of(p, "markup", "наценка"),
        )

    return _run_lot_step(lot_key, step)


@bp.route("/api/lot/<path:lot_key>/approve-price", methods=["POST"])
def api_lot_approve_price(lot_key):
    """Шаг «Утвердить цену»: вторая точка согласования регламента."""

    def step(reg, p):
        bid = _num_of(p, "bid_price", "цена подачи")
        # Утверждают конкретную цифру: «утверждена неизвестно что» не
        # имеет смысла и ломает подсчёт отклонения после итога.
        if bid is None:
            raise ValueError("укажите цену подачи — утверждают конкретную цифру")
        return reg.approve_price(lot_key, str(p.get("by") or "").strip(), bid)

    return _run_lot_step(lot_key, step)


@bp.route("/api/lot/<path:lot_key>/submit", methods=["POST"])
def api_lot_submit(lot_key):
    """Шаг «Подал»: фиксация того, что человек подал заявку на площадке.

    Программа НЕ подаёт заявки сама — это граница регламента: подача
    создаёт обязательства, и решение о ней остаётся за человеком. Здесь
    появляется только отметка и номер, который дала площадка: по нему
    заявку потом ищут в протоколах рассмотрения.
    """

    def step(reg, p):
        no = str(p.get("submission_no") or "").strip()
        if not no:
            raise ValueError("укажите номер поданной заявки — его даёт площадка")
        return reg.submit(lot_key, _who_of(p, "подал заявку"), no)

    return _run_lot_step(lot_key, step)


@bp.route("/api/lot/<path:lot_key>/result", methods=["POST"])
def api_lot_result(lot_key):
    """Шаг «Итог»: выиграли или проиграли, цена победителя, причина проигрыша."""

    def step(reg, p):
        result = str(p.get("result") or "").strip()
        if result not in ("won", "lost"):
            raise ValueError("итог — won (выиграли) или lost (проиграли)")
        loss_reason = _str_of(p, "loss_reason")
        if result == "lost" and not loss_reason:
            raise ValueError(
                "проигрыш без причины не принимается — напишите, почему проиграли"
            )
        return reg.set_result(
            lot_key,
            result,
            _who_of(p, "подвёл итог"),
            _num_of(p, "winner_price", "цена победителя"),
            loss_reason or "",
        )

    return _run_lot_step(lot_key, step)


@bp.route("/api/lot/<path:lot_key>/followup", methods=["POST"])
def api_lot_followup(lot_key):
    """Шаг «Follow-up»: контакт и дата следующего касания после итога.

    Проигранный тендер — не конец отношений с заказчиком: контакт,
    заведённый сразу, дешевле нового поиска в следующий раз.
    """

    def step(reg, p):
        return reg.followup(
            lot_key,
            who=_who_of(p, "ведёт follow-up"),
            contact=_str_of(p, "contact"),
            next_touch=_str_of(p, "next_touch"),
            note=_str_of(p, "note"),
        )

    return _run_lot_step(lot_key, step)
