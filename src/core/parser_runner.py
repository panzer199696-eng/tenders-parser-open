"""
Parser runner - OPTIMIZED with parallel processing
"""

import asyncio
import logging
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from ..config import COMPANIES
from ..parsers import (
    BidzaarParser,
    GPBParser,
    RoseltorgParser,
    TektorgParser,
    TenderProParser,
    ZakupkiParser,
)
from ..parsers import tektorg_parser as _tektorg_mod
from ..parsers.tektorg_transport import TektorgBrowserTransport, probe_direct
from ..platforms import PLATFORMS, name_of
from ..runtime_paths import user_data_dir
from ..settings_store import load_settings
from ..utils.browser_manager import PROXY_SERVER
from ..utils.classifier import (
    _norm_nomenclature,
    nomenclature_hits,
)
from ..watch_clients import is_watched, watch_names

# Признак «это работы, а не поставка». Нужен как вето для лотов, найденных по
# фразе: крупные подрядные лоты перечисляют в названии всё подряд и совпадают
# случайным словом («…ремонт цеха… замена насосной станции» ↔ «Насосы»).
# Своим списком, а не is_construction: тот ищет маркеры подстрокой, и «мост»
# находился внутри другого слова, а «прокладк» — в уплотнительной прокладке
# клапана, из-за чего терялись обычные лоты поставки.
WORKS_PHRASES = (
    "выполнение работ",
    "строительство",
    "строительно-монтажные работы",
    "капитальный ремонт",
    "текущий ремонт",
    "капремонт",
    "реконструкция",
    "монтаж",
    "демонтаж",
    "подряд",
    "пусконаладочные работы",
    "проектные работы",
    "инженерные изыскания",
)


def looks_like_works(name: str) -> bool:
    """Лот про выполнение работ (подряд), а не про поставку товара."""
    return bool(nomenclature_hits(name, WORKS_PHRASES))


def _plural_lots(n: int) -> str:
    """Форма слова «лот» для счётчика в журнале: 1 лот, 3 лота, 5 лотов."""
    if n % 10 == 1 and n % 100 != 11:
        return "лот"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "лота"
    return "лотов"


logger = logging.getLogger(__name__)


# ── Чистые функции фильтрации (тестируемые без браузера) ─────────────────────


def apply_price_filter(tenders: Iterable[dict], settings: dict) -> list[dict]:
    """Фильтр цены согласно settings.

    Правила (см. ТЗ-1, п.3):
      - заказчик особого контроля (``watch_clients``) проходит без проверки
        цены: его лот за 1000 ₽ — находка, а не шум, порог min_price на него
        не смотрит;
      - price is None → включать только если `include_no_price`;
      - min_price > 0 и price < min_price → отброс (debug-лог);
      - max_price > 0 и price > max_price → отброс (debug-лог);
      - иначе включать.

    Возвращает новый список, исходные данные не мутирует.
    """
    min_price = settings.get("min_price", 0) or 0
    max_price = settings.get("max_price", 0) or 0
    include_no_price = bool(settings.get("include_no_price", True))

    # Список особого контроля читаем один раз: это файл на диске.
    watched = watch_names()

    kept: list[dict] = []
    for t in tenders:
        # Особый контроль проходит первым: остальные проверки цены
        # к этому заказчику не применяются вовсе.
        if watched and is_watched(t.get("company"), watched):
            logger.debug(
                "Лот заказчика особого контроля прошёл без фильтра цены: %s",
                str(t.get("name", "?"))[:60],
            )
            kept.append(t)
            continue
        price = t.get("sum")
        if price is None:
            if include_no_price:
                kept.append(t)
            else:
                logger.debug(
                    "Отброшен (нет цены, include_no_price=False): %s",
                    str(t.get("name", "?"))[:60],
                )
            continue
        # Значения price могут прийти нечисловыми (грязный парс) — пропускаем
        # такие через фильтр, как и None, чтобы не ронять агрегацию.
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            if include_no_price:
                kept.append(t)
            continue
        if min_price > 0 and price < min_price:
            logger.debug(
                "Отброшен по MIN_PRICE (%s): %s — цена %s",
                min_price,
                str(t.get("name", "?"))[:60],
                price,
            )
            continue
        if max_price > 0 and price > max_price:
            logger.debug(
                "Отброшен по MAX_PRICE (%s): %s — цена %s",
                max_price,
                str(t.get("name", "?"))[:60],
                price,
            )
            continue
        kept.append(t)
    return kept


