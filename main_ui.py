"""
TendersParser — тонкий Flask-бэкенд нового интерфейса (ТЗ-3).

Разметка и логика клиента — в src/ui/ (templates/index.html,
static/app.css, static/app.js). С рефакторинга 16.10.2026 здесь только
точка входа: журнал, приложение Flask, обслуживание базы при старте и
режимы командной строки. Маршруты — src/ui/routes/ (по разделам окна),
сбор — src/core/collect.py, пути и список компаний — src/core/context.py.

Run:   python main_ui.py
URL:   http://localhost:5000
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask

from src.core import collect
from src.core import context as ctx

# Сбор живёт в src/core/collect.py; имена оставлены здесь для старых вызовов
# (тесты, внешние скрипты). Состояние (_parse_state и пр.) — только там.
from src.core.collect import (  # noqa: F401
    EXIT_ERROR,
    EXIT_INCOMPLETE,
    EXIT_OK,
    _build_completeness,
    _catch_up_if_needed,
    _do_parse,
    _retry_incomplete_once,
    _run_collect_once,
    _run_parse_async,
    _start_parse,
)
from src.net_ssl import inject_truststore
from src.runtime_paths import bundle_dir, ensure_user_files, user_data_dir
from src.ui.routes import register_routes

# Системная верификация TLS (повторный вызов — no-op). См. src/net_ssl.py.
inject_truststore()

# Папка данных пользователя должна быть готова до первого обращения к файлам.
ensure_user_files()

load_dotenv(ctx.BASE_DIR / ".env")

# ── Логирование (как в старом UI: консоль INFO, файл DEBUG, UI-хвост) ──────────
_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
_root = logging.getLogger()
_root.setLevel(logging.DEBUG)

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
_root.addHandler(_ch)

_file_log_enabled = False
try:
    ctx.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Ротация обязательна: без неё parser.log за месяц вырастал до полутора
    # мегабайт и тянул за собой диагностический архив.
    _fh = RotatingFileHandler(
        ctx.LOG_FILE, maxBytes=3_000_000, backupCount=3, encoding="utf-8"
    )
except OSError as e:
    _ch.handle(
        logging.LogRecord(
            name=__name__,
            level=logging.WARNING,
            pathname=__file__,
            lineno=30,
            msg=f"Файл логов недоступен ({e}); продолжаю без файлового лога",
            args=(),
            exc_info=None,
        )
    )
else:
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(_fmt)
    _root.addHandler(_fh)
    _file_log_enabled = True

logger = logging.getLogger(__name__)

if _file_log_enabled:
    logger.info(f"Лог-файл: {ctx.LOG_FILE}")


_root.addHandler(collect._UIHandler())

app = Flask(
    __name__,
    template_folder=str(bundle_dir() / "src" / "ui" / "templates"),
    static_folder=str(bundle_dir() / "src" / "ui" / "static"),
)
register_routes(app)


def _migrate_legacy_databases() -> None:
    """Перенести данные из прежних баз в единую — один раз при обновлении (T039).

    У пользователя, который обновил программу, лоты и реестр лежат в старых
    файлах. Если их не забрать, он откроет новую версию и увидит пустую ленту —
    для него это будет выглядеть как потеря всей работы.

    Миграция читает старые базы ТОЛЬКО на чтение и не трогает их: если что-то
    пойдёт не так, откат — это удаление новой базы.
    """
    try:
        from src.db import connect, ensure_schema, migrate

        if ctx.UNIFIED_DB_PATH.exists():
            conn = connect(ctx.UNIFIED_DB_PATH)
            try:
                ensure_schema(conn)
                already = conn.execute("SELECT count(*) FROM lots").fetchone()[0]
            finally:
                conn.close()
            if already:
                return  # данные уже на месте

        data_dir = user_data_dir() / "data"
        sources = [
            data_dir / "tenders.db",
            data_dir / "registry.db",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "TendersParser"
            / "data"
            / "tenders.db",
        ]
        sources = [p for p in sources if p.exists()]
        if not sources:
            return

        logger.info(
            "Перенос данных из прежних баз: %s", ", ".join(str(p) for p in sources)
        )
        report = migrate(sources, ctx.UNIFIED_DB_PATH)
        logger.info("Перенос завершён. %s", " · ".join(report.as_text().splitlines()))

        # Лоты из старых баз приехали без оценки профиля: он появился позже.
        # Без пересчёта они все окажутся в корзине «Прочее».
        if report.lots_added:
            from src.db import rescore_lots
            from src.profile import Profile

            conn = connect(ctx.UNIFIED_DB_PATH)
            try:
                scored = rescore_lots(conn, Profile())
            finally:
                conn.close()
            logger.info("Оценка перенесённых лотов: %s", scored)
    except Exception:
        # Перенос — удобство при обновлении: его сбой не должен мешать
        # открыть программу и работать дальше.
        logger.exception("Не удалось перенести данные из прежних баз")


def _dedup_existing_lots() -> None:
    """Схлопнуть дубли между площадками в уже собранной базе (ТЗ-7).

    Дедупликация появилась, когда в базе уже накопились пары
    «площадка ↔ агрегатор»: nightly-сбор будет схлопывать новые публикации,
    а эти жили до правила. Миграция идемпотентна и с чистой базой не
    делает ничего, поэтому безопасна при каждом старте.
    """
    try:
        from src.db import connect, ensure_schema
        from src.db.dedup import merge_duplicates, promote_primary_sources

        conn = connect(ctx.UNIFIED_DB_PATH)
        try:
            ensure_schema(conn)
            report = merge_duplicates(conn, db_path=ctx.UNIFIED_DB_PATH)
            # И сразу выправляем «лицо» лота по приоритету площадок: лот,
            # который публикуется и на площадке, и у агрегатора, должен
            # числиться за первоисточником — там подаётся заявка (04.09.2026).
            report["promoted"] = promote_primary_sources(conn)
        finally:
            conn.close()
        if report.get("merged"):
            logger.info(
                "Схлопнуто дублей: %d (групп %d), бэкап: %s",
                report["merged"],
                report["groups"],
                report.get("backup") or "—",
            )
    except Exception:
        # Схлопывание — не условие запуска: сбой миграции не должен
        # мешать открыть программу и работать с лентой.
        logger.exception("Не удалось схлопнуть дубли в существующей базе")


if __name__ == "__main__":
    if "--collect" in sys.argv:
        if "--retry-incomplete" in sys.argv:
            raise SystemExit(collect._retry_incomplete_once())
        raise SystemExit(collect._run_collect_once())

    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print(f"  TendersParser UI → http://localhost:{port}")
    print("=" * 60)
    # Только петля: раньше сервер слушал 0.0.0.0 и вместе с ним по локальной
    # сети открывались настройки, логи и учётные данные пользователя.
    _migrate_legacy_databases()
    _dedup_existing_lots()
    collect._catch_up_if_needed()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
