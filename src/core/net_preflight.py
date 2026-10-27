"""
Проверка связи с площадками перед сбором.

Зачем: за 21 ночь Bidzaar, Tender.pro и Росэлторг падали «обрывом связи»
10 раз, причём все разом, а ТЭК-Торг — почти каждую ночь. Итог прогона при
этом выглядел обычным «ок», и из журнала нельзя было понять, площадка ли
сломалась или связь. Частая причина — зарубежный адрес машины (VPN, прокси):
российские площадки режут иностранные адреса.

Поэтому до сбора — по одному запросу на каждую включённую площадку.
Не ответила — ждём и повторяем: ночью сеть после простоя поднимается не
сразу. Так и не ответила — в журнал идёт строка, которая называет площадку
и прямо говорит, где смотреть.

«Доступна» здесь значит «ответила хоть чем-то»: 503 или 403 — это уже
разговор с сервером, а не обрыв. Антибот ТЭК-Торга ловит своя проба
(``tektorg_transport.probe_direct``).
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Ключ настройки → (имя площадки как в отчётах полноты, адрес для пробы).
from ..platforms import PLATFORMS as _PLATFORMS

PLATFORM_PROBES: dict[str, tuple[str, str]] = {
    p.key: (p.name, p.probe_url) for p in _PLATFORMS
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def probe(url: str, timeout: float = 15, opener: Callable | None = None) -> bool:
    """Ответила ли площадка хоть чем-то (любой HTTP-статус = на связи)."""
    open_ = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with open_(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def check_platforms(
    settings: dict,
    *,
    probe_fn: Callable[[str], bool] | None = None,
    attempts: int = 3,
    pauses: tuple[float, ...] = (30, 90),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, bool]:
    """Доступность включённых площадок: {имя площадки: ответила ли}.

    Повторяются только неответившие; между попытками — паузы из ``pauses``
    (последняя повторяется, если попыток больше). Худший случай по
    умолчанию — около двух минут, это дёшево на фоне получасового прогона.
    """
    probe_fn = probe_fn or probe
    pending = {
        name: url for key, (name, url) in PLATFORM_PROBES.items() if settings.get(key)
    }
    result: dict[str, bool] = {name: False for name in pending}
    for attempt in range(max(1, attempts)):
        if attempt:
            pause = pauses[min(attempt - 1, len(pauses) - 1)] if pauses else 0
            logger.info(
                "Связь с площадками: не ответили %s — повтор через %s с",
                ", ".join(pending),
                pause,
            )
            sleep(pause)
        for name, url in list(pending.items()):
            if probe_fn(url):
                result[name] = True
                del pending[name]
        if not pending:
            break
    return result


def describe(result: dict[str, bool]) -> str:
    """Строка для журнала о неответивших площадках (пусто — все на связи)."""
    down = [name for name, ok in result.items() if not ok]
    if not down:
        return ""
    return (
        f"Перед сбором не ответили: {', '.join(down)}. Если площадки молчат "
        "несколько ночей подряд — проверьте подключение, прокси или VPN: "
        "российские площадки часто не пускают зарубежные адреса."
    )