def apply_keyword_filter(
    tenders: Iterable[dict],
    phrases: Iterable[str],
    client_labels: set[str] | None = None,
) -> list[dict]:
    """Мягкий фильтр соответствия нашей номенклатуре (режим B).

    Фильтр мягкий: он проверяет ТОЛЬКО лоты, найденные keyword-поиском по
    фразе (``search_key`` — одна из включённых фраз), и отсекает из них лишь
    нефильтрованную ленту, которую площадка отдаёт первой страницей поиска
    (поиск «Насосы» тащит и «Организацию питания вахтового поселка»).
    Всё остальное не трогает:

      - лот, найденный НЕ по фразе (``search_key`` — имя заказчика: адресный
        обход по организатору или поиск по компаниям), остаётся без проверки —
        это лоты закреплённых клиентов, а не поисковый шум;
      - лот, найденный по фразе, остаётся, если его название совпало с ЛЮБОЙ
        включённой фразой профиля (``matches_nomenclature``: падежи, порядок
        слов), а не только с той, по которой искали, — заказчик мог назвать
        позицию иначе («Сорбционная колонна» ↔ «Колонны сорбционные»);
      - лот остаётся, если его организатор (``company``) есть среди
        ``client_labels`` — лот закреплённого заказчика ценен независимо
        от формулировки названия;
      - лот заказчика особого контроля (``watch_clients`` в настройках)
        проходит без проверки: сверка идёт по целым словам названия (см.
        src/watch_clients.py), поэтому «АО "Ромашка-Север"» узнаётся, хотя
        точного равенства с «Ромашка» из client_labels тут нет.

      - строительный лот, совпавший случайным словом внутри длинного
        названия («…ремонт цеха… замена насосной станции»), отсекается,
        если ни одна из совпавших фраз сама не про работы.

    Сравнение ``search_key`` с фразами и ``company`` с ``client_labels`` —
    нормализованное (``_norm_nomenclature``: регистр, ё→е, кавычки).
    Если включённых фраз нет — возвращается исходный список (warning),
    чтобы фильтр нельзя было случайно «обнулить» выдачу.
    """
    tenders = list(tenders)
    clean = [p for p in phrases if isinstance(p, str) and p.strip()]
    if not clean:
        logger.warning(
            "keyword_filter включён, но включённых фраз нет — фильтр пропускается"
        )
        return tenders

    phrase_norms = {_norm_nomenclature(p) for p in clean}
    phrase_norms.discard("")
    clients_norm = set()
    for c in client_labels or ():
        if isinstance(c, str):
            n = _norm_nomenclature(c)
            if n:
                clients_norm.add(n)

    # Список особого контроля читаем один раз: это файл на диске.
    watched = watch_names()

    kept: list[dict] = []
    for t in tenders:
        # Найден не по фразе (адресный обход / поиск по компании) — не проверяем.
        key = _norm_nomenclature(str(t.get("search_key") or ""))
        if not key or key not in phrase_norms:
            kept.append(t)
            continue
        # Особый контроль — до client_labels: равенство полных названий
        # здесь не работает («АО "Ромашка-Север"» != «Ромашка»), а слово-в-слово
        # сверка watch_clients узнаёт организатора при любой форме владения.
        if watched and is_watched(t.get("company"), watched):
            logger.debug(
                "Лот заказчика особого контроля прошёл без keyword-фильтра: %s",
                str(t.get("name", "?"))[:60],
            )
            kept.append(t)
            continue
        # Организатор — наш закреплённый заказчик.
        company = _norm_nomenclature(str(t.get("company") or ""))
        if company and company in clients_norm:
            kept.append(t)
            continue
        # Совпадение с любой включённой фразой профиля (падежи, порядок слов).
        hits = nomenclature_hits(t.get("name"), clean)
        if hits:
            # Крупные строительные лоты перечисляют в названии всё подряд и
            # ловятся случайным словом: «Выполнение работ СМР… замена
            # насосной станции» совпадает с «Насосы». Такие лоты в выдачу не
            # пускаем — но только если ни одна из совпавших фраз не про работы:
            # когда строительные фразы включены сознательно, их лоты законны.
            if not any(looks_like_works(h) for h in hits) and looks_like_works(
                str(t.get("name") or "")
            ):
                logger.debug(
                    "Отброшен keyword-фильтром (строительный лот, совпал "
                    "случайным словом): %s",
                    str(t.get("name", "?"))[:60],
                )
                continue
            kept.append(t)
        else:
            logger.debug(
                "Отброшен keyword-фильтром (нефильтрованная лента поиска "
                "по фразе %r): %s",
                t.get("search_key"),
                str(t.get("name", "?"))[:60],
            )
    return kept


def _tektorg_phrases(phrases: Iterable[str], settings: dict | None) -> list[str]:
    """Фразы для keyword-поиска ТЭК-Торга без заведомо пустых.

    Площадка ищет по коротким обозначениям позиций (марки, артикулы) как
    попало: такая фраза может каждую ночь листать десятки страниц чужих
    закупок, упираться в потолок страниц и порождать ошибки 503, не принося
    ни одного лота.

    Режем адресно, а не в справочнике фраз: на других площадках те же фразы
    могут работать. Список правится в настройках (``tektorg_skip_phrases``),
    а не в коде: завтра площадка может начать искать по ним нормально.
    """
    from ..settings_store import DEFAULTS

    settings = settings or {}
    skip_raw = settings.get("tektorg_skip_phrases", DEFAULTS["tektorg_skip_phrases"])
    # Сравниваем по нормализованному виду: в справочнике фраза может быть
    # записана с другим регистром или лишними пробелами.
    skip = {
        " ".join(str(s).split()).casefold() for s in (skip_raw or []) if str(s).strip()
    }
    if not skip:
        return list(phrases)

    kept, dropped = [], []
    for phrase in phrases:
        if " ".join(str(phrase).split()).casefold() in skip:
            dropped.append(phrase)
        else:
            kept.append(phrase)
    if dropped:
        logger.info(
            "ТЭК-Торг: пропущены фразы без отдачи (%d): %s",
            len(dropped),
            ", ".join(dropped),
        )
    return kept


