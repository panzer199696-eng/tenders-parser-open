"""
Общий контекст программы: папка данных, ключевые файлы, единая база.

Вынесено из main_ui.py (рефакторинг 16.10.2026): эти пути нужны и сбору
(src/core/collect.py), и маршрутам окна (src/ui/routes/). Читать их надо
атрибутом модуля — `context.UNIFIED_DB_PATH`, а не копией через
`from … import`: тесты подменяют их здесь, и копия подмены не увидит.
"""

import json

from ..runtime_paths import user_data_dir

BASE_DIR = user_data_dir()

LOG_FILE = BASE_DIR / "logs" / "parser.log"
ENV_FILE = BASE_DIR / ".env"
COMPANIES_FILE = BASE_DIR / "companies.json"

# Единая база программы (фича 002). Живёт рядом с прежней tenders.db
# в папке данных пользователя, поэтому переживает обновление программы.
UNIFIED_DB_PATH = BASE_DIR / "data" / "tenderwork.db"


def load_companies():
    if COMPANIES_FILE.exists():
        try:
            with open(COMPANIES_FILE, encoding="utf-8") as f:
                return json.load(f).get("companies", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_companies(companies):
    COMPANIES_FILE.write_text(
        json.dumps({"companies": companies}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def unified_conn():
    """Соединение с единой базой со свежей схемой."""
    from ..db import connect as _connect
    from ..db import ensure_schema as _ensure

    conn = _connect(UNIFIED_DB_PATH)
    _ensure(conn)
    return conn
