"""
Общие проверки сторожа площадок (`src/parsers/_common.py`, 16.10.2026):
страница антибота вместо выдачи и «лента не обновляется».
"""

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers._common import looks_blocked, stale_since


def test_капча_и_антибот_узнаются():
    assert looks_blocked("<html><body>Подтвердите, что вы не робот</body></html>")
    assert looks_blocked('<div class="smart-captcha">…</div>')
    assert looks_blocked("<title>Access denied</title>")
    assert looks_blocked("<script src='https://ddos-guard.net/x.js'></script>")
    assert looks_blocked("Доступ запрещён")


def test_обычная_выдача_не_антибот():
    assert not looks_blocked("")
    assert not looks_blocked('{"items": [], "totalCount": 0}')
    assert not looks_blocked('<div class="search-results__item">Насосы</div>')


def test_два_рабочих_дня_назад():
    # пятница 16.10 → среда 14.10
    assert stale_since(date(2026, 10, 16)) == date(2026, 10, 14)
    # понедельник 19.10 → четверг 15.10 (выходные не считаются)
    assert stale_since(date(2026, 10, 19)) == date(2026, 10, 15)
    # воскресенье 18.10 → четверг 15.10
    assert stale_since(date(2026, 10, 18)) == date(2026, 10, 15)
