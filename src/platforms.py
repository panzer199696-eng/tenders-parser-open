"""
Реестр площадок — одно место, где перечислены все площадки программы.

Раньше список жил примерно в десяти местах (раннер, проверка связи, журнал
прогонов, лента, склейка дублей...), и они расходились: в журнале прогонов
не было Bidzaar, а таблица прогонов писала сырое «tektorg» вместо «ТЭК-Торг»
(найдено ревью 16.10.2026). Новая площадка добавляется строкой здесь.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    key: str  # ключ настройки parse_* в settings.json
    name: str  # имя площадки в лотах и отчётах полноты
    probe_url: str  # адрес для проверки связи перед сбором
    runner: str  # метод ParserRunner, который обходит площадку


# Порядок — приоритет первоисточника (ТЗ-7): чем выше, тем ближе к месту
# подачи заявки; агрегатор Bidzaar — последним. Площадки, выключенные по
# умолчанию (ГПБ, ЕИС), идут после известных.
PLATFORMS: tuple[Platform, ...] = (
    Platform("parse_tektorg", "ТЭК-Торг", "https://www.tektorg.ru/", "_run_tektorg"),
    Platform(
        "parse_roseltorg", "Росэлторг", "https://www.roseltorg.ru/", "_run_roseltorg"
    ),
    Platform(
        "parse_tender_pro", "Tender.pro", "https://www.tender.pro/", "_run_tender_pro"
    ),
    Platform("parse_bidzaar", "Bidzaar", "https://bidzaar.com/", "_run_bidzaar"),
    Platform("parse_gpb", "ГПБ", "https://new.etpgpb.ru/", "_run_gpb"),
    Platform("parse_zakupki", "ЕИС", "https://zakupki.gov.ru/", "_run_zakupki"),
)

# Площадки, которые участвуют в приоритете источника. ГПБ и ЕИС в приоритет
# не входят: они выключены по умолчанию, а незнакомая площадка и так идёт
# в конец (см. source_rank).
_PRIORITY_NAMES = (
    "ТЭК-Торг",
    "Росэлторг",
    "Tender.pro",
    "Bidzaar",
)

BY_KEY: dict[str, Platform] = {p.key: p for p in PLATFORMS}
BY_NAME: dict[str, Platform] = {p.name: p for p in PLATFORMS}
KEYS: tuple[str, ...] = tuple(p.key for p in PLATFORMS)


def name_of(key: str) -> str:
    """Имя площадки по ключу настройки; незнакомый ключ — без префикса."""
    platform = BY_KEY.get(key)
    return platform.name if platform else key.removeprefix("parse_")


def key_of(name: str) -> str | None:
    """Ключ настройки по имени площадки (None — незнакомая площадка)."""
    platform = BY_NAME.get(name)
    return platform.key if platform else None


def priority_names() -> tuple[str, ...]:
    """Имена площадок в порядке предпочтения первоисточника."""
    return _PRIORITY_NAMES


def source_rank(name: str) -> int:
    """Место площадки в приоритете первоисточника: меньше — главнее.

    Незнакомая площадка (появится новая — ГПБ, ЕИС) идёт после известных:
    скорее всего это агрегатор либо непроверенный источник.
    """
    name = str(name or "").strip()
    try:
        return _PRIORITY_NAMES.index(name)
    except ValueError:
        return len(_PRIORITY_NAMES)
