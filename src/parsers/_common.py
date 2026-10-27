"""
Общие мелочи парсеров площадок: одна функция вместо копий в каждом файле.

Копии расходились, и каждая ошибалась по-своему (16.10.2026): один парсер из
«1 234 567,89 руб.» получал 123456789.0 — точка «руб.» склеивала копейки
с рублями, сумма вырастала в сто раз и лот уезжал за порог цены; Росэлторг
из «1.234.567,89» брал 1.234; ГПБ выбрасывал десятичный разделитель вовсе.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date, timedelta

from ..watch_clients import company_words

# ── Очистка текстовых полей лота ────────────────────────────────────────────
#
# Площадки отдают названия с HTML-мнемониками и тегами подсветки совпадения:
# `НАО &quot;РОМАШКА&quot;`, `&laquo;Открытый запрос&raquo;`,
# `Запчасти к <mark>насосам</mark>`. Встречается двойное экранирование
# (`&amp;quot;`) — из API приходит уже экранированный HTML. Без очистки этот
# мусор доезжает до Excel и делает список нечитаемым.

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_field(text) -> str:
    """Человеческий текст из значения площадки: без мнемоник, тегов и двойных пробелов."""
    if not text or not isinstance(text, str):
        return ""
    # Двойной прогон: из API приходит `&amp;quot;` → `&quot;` → `"`.
    out = html_lib.unescape(html_lib.unescape(text))
    out = _TAG_RE.sub("", out)
    return _WS_RE.sub(" ", out).strip()


# Первое число в строке: цифры с разрядными пробелами (обычный, неразрывный,
# узкий), точками, запятыми и апострофом. Число кончается на первой букве —
# поэтому ни точка «руб.», ни «НДС 20%» в него уже не попадают.
_NUMBER_RE = re.compile(r"\d[\d\s  '.,]*")
_SPACES_RE = re.compile(r"[\s  ']")


def parse_price(text) -> float | None:
    """Цена из строки площадки: «1 234 567,89 руб. (с НДС 20%)» → 1234567.89.

    None — цены нет или она не разобрана («Без указания цены», «НМЦ не
    указано»): ноль значил бы «бесплатно» и увёл бы лот под любой порог.
    Число из API (int/float) возвращается как есть.

    Разделители:
      • есть и точка, и запятая — десятичный тот, что правее
        («1.234.567,89», «1,234,567.89»);
      • несколько одинаковых — это разряды («1.234.567», «1,234,567»);
      • одна запятая или одна точка — десятичный разделитель
        («2467180.00», «1234,5»): так пишут все наши площадки.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _NUMBER_RE.search(str(text))
    if not match:
        return None
    raw = _SPACES_RE.sub("", match.group()).rstrip(".,")
    if not raw:
        return None
    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal = "." if last_dot > last_comma else ","
        thousands = "," if decimal == "." else "."
        raw = raw.replace(thousands, "").replace(decimal, ".")
    elif raw.count(",") > 1 or raw.count(".") > 1:
        raw = raw.replace(",", "").replace(".", "")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


# ── Организатор: сверка по целым словам ─────────────────────────────────────

# Формы собственности — не часть имени: «АО "Ромашка"» и «Ромашка» — одно.
_LEGAL_FORMS = frozenset(
    ("ооо", "оао", "пао", "зао", "ао", "ип", "нао", "тд", "фгуп", "гуп")
)


def org_words(text) -> frozenset[str]:
    """Слова названия организации без кавычек, знаков и формы собственности."""
    return frozenset(company_words(str(text or "")) - _LEGAL_FORMS)


def organizer_match(query, organizer) -> bool:
    """Организатор лота — тот, кого искали? Сверка ПО ЦЕЛЫМ СЛОВАМ.

    Совпадение — когда все слова одного названия есть среди слов другого
    («Ромашка» ↔ «ООО "ТД РОМАШКА"»). Подстрока врала: «ромашк» входит в
    «Ромашкина», и санаторий имени Ромашкина считался лотом заказчика
    (правило проекта — см. src/watch_clients.py, 07.09.2026).
    """
    q, o = org_words(query), org_words(organizer)
    if not q or not o:
        return False
    return q <= o or o <= q


# ── Сторож площадки: антибот и застывшая лента ──────────────────────────────

# Признаки страницы «не пускаем» вместо выдачи. Площадка отдаёт её с кодом
# 200, и без этой проверки она читалась как «закупок нет» (ТЭК-Торг за
# Variti — 18 ночей нуля под видом обрыва связи, 12.10.2026).
_BLOCK_MARKERS = (
    "captcha",
    "капча",
    "не робот",
    "access denied",
    "доступ запрещ",
    "ddos-guard",
    "variti",
    "cf-chl",
    "too many requests",
)


def looks_blocked(text) -> bool:
    """Ответ площадки — страница антибота/капчи/запрета, а не выдача."""
    low = str(text or "").lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


def stale_since(today: date) -> date:
    """Дата «два рабочих дня назад»: публикации старше — лента застыла.

    Рабочие дни, а не календарные: в понедельник свежайший лот пятничный,
    и это норма, а не поломка площадки.
    """
    day, left = today, 2
    while left:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day
