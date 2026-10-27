# -*- coding: utf-8 -*-
"""
Хранилище ИНН заказчиков (адресный отбор на нашей стороне).

Файл `client_inns.json` в папке данных пользователя:
    {
      "_comment": "...",
      "updated": "2026-08-31",
      "targets": [
        {"inn": "0000000000", "name": "ООО «Ромашка»",
         "client": "romashka", "verified": false}
      ]
    }

Зачем ИНН, а не названия: у ТЭК-Торга НЕТ фильтра по заказчику на входе,
отбор идёт по выдаче — и организаторы в выдаче («ООО ПК "Северное"»,
«Филиал ПАО «ГМК «Пример"») практически не совпадают со справочником
companies.json («Северная горная», «Пример»).
Поле inn в выдаче совпадает с ЕГРЮЛ один в один, поэтому адресный режим
идёт по ИНН; названия остаются запасным путём, пока ИНН не заданы.

`verified=false` — «совпадение по названию не сверено с ЕГРЮЛ», пометка
для человека: на отбор НЕ влияет, используем все ИНН из targets[].

`load_client_inns()` — как keywords_store: нет/битый файл, нет ключа
targets → [] и warning, прогон не рушится (файла у пользователя может
не быть).
"""

import json
import logging
from pathlib import Path

from .runtime_paths import user_data_dir

BASE_DIR = user_data_dir()
CLIENT_INNS_FILE = BASE_DIR / "client_inns.json"

logger = logging.getLogger(__name__)


def load_client_inns(path: Path | None = None) -> list[str]:
    """Прочитать `client_inns.json` и вернуть список ИНН строками.

    - путь по умолчанию — папка данных пользователя (как COMPANIES_FILE
      в config.py: user_data_dir(), в dev-запуске — корень проекта);
    - порядок targets[] сохраняется, пустые и повторы пропускаются;
    - inn может прийти числом (JSON без кавычек) — приводим к str и strip;
    - нет файла / битый JSON / нет ключа targets / OSError → [] и warning:
      отсутствие справочника не должно ронять прогон, адресный обход
      просто уйдёт в запасной отбор по названиям компаний.
    """
    file = path if path is not None else CLIENT_INNS_FILE

    try:
        if not file.exists():
            logger.warning("client_inns.json не найден (%s) — возвращаю []", file)
            return []
        raw = file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("client_inns.json бит/нечитаем — возвращаю [] (%s)", e)
        return []

    if not isinstance(data, dict):
        logger.warning("client_inns.json: верхний уровень не object — возвращаю []")
        return []

    targets = data.get("targets")
    if not isinstance(targets, list):
        logger.warning(
            "client_inns.json: нет ключа 'targets' (или не list) — возвращаю []"
        )
        return []

    inns: list[str] = []
    seen: set[str] = set()
    for item in targets:
        if not isinstance(item, dict):
            continue
        raw_inn = item.get("inn")
        # ИНН мог приехать числом — приводим к строке; bool и прочие типы
        # (float с хвостом «.0», вложенные объекты) — мусор, пропускаем.
        if isinstance(raw_inn, bool) or not isinstance(raw_inn, (str, int)):
            continue
        inn = str(raw_inn).strip()
        if not inn or inn in seen:
            continue
        seen.add(inn)
        inns.append(inn)
    return inns