# ── Глубина обхода по дате публикации ────────────────────────────────────────

# Больше недели назад не листаем: площадки с отбором по дате публикации
# (Tender.pro) за неделю простоя отдают на порядок больше страниц, а
# лоты старше недели почти всегда уже с истёкшим сроком.
MAX_DEPTH_DAYS = 7


def collection_depth(
    platform: str,
    default: int,
    *,
    db_path: Path | str | None = None,
    today: date | None = None,
) -> int:
    """Сколько дней публикаций листать площадке: дни с последнего прогона,
    где она собрана целиком, + 1, но не больше недели и не меньше настройки.

    Найдено ревью 16.10.2026: Tender.pro листал только days_back=1,
    и после ночи без связи лоты пропущенного дня не собирались никогда.
    База открывается только на чтение; любая ошибка — прежний ``default``
    (сбор не должен падать из-за журнала). Площадка ни разу не собрана
    целиком — неделя.
    """
    path = Path(db_path) if db_path else user_data_dir() / "data" / "tenderwork.db"
    try:
        if not path.exists():
            return default
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT MAX(r.started_at) FROM runs r"
                " JOIN run_platforms p ON p.run_id = r.id"
                " WHERE p.platform = ? AND p.complete = 1 AND r.outcome = 'ok'",
                (platform,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Глубина обхода %s: журнал прогонов не прочитан (%s)", platform, e)
        return default
    if not row or not row[0]:
        depth = MAX_DEPTH_DAYS
    else:
        try:
            last = datetime.fromisoformat(str(row[0])).date()
        except ValueError:
            return default
        # started_at пишется по часам ПК — и «сегодня» берём по ним же.
        days = ((today or date.today()) - last).days
        depth = min(max(days, 0) + 1, MAX_DEPTH_DAYS)
    return max(depth, default)


# ── Раннер ───────────────────────────────────────────────────────────────────


def _lot_key(tender: dict) -> str:
    """Ключ лота для склейки заходов: ссылка, иначе номер, иначе название."""
    return str(
        tender.get("href")
        or tender.get("lot_id")
        or f"{tender.get('company')}|{tender.get('name')}"
    )


class ParserRunner:
    def __init__(
        self,
        browser_manager,
        days_back: int | None = None,
        settings: dict | None = None,
        companies: list[str] | None = None,
    ):
        """Конструктор.

        :param browser_manager: объект браузера (как раньше).
        :param days_back: если передан явно — приоритет над settings.days_back
                          (обратная совместимость с прежним API).
        :param settings: настройки. Если None — берём load_settings() из
                         settings_store.
        :param companies: список компаний. Если None — берётся COMPANIES из
                          config. Новый UI передаёт свежий список из
                          companies.json, чтобы правки пользователя сразу
                          попадали в парсинг.
        """
        self.browser_manager = browser_manager
        # Отчёты о полноте сбора по объектам обхода — их заполняют все
        # парсеры (ТЗ-8), и у каждого отчёта есть поле
        # platform. Контракт run_all() -> List[Dict] не меняется: сводка
        # забирается отсюда.
        self.unit_reports: list[dict] = []
        # Сколько лотов дала каждая площадка ДО фильтров отбора (цена, фразы,
        # срок). Это честная мера «площадка жива»: после фильтров ноль бывает
        # и у здоровой площадки. Читают сторож платформ и журнал прогонов.
        self.platform_lots: dict[str, int] = {}
        self.settings = settings if settings is not None else load_settings()
        # Явный аргумент имеет приоритет (обратная совместимость).
        self.days_back = (
            days_back
            if days_back is not None
            else int(self.settings.get("days_back", 1))
        )
        self.companies = companies if companies is not None else COMPANIES

    async def run_all(self) -> list[dict]:
        """Run all enabled parsers in parallel.

        Компании обязательны только для parse_gpb/parse_tender_pro. Парсеры,
        которым компании не нужны (ЕИС — keyword-only; ТЭК-Торг, Росэлторг,
        Bidzaar — фразы и/или ИНН), запускаются даже при пустом self.companies.
        Парсеры, требующие компаний, при пустом списке пропускаются с
        предупреждением (аналогично тому, как ``_run_zakupki`` пропускает этап
        при отсутствии фраз), а не роняют весь цикл парсинга.
        """
        all_tenders = []

        companies_empty = not self.companies
        if companies_empty:
            logger.warning(
                "Список компаний пуст — парсеры, требующие компаний, "
                "будут пропущены; keyword-only парсеры запустятся"
            )
        else:
            logger.info(f"Начало парсинга ({len(self.companies)} компаний)")

        logger.info(
            "Настройки: parse_gpb=%s, parse_tender_pro=%s, "
            "parse_zakupki=%s, parse_tektorg=%s, "
            "parse_bidzaar=%s, "
            "keyword_search_enabled=%s, keyword_filter_enabled=%s, "
            "min_price=%s, max_price=%s, include_no_price=%s, days_back=%s",
            bool(self.settings.get("parse_gpb")),
            bool(self.settings.get("parse_tender_pro")),
            bool(self.settings.get("parse_zakupki")),
            bool(self.settings.get("parse_tektorg")),
            bool(self.settings.get("parse_bidzaar")),
            bool(self.settings.get("keyword_search_enabled")),
            bool(self.settings.get("keyword_filter_enabled")),
            self.settings.get("min_price"),
            self.settings.get("max_price"),
            bool(self.settings.get("include_no_price")),
            self.days_back,
        )

        # Create tasks for parallel execution
        tasks = []

        if self.settings.get("parse_gpb"):
            if companies_empty:
                logger.warning("GPB: список компаний пуст — этап пропущен")
            else:
                tasks.append(("parse_gpb", self._run_gpb()))
        if self.settings.get("parse_tender_pro"):
            if companies_empty:
                logger.warning("Tender.pro: список компаний пуст — этап пропущен")
            else:
                tasks.append(("parse_tender_pro", self._run_tender_pro()))
        if self.settings.get("parse_zakupki"):
            # ЕИС — keyword-only по RSS (браузер/компании не нужны); сам
            # _run_zakupki пропустит этап, если нет активных фраз.
            tasks.append(("parse_zakupki", self._run_zakupki()))
        if self.settings.get("parse_roseltorg"):
            # Росэлторг — как ТЭК-Торг: оба режима вместе, адресный
            # обход по ИНН плюс поиск по фразам.
            tasks.append(("parse_roseltorg", self._run_roseltorg()))

        if self.settings.get("parse_tektorg"):
            # ТЭК-Торг — JSON API без браузера и авторизации. При наличии
            # фраз работает keyword-режим, иначе — секционный обход с
            # отбором по компаниям; запускается и при пустом self.companies.
            tasks.append(("parse_tektorg", self._run_tektorg()))

        if self.settings.get("parse_bidzaar"):
            # Bidzaar — агрегатор всех площадок России, открытый JSON API
            # без браузера и авторизации; оба режима (фразы и заказчики)
            # работают вместе внутри одного парсера.
            tasks.append(("parse_bidzaar", self._run_bidzaar()))

        # Run all parsers in parallel
        if tasks:
            results = await self._gather_platforms(tasks)
            crashed: set[str] = set()
            for (key, _coro), result in zip(tasks, results):
                if isinstance(result, list):
                    all_tenders.extend(result)
                elif isinstance(result, BaseException):
                    crashed.add(name_of(key))
                    logger.error(
                        "Сбой площадки %s: %s", name_of(key), result, exc_info=result
                    )

            # Второй заход по площадкам, упавшим по связи (12.10.2026):
            # раньше такая площадка терялась до следующей ночи.
            all_tenders = await self._retry_network_failures(
                [key for key, _coro in tasks], all_tenders
            )
            self._guard_platforms([key for key, _coro in tasks], all_tenders, crashed)

        # ── Фильтр цены (новая логика из settings) ───────────────────────────
        filtered = apply_price_filter(all_tenders, self.settings)
        logger.info(
            "Всего найдено: %d, после фильтра цены: %d "
            "(min_price=%s, max_price=%s, include_no_price=%s)",
            len(all_tenders),
            len(filtered),
            self.settings.get("min_price"),
            self.settings.get("max_price"),
            self.settings.get("include_no_price"),
        )

        # ── Keyword-фильтр (режим B, мягкий) ─────────────────────────────────
        if self.settings.get("keyword_filter_enabled"):
            from ..keywords_store import enabled_phrases

            phrases = enabled_phrases()
            before_kw = len(filtered)
            filtered = apply_keyword_filter(
                filtered, phrases, client_labels=self._client_labels()
            )
            logger.info(
                "Проверка соответствия номенклатуре: осталось %d из %d "
                "(лоты заказчиков и адресный обход не проверялись)",
                len(filtered),
                before_kw,
            )

        # Фильтр активности по дедлайну (не завершён на дату запуска) + классификация.
        # ВАЖНО: особый контроль НЕ обходит этот фильтр: истёкший лот не
        # нужен даже по заказчику особого контроля — подавать заявку некуда.
        from ..dates import today_msk
        from ..utils.classifier import classify, is_active

        # «Сегодня» — московское: сбор в 03:00 по ПК (UTC+7) — это 23:00 МСК,
        # и по дате ПК лоты «до конца сегодня» отсекались (Г2, 16.10.2026).
        today = today_msk()
        active = [t for t in filtered if is_active(t, today=today)]
        logger.info(
            f"После фильтра по дедлайну (не завершён на {today.strftime('%d.%m.%Y')}): "
            f"{len(active)} (было {len(filtered)})"
        )

        classified = [classify(t) for t in active]

        if self.unit_reports:
            from ..parsers.completeness import summarize_by_platform

            # Разбивка по площадкам: «Bidzaar упёрся в потолок» и «ТЭК-Торг
            # обошёл всё» — разные новости, и сваливать их в одну строку значило бы
            # прятать виновника неполноты.
            for row in summarize_by_platform(self.unit_reports):
                if row["data_complete"]:
                    # «Целиком» и «объём неизвестен» раздельно: раньше здесь
                    # было «все 0 объектов обойдены целиком» (16.10.2026).
                    logger.info(
                        "Полнота сбора, %s: целиком %d из %d объектов, "
                        "объём неизвестен у %d",
                        row["platform"],
                        row["complete"],
                        row["units"],
                        row["unknown"],
                    )
                else:
                    logger.warning(
                        "Полнота сбора, %s: неполных %d, не обойдено %d "
                        "из %d объектов — данные собраны НЕ полностью: %s",
                        row["platform"],
                        row["partial"],
                        row["skipped"],
                        row["units"],
                        row["reason"],
                    )

        # ── Особый контроль: итог цепочки фильтров ─────────────────────────
        # Строка для человека: сразу видно, что правило живое и сколько лотов
        # оно провело. Пустой список имён — строку не пишем: правила нет.
        watched_names = watch_names()
        if watched_names:
            watched_passed = sum(
                1 for t in classified if is_watched(t.get("company"), watched_names)
            )
            # Пишем строку и при нуле: «прошло 0 лотов» — это не пустой шум,
            # а ответ на вопрос «почему в книге пусто по заказчику». Молчание
            # здесь неотличимо от сломанного правила.
            logger.info(
                "Особый контроль (%s): прошло без фильтров %d %s",
                ", ".join(watched_names),
                watched_passed,
                _plural_lots(watched_passed),
            )

        return classified

    def _client_labels(self) -> set[str]:
        """Имена закреплённых заказчиков для мягкого keyword-фильтра.

        Список компаний (companies.json) плюс имена и алиасы заказчиков из
        справочника (data/clients.json). Лоты этих организаторов фильтр не
        проверяет — см. apply_keyword_filter.
        """
        labels: set[str] = {c for c in (self.companies or []) if c}
        try:
            from ..store.catalog import load_clients

            clients = load_clients().get("clients", [])
        except Exception as e:
            logger.warning(
                "Не удалось прочитать справочник заказчиков для client_labels "
                "(%s) — проверяю только по списку компаний",
                e,
            )
            return labels
        for client in clients:
            for name in (client.get("name", ""), *client.get("aliases", [])):
                if isinstance(name, str) and name.strip():
                    labels.add(name)
        return labels

    # Метод запуска площадки по ключу настройки — для второго захода.
    _RUNNERS = {p.key: p.runner for p in PLATFORMS}

    async def _retry_network_failures(
        self, keys: list[str], all_tenders: list[dict]
    ) -> list[dict]:
        """Повторно обойти площадки, у которых есть «обрыв связи».

        За 21 ночь (23.09–12.10.2026) четыре площадки падали по связи
        10 раз, и каждый раз их лоты терялись до следующей ночи. Обрыв
        обычно короткий, поэтому — пауза (по умолчанию 10 минут) и ещё
        один заход только по упавшим площадкам.

        Отчёты о полноте площадки заменяются отчётами второго захода, если
        он прошёл не хуже; лоты обоих заходов объединяются без дублей —
        собранное в первом заходе до обрыва не выбрасывается.
        """
        from ..parsers.completeness import REASON_NETWORK
        from .net_preflight import PLATFORM_PROBES

        if not self.settings.get("network_retry", True):
            return all_tenders
        reports = getattr(self, "unit_reports", None) or []

        def network_units(rows, name):
            return sum(
                1
                for r in rows
                if r.get("platform") == name and r.get("reason") == REASON_NETWORK
            )

        failed = [
            key
            for key in keys
            if key in self._RUNNERS
            and key in PLATFORM_PROBES
            and network_units(reports, PLATFORM_PROBES[key][0])
        ]
        if not failed:
            return all_tenders

        names = [PLATFORM_PROBES[k][0] for k in failed]
        pause = float(self.settings.get("network_retry_pause_s", 600))
        logger.warning(
            "Обрыв связи: %s — повторный заход через %d мин",
            ", ".join(names),
            round(pause / 60),
        )
        if pause > 0:
            await asyncio.sleep(pause)

        # Отчёты первого захода откладываем: второй заход допишет свои.
        before = {
            name: [r for r in reports if r.get("platform") == name] for name in names
        }
        self.unit_reports = [r for r in reports if r.get("platform") not in names]

        results = await self._gather_platforms(
            [(k, getattr(self, self._RUNNERS[k])()) for k in failed]
        )

        seen = {_lot_key(t) for t in all_tenders}
        for key, result in zip(failed, results):
            name = PLATFORM_PROBES[key][0]
            if isinstance(result, Exception):
                logger.error("Повторный заход, %s: %s", name, result, exc_info=result)
                result = []
            added = 0
            for t in result or []:
                k = _lot_key(t)
                if k not in seen:
                    seen.add(k)
                    all_tenders.append(t)
                    added += 1

            after = [r for r in self.unit_reports if r.get("platform") == name]
            if not after or network_units(after, name) > network_units(
                before[name], name
            ):
                # Второй заход вышел хуже первого — честнее оставить первый.
                self.unit_reports = [
                    r for r in self.unit_reports if r.get("platform") != name
                ] + before[name]
                after = before[name]
            left = network_units(after, name)
            logger.info(
                "Повторный заход, %s: +%d лотов, %s",
                name,
                added,
                "связь восстановилась" if not left else f"обрыв связи остался ({left})",
            )
        return all_tenders

    async def _gather_platforms(self, jobs: list[tuple]) -> list:
        """Обойти площадки параллельно, каждую — не дольше отведённого (Д2).

        27–30.09 и 10–11.10.2026 прогон шёл по три часа: площадка без связи
        висела, и всё остальное ждало её. Предел площадки — настройка
        platform_timeout_min (90 мин), но не дальше срока всего прогона
        (self.deadline по time.monotonic, ставит main_ui). Не уложилась —
        отчёт «площадка целиком» с причиной timeout и пустой список: лоты
        остальных площадок не теряются. Порядок результатов — как у jobs.

        Здесь же — события прогресса для окна (self.progress): «план»,
        «площадка начата», «площадка закончена» с лотами и объектами обхода.
        """
        import time

        from ..parsers.completeness import REASON_TIMEOUT, platform_unit

        limit = float(self.settings.get("platform_timeout_min", 90)) * 60
        self._progress("plan", None, platforms=[name_of(key) for key, _coro in jobs])

        async def one(key, coro):
            name = name_of(key)
            timeout = limit
            deadline = getattr(self, "deadline", None)
            if isinstance(deadline, (int, float)):
                timeout = max(0.0, min(timeout, deadline - time.monotonic()))
            self._progress("start", name)
            result: list | BaseException = []
            try:
                result = await asyncio.wait_for(coro, timeout)
                return result
            except asyncio.TimeoutError:
                logger.error(
                    "%s: не уложилась в %d мин — обход прерван", name, round(timeout / 60)
                )
                self.unit_reports = getattr(self, "unit_reports", None) or []
                self.unit_reports.append(platform_unit(name, REASON_TIMEOUT))
                result = TimeoutError()
                return []
            except BaseException as e:
                result = e
                raise
            finally:
                self._progress(
                    "done",
                    name,
                    lots=len(result) if isinstance(result, list) else 0,
                    units=sum(
                        1
                        for r in getattr(self, "unit_reports", None) or []
                        if r.get("platform") == name
                    ),
                    ok=isinstance(result, list),
                )

        return await asyncio.gather(
            *(one(key, coro) for key, coro in jobs), return_exceptions=True
        )

    def _progress(self, event: str, platform: str | None, **info) -> None:
        """Сообщить окну о ходе сбора; сбой окна не должен ронять сбор.

        Прогресс раньше считался регулярками по логам парсеров и не знал
        ТЭК-Торг, Росэлторг и Bidzaar (16.10.2026). Теперь — по площадкам.
        """
        callback = getattr(self, "progress", None)
        if callback is None:
            return
        try:
            callback(event, platform, **info)
        except Exception:
            logger.debug("Колбэк прогресса упал", exc_info=True)

    def _guard_platforms(
        self, keys: list[str], tenders: list[dict], crashed: set[str]
    ) -> None:
        """Общий сторож: площадка без отчётов или с нулём лотов — неполная.

        Считает лоты по площадкам до фильтров (self.platform_lots) и добавляет
        отчёты «площадка целиком» там, где парсер промолчал (см.
        completeness.guard_platforms). Найдено 16.10.2026: ТЭК-Торг дал ноль,
        а сводка написала «все 77 объектов обойдены целиком».
        """
        from ..parsers.completeness import guard_platforms, reason_text

        counts: dict[str, int] = {}
        for tender in tenders:
            name = str(tender.get("platform") or "")
            counts[name] = counts.get(name, 0) + 1
        enabled = [name_of(key) for key in keys]
        self.platform_lots = {name: counts.get(name, 0) for name in enabled}
        # Свежайшая публикация до фильтров — для межпрогонной проверки (В3).
        from ..run_journal import newest_by_platform

        self.platform_newest = newest_by_platform(tenders)
        self.unit_reports = getattr(self, "unit_reports", None) or []
        added = guard_platforms(self.unit_reports, enabled, counts, crashed)
        for report in added:
            logger.warning("%s ⚠ %s", report["platform"], reason_text(report["reason"]))
        self.unit_reports.extend(added)

    def _take_reports(self, parser) -> None:
        """Забрать у парсера отчёты о полноте (если он их ведёт).

        Отчёты собираем даже у упавшего парсера: неполнота обязана быть видна
        именно тогда, когда что-то пошло не так. Парсеры без механизма отчётов
        (например, ГПБ) просто не имеют атрибута — это не ошибка.
        """
        reports = getattr(parser, "unit_reports", None)
        if not reports:
            return
        # getattr с умолчанием: раннер мог быть создан в обход __init__
        # (так делают тесты) — падение на сводке портить им смысл.
        self.unit_reports = getattr(self, "unit_reports", None) or []
        self.unit_reports.extend(reports)

    async def _run_gpb(self) -> list[dict]:
        try:
            parser = GPBParser(self.browser_manager, self.companies, self.days_back)
            return await parser.parse()
        except Exception:
            logger.exception("ГПБ ошибка")
            return []

    def _depth(self, platform: str) -> int:
        """Глубина по дате публикации для площадки (см. collection_depth)."""
        depth = collection_depth(platform, self.days_back)
        if depth != self.days_back:
            logger.info(
                "%s: листаю публикации за %d дн. (с последнего полного сбора)",
                platform,
                depth,
            )
        return depth

    async def _run_tender_pro(self) -> list[dict]:
        parser = None
        try:
            parser = TenderProParser(
                self.browser_manager, self.companies, self._depth("Tender.pro")
            )
            return await parser.parse()
        except Exception:
            logger.exception("Tender.pro ошибка")
            return []
        finally:
            if parser is not None:
                self._take_reports(parser)

    async def _run_zakupki(self) -> list[dict]:
        try:
            # ЕИС ищет только по фразам (RSS расширенного поиска) — компании
            # не используются, браузер не нужен.
            from ..keywords_store import enabled_phrases

            phrases = enabled_phrases()
            if not phrases:
                logger.info("ЕИС: нет включённых ключевых фраз — этап пропущен")
                return []
            logger.info(
                "ЕИС: keyword-поиск по RSS, %d фраз (%s)",
                len(phrases),
                ", ".join(phrases),
            )
            parser = ZakupkiParser(
                self.browser_manager,
                self.companies,
                self.days_back,
                keyword_phrases=phrases,
            )
            try:
                return await parser.parse()
            finally:
                self._take_reports(parser)
        except Exception:
            logger.exception("ЕИС ошибка")
            return []

    async def _run_roseltorg(self) -> list[dict]:
        """Росэлторг: адресный обход по ИНН плюс поиск по фразам.

        Поиск по фразе здесь морфологический и слабый («насос центробежный»
        тянет всё со словом «насос»), поэтому ценность площадки — именно в ИНН:
        поле поиска их принимает, и запрос отдаёт лоты конкретного заказчика.
        Отсев лишнего из фразовой выдачи — работа профиля, не парсера.
        """
        try:
            from ..client_inns_store import load_client_inns
            from ..keywords_store import enabled_phrases

            # Фразовый поиск площадки по умолчанию выключен: он тянет
            # потолок выдачи на любой узкой марке (см. settings_store).
            use_phrases = bool(self.settings.get("roseltorg_keyword_search"))
            phrases = enabled_phrases() if use_phrases else []
            inns = load_client_inns()
            if not phrases and not inns:
                logger.info("Росэлторг: ни фраз, ни ИНН — этап пропущен")
                return []
            logger.info(
                "Росэлторг: %d ИНН и %d фраз%s",
                len(inns),
                len(phrases),
                "" if use_phrases else " (поиск по фразам выключен настройкой)",
            )
            parser = RoseltorgParser(
                self.browser_manager,
                self.companies,
                self.days_back,
                keyword_phrases=phrases or None,
                client_inns=inns or None,
            )
            try:
                return await parser.parse()
            finally:
                self._take_reports(parser)
        except Exception:
            logger.exception("Росэлторг ошибка")
            return []

    async def _run_tektorg(self) -> list[dict]:
        """ТЭК-Торг: гибрид из двух режимов, как на Bidzaar и Росэлторге.

        Режимы дополняют друг друга и работают ОБА, а не «или-или»: фразы
        включены почти всегда, и при `if phrases … else …` адресный обход
        по заказчикам не запускался бы никогда — а нужен именно он, потому
        что закреплённый клиент прячет нашу позицию внутри сборного лота
        («Расходники, реактивы, посуда»), где ни одна фраза не сработает.
        Фильтра по заказчику у площадки нет (см. tektorg_parser.py), поэтому
        секционный режим отбирает выдачу сам — по ИНН, а пока их нет в
        справочнике, по названию организатора.
        """
        try:
            from ..client_inns_store import load_client_inns
            from ..keywords_store import enabled_phrases

            collected: list[dict] = []
            all_phrases = enabled_phrases()
            phrases = _tektorg_phrases(all_phrases, self.settings)
            client_inns = load_client_inns()
            if not (phrases or client_inns or self.companies):
                return []

            transport = await self._tektorg_transport()
            try:
                collected = await self._tektorg_walk(
                    phrases, client_inns, transport, all_phrases
                )
            finally:
                if transport is not None:
                    await transport.close()

            if not collected:
                return []
            # Лот закреплённого заказчика может прийти обоими путями — дедуп
            # по той же логике, что внутри парсера.
            deduped = TektorgParser._dedup(collected)
            logger.info(
                "ТЭК-Торг: собрано %d лотов (до дедупа %d)",
                len(deduped),
                len(collected),
            )
            return deduped
        except Exception:
            logger.exception("ТЭК-Торг ошибка")
            return []

    async def _tektorg_transport(self):
        """Путь к API ТЭК-Торга: None — прямой urllib, иначе окно браузера.

        С ~20.09.2026 площадка за антиботом, который режет запросы с
        иностранного IP. Прямой путь в разы быстрее, поэтому
        сначала один пробный запрос; окно — только если его режет антибот.
        """
        verdict = await asyncio.to_thread(probe_direct, _tektorg_mod._fetch_procedures)
        if verdict != "antibot":
            logger.info(
                "ТЭК-Торг: прямой доступ к API — %s",
                "есть"
                if verdict == "ok"
                else "площадка не ответила, обход с повторами",
            )
            return None
        logger.warning(
            "ТЭК-Торг: антибот режет прямые запросы (иностранный IP? проверь "
            "VPN/прокси) — обход через окно браузера"
        )
        transport = TektorgBrowserTransport(proxy=PROXY_SERVER or None)
        if await transport.start():
            return transport
        await transport.close()
        logger.warning(
            "ТЭК-Торг: окно браузера тоже не прошло антибот — площадка в этом "
            "прогоне не соберётся"
        )
        return None

    async def _tektorg_walk(
        self, phrases, client_inns, transport, all_phrases=None
    ) -> list[dict]:
        """Оба режима ТЭК-Торга через общий транспорт (или без него).

        С 16.10.2026 поиск площадки мёртв, и оба режима берут лоты из ОДНОГО
        обхода списка лотов (TektorgOpenWalk): карточка процедуры — самый
        дорогой запрос, качать её дважды незачем. Фразы туда идут все, без
        tektorg_skip_phrases: те резались из-за слабого поиска площадки, а
        сверка названий на нашей стороне ничего не стоит. Состояние обхода
        — tektorg_state.json в папке данных; база — только чтение.
        """
        from ..runtime_paths import user_data_dir

        data_dir = user_data_dir() / "data"
        open_walk = _tektorg_mod.TektorgOpenWalk(
            phrases=phrases if all_phrases is None else all_phrases,
            client_inns=client_inns,
            companies=self.companies,
            transport=transport,
            state_path=data_dir / "tektorg_state.json",
            known_db=data_dir / "tenderwork.db",
        )
        collected: list[dict] = []
        try:
            if phrases:
                logger.info(
                    "ТЭК-Торг: keyword-поиск, %d фраз (%s)",
                    len(phrases),
                    ", ".join(phrases),
                )
                parser = TektorgParser(
                    self.browser_manager,
                    self.companies,
                    self.days_back,
                    keyword_phrases=phrases,
                    transport=transport,
                    open_walk=open_walk,
                )
                try:
                    collected.extend(await parser.parse())
                finally:
                    self._take_reports(parser)

            # ИНН — точный ключ отбора: названия организаторов на площадке и
            # в companies.json расходятся («ООО ПК "Северное"» против
            # «Северная горная»), и обход по именам теряет лоты.
            if client_inns or self.companies:
                if client_inns:
                    logger.info(
                        "ТЭК-Торг: адресный обход профильных секций, отбор по %d ИНН",
                        len(client_inns),
                    )
                else:
                    logger.info(
                        "ТЭК-Торг: адресный обход профильных секций, отбор по %d "
                        "названиям компаний (ИНН не заданы — см. client_inns.json)",
                        len(self.companies),
                    )
                parser = TektorgParser(
                    self.browser_manager,
                    self.companies,
                    self.days_back,
                    client_inns=client_inns or None,
                    transport=transport,
                    open_walk=open_walk,
                )
                try:
                    collected.extend(await parser.parse())
                finally:
                    self._take_reports(parser)
        finally:
            if open_walk.ran:
                # Отбор фраз приходит и без keyword-парсера (все фразы в
                # исключениях старого поиска), отчёты обхода — в сводку.
                collected.extend(open_walk.undelivered())
                self._take_reports(open_walk)
        return collected

    async def _run_bidzaar(self) -> list[dict]:
        """Bidzaar: агрегатор закупок России, оба режима вместе.

        Фразы — из keywords.json (enabled_phrases), адресный обход — по
        НАЗВАНИЯМ заказчиков из companies.json: фильтра по companyIds у
        API Bidzaar нет, поиск по названию через тот же `search` —
        единственный рабочий путь (разведка 03.09.2026). Оба режима
        выполняются одним парсером и дедупятся внутри него по id лота.
        """
        try:
            from ..keywords_store import enabled_phrases

            phrases = enabled_phrases()
            client_names = [c for c in (self.companies or []) if c]
            if not phrases and not client_names:
                logger.info("Bidzaar: ни фраз, ни заказчиков — этап пропущен")
                return []
            logger.info(
                "Bidzaar: %d фраз и %d заказчиков",
                len(phrases),
                len(client_names),
            )
            parser = BidzaarParser(
                self.browser_manager,
                self.companies,
                self.days_back,
                keyword_phrases=phrases or None,
                client_names=client_names or None,
            )
            try:
                return await parser.parse()
            finally:
                self._take_reports(parser)
        except Exception:
            logger.exception("Bidzaar ошибка")
            return []
