"""
Единый источник справочников (задача T004).

**Источники истины** — три файла, которые правит человек:

    data/nomenclature.json   группы номенклатуры: фразы, вес, tier
    data/clients.json        заказчики: уровень работы, алиасы, ИНН
    data/rules.json          правила отбора: пороги, исключения, подсказки

**Производные** — рабочие файлы программы, которые из них получаются:

    keywords.json  companies.json  client_inns.json

До слияния источники жили в одной программе, производные в другой, а между
ними стоял скрипт, который человек запускал руками. Забыл запустить —
справочники разъехались, и лоты закреплённого заказчика переставали
находиться. Теперь производные пересобираются при каждом сохранении
источника (`save_clients`, `save_nomenclature`), и разойтись им негде.

Ручные переключения фраз при этом сохраняются: см. `derive.build_keywords`
и `derive.adopt_manual_flags` — решение человека сильнее пересчёта.
"""

import json
import os
import tempfile
from pathlib import Path

from ..runtime_paths import user_data_dir
from . import derive

BASE_DIR = user_data_dir()
DATA_DIR = BASE_DIR / "data"

NOMENCLATURE_FILE = DATA_DIR / "nomenclature.json"
CLIENTS_FILE = DATA_DIR / "clients.json"
RULES_FILE = DATA_DIR / "rules.json"

KEYWORDS_FILE = BASE_DIR / "keywords.json"
COMPANIES_FILE = BASE_DIR / "companies.json"
CLIENT_INNS_FILE = BASE_DIR / "client_inns.json"


def _read(path: Path, default: dict | None = None) -> dict:
    """Прочитать JSON; нет файла или битый — вернуть значение по умолчанию.

    Молчаливое падение здесь недопустимо, но и ронять программу из-за
    справочника нельзя: без номенклатуры сбор просто не найдёт ничего нового,
    а с исключением не запустится вовсе.
    """
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(default or {})
    return data if isinstance(data, dict) else dict(default or {})


def _write_atomic(path: Path, payload: dict) -> None:
    """Атомарная запись: либо целый новый файл, либо прежний.

    Справочник читается прогоном сбора; недописанный файл в этот момент
    означал бы потерю всех целей обхода.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Источники ────────────────────────────────────────────────────────────────


def load_nomenclature() -> dict:
    return _read(NOMENCLATURE_FILE, {"groups": []})


def load_clients() -> dict:
    return _read(CLIENTS_FILE, {"clients": []})


def load_rules() -> dict:
    return _read(RULES_FILE, {})


def _merged(path: Path, data: dict, default: dict) -> dict:
    """Слить входящий документ с тем, что уже лежит на диске.

    Интерфейс правит справочник по разделам и присылает только тот, который
    менял: экран номенклатуры — «groups», экран заказчиков — «clients».
    Записать это как весь документ значит молча стереть остальное: так
    исчезали стоп-слова номенклатуры и веса приоритетов заказчиков при первом
    же нажатии «Сохранить» (найдено 01.09.2026). Поэтому верхний уровень
    сливается, а не заменяется: переданные разделы обновляются, остальные
    остаются как были.
    """
    current = _read(path, default)
    return {**current, **data}


def save_nomenclature(data: dict, tier: str = derive.TIER_DAILY) -> None:
    """Сохранить номенклатуру и сразу пересобрать фразы поиска."""
    _write_atomic(NOMENCLATURE_FILE, _merged(NOMENCLATURE_FILE, data, {"groups": []}))
    sync_derived(tier=tier)


def save_clients(data: dict) -> None:
    """Сохранить заказчиков и сразу пересобрать объекты обхода."""
    _write_atomic(CLIENTS_FILE, _merged(CLIENTS_FILE, data, {"clients": []}))
    sync_derived()


def save_rules(data: dict) -> None:
    _write_atomic(RULES_FILE, _merged(RULES_FILE, data, {}))


# ── Производные ──────────────────────────────────────────────────────────────


def sync_derived(tier: str = derive.TIER_DAILY) -> dict:
    """Пересобрать рабочие файлы из источников. Возвращает сводку по файлам.

    Вызывается при каждом сохранении справочника и при первом запуске
    программы. Идемпотентна: повторный вызов без правок даёт то же самое.
    """
    nomenclature = load_nomenclature()
    clients = load_clients()

    keywords = derive.build_keywords(
        nomenclature, tier=tier, previous=_read(KEYWORDS_FILE)
    )
    companies = derive.build_companies(clients)
    client_inns = derive.build_client_inns(clients)

    _write_atomic(KEYWORDS_FILE, keywords)
    _write_atomic(COMPANIES_FILE, companies)
    _write_atomic(CLIENT_INNS_FILE, client_inns)

    return {
        "keywords": len(keywords.get("keywords", [])),
        "keywords_enabled": sum(
            1 for k in keywords.get("keywords", []) if k.get("enabled")
        ),
        "companies": len(companies.get("companies", [])),
        "client_inns": len(client_inns.get("targets", [])),
    }


def adopt_existing_keywords(tier: str = derive.TIER_DAILY) -> int:
    """Признать текущие переключения фраз ручными (задача T006).

    Разовая операция при переходе на единый справочник: в рабочем
    `keywords.json` уже есть решения человека (31.08.2026 включены 12 широких
    фраз), но признака `manual` в файле нет. Без этого первая же генерация
    их отменит. Возвращает число помеченных фраз.
    """
    existing = _read(KEYWORDS_FILE)
    if not existing.get("keywords"):
        return 0
    adopted = derive.adopt_manual_flags(existing, load_nomenclature(), tier=tier)
    _write_atomic(KEYWORDS_FILE, adopted)
    return sum(1 for row in adopted.get("keywords", []) if row.get("manual"))
