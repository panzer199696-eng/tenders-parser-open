# -*- coding: utf-8 -*-
"""
Секреты не уезжают вместе с кодом.

Программа работает только с открытыми разделами площадок, но `.env` может
содержать токены (Telegram, SMTP, прокси с паролем). Здесь проверяется не
«зашифровано ли», а «не уедет ли»: файл не должен попадать в git, а
значения паролей — в лог.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECRET_PATTERNS = (".env", "*_session.json")


def test_секреты_перечислены_в_gitignore():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in SECRET_PATTERNS:
        assert pattern in text, f"{pattern} не защищён от коммита"


def test_секреты_не_отслеживаются_git():
    """Файл мог попасть в индекс до появления .gitignore."""
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    tracked = result.stdout.split()
    assert ".env" not in tracked, ".env лежит в репозитории"
    assert not [t for t in tracked if t.endswith("_session.json")]


def test_пароль_не_логируется():
    """Пароль не должен утечь в лог-файл, который кладётся в диагностику."""
    sources = list((ROOT / "src").rglob("*.py")) + [ROOT / "main_ui.py"]
    offenders = []
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            low = line.lower()
            if ("logger." in low or "log(" in low) and "password" in low:
                # Упоминание слова допустимо, подстановка значения — нет.
                if "{password" in low or "%s" in low and "password" in low.split("%s")[0]:
                    offenders.append(f"{path.name}: {line.strip()[:80]}")
    assert not offenders, "пароль попадает в лог: " + "; ".join(offenders)
