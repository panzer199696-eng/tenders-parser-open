"""
Слой настроек приложения.

Хранит настройки в `settings.json` (корень проекта, рядом с `companies.json`).
Настройки из `.env` (DAYS_BACK, MIN_PRICE, PARSE_*, BROWSER_HEADLESS)
переносятся сюда один раз при первом запуске (если settings.json отсутствует),
после чего `.env` перестаёт быть источником истины для раннера.
"""

import json
import logging
import os
import tempfile
from typing import Any

# Настройки — файл пользователя: в dev-запуске корень проекта, в сборке
# %LOCALAPPDATA%\TendersParser (см. runtime_paths.user_data_dir).
from .runtime_paths import user_data_dir

BASE_DIR = user_data_dir()
SETTINGS_FILE = BASE_DIR / "settings.json"

logger = logging.getLogger(__name__)


DEFAULTS: dict[str, Any] = {
    # Кто работает в программе: этим именем подписываются решения «Беру» и
    # «Мимо» в реестре. Пустое значение — не ошибка: интерфейс спросит имя
    # при первом решении. Раньше имя было зашито в коде страницы, и на
    # чужой машине все отметки подписывались чужим именем.
    "manager_name": "",
    # Заказчики «на особом контроле»: их лоты проходят без фильтров отбора
    # (порог цены, мягкий keyword-фильтр, корзины ленты) — кроме срока подачи.
    # Сверка — по целым словам названия организатора (src/watch_clients.py).
    # По умолчанию список пуст: заполняется в настройках программы.
    "watch_clients": [],
    # Фразы, которые НЕ отдаём в keyword-поиск ТЭК-Торга. Площадка ищет по
    # коротким обозначениям (марки, артикулы) как попало: такая фраза может
    # листать десятки страниц чужих закупок, упираться в потолок страниц и
    # порождать ошибки 503. На других площадках те же фразы могут работать,
    # поэтому из справочника их не убираем — режем адресно, здесь.
    "tektorg_skip_phrases": [],
    "days_back": 1,
    "min_price": 0,  # 0 = нет порога
    "max_price": 0,  # 0 = нет потолка
    "include_no_price": True,
    "parse_tender_pro": True,
    "parse_gpb": False,
    # ЕИС (zakupki.gov.ru) — RSS расширенного поиска по фразам, работает без
    # авторизации (см. src/parsers/zakupki_parser.py). ВЫКЛЮЧЕН по умолчанию:
    # поиск по общей номенклатуре на госзакупках тащит много нерелевантного
    # (медицина, ЖКХ: «активированный уголь» → лекарственные препараты).
    # Включать осознанно, если ваши заказчики публикуются по 44-ФЗ/223-ФЗ.
    "parse_zakupki": False,
    # ТЭК-Торг (tektorg.ru) — JSON API без авторизации и браузера (см.
    # src/parsers/tektorg_parser.py). Корпоративные секции крупных
    # промышленных заказчиков, входа и капчи нет.
    "parse_tektorg": True,
    # Росэлторг (roseltorg.ru) — HTML-фрагмент выдачи, без авторизации и
    # браузера. Включён по умолчанию ради адресного обхода: поле поиска
    # принимает ИНН, и запрос по нему даёт лоты конкретного заказчика.
    "parse_roseltorg": True,
    # Поиск по фразам на Росэлторге морфологический и очень слабый: узкие
    # марки упираются в потолок выдачи (200 лотов) почти сплошного шума.
    # Ценность площадки — в обходе по ИНН, он даёт лоты конкретного
    # заказчика. Поэтому фразы здесь выключены по умолчанию; включить можно
    # в настройках, если площадка исправится.
    "roseltorg_keyword_search": False,
    # Bidzaar (bidzaar.com) — агрегатор закупок со всех площадок России,
    # открытый JSON API без авторизации, кук и браузера (см.
    # src/parsers/bidzaar_parser.py). Включён сразу: площадка бесплатна,
    # не требует входа и по фразам находит лоты, которых нет на
    # корпоративных площадках.
    "parse_bidzaar": True,
    # Расписание сбора (фича 002, Э4): время суток и дни недели. Выключено по
    # умолчанию — программа не должна начинать ходить по площадкам без ведома
    # пользователя. Подробности — src/schedule/schedule.py.
    "schedule": {
        "enabled": False,
        "time": "03:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "catch_up": True,
    },
    "browser_headless": True,
    "keyword_search_enabled": True,  # режим A — вкл. по умолчанию с 2026-07-07
    # Режим B — вкл. по умолчанию с 26.08.2026: фильтр МЯГКИЙ (см.
    # apply_keyword_filter в core/parser_runner.py) — проверяет только лоты,
    # найденные keyword-поиском, и отсекает нефильтрованную ленту площадки;
    # лоты закреплённых заказчиков и адресного обхода не трогает.
    "keyword_filter_enabled": True,
}


# ── Публичный API ────────────────────────────────────────────────────────────


