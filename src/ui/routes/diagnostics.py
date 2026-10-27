"""
Раздел «Диагностика»: хвост журнала и локальный диагностический архив.

Архив только сохраняется на диск — программа никуда его не отправляет.
"""

import json
import logging
import platform
import sys
import zipfile
from datetime import datetime
from io import BytesIO

from flask import Blueprint, jsonify, request, send_file

from ...core import context as ctx
from ...runtime_paths import results_dir
from ...version import APP_VERSION
from .reports import _open_in_explorer

logger = logging.getLogger(__name__)

bp = Blueprint("diagnostics", __name__)

# Сколько последних строк журнала кладём в архив.
ZIP_LOG_TAIL = 3000

# Ключи настроек, значения которых в архив не попадают ни при каких условиях.
_SECRET_MARKERS = ("password", "token", "secret", "login", "key")


def _redact(data):
    """Скрыть значения ключей, похожих на секреты (рекурсивно)."""
    if isinstance(data, dict):
        return {
            k: "***"
            if any(m in str(k).lower() for m in _SECRET_MARKERS)
            else _redact(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact(v) for v in data]
    return data


def _log_tail(lines: int) -> str:
    if not ctx.LOG_FILE.exists():
        return ""
    try:
        text = ctx.LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def build_diag_zip() -> bytes:
    """Диагностический zip: хвост журнала, настройки без секретов, справочники."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        tail = _log_tail(ZIP_LOG_TAIL)
        if tail:
            zf.writestr("logs/parser.log", tail)

        settings_file = ctx.BASE_DIR / "settings.json"
        if settings_file.exists():
            try:
                parsed = json.loads(settings_file.read_text(encoding="utf-8"))
                zf.writestr(
                    "settings.json",
                    json.dumps(_redact(parsed), ensure_ascii=False, indent=2),
                )
            except (json.JSONDecodeError, OSError):
                pass

        for name in ("companies.json", "keywords.json"):
            path = ctx.BASE_DIR / name
            if path.exists():
                try:
                    zf.writestr(name, path.read_text(encoding="utf-8"))
                except OSError:
                    pass

        zf.writestr(
            "info.txt",
            "\n".join(
                [
                    f"Версия: {APP_VERSION}",
                    f"Собрано: {datetime.now():%Y-%m-%d %H:%M:%S}",
                    f"Python: {sys.version.split()[0]}",
                    f"ОС: {platform.platform()}",
                ]
            )
            + "\n",
        )
    return buf.getvalue()


# ── Маршруты: логи ────────────────────────────────────────────────────────────


@bp.route("/api/logs")
def api_logs():
    lines = request.args.get("lines", "500", type=int)
    if not ctx.LOG_FILE.exists():
        return jsonify({"lines": ""})
    try:
        all_lines = ctx.LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return jsonify({"lines": ""})
    tail = all_lines[-lines:] if lines and lines > 0 else all_lines
    return jsonify({"lines": "\n".join(tail)})


# ── Маршруты: диагностика ─────────────────────────────────────────────────────


@bp.route("/api/diagnostics")
def api_diagnostics():
    """Скачать диагностический zip."""
    try:
        data = build_diag_zip()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            BytesIO(data),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"diagnostics_{ts}.zip",
        )
    except Exception as e:
        logger.exception("diagnostics error")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/diagnostics/save", methods=["POST"])
def api_diagnostics_save():
    """Сохранить диагностический архив в папку результатов и показать его."""
    try:
        data = build_diag_zip()
        target = results_dir() / f"Диагностика_{datetime.now():%Y-%m-%d_%H-%M}.zip"
        target.write_bytes(data)
    except Exception as e:
        logger.exception("diagnostics save error")
        return jsonify({"ok": False, "error": str(e)}), 500
    _open_in_explorer(target)
    return jsonify({"ok": True, "path": str(target)})
