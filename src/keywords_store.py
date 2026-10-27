"""
Хранилище ключевых слов (режим B — фильтр по фразам).

Файл `keywords.json` в корне проекта:
    { "keywords": [ {"phrase": "строительство моста", "enabled": true} ] }

- `load_keywords()` — как settings_store: битый/нет файла → [].
- `save_keywords(list)` — атомарная запись; фразы strip(), пустые не
  сохранять, дубли (case-insensitive) схлопывать.
- `enabled_phrases()` — только включённые, в порядке файла.

"""

import json
import logging
import os
import tempfile
from typing import Any

from .runtime_paths import user_data_dir

BASE_DIR = user_data_dir()
KEYWORDS_FILE = BASE_DIR / "keywords.json"

logger = logging.getLogger(__name__)


# ── Публичный API ────────────────────────────────────────────────────────────


def load_keywords() -> list[dict[str, Any]]:
    """Прочитать `keywords.json` и вернуть список записей.

    Битый/отсутствующий файл → пустой список (никаких исключений).
    Каждая запись приводится к каноническому виду {phrase, enabled}.
    """
    if not KEYWORDS_FILE.exists():
        return []

    try:
        raw = KEYWORDS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("keywords.json бит/нечитаем — возвращаю [] (%s)", e)
        return []

    if not isinstance(data, dict):
        logger.warning("keywords.json: верхний уровень не object — возвращаю []")
        return []

    raw_list = data.get("keywords")
    if not isinstance(raw_list, list):
        logger.warning("keywords.json: поле 'keywords' не list — возвращаю []")
        return []

    cleaned: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        phrase = item.get("phrase")
        if not isinstance(phrase, str):
            continue
        phrase = phrase.strip()
        if not phrase:
            continue
        enabled = bool(item.get("enabled", False))
        row = {"phrase": phrase, "enabled": enabled}
        # Пометку «выставил человек» переносим при чтении: интерфейс читает
        # список, правит одну фразу и сохраняет обратно — потеряв пометку
        # здесь, мы стёрли бы решения пользователя по всем остальным.
        if item.get("manual"):
            row["manual"] = True
        cleaned.append(row)
    return cleaned


def save_keywords(keywords: list[dict[str, Any]]) -> None:
    """Атомарно записать список фраз в `keywords.json`.

    - strip() каждая фраза;
    - пустые фразы пропускаются;
    - дубли схлопываются (case-insensitive), сохраняется первый по порядку;
    - enabled по умолчанию False, если не указано иное.
    """
    normalized: list[dict[str, Any]] = []
    seen_lower = set()
    for item in keywords:
        if not isinstance(item, dict):
            continue
        phrase = item.get("phrase")
        if not isinstance(phrase, str):
            continue
        phrase = phrase.strip()
        if not phrase:
            continue
        key = phrase.casefold()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        enabled = bool(item.get("enabled", False))
        row = {"phrase": phrase, "enabled": enabled}
        # Пометка «состояние выставил человек»: генерация справочников из
        # номенклатуры обязана её уважать, иначе очередное сохранение молча
        # отменяло бы настройку пользователя (см. src/store/derive.py).
        if item.get("manual"):
            row["manual"] = True
        normalized.append(row)

    payload = {"keywords": normalized}

    KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".keywords-", suffix=".tmp", dir=str(KEYWORDS_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, KEYWORDS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def enabled_phrases() -> list[str]:
    """Только включённые фразы, в порядке файла."""
    return [kw["phrase"] for kw in load_keywords() if kw.get("enabled")]
