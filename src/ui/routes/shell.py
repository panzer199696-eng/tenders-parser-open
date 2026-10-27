"""
Оболочка окна: главная страница и выход из программы.

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import os
import threading
import time

from flask import Blueprint, jsonify, render_template, request

from ...version import APP_VERSION


bp = Blueprint("shell", __name__)


# ── Маршруты: разметка ────────────────────────────────────────────────────────


@bp.route("/")
def index():
    return render_template("index.html", version=APP_VERSION)


@bp.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    stop = request.environ.get("werkzeug.server.shutdown")

    def _do():
        time.sleep(0.3)
        try:
            if stop:
                stop()
            else:
                os._exit(0)
        except Exception:
            os._exit(0)

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})
