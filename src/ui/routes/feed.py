"""
Раздел «Лента»: найденные лоты, карточка лота, «взять» и «мимо»,
пересчёт оценок профилем.

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import logging

from flask import Blueprint, jsonify, request

from ... import run_journal
from ...core import context as ctx

logger = logging.getLogger(__name__)

bp = Blueprint("feed", __name__)


@bp.route("/api/lot/<path:lot_key>/take", methods=["POST"])
def api_lot_take(lot_key):
    """«Беру» — лот попадает в реестр и закрепляется за менеджером."""
    from src.registry import Registry, TransitionError

    manager = (request.get_json(silent=True) or {}).get("manager", "").strip()
    try:
        conn = ctx.unified_conn()
        try:
            return jsonify(Registry(conn).take(lot_key, manager))
        finally:
            conn.close()
    except (TransitionError, ValueError) as e:
        # Это не сбой программы, а несоблюдение порядка работы — показываем
        # человеку словами, а не пятисоткой.
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Не удалось взять лот")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/lot/<path:lot_key>/skip", methods=["POST"])
def api_lot_skip(lot_key):
    """«Мимо» — отказ с обязательной причиной: без причин мы не учимся."""
    from src.registry import Registry, TransitionError

    payload = request.get_json(silent=True) or {}
    try:
        conn = ctx.unified_conn()
        try:
            return jsonify(
                Registry(conn).skip(
                    lot_key,
                    payload.get("manager", "").strip(),
                    payload.get("reason", "").strip(),
                )
            )
        finally:
            conn.close()
    except (TransitionError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Не удалось отклонить лот")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/feed")
def api_feed():
    """Лента лотов из единой базы: корзины разбора и счётчики.

    Оценка берётся готовой из базы (её посчитал профиль при сборе), а
    срочность и истечение срока считаются здесь, на сегодняшнюю дату:
    сохранённые, они назавтра врали бы.
    """
    from src.db import connect as _connect
    from src.db import ensure_schema as _ensure
    from src.db import load_feed as _load_feed

    try:
        conn = _connect(ctx.UNIFIED_DB_PATH)
        _ensure(conn)
        try:
            data = _load_feed(conn)
        finally:
            conn.close()

        # Состояние сбора — на первом же экране (FR-017). Пользователь должен
        # видеть, когда собирали и полны ли данные, не переходя в другой раздел:
        # список без этой отметки выглядит полным, даже когда он обрезан.
        last = run_journal.read_last_run()
        if last:
            data["last_run"] = {
                "finished_at": last.get("finished_at") or last.get("started_at"),
                "outcome": last.get("outcome"),
                "data_complete": bool(last.get("data_complete")),
                "summary": last.get("summary"),
            }
        else:
            data["last_run"] = None
        return jsonify(data)
    except Exception as e:
        logger.exception("Лента недоступна")
        return jsonify({"error": f"Лента недоступна: {e}"}), 500


@bp.route("/api/lot/<path:lot_key>")
def api_lot(lot_key):
    """Карточка одного лота: оценка, места публикации, заявка."""
    from src.db import connect as _connect
    from src.db import ensure_schema as _ensure
    from src.db import load_lot as _load_lot

    try:
        conn = _connect(ctx.UNIFIED_DB_PATH)
        _ensure(conn)
        try:
            lot = _load_lot(conn, lot_key)
        finally:
            conn.close()
        if lot is None:
            return jsonify({"error": "Лот не найден"}), 404
        # Названия статусов — рядом с данными: интерфейс не должен знать
        # внутренние коды воронки.
        from src.registry import STATUS_TITLES

        lot["status_titles"] = STATUS_TITLES
        return jsonify(lot)
    except Exception as e:
        logger.exception("Карточка лота недоступна")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/rescore", methods=["POST"])
def api_rescore():
    """Пересчитать оценки лотов по текущему справочнику.

    Нужно после правки номенклатуры или клиентов: лоты, собранные раньше,
    иначе остались бы с прежней оценкой и не поднялись бы в ленте.
    """
    from src.db import connect as _connect
    from src.db import ensure_schema as _ensure
    from src.db import rescore_lots as _rescore
    from src.profile import Profile as _Profile

    only_missing = bool((request.get_json(silent=True) or {}).get("only_missing", True))
    try:
        conn = _connect(ctx.UNIFIED_DB_PATH)
        _ensure(conn)
        try:
            summary = _rescore(conn, _Profile(), only_missing=only_missing)
        finally:
            conn.close()
        return jsonify(summary)
    except Exception as e:
        logger.exception("Пересчёт оценок не удался")
        return jsonify({"error": str(e)}), 500
