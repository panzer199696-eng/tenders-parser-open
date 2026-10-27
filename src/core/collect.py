"""
Сбор лотов: запуск, прогон, журнал, книга Excel, коды выхода.

Вынесено из main_ui.py (рефакторинг 16.10.2026) чистым переносом: окно
(маршруты /api/parse/*, /api/status) и командная строка (--collect,
--retry-incomplete) запускают один и тот же код. Состояние прогона —
_parse_state под _parse_lock; пути и список компаний — src/core/context.py.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import collect_lock, run_journal
from ..runtime_paths import results_dir
from ..version import APP_VERSION
from . import context as ctx

logger = logging.getLogger(__name__)

# ── Состояние парсинга/входа ──────────────────────────────────────────────────
_parse_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "progress": [],
    "tenders": [],
    "error": None,
    "total_companies": 0,
    "current_company": "",
    "saved": None,
    "data_saved": False,
    # Прогресс по «юнитам» (фирма/фраза/компания на площадке) — см. _UIHandler.
    "units_done": 0,
    "total_units": 0,
    # Остановка по кнопке: stopping — запрошена, stopped — фактически прервано.
    "stopping": False,
    "stopped": False,
    # Путь к сохранённому файлу результатов последнего прогона.
    "excel_path": None,
    # Сколько лотов уже принесли завершённые юниты (до дедупа и фильтров).
    "found_so_far": 0,
    # Полнота сбора: сводка «сколько объектов обойдено целиком» + список неполных.
    # None до первого прогона. См. src/parsers/completeness.py.
    "completeness": None,
}
_parse_lock = threading.Lock()

# Ссылки на event loop и задачу текущего парсинга — для /api/parse/stop
# (task.cancel() надо звать в потоке парсинга через call_soon_threadsafe).
_parse_cancel = {"loop": None, "task": None}

# Запас сверх срока прогона (run_timeout_min) на то, что идёт после обхода
# площадок: фильтры, запись в базу, книга Excel (Д2).
RUN_TIMEOUT_GRACE_S = 15 * 60

# Межпроцессный замок сбора (А4): держится от занятия запуска до конца
# прогона. Флаг running выше видит только этот процесс.
_collect_lock = {"held": None}

def _on_progress(event: str, platform: str | None, **info) -> None:
    """События раннера → прогресс в окне: «N из M площадок», что идёт сейчас,
    сколько лотов уже принесли законченные площадки.

    Раньше прогресс считался регулярками по строкам логов парсеров и не знал
    ТЭК-Торг, Росэлторг и Bidzaar — по ним полоса стояла (16.10.2026).
    """
    with _parse_lock:
        running = _parse_state.setdefault("platforms_running", [])
        done = _parse_state.setdefault("platforms_done", {})
        if event == "plan":
            # Повторный заход по упавшим площадкам — тоже «план», но общее
            # число площадок прогона он не меняет.
            if not _parse_state.get("plan_received"):
                _parse_state["plan_received"] = True
                _parse_state["total_units"] = len(info.get("platforms") or [])
            return
        if event == "start":
            done.pop(platform, None)
            if platform not in running:
                running.append(platform)
        elif event == "done":
            if platform in running:
                running.remove(platform)
            lots = int(info.get("lots") or 0)
            done[platform] = lots
            status = "" if info.get("ok", True) else " — с ошибкой"
            _parse_state["progress"].append(
                f"{platform}: {lots} лотов, объектов {info.get('units', 0)}{status}"
            )
        _parse_state["units_done"] = len(done)
        _parse_state["found_so_far"] = sum(done.values())
        _parse_state["current_company"] = (
            "Идут: " + ", ".join(running) if running else ""
        )


class _UIHandler(logging.Handler):
    """Перехватывает INFO-логи парсеров (src.*) → лента прогресса в UI."""

    def emit(self, record):
        try:
            # Свои строки сбор кладёт в ленту сам (log() в _do_parse), а логи
            # маршрутов окна в ленту сбора не шли и раньше (жили в main_ui) —
            # после переноса в src/ без этой проверки они попали бы туда.
            if not record.name.startswith("src.") or record.name == __name__:
                return
            if record.name.startswith("src.ui."):
                return
            if record.levelno < logging.INFO:
                return
            msg = record.getMessage()
            with _parse_lock:
                if not _parse_state.get("running"):
                    return
                _parse_state["progress"].append(msg)
        except Exception:
            pass


def _save_excel_file(run_id: int | None = None) -> Path:
    """Сохранить Excel в «Документы\\TendersParser» и вернуть путь.

    Скачивание через браузерную ссылку в окне приложения (WebView2) молча не
    срабатывало — пользователь жал «Скачать» по нескольку раз и не находил
    файл. Поэтому файл всегда пишем на диск сами, а интерфейс показывает путь
    и кнопку «Открыть папку».

    ТЗ-5: книга собирается из единой базы, а не из лотов текущей сессии —
    отчёт возможен всегда, когда база есть, даже если парсинг ещё не
    запускался.

    `run_id` — прогон, для которого строится книга (шапка и блок полноты);
    None — последний в базе (кнопка «Сохранить»).
    """
    from src.core.excel_report import build_report
    from src.db import connect as _db_connect
    from src.db import ensure_schema as _db_schema

    conn = _db_connect(ctx.UNIFIED_DB_PATH)
    try:
        # База могла ещё не существовать (первый запуск) — схема создаётся
        # здесь же: пустая книга лучше ошибки «сначала запустите парсинг».
        _db_schema(conn)
        data = build_report(conn, run_id=run_id)
    finally:
        conn.close()
    target = results_dir() / f"Тендеры_{datetime.now():%Y-%m-%d_%H-%M}.xlsx"
    target.write_bytes(data)
    return target


# ── Фоновый парсинг ───────────────────────────────────────────────────────────


def _start_parse(
    companies, *, background=True, catch_up_for=None, only_platforms=None, trigger=None
) -> bool:
    """Занять запуск до создания потока — одинаково для кнопки и расписания.

    Сбор в другом процессе (ночной --collect, окно программы) — исключение
    collect_lock.CollectBusy: вызывающий объясняет человеку, кто занят.
    """
    with _parse_lock:
        if _parse_state["running"]:
            return False
        _collect_lock["held"] = collect_lock.acquire()
        _parse_state.update(
            running=True,
            started_at=datetime.now().strftime("%H:%M:%S"),
            finished_at=None,
            progress=[],
            tenders=[],
            error=None,
            total_companies=0,
            current_company="",
            saved=None,
            data_saved=False,
            units_done=0,
            total_units=0,
            stopping=False,
            stopped=False,
            excel_path=None,
            found_so_far=0,
            completeness=None,
            # Прогресс по площадкам (_on_progress).
            platforms_running=[],
            platforms_done={},
            plan_received=False,
            catch_up_for=catch_up_for,
            # Догоняющий прогон обходит только упавшие ночью площадки.
            only_platforms=list(only_platforms) if only_platforms else None,
            trigger=trigger,
            network_note="",
            # Итог полноты из журнала прогона — по нему --collect выбирает
            # код выхода (Д2); None — журнал ещё не записан.
            data_complete=None,
            warning=None,
        )
    try:
        if background:
            threading.Thread(
                target=_run_parse_async, args=(companies,), daemon=True
            ).start()
        else:
            _run_parse_async(companies)
    except Exception:
        logger.exception("Не удалось запустить сбор")
        _release_collect_lock()
        with _parse_lock:
            _parse_state.update(
                running=False,
                stopping=False,
                error="Не удалось начать сбор. Попробуйте запустить его ещё раз.",
                finished_at=datetime.now().strftime("%H:%M:%S"),
            )
        raise
    return True


def _release_collect_lock() -> None:
    held, _collect_lock["held"] = _collect_lock["held"], None
    if held is not None:
        held.release()


def _run_parse_async(companies):
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(_do_parse(companies))
        with _parse_lock:
            _parse_cancel["loop"] = loop
            _parse_cancel["task"] = task
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        with _parse_lock:
            _parse_state.update(stopped=True, error=None)
    except Exception as e:
        with _parse_lock:
            _parse_state["error"] = str(e)
    finally:
        try:
            if loop is not None:
                loop.close()
            asyncio.set_event_loop(None)
            with _parse_lock:
                _parse_state["finished_at"] = datetime.now().strftime("%H:%M:%S")
        finally:
            # Не пускаем следующий сбор, пока старый браузер ещё
            # заканчивает работу с общим состоянием.
            with _parse_lock:
                _parse_cancel.update(loop=None, task=None)
                _release_collect_lock()
                _parse_state.update(
                    running=False,
                    stopping=False,
                    finished_at=datetime.now().strftime("%H:%M:%S"),
                )


def _runner_dict(runner, name: str) -> dict | None:
    """Словарь раннера по площадкам или None, если раннер до него не дошёл."""
    value = getattr(runner, name, None)
    return value if isinstance(value, dict) else None


def _проверить_по_прошлым_ночам(runner, started_at, platform_new) -> list[str]:
    """Сравнить площадки с прошлыми ночами (src/core/run_health.py).

    Вердикты дописываются в runner.unit_reports; возвращаются строки для
    итога. Догон не проверяем: он обходит две-три площадки, и сравнивать
    его с полной ночью бессмысленно. Сбой проверки прогон не роняет.
    """
    from src.core.catchup import TRIGGER_CATCHUP

    with _parse_lock:
        trigger = _parse_state.get("trigger")
    lots = _runner_dict(runner, "platform_lots")
    if trigger == TRIGGER_CATCHUP or not lots or platform_new is None:
        return []
    try:
        from src.core.run_health import check_run

        from src.db import connect as _db_connect

        conn = _db_connect(ctx.UNIFIED_DB_PATH)
        try:
            return check_run(
                conn,
                started_at,
                runner.unit_reports,
                platform_lots=lots,
                platform_new=platform_new,
                platform_newest=_runner_dict(runner, "platform_newest"),
            )
        finally:
            conn.close()
    except Exception:
        logger.exception("Проверка площадок по прошлым ночам не удалась")
        return []


def _записать_журнал(
    started_at,
    outcome: str,
    settings: dict,
    runner,
    classified,
    saved,
    log,
    error: str = "",
    lots_merged: int | None = None,
    platform_new: dict | None = None,
    health_notes: list[str] | None = None,
) -> int | None:
    """Сохранить итоги прогона — успешного, прерванного или упавшего.

    Возвращает id прогона в единой базе (None — записать не удалось): по
    нему строится книга Excel этого прогона.

    Неполнота обязана пережить прогон: соседние программы показывают цифры
    по базе и без журнала выдают неполный сбор за полный. Поэтому пишем и
    после ошибки, и после остановки — там данные заведомо неполные, и
    молчание об этом хуже любой ошибки записи.

    `saved` — новых лотов прогона (по единой базе, иначе по старой);
    `platform_new` — то же по площадкам (src.db.runs.new_lots_by_platform).
    """
    with _parse_lock:
        trigger = _parse_state.get("trigger")
        network_note = _parse_state.get("network_note") or ""
    record = run_journal.build_record(
        started_at=started_at,
        finished_at=datetime.now(),
        app_version=APP_VERSION,
        outcome=outcome,
        settings=settings,
        unit_reports=getattr(runner, "unit_reports", None),
        lots_found=len(classified) if classified is not None else None,
        lots_new=saved,
        lots_merged=lots_merged,
        error=error,
        trigger=trigger,
        network_note=network_note,
        platform_lots=_runner_dict(runner, "platform_lots"),
        platform_new=platform_new,
        platform_newest=_runner_dict(runner, "platform_newest"),
        health_notes=health_notes,
    )
    with _parse_lock:
        _parse_state["data_complete"] = bool(record.get("data_complete"))
    if run_journal.write_run_safe(record) is None:
        log("⚠ Журнал прогона записать не удалось — полнота сбора не сохранена")

    # ТЗ-5: тот же прогон — и в базу: листу отчёта «когда собрано» нужен
    # источник, который не зависит от того, уцелел ли файл журнала.
    # Сбой записи в базу не должен ронять завершение прогона — сбор важнее.
    run_id = None
    try:
        from src.db import connect as _db_connect
        from src.db import ensure_schema as _db_schema
        from src.db.runs import record_run as _record_run

        unified = _db_connect(ctx.UNIFIED_DB_PATH)
        try:
            _db_schema(unified)
            run_id = _record_run(unified, record)
        finally:
            unified.close()
    except Exception:
        logger.exception("Не удалось записать прогон в таблицы runs/run_platforms")
    return run_id


def _build_completeness(unit_reports, log) -> dict:
    """Сводка полноты сбора + сообщения пользователю о том, чего не хватает.

    Возвращает словарь для `/api/status`: сколько объектов собрано целиком,
    сколько неполны, перечень неполных с причинами человеческим языком.
    С ТЗ-8 отчёты приходят со всех площадок, поэтому у каждого объекта
    есть имя площадки, а сводка получает разбивку `platforms` — лента и
    лист Excel «Полнота сбора» показывают строку с площадкой, а не общую
    безликую цифру.
    """
    from src.parsers.completeness import (
        reason_text,
        summarize_by_platform,
        summarize_units,
    )

    if not unit_reports:
        return {
            "checked": False,
            "data_complete": True,
            "units": [],
            "platforms": [],
        }

    s = summarize_units(unit_reports)
    platforms = summarize_by_platform(unit_reports)
    units = [
        {
            "platform": u.get("platform") or "",
            # Площадка — в самой строке: лента и Excel обязаны называть
            # виновника неполноты по имени, а не сваливать шесть площадок
            # в одну кучу (ТЗ-8).
            "label": (
                f"{u.get('platform')}: {u['label']}"
                if u.get("platform")
                else u["label"]
            ),
            "kind": u["kind"],
            "status": u["status"],
            "collected": u["collected_lots"],
            "expected_lots": u["expected_lots"],
            "collected_pages": u["collected_pages"],
            "expected_pages": u["expected_pages"],
            "reason": reason_text(u["reason"]),
        }
        for u in s["incomplete_units"]
    ]

    if s["data_complete"]:
        if s["unknown"]:
            # «Целиком» и «объём неизвестен» — раздельно (В1, 16.10.2026).
            log(
                f"✅ Полнота: целиком {s['complete']} объектов, "
                f"объём неизвестен у {s['unknown']} (площадка не сообщает)"
            )
        else:
            log(f"✅ Полнота: все {s['complete']} объектов обойдены целиком")
    else:
        # Сначала — по площадкам: пользователь должен увидеть, КТО недобрал,
        # прежде чем разбираться с отдельными объектами.
        for row in platforms:
            if row["data_complete"]:
                continue
            log(
                f"⚠ {row['platform']}: неполных {row['partial']}, "
                f"не обойдено {row['skipped']} из {row['units']} объектов — "
                f"{row['reason']}"
            )
        log(
            f"⚠ Данные собраны НЕ полностью: {s['partial']} объектов обойдены "
            f"частично, {s['skipped']} не обойдены вовсе"
        )
        for u in units[:10]:
            if u["expected_pages"] is not None and u["status"] == "partial":
                detail = f"страниц {u['collected_pages']} из {u['expected_pages']}"
            elif u["expected_lots"] is not None:
                detail = f"собрано {u['collected']} из {u['expected_lots']}"
            else:
                detail = "не обойдено"
            log(f"   • {u['label']}: {detail} — {u['reason']}")
        if len(units) > 10:
            log(f"   …и ещё {len(units) - 10}")

    return {
        "checked": True,
        "data_complete": s["data_complete"],
        "complete": s["complete"],
        "partial": s["partial"],
        "skipped": s["skipped"],
        "unknown": s["unknown"],
        "units": units,
        "platforms": platforms,
    }


async def _do_parse(companies):
    """Использует ParserRunner (чистые фильтры ТЗ-2) + автосохранение в БД."""
    from src.core.parser_runner import ParserRunner
    from src.settings_store import load_settings
    from src.utils.browser_manager import BrowserManager

    def log(msg):
        with _parse_lock:
            _parse_state["progress"].append(msg)
        logger.info(msg)

    # Подготовка тоже может завершиться ошибкой/отменой.
    started_at = datetime.now()
    run_clock = time.monotonic()
    settings = {}
    browser = None
    runner = None
    classified = None
    saved = None
    merged = None
    platform_new = None
    try:
        settings = load_settings()
        headless = bool(settings.get("browser_headless", True))

        # Догоняющий прогон (07:00): включены только площадки, упавшие ночью
        # по связи. Настройки меняются лишь в памяти этого прогона.
        with _parse_lock:
            only_platforms = _parse_state.get("only_platforms")
        if only_platforms:
            from src.core.net_preflight import PLATFORM_PROBES

            for key in PLATFORM_PROBES:
                settings[key] = key in only_platforms
            log(
                "Догоняющий прогон: "
                + ", ".join(PLATFORM_PROBES[k][0] for k in only_platforms)
            )

        # Связь с площадками до сбора (12.10.2026): не ответила — ждём и
        # повторяем; так и молчит — строка с подсказкой про сеть в итог.
        try:
            from src.core import net_preflight

            reach = await asyncio.to_thread(net_preflight.check_platforms, settings)
            note = net_preflight.describe(reach)
            with _parse_lock:
                _parse_state["network_note"] = note
            if note:
                log(f"⚠ {note}")
            else:
                log(f"Связь с площадками: на связи все ({len(reach)})")
        except Exception:
            logger.exception("Проверка связи с площадками не удалась")

        # Прогресс — по площадкам (события раннера, _on_progress): общее
        # число — включённые площадки реестра, раннер уточнит его планом.
        from src.platforms import PLATFORMS

        total_units = sum(1 for p in PLATFORMS if settings.get(p.key))

        with _parse_lock:
            _parse_state["total_companies"] = len(companies)
            _parse_state["current_company"] = ""
            _parse_state["total_units"] = total_units
            _parse_state["units_done"] = 0

        log(
            f"Запуск: {len(companies)} компаний, days_back={settings.get('days_back')}, "
            f"min_price={settings.get('min_price')}, headless={headless}"
        )

        browser = BrowserManager(headless=headless)
        await browser.start()

        runner = ParserRunner(browser, settings=settings, companies=companies)
        runner.progress = _on_progress
        # Срок всего прогона (Д2): раннер обрезает по нему обход площадок,
        # а здесь — страховка на случай, если завис не обход, а то, что после.
        run_limit_min = float(settings.get("run_timeout_min", 180))
        runner.deadline = run_deadline = run_clock + run_limit_min * 60
        try:
            classified = await asyncio.wait_for(
                runner.run_all(),
                max(0.0, run_deadline - time.monotonic()) + RUN_TIMEOUT_GRACE_S,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Сбор не уложился в {round(run_limit_min)} мин и прерван — "
                "похоже, площадка зависла; догон в 07:00 повторит площадки"
            ) from None

        # Полнота сбора: пользователь должен узнать о неполноте здесь, а не
        # обнаружить нехватку лотов через неделю.
        completeness = _build_completeness(runner.unit_reports, log)
        # Результат остаётся доступным и после ошибки последующей записи.
        with _parse_lock:
            _parse_state["tenders"] = classified
            _parse_state["completeness"] = completeness

        # Единая база (фича 002): лот сохраняется сразу с оценкой профиля —
        # «Насосы +30 · закреплённый клиент +40 · совпало: „насос“».
        # Раньше оценка считалась при показе и в отчёты не попадала.
        # Лента и Excel уже читают единую базу: её сбой нельзя скрывать
        # успешной записью в прежнюю базу или отчётом по вчерашним данным.
        try:
            from src.db import connect as _db_connect
            from src.db import ensure_schema as _db_schema
            from src.db import save_lots as _db_save_lots
            from src.db.runs import new_lots_by_platform as _new_by_platform
            from src.profile import Profile as _Profile

            unified = _db_connect(ctx.UNIFIED_DB_PATH)
            try:
                _db_schema(unified)
                summary = _db_save_lots(unified, classified or [], _Profile())
                platform_new = _new_by_platform(unified, started_at)
            finally:
                unified.close()
            # Схлопнувшиеся дубли — часть итогов прогона (ТЗ-7): цифра едет
            # в журнал, чтобы рост дедупа был виден по сводке, а не по жалобе.
            merged = summary.get("merged") or 0
            # «Новых» в журнале — по единой базе: старая может и не записаться.
            saved = summary.get("added")
            with _parse_lock:
                _parse_state["data_saved"] = True
                _parse_state["saved"] = saved
            log(
                "Единая база: новых {added}, обновлено {updated}, "
                "оценено профилем {scored}".format(**summary)
            )
            # Срок, который программа не поняла, делает лот вечным: он не
            # уйдёт в «Срок истёк» и будет висеть среди рабочих (16.10.2026).
            for platform_name, info in (
                summary.get("bad_deadline_by_platform") or {}
            ).items():
                log(
                    f"⚠ {platform_name}: у {info['count']} лотов не разобран срок "
                    f"(пример: «{info['example']}») — они не истекут сами, "
                    "нужна правка парсера"
                )
        except Exception as e:
            logger.exception("Не удалось записать в единую базу")
            raise RuntimeError(
                "Лоты собраны, но сохранить их в основной список не удалось. "
                "Отчёт не создан. Проверьте свободное место и повторите сбор."
            ) from e

        # Межпрогонная проверка (В3): площадка, давшая сегодня в разы меньше
        # обычного или с застывшей лентой, — неполная, ДО записи журнала.
        health_notes = _проверить_по_прошлым_ночам(runner, started_at, platform_new)
        if health_notes:
            for note in health_notes:
                log(f"⚠ Проверка по прошлым ночам: {note}")
            completeness = _build_completeness(runner.unit_reports, lambda _msg: None)

        with _parse_lock:
            _parse_state["tenders"] = classified
            _parse_state["saved"] = saved
            _parse_state["error"] = None
            _parse_state["completeness"] = completeness

        # Сначала — запись прогона, потом книга (В2): книга строилась до
        # журнала, и шапка «Когда собрано» с блоком полноты показывали
        # ПРЕДЫДУЩИЙ прогон (ночной файл 16.10: «15.10 в 07:06»).
        run_id = _записать_журнал(
            started_at,
            run_journal.OUTCOME_OK,
            settings,
            runner,
            classified,
            saved,
            log,
            lots_merged=merged,
            platform_new=platform_new,
            health_notes=health_notes,
        )

        # Excel пишем сразу: пользователь не должен ничего «скачивать»,
        # файл уже лежит в «Документы\TendersParser». Книга собирается из
        # единой базы (ТЗ-5), поэтому пишем и тогда, когда прогон нашёл
        # мало: в базе остаются лоты прошлых дней. Сбой книги — не сбой
        # прогона: лоты собраны и сохранены, книгу можно пересобрать кнопкой.
        excel_path = None
        warning = None
        try:
            excel_path = str(_save_excel_file(run_id))
            log(f"📗 Файл сохранён: {excel_path}")
        except Exception as e:
            logger.exception("Не удалось сохранить Excel")
            warning = (
                f"файл Excel создать не удалось ({e}) — закройте открытый отчёт, "
                "проверьте свободное место и нажмите «Сохранить ещё раз»"
            )
        with _parse_lock:
            _parse_state["excel_path"] = excel_path
            _parse_state["warning"] = warning

        tail = f" ⚠ Лоты сохранены, но {warning}" if warning else ""
        log(f"✅ Готово! Найдено: {len(classified)}, новых: {saved}.{tail}")
    except asyncio.CancelledError:
        log("⏹ Парсинг остановлен пользователем")
        _записать_журнал(
            started_at,
            run_journal.OUTCOME_STOPPED,
            settings,
            runner,
            classified,
            saved,
            log,
            lots_merged=merged,
            platform_new=platform_new,
        )
        with _parse_lock:
            _parse_state["stopped"] = True
            _parse_state["error"] = None
        raise
    except Exception as e:
        log(f"❌ Ошибка: {e}")
        _записать_журнал(
            started_at,
            run_journal.OUTCOME_ERROR,
            settings,
            runner,
            classified,
            saved,
            log,
            error=str(e),
            lots_merged=merged,
            platform_new=platform_new,
        )
        with _parse_lock:
            _parse_state["error"] = str(e)
        raise
    finally:
        if browser is not None:
            await browser.close()


def _preflight_problems() -> list:
    """Причины, по которым запускать парсинг бессмысленно (человеческим языком).

    Пустой список — можно стартовать. Проверяем то, из-за чего пользователь
    раньше получал молчаливый ноль: не выбрана ни одна площадка, нечего искать.
    """
    from src.keywords_store import enabled_phrases
    from src.platforms import KEYS
    from src.settings_store import load_settings

    settings = load_settings()
    problems = []

    if not any(settings.get(p) for p in KEYS):
        problems.append(
            "Не выбрана ни одна площадка — включите хотя бы одну в «Настройках»"
        )

    companies = ctx.load_companies()
    phrases = enabled_phrases()
    if not companies and not phrases:
        problems.append(
            "Нечего искать: добавьте компании на вкладке «Компании» "
            "или включите фразы на вкладке «Ключевые слова»"
        )
    return problems


def _run_collect_once() -> int:
    """Один прогон сбора без интерфейса — так его запускает планировщик.

    Планировщик Windows не умеет «открыть программу и нажать кнопку»: ему
    нужна команда, которая сама всё сделает и завершится. Отсюда отдельный
    режим `--collect`.
    """
    print(f"TendersParser: сбор по расписанию, {datetime.now():%d.%m.%Y %H:%M}")
    return _collect_exit_code("Сбор")


# Коды выхода `--collect` (Д2): планировщик и VBS видят исход прогона, а не
# «процесс запущен». 2 — прогон дошёл до конца, но данные неполные.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 2


def _collect_exit_code(label: str, **start_kwargs) -> int:
    """Прогнать сбор в этом потоке и вернуть код выхода для планировщика."""
    try:
        started = _start_parse(ctx.load_companies(), background=False, **start_kwargs)
    except collect_lock.CollectBusy as e:
        # pythonw из планировщика печатает в никуда — причину пишем и в лог.
        print(str(e))
        logger.warning("%s не запущен: %s", label, e)
        return EXIT_ERROR
    if not started:
        print("Сбор уже выполняется")
        logger.warning("%s не запущен: сбор уже выполняется", label)
        return EXIT_ERROR
    state = dict(_parse_state)
    if state.get("error"):
        print(f"{label} завершился ошибкой: {state['error']}")
        return EXIT_ERROR
    if state.get("stopped"):
        print(f"{label} остановлен пользователем")
        return EXIT_ERROR
    print(f"Собрано лотов: {len(state.get('tenders') or [])}")
    if not state.get("data_complete", True):
        print("Данные собраны не полностью — см. журнал прогона")
        return EXIT_INCOMPLETE
    return EXIT_OK


def _retry_incomplete_once() -> int:
    """Догоняющий прогон после неполной ночи — запускает планировщик в 07:00.

    Читает журнал последнего прогона и повторно обходит площадки, которым
    повтор может помочь (src/core/catchup.py). Полная ночь — выходим сразу.
    Площадки, которым нужна правка программы или проверка, не догоняются —
    о них строка в лог и код 2.
    """
    from src.platforms import name_of

    from src.core.catchup import (
        TRIGGER_CATCHUP,
        platforms_need_human,
        platforms_to_retry,
    )

    record = run_journal.read_last_run()
    keys = platforms_to_retry(record)
    human = (
        platforms_need_human(record)
        if record and not record.get("data_complete")
        else []
    )
    if human:
        # pythonw из планировщика печатает в никуда — итог пишем и в лог.
        msg = "Догон не поможет — нужна правка программы или проверка площадки: " + (
            ", ".join(f"{name} ({text})" for name, text in human)
        )
        print(msg)
        logger.warning(msg)
    if not keys:
        msg = "Догоняющий прогон: ночной сбор полный или свежего нет — догонять нечего"
        print(msg)
        logger.info(msg)
        return EXIT_INCOMPLETE if human else EXIT_OK
    names = ", ".join(name_of(k) for k in keys)
    print(f"TendersParser: догоняющий прогон, {datetime.now():%d.%m.%Y %H:%M}: {names}")
    logger.info("Догоняющий прогон: %s", names)
    return _collect_exit_code(
        "Догоняющий прогон", only_platforms=keys, trigger=TRIGGER_CATCHUP
    )


def _catch_up_if_needed() -> None:
    """Догнать пропущенный слот при запуске программы (FR-006).

    Планировщик пропущенное время не повторяет: если ПК был выключен ночью,
    день выпал бы молча. Поэтому при старте сверяем последний прогон с
    последним запланированным слотом.
    """
    try:
        from src.run_journal import read_last_run
        from src.schedule import missed_run, normalize
        from src.settings_store import load_settings

        schedule = normalize(load_settings().get("schedule"))
        if not schedule["enabled"]:
            return

        last = read_last_run()
        last_at = None
        if last:
            stamp = last.get("finished_at") or last.get("started_at")
            if stamp:
                try:
                    last_at = datetime.fromisoformat(stamp)
                except ValueError:
                    last_at = None

        slot = missed_run(schedule, last_at)
        if slot is None:
            return

        logger.info(
            "Пропущен сбор за %s — запускаю догоняющий прогон",
            slot.strftime("%d.%m %H:%M"),
        )
        _start_parse(ctx.load_companies(), catch_up_for=slot.isoformat(timespec="minutes"))
    except collect_lock.CollectBusy as e:
        logger.info("Пропущенный сбор не запущен: %s", e)
    except Exception:
        # Догоняющий прогон — удобство, а не обязанность: его сбой не должен
        # мешать открыть программу.
        logger.exception("Не удалось проверить пропущенный прогон")