def load_settings() -> dict[str, Any]:
    """Вернуть настройки приложения.

    - Если `settings.json` есть и валиден — накладываем его поверх DEFAULTS
      (неизвестные ключи игнорируются, отсутствующие берутся из DEFAULTS).
    - Если файла нет — мигрируем значения из `.env` (через os.getenv),
      сохраняем `settings.json` и возвращаем его. Так старый `.env` не
      теряется при первом запуске после обновления.
    - Если файл есть, но битый JSON — возвращаем копию DEFAULTS (и warning),
      НЕ перезаписывая его (чтобы пользователь мог руками починить).
    """
    if not SETTINGS_FILE.exists():
        migrated = _migrate_from_env()
        try:
            save_settings(migrated)
        except Exception as e:  # pragma: no cover - отказ записи некритичен
            logger.warning("Не удалось сохранить settings.json при миграции: %s", e)
        return migrated

    try:
        raw = SETTINGS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "settings.json бит/нечитаем — возвращаю DEFAULTS (%s). "
            "Файл не перезаписываю, почините вручную.",
            e,
        )
        return _copy_defaults()

    if not isinstance(data, dict):
        logger.warning(
            "settings.json должен содержать JSON-объект на верхнем уровне — "
            "возвращаю DEFAULTS."
        )
        return _copy_defaults()

    merged = _copy_defaults()
    for k, default_val in DEFAULTS.items():
        if k not in data:
            continue
        val = data[k]
        # Жёсткая проверка типа по DEFAULTS: int/bool/float.
        if _type_matches(val, default_val):
            merged[k] = val
        else:
            logger.warning(
                "settings.json: ключ %r имеет неверный тип (%s), использую DEFAULTS=%r",
                k,
                type(val).__name__,
                default_val,
            )
    return merged


def save_settings(d: dict[str, Any]) -> None:
    """Атомарно записать настройки в `settings.json`.

    Валидация типов делается по DEFAULTS: ключи с неверным типом логируются
    и заменяются на умолчания. Запись идёт через tmp-файл + os.replace, чтобы
    при падении в середине записи файл не оказался битым.
    """
    cleaned = _copy_defaults()
    for k, default_val in DEFAULTS.items():
        if k in d and _type_matches(d[k], default_val):
            cleaned[k] = d[k]
        elif k in d:
            logger.warning(
                "save_settings: ключ %r неверного типа (%s) — ставлю DEFAULTS",
                k,
                type(d[k]).__name__,
            )

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # tempfile внутри той же директории — os.replace атомарна на одной ФС.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".settings-", suffix=".tmp", dir=str(SETTINGS_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, SETTINGS_FILE)
    except Exception:
        # Чистим tmp, если что-то пошло не так до replace.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Внутренние хелперы ───────────────────────────────────────────────────────


def _copy_defaults() -> dict[str, Any]:
    return dict(DEFAULTS)


def _type_matches(value: Any, template: Any) -> bool:
    """Проверка типа значения по шаблону из DEFAULTS.

    bool — это подтип int в Python, поэтому проверяем строго: если шаблон
    bool, значение обязано быть bool (а не int). Если шаблон int/float —
    принимаем любой числовой тип (int для float-поля и наоборот допустим).
    """
    if isinstance(template, bool):
        return isinstance(value, bool)
    if isinstance(template, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    # На будущее — строки/списки/словари.
    return isinstance(value, type(template))


def _migrate_from_env() -> dict[str, Any]:
    """Перенести значения из `.env`/окружения в настройки.

    Используем os.getenv (python-dotenv уже вызвал load_dotenv в src/config.py
    при импорте, но мы не зависим от этого — здесь только getenv). Берём только
    переменные из ТЗ; отсутствующие остаются как в DEFAULTS.
    """
    settings = _copy_defaults()

    days_back_raw = os.getenv("DAYS_BACK")
    if days_back_raw:
        try:
            settings["days_back"] = int(days_back_raw)
        except ValueError:
            logger.warning("DAYS_BACK=%r не int — пропускаю", days_back_raw)

    min_price_raw = os.getenv("MIN_PRICE")
    if min_price_raw:
        try:
            settings["min_price"] = float(min_price_raw)
        except ValueError:
            logger.warning("MIN_PRICE=%r не число — пропускаю", min_price_raw)

    # MAX_PRICE в .env historically нет, но если когда-то появится — подхватим.
    max_price_raw = os.getenv("MAX_PRICE")
    if max_price_raw:
        try:
            settings["max_price"] = float(max_price_raw)
        except ValueError:
            logger.warning("MAX_PRICE=%r не число — пропускаю", max_price_raw)

    headless_raw = os.getenv("BROWSER_HEADLESS")
    if headless_raw is not None:
        settings["browser_headless"] = headless_raw.strip().lower() == "true"

    # Флаги площадок — по умолчанию как в DEFAULTS (см. .env.example), но
    # если переменная задана явно — слушаем её.
    settings["parse_gpb"] = _env_bool("PARSE_GPB", default=False)
    settings["parse_tender_pro"] = _env_bool("PARSE_TENDER_PRO", default=True)
    settings["parse_zakupki"] = _env_bool("PARSE_ZAKUPKI", default=False)
    settings["parse_tektorg"] = _env_bool("PARSE_TEKTORG", default=True)
    settings["parse_roseltorg"] = _env_bool("PARSE_ROSELTORG", default=True)

    logger.info(
        "Миграция .env → settings.json: days_back=%s, min_price=%s, "
        "headless=%s, gpb=%s, tender_pro=%s, zakupki=%s, "
        "tektorg=%s, roseltorg=%s",
        settings["days_back"],
        settings["min_price"],
        settings["browser_headless"],
        settings["parse_gpb"],
        settings["parse_tender_pro"],
        settings["parse_zakupki"],
        settings["parse_tektorg"],
        settings["parse_roseltorg"],
    )
    return settings


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() == "true"
