"""
Раздел «Настройки»: параметры сбора, компании, фразы, расписание.

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import logging
import sys

from flask import Blueprint, jsonify, request

from ...core import context as ctx
from ...runtime_paths import app_base_dir

logger = logging.getLogger(__name__)

bp = Blueprint("settings", __name__)


# ── Маршруты: настройки ───────────────────────────────────────────────────────


@bp.route("/api/settings")
def api_get_settings():
    from src.settings_store import load_settings

    return jsonify(load_settings())


@bp.route("/api/settings", methods=["POST"])
def api_save_settings():
    from src.settings_store import load_settings, save_settings

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Некорректный JSON в теле запроса"}), 400
    merged = load_settings()  # сохраняем не указанные в запросе поля
    for k, v in body.items():
        merged[k] = v
    save_settings(merged)
    return jsonify({"ok": True})


# ── Маршруты: компании ────────────────────────────────────────────────────────


@bp.route("/api/companies")
def api_get_companies():
    return jsonify({"companies": ctx.load_companies()})


@bp.route("/api/companies", methods=["POST"])
def api_save_companies():
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Некорректный JSON в теле запроса"}), 400
    companies = body.get("companies")
    if not isinstance(companies, list):
        return jsonify({"error": "Ожидался объект {companies: [...]}"}), 400
    seen = set()
    clean = []
    for c in companies:
        name = str(c).strip()
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            clean.append(name)
    ctx.save_companies(clean)
    return jsonify({"ok": True, "companies": clean})


# ── Маршруты: ключевые слова ──────────────────────────────────────────────────


@bp.route("/api/keywords")
def api_get_keywords():
    from src.keywords_store import load_keywords
    from src.settings_store import load_settings

    s = load_settings()
    return jsonify(
        {
            "keywords": load_keywords(),
            "keyword_search_enabled": bool(s.get("keyword_search_enabled", False)),
            "keyword_filter_enabled": bool(s.get("keyword_filter_enabled", True)),
        }
    )


@bp.route("/api/keywords", methods=["POST"])
def api_save_keywords():
    from src.keywords_store import save_keywords
    from src.settings_store import load_settings, save_settings

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Некорректный JSON в теле запроса"}), 400
    if "keywords" in body:
        kws = body["keywords"]
        if not isinstance(kws, list):
            return jsonify({"error": "keywords должен быть списком"}), 400
        save_keywords(kws)

    if "keyword_search_enabled" in body or "keyword_filter_enabled" in body:
        s = load_settings()
        if "keyword_search_enabled" in body:
            s["keyword_search_enabled"] = bool(body["keyword_search_enabled"])
        if "keyword_filter_enabled" in body:
            s["keyword_filter_enabled"] = bool(body["keyword_filter_enabled"])
        save_settings(s)
    return jsonify({"ok": True})


def _collect_command() -> str:
    """Чем планировщик будет запускать сбор.

    В сборке это сам exe с флагом, в dev-запуске — python с main_ui.py
    (рядом с программой: app_base_dir).
    Путь берём фактический, а не угаданный: задача в планировщике переживёт
    перенос программы в другую папку только если её пересоздать, и об этом
    сказано в подсказке интерфейса.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --collect'
    return f'"{sys.executable}" "{(app_base_dir() / "main_ui.py").resolve()}" --collect'


@bp.route("/api/schedule")
def api_schedule():
    """Текущее расписание и его состояние в планировщике Windows."""
    from src.schedule import describe, normalize, task_exists
    from src.settings_store import load_settings

    schedule = normalize(load_settings().get("schedule"))
    return jsonify(
        {
            "schedule": schedule,
            "text": describe(schedule),
            "task_registered": task_exists(),
            "command": _collect_command(),
        }
    )


@bp.route("/api/schedule", methods=["POST"])
def api_schedule_save():
    """Сохранить расписание и поставить задачу в планировщик Windows."""
    from src.schedule import apply as apply_schedule
    from src.schedule import describe, normalize
    from src.settings_store import load_settings, save_settings

    payload = request.get_json(silent=True) or {}
    schedule = normalize(payload.get("schedule"))
    try:
        settings = load_settings()
        settings["schedule"] = schedule
        save_settings(settings)
        result = apply_schedule(schedule, _collect_command())
        return jsonify(
            {
                "schedule": schedule,
                "text": describe(schedule),
                "ok": result["ok"],
                "message": result["message"],
            }
        )
    except Exception as e:
        logger.exception("Не удалось сохранить расписание")
        return jsonify({"error": str(e)}), 500
