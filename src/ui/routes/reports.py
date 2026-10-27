"""
Раздел «Отчёты»: сводки по базе, итоги последнего прогона, книга Excel
и папка результатов.

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import logging
import os
import subprocess
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Blueprint, jsonify, send_file

from ... import run_journal
from ...core import collect
from ...core import context as ctx
from ...runtime_paths import results_dir

logger = logging.getLogger(__name__)

bp = Blueprint("reports", __name__)


def _open_in_explorer(path: Path) -> bool:
    """Показать файл или папку в проводнике. False — не удалось."""
    try:
        if sys.platform.startswith("win"):
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path if path.is_dir() else path.parent)])
        return True
    except Exception:
        logger.exception("Не удалось открыть папку результатов")
        return False


@bp.route("/api/reports")
def api_reports():
    """Сводка: вклад площадок, группы номенклатуры, воронка, сроки."""
    from src.db import reports_summary

    try:
        conn = ctx.unified_conn()
        try:
            return jsonify(reports_summary(conn))
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Отчёты недоступны")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/last-run")
def api_last_run():
    """Итоги последнего сбора: полнота словами, чтобы цифрам можно было верить.

    Этим же ответом пользуется соседняя программа тендерной работы: она
    показывает цифры по базе и без него не может сказать, полны ли они.
    """
    record = run_journal.read_last_run()
    return jsonify(
        {
            "record": record,
            "text": run_journal.describe(record),
            "data_complete": bool(record.get("data_complete")) if record else None,
            "stale": run_journal.is_stale(record),
        }
    )


# ── Маршруты: Excel / остановка ───────────────────────────────────────────────


@bp.route("/api/excel/latest")
def api_excel_latest():
    """Книга по темам из единой базы — доступна всегда, без прогона.

    ТЗ-5: база есть всегда, значит и отчёт есть всегда. Прежний ответ
    «Сначала запустите парсинг» заставлял человека гонять сбор ради
    вчерашних данных.
    """
    from src.core.excel_report import build_report
    from src.db import connect as _db_connect
    from src.db import ensure_schema as _db_schema

    conn = _db_connect(ctx.UNIFIED_DB_PATH)
    try:
        _db_schema(conn)
        data = build_report(conn)
    finally:
        conn.close()
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"tenders_{ts}.xlsx",
    )


@bp.route("/api/excel/save", methods=["POST"])
def api_excel_save():
    """Сохранить книгу из единой базы в «Документы\\TendersParser»."""
    try:
        path = collect._save_excel_file()
    except Exception as e:
        logger.exception("Не удалось сохранить Excel")
        return jsonify({"ok": False, "error": f"Не удалось сохранить файл: {e}"}), 500
    with collect._parse_lock:
        collect._parse_state["excel_path"] = str(path)
    return jsonify({"ok": True, "path": str(path), "folder": str(path.parent)})


@bp.route("/api/results/open", methods=["POST"])
def api_results_open():
    """Открыть проводник на папке результатов (или подсветить файл)."""
    with collect._parse_lock:
        last = collect._parse_state.get("excel_path")
    target = Path(last) if last and Path(last).exists() else results_dir()
    if not _open_in_explorer(target):
        return jsonify(
            {
                "ok": False,
                "error": f"Не удалось открыть папку. Она здесь: {results_dir()}",
            }
        ), 500
    return jsonify({"ok": True, "path": str(target)})
