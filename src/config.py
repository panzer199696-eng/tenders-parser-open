"""
Simplified config - only essentials
"""

import json
import os

from dotenv import load_dotenv

from .runtime_paths import user_data_dir

# Paths (в сборке PyInstaller — папка рядом с exe, см. runtime_paths)
BASE_DIR = user_data_dir()

load_dotenv(BASE_DIR / ".env")
COMPANIES_FILE = BASE_DIR / "companies.json"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"

# Create directories
LOGS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)


# Load companies
def load_companies():
    if COMPANIES_FILE.exists():
        with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("companies", [])
    return []


COMPANIES = load_companies()

# Порог цены. Единого порога больше нет: 5 000 000 ₽ (прежний дефолт этой
# константы) отсекал ровно профильные лоты — небольшие партии и запчасти
# стоят сотни тысяч. Порог теперь профильный, по группе
# номенклатуры (data/rules.json → min_price.by_group), и лот ниже порога не
# выбрасывается молча, а помечается. Рабочее значение — в настройках
# (settings_store, дефолт 0 — не отсекать).
#
# Остальные переключатели (площадки, глубина, браузер) читает settings_store
# из .env сам; здесь их копии никем не использовались и удалены 16.10.2026.
MIN_PRICE = float(os.getenv("MIN_PRICE", "0"))
