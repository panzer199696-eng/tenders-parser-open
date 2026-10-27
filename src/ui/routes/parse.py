"""
Раздел «Сбор»: статус и прогресс прогона, проверка перед стартом, кнопки
«Запустить» и «Остановить». Сам сбор — src/core/collect.py.

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import logging

from flask import Blueprint, jsonify, request

from ... import collect_lock
from ...core import collect
from ...core import context as ctx
from ...version import APP_VERSION

logger = logging.getLogger(__name__)

bp = Blueprint("parse", __name__)


def _count_tenders_today():
    """Плитка «Сегодня»: лоты единой базы, впервые увиденные сегодня по Москве.

    Раньше считалось по старой tenders.db, которую сбор вёл только ради
    этой плитки (рефакторинг 16.10.2026).
    """
    if not ctx.UNIFIED_DB_PATH.exists():
        return 0
    try:
        from src.db import connect as _db_connect
        from src.db.runs import new_lots_today

        conn = _db_connect(ctx.UNIFIED_DB_PATH)
        try:
            return new_lots_today(conn)
        finally:
            conn.close()
    except Exception:
        logger.debug("Плитка «Сегодня» не посчиталась", exc_info=True)
        return 0


# ── Маршруты: статус / парсинг ────────────────────────────────────────────────


@bp.route("/api/status")
def api_status():
    from src.keywords_store import load_keywords

    with collect._parse_lock:
        snapshot = {
            "running": collect._parse_state["running"],
            "started_at": collect._parse_state["started_at"],
            "finished_at": collect._parse_state["finished_at"],
            "progress": list(collect._parse_state["progress"]),
            "current_company": collect._parse_state["current_company"],
            "total_companies": collect._parse_state["total_companies"],
            "units_done": collect._parse_state["units_done"],
            "total_units": collect._parse_state["total_units"],
            "error": collect._parse_state["error"],
            "tenders_count": len(collect._parse_state["tenders"]),
            "stopping": collect._parse_state.get("stopping", False),
            "stopped": collect._parse_state.get("stopped", False),
            "excel_path": collect._parse_state.get("excel_path"),
            "data_saved": collect._parse_state.get("data_saved", False),
            "found_so_far": collect._parse_state.get("found_so_far", 0),
            "completeness": collect._parse_state.get("completeness"),
            "version": APP_VERSION,
        }

    kws = load_keywords()
    snapshot["counters"] = {
        "companies": len(ctx.load_companies()),
        "keywords_enabled": sum(1 for k in kws if k.get("enabled")),
        "tenders_today": _count_tenders_today(),
    }
    return jsonify(snapshot)


@bp.route("/api/preflight")
def api_preflight():
    """Готовность к запуску: что мешает начать парсинг прямо сейчас."""
    problems = collect._preflight_problems()
    return jsonify({"ok": not problems, "problems": problems})


@bp.route("/api/parse/start", methods=["POST"])
def api_parse_start():
    # Пользователь мог согласиться запуститься «как есть» — тогда приходит
    # force=true и предупреждения не блокируют старт.
    body = request.get_json(silent=True) or {}
    if not body.get("force"):
        problems = collect._preflight_problems()
        if problems:
            return jsonify({"ok": False, "problems": problems}), 428

    companies = ctx.load_companies()
    try:
        if not collect._start_parse(companies):
            return jsonify({"ok": False, "error": "Парсинг уже идёт"}), 409
    except collect_lock.CollectBusy as e:
        return jsonify(
            {
                "ok": False,
                "error": f"{e}: сбор по расписанию или в другом окне программы. "
                "Дождитесь его окончания.",
            }
        ), 409
    except Exception:
        return jsonify(
            {"ok": False, "error": "Не удалось начать сбор. Попробуйте ещё раз."}
        ), 500
    return jsonify({"ok": True})


@bp.route("/api/parse/stop", methods=["POST"])
def api_parse_stop():
    """Остановить идущий парсинг: отменяем asyncio-задачу в потоке парсинга.

    Отмена прилетает в ближайший await (парсеры постоянно ждут сеть), браузер
    закрывается в finally collect._do_parse, статус — «остановлено пользователем».
    """
    with collect._parse_lock:
        if not collect._parse_state["running"]:
            return jsonify({"ok": False, "error": "Парсинг не запущен"}), 409
        if collect._parse_state.get("stopping"):
            return jsonify({"ok": True, "note": "Остановка уже запрошена"})
        loop = collect._parse_cancel.get("loop")
        task = collect._parse_cancel.get("task")
        if loop is None or task is None:
            return jsonify({"ok": False, "error": "Задача парсинга не найдена"}), 500
        collect._parse_state["stopping"] = True
        collect._parse_state["progress"].append("⏹ Останавливаю парсинг…")
    try:
        loop.call_soon_threadsafe(task.cancel)
    except RuntimeError as e:
        # Loop уже закрыт — парсинг успел завершиться сам.
        return jsonify({"ok": False, "error": f"Парсинг уже завершился ({e})"}), 409
    return jsonify({"ok": True})
