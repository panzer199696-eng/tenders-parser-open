"""
Полнота сбора: отчёт «ожидалось / собрано» по каждому объекту обхода.

Механизм родился внутри одного парсера и вынесен в общий модуль по ТЗ-8:
неполный сбор, выглядящий полным, дороже честного «собрано не всё, вот почему»,
а молчаливая неполнота была у всех площадок — ТЭК-Торг обрезает выдачу на
сороковой странице, Bidzaar упирается в потолок страниц, и всё это раньше не
выходило наружу. На эти имена завязаны `parser_runner`, `collect` и тесты.

Ключевые решения (менять нельзя — механизм выверен на живых данных):

* Полнота считается ПО СТРАНИЦАМ, а не по лотам: у объекта обхода с шестью
  страницами выдачи последняя почти всегда неполная (51 лот вместо 60),
  и сравнение по лотам объявляло бы неполным каждый успешный обход.
* `unknown` — не ошибка: если площадка не сообщает, сколько ожидать, честное
  «не знаю» лучше выдуманного «всё собрано».
* Упор в потолок страниц (`page_cap`) обязан быть виден наружу: раньше парсеры
  писали об этом только в лог, и обрезанный сбор выглядел полным.
"""

from __future__ import annotations

UNIT_COMPLETE = "complete"  # обошли всё обещанное
UNIT_PARTIAL = "partial"  # обошли меньше, чем обещано
UNIT_SKIPPED = "skipped"  # до объекта не дошли
UNIT_UNKNOWN = "unknown"  # площадка не сказала, сколько ожидать

REASON_RATE_LIMITED = "rate_limited"
REASON_NOT_LOGGED_IN = "not_logged_in"
REASON_LIMITED_ACCOUNT = "limited_account"
REASON_NETWORK = "network"
REASON_PAGE_CAP = "page_cap"
REASON_STOPPED = "stopped"
REASON_FORMAT_CHANGED = "format_changed"
# Причины уровня площадки (общий сторож раннера, 16.10.2026): парсер мог
# упасть или вернуть ноль, ни разу не сказав «неполно».
REASON_CRASHED = "crashed"
REASON_NO_REPORT = "no_report"
REASON_EMPTY_PLATFORM = "empty_platform"
# Площадка отвечает, но не пускает: антибот, 403, страница проверки.
REASON_BLOCKED = "blocked"
# Межпрогонные: лента площадки не обновляется / лотов резко меньше обычного.
REASON_STALE = "stale"
REASON_ANOMALY = "anomaly"
# Площадка не уложилась в отведённое время (Д2, 16.10.2026): обход прерван,
# собранное другими площадками сохраняется.
REASON_TIMEOUT = "timeout"
# Ночной лимит запросов к площадке (ТЭК-Торг с 16.10.2026: каждая процедура —
# отдельная карточка). Не поломка: недобранное догоняется следующим прогоном.
REASON_REQUEST_CAP = "request_cap"
REASON_UNKNOWN = "unknown"

# Причины, при которых повтор тем же кодом бесполезен: нужна правка
# программы или вмешательство человека, а не догоняющий сбор.
REASONS_NEED_HUMAN = frozenset({REASON_FORMAT_CHANGED, REASON_BLOCKED, REASON_STALE})

# Объект отчёта «площадка целиком» — для причин, которые относятся не к
# одной фразе или заказчику, а ко всей площадке.
KIND_PLATFORM = "platform"
LABEL_PLATFORM = "площадка целиком"

# Тексты для пользователя. Технических терминов здесь быть не должно — человек,
# который это читает, не знает слов «капча», «сессия» и «page_count».
_REASON_TEXTS = {
    REASON_RATE_LIMITED: "площадка ограничила скорость просмотра",
    REASON_NOT_LOGGED_IN: "вход на площадку не выполнен",
    REASON_LIMITED_ACCOUNT: (
        "у аккаунта площадки урезанный доступ: поиск по фразам отдаёт только "
        "первую страницу (20 лотов)"
    ),
    REASON_NETWORK: "обрыв связи с площадкой",
    REASON_PAGE_CAP: "упёрлись в потолок страниц",
    REASON_STOPPED: "обход остановлен пользователем",
    REASON_FORMAT_CHANGED: (
        "площадка изменила формат ответа — лоты не получены, нужна правка программы"
    ),
    REASON_CRASHED: "сбой программы при обходе площадки",
    REASON_NO_REPORT: "площадка не отчиталась о сборе — вероятно, сбой программы",
    REASON_EMPTY_PLATFORM: (
        "площадка не вернула ни одного лота — похоже, изменился формат ответа"
    ),
    REASON_BLOCKED: "площадка не пускает программу (антибот или запрет доступа)",
    REASON_STALE: "лента площадки не обновляется — новых лотов нет несколько дней",
    REASON_ANOMALY: "лотов резко меньше обычного — возможна поломка сбора",
    REASON_TIMEOUT: "площадка не уложилась в отведённое время — обход прерван",
    REASON_REQUEST_CAP: (
        "упёрлись в ночной лимит запросов к площадке — остальное доберём "
        "следующим прогоном"
    ),
    REASON_UNKNOWN: "причина неизвестна",
}


