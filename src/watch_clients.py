"""
Заказчики на особом контроле — единственное место правила.

Правило (07.09.2026, расширение 16.09.2026): лот заказчика особого
контроля не отсекается фильтрами сбора — ни порогом цены, ни мягким
keyword-фильтром — и не прячется в корзину «Не наш профиль». По этим
заказчикам работа идёт адресно, и их лот с ценой 1000 ₽ — не шум, а
находка: обычные фильтры на него не смотрят.

Список имён живёт в настройках (``watch_clients``, по умолчанию пуст).
Сверка — ТОЛЬКО по целым словам, не подстрокой: «кедр» входит в «Кедрова»,
и пока сравнивали подстрокой, на лист особого контроля приезжали санаторий
имени Кедрова и одноимённый детский лагерь (живая база, 07.09.2026).

Единственное исключение — истёкший дедлайн: просроченный лот не нужен даже
по заказчику особого контроля, подавать заявку некуда. Это правило фильтра
дедлайна, оно сюда не входит.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Разбивка названия на слова: всё, что не буква и не цифра, — разделитель.
_WORD_SPLIT = re.compile(r"[^0-9a-zA-Zа-яёА-ЯЁ]+")


def watch_names() -> list[str]:
    """Имена заказчиков особого контроля из настроек.

    Настройки читаем внутри функции, а не импортом на уровне модуля:
    тесты монкейпатчат ``settings_store.load_settings``, и связанный
    импорт сломал бы их. Пустой список — правило выключено. Любая ошибка
    чтения — тоже пустой список: сбор не должен падать из-за битого
    settings.json.
    """
    try:
        from .settings_store import load_settings

        names = load_settings().get("watch_clients") or []
    except Exception:
        logger.exception("Не удалось прочитать список заказчиков особого контроля")
        return []
    return [str(n).strip() for n in names if str(n).strip()]


def company_words(company: str) -> set[str]:
    """Слова названия организатора без кавычек и знаков."""
    return {w for w in _WORD_SPLIT.split((company or "").lower().replace("ё", "е")) if w}


def is_watched(company: str | None, names: list[str] | None = None) -> bool:
    """Организатор — заказчик особого контроля?

    ``names=None`` — взять :func:`watch_names`; пустой список имён — False.
    Сверяем ТОЛЬКО по целым словам: имя из нескольких слов совпало, когда
    ВСЕ его слова есть среди слов организатора (``wanted <= words``).
    Подстрока врёт: «кедр» входит в «Кедрова», и на лист особого
    контроля приезжал санаторий имени Кедрова (07.09.2026).
    """
    if names is None:
        names = watch_names()
    if not names:
        return False
    words = company_words(str(company or ""))
    if not words:
        return False
    for name in names:
        wanted = company_words(name)
        if wanted and wanted <= words:
            return True
    return False