def reason_text(key: str) -> str:
    """Причина неполноты человеческим языком."""
    return _REASON_TEXTS.get(key, _REASON_TEXTS[REASON_UNKNOWN])


def build_unit_report(
    kind: str,
    label: str,
    *,
    platform: str = "",
    firm_id: str = "",
    expected_pages: int | None = None,
    collected_pages: int = 0,
    expected_lots: int | None = None,
    collected_lots: int = 0,
    reason: str = "",
    visited: bool = True,
) -> dict:
    """Отчёт о полноте обхода одного объекта (заказчик, фраза, секция).

    Полнота определяется ПО СТРАНИЦАМ: у заказчика с шестью страницами выдачи
    последняя почти всегда неполная (51 лот вместо 60), и сравнение по лотам
    объявляло бы неполным каждый успешный обход. Точный счётчик лотов
    («Актуально • N» в вёрстке площадки) используется дополнительно —
    там, где площадка его действительно даёт.

    Расхождение в плюс (собрано больше обещанного — лот добавился во время
    обхода) неполнотой не считается.

    ``platform`` — какая площадка обходит объект: с вводом отчёта у всех
    парсеров сводка обязана называть виновника неполноты по имени, а не
    сваливать шесть площадок в одну кучу.
    """
    if not visited:
        status = UNIT_SKIPPED
    elif expected_pages is None and expected_lots is None:
        # Неизвестный объём не отменяет известный обрыв обхода.
        status = UNIT_PARTIAL if reason else UNIT_UNKNOWN
    elif (
        expected_pages is not None
        and collected_pages < expected_pages
        or expected_lots is not None
        and collected_lots < expected_lots
    ):
        status = UNIT_PARTIAL
    else:
        status = UNIT_COMPLETE

    if status in (UNIT_PARTIAL, UNIT_SKIPPED):
        reason = reason or REASON_UNKNOWN
    else:
        reason = ""

    return {
        "platform": platform,
        "kind": kind,
        "label": label,
        "firm_id": firm_id,
        "expected_pages": expected_pages,
        "collected_pages": collected_pages,
        "expected_lots": expected_lots,
        "collected_lots": collected_lots,
        "status": status,
        "reason": reason,
    }


def cap_expected_pages(expected: int | None, collected: int) -> int:
    """Ожидаемое число страниц при упоре в потолок обхода.

    Когда цикл пагинации исчерпал MAX_PAGES без естественной остановки,
    данных точно больше, чем собрано, — иначе обход остановился бы сам.
    Если площадка не сообщила объём (expected неизвестен), минимальная
    честная оценка — «собранное + одна несобранная страница»: статус
    станет partial с причиной page_cap, и обрезанный сбор не будет
    выглядеть полным.
    """
    return max(expected or 0, collected + 1)


def failed_unit(kind: str, label: str, platform: str, reason: str) -> dict:
    """Отчёт «объект не обойден» — для исключений и несовместимых ответов.

    Любой `except` в парсере обязан оставить такой отчёт: объект, о котором
    ничего не сказано, в сводке выглядит так, будто его и не было.
    """
    return build_unit_report(
        kind,
        label,
        platform=platform,
        expected_pages=None,
        collected_pages=0,
        reason=reason,
        visited=False,
    )


def platform_unit(platform: str, reason: str) -> dict:
    """Отчёт о площадке целиком: «не отчиталась», «ноль лотов», «не обновляется»."""
    return failed_unit(KIND_PLATFORM, LABEL_PLATFORM, platform, reason)


def guard_platforms(
    reports: list[dict],
    enabled: list[str],
    lots_by_platform: dict[str, int],
    crashed: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    """Общий сторож площадок: отчёты, которых не хватает в сводке.

    Два случая, которые парсер сам не замечает (16.10.2026: ТЭК-Торг дал ноль
    с вердиктом «все 77 объектов обойдены целиком»):

      • площадка включена, но ни одного отчёта о полноте нет — парсер упал
        до отчётов или не умеет их писать; без строки она пропадала из сводки;
      • все объекты площадки «обойдены», а лотов ноль — у любой из наших
        площадок за ночь всегда есть хоть что-то, так что это несовместимый
        ответ, а не «закупок нет».

    Возвращает НОВЫЕ отчёты (их добавляют к остальным); существующие не трогает.
    """
    added: list[dict] = []
    for name in enabled:
        own = [r for r in reports if r.get("platform") == name]
        if not own:
            reason = REASON_CRASHED if name in crashed else REASON_NO_REPORT
            added.append(platform_unit(name, reason))
            continue
        failed = any(r.get("status") in (UNIT_PARTIAL, UNIT_SKIPPED) for r in own)
        if not failed and int(lots_by_platform.get(name) or 0) == 0:
            added.append(platform_unit(name, REASON_EMPTY_PLATFORM))
    return added


def summarize_units(reports: list[dict]) -> dict:
    """Сводка прогона: сколько объектов собрано полностью, сколько нет.

    `data_complete=False` означает, что отчёт заведомо неполон — интерфейс и
    Excel обязаны сказать об этом пользователю.
    """
    counts = {UNIT_COMPLETE: 0, UNIT_PARTIAL: 0, UNIT_SKIPPED: 0, UNIT_UNKNOWN: 0}
    incomplete = []
    for r in reports:
        status = r.get("status", UNIT_UNKNOWN)
        counts[status] = counts.get(status, 0) + 1
        if status in (UNIT_PARTIAL, UNIT_SKIPPED):
            incomplete.append(r)
    return {
        "complete": counts[UNIT_COMPLETE],
        "partial": counts[UNIT_PARTIAL],
        "skipped": counts[UNIT_SKIPPED],
        "unknown": counts[UNIT_UNKNOWN],
        "incomplete_units": incomplete,
        "data_complete": not incomplete,
    }


# Имя площадки для отчётов, в которых её нет (например, собранных старой
# версией программы или руками в тестах): честное «неизвестно», а не догадка.
PLATFORM_UNKNOWN = "Без площадки"


def summarize_by_platform(reports: list[dict]) -> list[dict]:
    """Сводка по площадкам: на каждую — строка с числом объектов и статусами.

    Прогон считается неполным, если ХОТЯ БЫ ОДНА площадка недобрала: у каждой
    строки свой `data_complete`, поэтому в журнале видно, что «Bidzaar не дошёл,
    а всё остальное — целиком», а не «прогон чем-то неполон».

    `reason` — причины неполноты этой площадки словами (не кодами): сводку
    читает руководитель, и «page_cap» не значит для него ничего. Причин может
    быть несколько — перечисляем без повторов, в порядке появления.
    """
    rows: dict[str, dict] = {}
    for r in reports:
        name = str(r.get("platform") or "").strip() or PLATFORM_UNKNOWN
        row = rows.setdefault(
            name,
            {
                "platform": name,
                "units": 0,
                "complete": 0,
                "partial": 0,
                "skipped": 0,
                "unknown": 0,
                "data_complete": True,
                "reason": "",
            },
        )
        row["units"] += 1
        status = r.get("status", UNIT_UNKNOWN)
        if status in (UNIT_COMPLETE, UNIT_PARTIAL, UNIT_SKIPPED, UNIT_UNKNOWN):
            row[status] += 1
        if status in (UNIT_PARTIAL, UNIT_SKIPPED):
            row["data_complete"] = False
            text = reason_text(r.get("reason"))
            if text and text not in row["reason"]:
                row["reason"] = f"{row['reason']}; {text}" if row["reason"] else text
    # Порядок — как в отчётах (первое появление площадки): сводка в журнале
    # и на листе Excel должна быть стабильной от прогона к прогону.
    return list(rows.values())


def platform_verdict(row: dict) -> str:
    """Вердикт площадки словами — для журнала, окна и Excel.

    Отдельная функция, потому что «полно» и «известно» — разные вещи.
    У площадки, которая не сообщает объём выдачи (пример: 73 объекта и
    ноль подтверждённо полных), прогон формально полон, но писать «собрано
    целиком» нельзя: мы не проверяли, всё ли она отдала.
    """
    units = int(row.get("units") or 0)
    complete = int(row.get("complete") or 0)
    unknown = int(row.get("unknown") or 0)
    if not row.get("data_complete"):
        return row.get("reason") or "собрано не всё"
    if units and unknown == units:
        return "площадка не сообщает объём выдачи"
    if units and complete == 0:
        return "объём подтвердить не удалось"
    return "собрано целиком"
