"""
ТЭК-Торг (tektorg.ru) parser — JSON API getProcedures, без браузера.

ТЭК-Торг — площадка корпоративных закупок крупных промышленных групп
(секции nornikel — 17599 процедур на 31.08.2026, enplusgroup и другие).
Там публикуется промышленная номенклатура, поэтому площадка включена в
конвейер.

Разведка 31.08.2026 показала рабочий путь без браузера, авторизации, кук
и сессии — площадка отвечает даже с иностранного IP:

    POST https://www.tektorg.ru/api/getProcedures
    Content-Type: application/json, User-Agent браузера (ОБЯЗАТЕЛЕН)
    {"params": {"name": "<фраза, опционально>",
                "sectionsCodes[0]": "nornikel", "sectionsCodes[1]": "…",
                "page": 1, "sort": "actual", "limit": 50}}

ГРАБЛИ, проверенные живьём (не «починить», а обойти — API такой):
  • sort принимает ТОЛЬКО "actual". Любое другое значение ("date",
    "datePublished") молча ломает ответ: total=null, data пуст.
  • limit работает до 50 включительно; limit=100 ломает ответ так же.
  • sort=actual ставит активные лоты вперёд, но НЕ сортирует по дате
    публикации (стр.1 может содержать 22.06, стр.2 — 24.08). Поэтому
    инкрементальный сбор «по дате» невозможен: секционный обход идёт
    «пока на странице есть хотя бы один активный лот» и останавливается
    на первой странице без активных.
  • Фильтра по ИНН/заказчику на входе НЕТ: параметры customerInn, inn,
    customer молча игнорируются (total не меняется). Отбор по заказчику
    делается на нашей стороне — по полям inn и organizerName.

⚠️ С ~20.09.2026 площадка за антиботом Variti: прямой POST получает 307 и
JS-проверку. Боевой путь — запросы из окна браузера, транспорт
TektorgBrowserTransport (tektorg_transport.py), его передаёт parser_runner.
Без транспорта сеть — urllib.request через asyncio.to_thread (тесты и
запасной путь). browser_manager принимается для единообразия, но не
используется: общий браузер фоновый, а его Variti режет.

⚠️ С ночи 16.10.2026 поиск площадки мёртв: getProcedures и open-api
/procedures на любые параметры отдают заглушку {"totalPages":1,
"currentPage":1,"limitPage":N,"hash":…} (hash — md5 тела, не токен), и на
самом сайте список тоже пуст. Живы список лотов секции (/open-api/lots, только
id лота и id процедуры, сортировка datePublished_desc) и карточка процедуры
(/open-api/procedures/{id}). Парсер пробует старый путь первым (один запрос):
заглушка вместо выдачи — переключается на TektorgOpenWalk в том же прогоне,
а починят поиск — сам вернётся к старому пути.

Временные ошибки загрузки (HTTP 429/5xx, таймаут, обрыв сети) повторяются
с нарастающей паузой: одиночный 503 у ТЭК-Торга — троттлинг, а не падение,
и лечится повтором, а не потерей секции (ТЗ-11). Постоянные (HTTP 4xx,
битый JSON) не повторяются: повтор вернул бы то же самое.

Словарь тендера совместим с остальными парсерами (platform, name,
tender_no, company, href, sum, сроки) плюс inn организатора. Нормализация названий —
_norm_org из tender_pro_parser.py (свой подход не изобретаем).
"""

import asyncio
import json
import logging
import os
import random
import re
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from ..dates import now_msk, parse_deadline
from ..net_ssl import ssl_context_ru
from ..utils.classifier import hits_prepared, name_words, prepare_terms
from ..watch_clients import is_watched, watch_names
from ._common import clean_field
from .completeness import (
    REASON_BLOCKED,
    REASON_CRASHED,
    REASON_FORMAT_CHANGED,
    REASON_NETWORK,
    REASON_PAGE_CAP,
    REASON_REQUEST_CAP,
    build_unit_report,
    cap_expected_pages,
    failed_unit,
    reason_text,
)
from .tektorg_transport import _looks_like_json
from .tender_pro_parser import TenderProParser, geo_hint

logger = logging.getLogger(__name__)

PLATFORM = "ТЭК-Торг"
API_URL = "https://www.tektorg.ru/api/getProcedures"

# Профильные секции ТЭК-Торга (горно-металлургические и промышленные).
# Полный список секций площадки шире (rzd, rosneft, transneft, les, …),
# но промышленная номенклатура закупается в корпоративных секциях:
# горно-металлургические (nornikel), En+ (enplusgroup),
# INTERRAO, «Силовые машины» (silovyi_machine), универсальные закупки
# (zakupki, market). Остальные секции — транспорт, нефть, лес, арестованное
# имущество — профильно не наши и лишь увеличивали бы обход.
#
# Порядок — приоритет обхода списка лотов (16.10.2026): каждая процедура
# стоит отдельной карточки, а потолок запросов за ночь общий. Объёмные
# секции (zakupki ~65 процедур в сутки, market ~300) идут последними, чтобы
# потолок обрезал их, а не профильную секцию (~20 в сутки).
PROFILE_SECTIONS = (
    "nornikel",
    "enplusgroup",
    "interrao",
    "silovyi_machine",
    "zakupki",
    "market",
)

# Единственное рабочее значение сортировки (см. грабли в шапке файла).
SORT_ACTUAL = "actual"
# Потолок размера страницы: limit=100 ломает ответ площадки.
PAGE_LIMIT = 50

# Потолок страниц на объект обхода. Секционный режим останавливается раньше
# (на первой странице без активных лотов — активных в секции сотни, а не
# тысячи), но потолок нужен как предохранитель от бесконечного листания
# keyword-выдачи и от зависшей пагинации.
MAX_PAGES = 40

# Пауза между запросами страниц: не долбим площадку (лимиты вежливости).
PAGE_DELAY_MIN = 1.0
PAGE_DELAY_MAX = 2.0

# ── Повтор временных ошибок (ТЗ-11) ───────────────────────────────────────────
#
# Восемь ночных прогонов подряд (04–09.09.2026) теряли объекты ТЭК-Торга
# из-за одиночных HTTP 503: площадка не падает — троттлит частые запросы,
# следующий объект собирается нормально. Повтор с паузой дешевле потери
# секции: секционный обход приносит 203 из 213 лотов площадки.

# Паузы повторов (с): 2 → 5 → 12, всего три повтора. Восемь прогонов
# показали, что 503 отпускает за секунды, поэтому первый повтор короткий;
# дальше интервал растёт с запасом — если площадку правда перегрузили,
# долбить её каждые две секунды бессмысленно. Итого до ~19 с на страницу —
# капля в ночном прогоне против целой потерянной секции.
RETRY_DELAYS = (2.0, 5.0, 12.0)

# HTTP-коды, которые стоит повторять: троттлинг (429) и перегрузка
# площадки/шлюзов (500/502/503/504). Коды 400/401/403/404 повторять
# бессмысленно: ответ детерминирован, повтор лишь растянет прогон.
RETRY_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

# Потолок ожидания по чужому заголовку Retry-After: верим площадке, но не
# даём ей остановить ночной прогон на «повторите через 10 минут».
RETRY_AFTER_CAP = 30.0

REQUEST_TIMEOUT = 30

# Обычный UA браузера: без него площадка не отвечает.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Активный статус: только такие лоты берём в результат. Встречаются также
# «Архив» и «Отменён» — их пропускаем.
ACTIVE_STATUS = "Приём заявок"

# ── Обход открытого API (поиск мёртв с 16.10.2026) ───────────────────────────

OPEN_API_URL = "https://api.tektorg.ru/open-api"
# Список лотов сортируется по дате публикации — это и даёт инкрементальный
# обход. sort=actual тут бесполезен: впереди висит старьё 2021 года.
OPEN_SORT = "datePublished_desc"
# Предохранитель на секцию: 20 страниц × 50 = 1000 лотов — больше суточной
# ленты самой объёмной секции (market, ~300 процедур в сутки).
OPEN_MAX_PAGES = 20
# Потолки на прогон (вежливость к площадке): карточек — включая перепроверку
# известных, запросов — всех вместе со страницами списка и повторами.
OPEN_MAX_CARDS = 600
OPEN_MAX_REQUESTS = 700
# Первый прогон (состояния ещё нет) идёт вглубь на 3 дня. Больше не нужно и
# не по карману: старый поиск жил до 15.10, а его активные лоты уже в базе и
# перепроверяются по карточкам. Месяц ленты шести секций — это ~4000
# карточек (замер 16.10.2026), в ночной потолок не влезает и за неделю.
OPEN_FIRST_HORIZON_DAYS = 3
# Глубже месяца не догоняем, даже если программа долго не запускалась.
OPEN_MAX_HORIZON_DAYS = 30
# Сколько помнить увиденные процедуры: дольше месяца они в начало ленты
# не вернутся, а файл состояния не должен расти бесконечно.
OPEN_SEEN_KEEP_DAYS = 35

# Ответы антибота, а не API. Повторять их бессмысленно и вредно: Variti
# ужесточает проверку на тех, кто долбит (решение 16.10.2026: секцию и весь
# обход — стоп, причина blocked). 429 повторялся бы в старом пути, но здесь
# каждая процедура — отдельный запрос, и троттлинг лечится только паузой.
BLOCKED_HTTP_CODES = frozenset({301, 302, 303, 307, 308, 403, 429})

# Статусы процедуры, которые не оживит никакой срок. Остальные не смотрим:
# в секции nornikel процедуры 2021 года до сих пор «Приём заявок» — активность
# решает срок подачи (dateEndRegistration), а не статус.
DEAD_STATUS_MARKERS = ("отмен", "заверш", "архив", "не состоял", "аннулир")

RECHECK_LABEL = "известные активные процедуры"


def _fetch_procedures(payload: dict) -> str:
    """POST к JSON API ТЭК-Торга (синхронная; вызывается через to_thread)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    # ТЭК-Торг отвечает и с иностранного IP, но сертификат может быть
    # российского НУЦ — контекст из net_ssl доверяет системным корням И
    # российским, проверка цепочки остаётся включённой.
    ctx = ssl_context_ru()
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект у API — подпись Variti (307 на тот же адрес с куками).

    Идти за ним бессмысленно: urllib без кук кружил бы до лимита
    редиректов. Пусть станет HTTPError с кодом 3xx — это причина blocked.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_get(url: str) -> str:
    """GET к открытому API ТЭК-Торга (синхронная; через to_thread)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
    )
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ssl_context_ru())
    )
    with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def known_procedures(db_path) -> dict[str, str]:
    """Процедуры ТЭК-Торга из единой базы: {id процедуры: срок как в базе}.

    Только чтение (`mode=ro`): парсер в базу не пишет. Нужны для
    перепроверки — обход списка лотов идёт до первой знакомой страницы, а
    лоты прошлых ночей должны приходить каждую ночь, как при старом поиске.
    Нет файла, нет таблиц, база занята — пустой словарь, прогон не падает.
    """
    path = Path(db_path) if db_path else None
    if path is None or not path.is_file():
        return {}
    uri = (
        "file:" + urllib.parse.quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"
    )
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        logger.warning(f"ТЭК-Торг: база для перепроверки не открылась ({e})")
        return {}
    known: dict[str, str] = {}
    try:
        for lot_id, deadline in conn.execute(
            "SELECT lot_id, date_deadline FROM lots WHERE platform = ?", (PLATFORM,)
        ):
            if lot_id:
                known[str(lot_id)] = str(deadline or "")
        # Лот, склеенный с другой площадкой, хранит наш номер в lot_sources.
        try:
            rows = conn.execute(
                "SELECT s.lot_id, l.date_deadline FROM lot_sources s"
                " JOIN lots l ON l.lot_key = s.lot_key WHERE s.platform = ?",
                (PLATFORM,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for lot_id, deadline in rows:
            if lot_id:
                known.setdefault(str(lot_id), str(deadline or ""))
    except sqlite3.Error as e:
        logger.warning(f"ТЭК-Торг: база для перепроверки не прочиталась ({e})")
        return {}
    finally:
        conn.close()
    return known


def _is_temporary_error(error: Exception) -> bool:
    """Временная ли ошибка — та, которую стоит повторить.

    HTTP 429/5xx — троттлинг или перегрузка: той же ночью соседние объекты
    собираются нормально, значит, повтор может выручить. HTTP 4xx, битый
    JSON и «не JSON-объект» — постоянные: повтор вернёт то же самое.
    Таймаут и URLError с сетевой причиной (обрыв, сброс соединения, DNS) —
    временные: следующий запрос может пройти нормально.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRY_HTTP_CODES
    # socket.timeout — историческое имя TimeoutError, проверяем оба.
    if isinstance(error, (socket.timeout, TimeoutError)):
        return True
    if isinstance(error, urllib.error.URLError):
        # Сетевые сбои приходят причиной-исключением (OSError: сброс
        # соединения, gaierror при разборе DNS). Строковая причина
        # («unknown url type» и т. п.) — не сетевой сбой, и повторять
        # её бессмысленно.
        return isinstance(error.reason, OSError)
    return False


def _retry_after_seconds(error: Exception) -> float | None:
    """Секунды из заголовка Retry-After, если площадка прислала число.

    Формат HTTP-даты не разбираем: по журналу ТЭК-Торг шлёт секунды, а
    выдумывать паузу из даты опаснее, чем взять штатную из RETRY_DELAYS.
    """
    if not isinstance(error, urllib.error.HTTPError):
        return None
    raw = error.headers.get("Retry-After") if error.headers else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _error_code(error: Exception) -> str:
    """Короткое имя ошибки для журнала повторов: «HTTP 503», «TimeoutError»."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__


def _parse_price(text) -> float | None:
    """«173981.72 ₽» → 173981.72; «НМЦ не установлена»/мусор → None.

    Общий разбор цены (src/parsers/_common.py): своя копия превращала
    «1 234 567,89 руб.» в 123456789 — точка «руб.» склеивала копейки.
    """
    from ._common import parse_price

    return parse_price(text)


def _fmt_date(iso_text) -> str:
    """ISO 8601 с таймзоной («2026-08-21T13:48:26+03:00») → «21.08.2026».

    Нет поля / не разобралось → «Не указана», не выдумываем дату.
    Для даты публикации время не нужно — остальные парсеры показывают день.
    """
    if not iso_text or not isinstance(iso_text, str):
        return "Не указана"
    try:
        return datetime.fromisoformat(iso_text.strip()).strftime("%d.%m.%Y")
    except ValueError:
        return "Не указана"


def _fmt_deadline(iso_text) -> str:
    """Срок подачи → «22.10.2026 20:00» по Москве.

    Время срока важно (16.10.2026): без него лот с подачей до 10:00 весь
    день выглядел живым. Пояс площадки переводится в МСК (src/dates.py).
    """
    if not iso_text or not isinstance(iso_text, str):
        return "Не указана"
    at = parse_deadline(iso_text)
    return at.strftime("%d.%m.%Y %H:%M") if at else "Не указана"


def _pick_href(item: dict) -> str:
    """Ссылка на процедуру: etpLink, иначе адресная кнопка подачи, иначе пусто.

    applBtn берём, только если в ней номер этой процедуры: в секции nornikel это
    общий вход в SRM (http://srm.nornik.ru/nwbc) — один на все лоты, а
    раннер склеивает лоты повторного захода по href (_lot_key), и вся
    секция слилась бы в один лот.
    """
    link = str(item.get("etpLink") or "").strip()
    if link:
        return link
    lots = item.get("lots") or []
    first = lots[0] if lots and isinstance(lots[0], dict) else {}
    button = str(first.get("applBtn") or "").strip()
    marks = {
        str(item.get("remoteId") or ""),
        str(item.get("registryNumber") or "").split("/")[0],
        str(item.get("id") or ""),
    }
    marks.discard("")
    if button and any(mark in button for mark in marks):
        return button
    return ""


def _item_price(item: dict) -> float | None:
    """Цена процедуры: sumPrice, иначе сумма начальных цен лотов."""
    price = _parse_price(item.get("sumPrice"))
    if price is not None:
        return price
    parts = [
        _parse_price(lot.get("startPrice"))
        for lot in item.get("lots") or []
        if isinstance(lot, dict)
    ]
    parts = [p for p in parts if p is not None]
    return sum(parts) if parts else None


def _tender_from(
    item: dict, search_key: str, organizer_match: bool, name: str | None = None
) -> dict | None:
    """Процедура (элемент старой выдачи или карточка) → словарь тендера.

    lot_id — id процедуры, как и до 16.10.2026: на нём ключ лота в базе
    («ТЭК-Торг|20060946»), реестр и история заявок.
    """
    try:
        dates = item.get("dates") or {}
        return {
            "platform": PLATFORM,
            "name": name or clean_field(str(item.get("title") or "")) or "Без названия",
            "tender_no": str(item.get("registryNumber") or "")
            or str(item.get("remoteId") or ""),
            "tender_type": clean_field(str(item.get("typeName") or "")),
            "lot_id": str(item.get("id") or ""),
            "company": clean_field(str(item.get("organizerName") or "")),
            "search_key": search_key,
            "organizer_href": None,
            "organizer_match": organizer_match,
            "href": _pick_href(item),
            "sum": _item_price(item),
            "date_published": _fmt_date(dates.get("datePublished")),
            "date_deadline": _fmt_deadline(dates.get("dateEndRegistration")),
            "date_to_display": _fmt_deadline(dates.get("dateEndRegistration")),
            "inn": str(item.get("inn") or ""),
        }
    except Exception as e:
        logger.debug(f"ТЭК-Торг: разбор лота не удался: {e}")
        return None


class TektorgOpenWalk:
    """Обход открытого API: список лотов секции → карточки новых процедур.

    Один обход на оба режима и два отбора (16.10.2026): карточка — самый
    дорогой запрос, и качать её дважды (для фраз и для ИНН) незачем.
      • «section» — организатор из client_inns (иначе по названию из
        companies) или заказчик особого контроля; search_key = секция;
      • «phrase» — фраза совпала с названием процедуры или предметом
        лота (поиска площадки нет — сверка на нашей стороне, та же, что у
        фильтра раннера); search_key = совпавшая фраза.

    Инкрементальность — по файлу состояния (state_path): какие процедуры
    уже видели и закончился ли прошлый обход секции целиком. Обход секции
    останавливается на странице, где все процедуры знакомы; после
    недобора (потолок, обрыв) — догоняет хвост до прежней границы.
    Отобранные активные процедуры прошлых ночей (из состояния и из базы,
    только чтение) перепроверяются по карточке: как при старом поиске, они
    приходят каждую ночь, а отменённые отпадают.

    Строго последовательные запросы с паузой, потолки OPEN_MAX_CARDS и
    OPEN_MAX_REQUESTS на прогон; антибот (HTML, 403/429/3xx) — стоп всего
    обхода с причиной blocked, без повторов.
    """

    def __init__(
        self,
        *,
        phrases=None,
        client_inns=None,
        companies=None,
        transport=None,
        state_path=None,
        known_db=None,
        now: datetime | None = None,
    ):
        self.phrases = [p for p in phrases or [] if isinstance(p, str) and p.strip()]
        self._terms = prepare_terms(self.phrases)
        self.client_inns = {
            str(i).strip() for i in (client_inns or []) if str(i).strip()
        }
        self.companies = [c for c in companies or [] if c]
        self.transport = transport
        self.state_path = Path(state_path) if state_path else None
        self.known_db = known_db
        self._now_override = now
        self.now: datetime = now or now_msk()
        self.search_dead = False
        self.ran = False
        self.unit_reports: list[dict] = []
        self.results: dict[str, list[dict]] = {"phrase": [], "section": []}
        self._delivered: set[str] = set()
        self.requests = 0
        self.cards = 0
        self.stats = {
            "pages": 0,
            "procedures": 0,
            "cards": 0,
            "active": 0,
            "by_inn": 0,
            "by_phrase": 0,
            "rechecked": 0,
        }
        # Причина, по которой обход встал целиком (антибот, потолок запросов).
        self._stop_reason = ""
        self._fetched: set[str] = set()
        self._published: dict[str, str] = {}
        self._watched: list[str] = []
        self._state: dict = {}

    # ── Выдача результатов режимам ─────────────────────────────────────────

    def take(self, mode: str) -> list[dict]:
        self._delivered.add(mode)
        return list(self.results.get(mode, []))

    def undelivered(self) -> list[dict]:
        """Отборы, которые не забрал ни один парсер (раннер добавит их сам)."""
        out: list[dict] = []
        for mode in ("phrase", "section"):
            if mode not in self._delivered:
                out.extend(self.take(mode))
        return out

    # ── Состояние между прогонами ──────────────────────────────────────────

    def _load_state(self) -> dict:
        state: dict = {}
        if self.state_path is not None and self.state_path.is_file():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
            except (OSError, ValueError) as e:
                logger.warning(
                    f"ТЭК-Торг: состояние обхода не прочиталось ({e}) — "
                    f"обход как в первый раз"
                )
        if not isinstance(state.get("sections"), dict):
            state["sections"] = {}
        if not isinstance(state.get("selected"), dict):
            state["selected"] = {}
        return state

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        keep_from = (self.now - timedelta(days=OPEN_SEEN_KEEP_DAYS)).date().isoformat()
        for st in self._state["sections"].values():
            st["seen"] = {k: v for k, v in st.get("seen", {}).items() if v >= keep_from}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning(f"ТЭК-Торг: состояние обхода не сохранилось ({e})")

    def _section_state(self, section: str) -> dict:
        st = self._state["sections"].setdefault(section, {})
        if not isinstance(st.get("seen"), dict):
            st["seen"] = {}
        return st

    def _floor(self, st: dict) -> datetime:
        """Граница по дате публикации, глубже которой секцию не листаем.

        После целого обхода — момент того обхода (минус сутки запаса: лента
        сортируется по публикации, а не по времени нашего прихода); в первый
        раз — OPEN_FIRST_HORIZON_DAYS, и эта граница запоминается, чтобы
        недобор догонялся до неё, а не до плывущего «сейчас минус 3 дня».
        """
        oldest = self.now - timedelta(days=OPEN_MAX_HORIZON_DAYS)
        anchor = None
        for key, shift in (("complete_at", timedelta(days=1)), ("floor", timedelta())):
            raw = st.get(key)
            if raw:
                try:
                    anchor = datetime.fromisoformat(raw) - shift
                    break
                except (TypeError, ValueError):
                    pass
        if anchor is None:
            anchor = self.now - timedelta(days=OPEN_FIRST_HORIZON_DAYS)
            st["floor"] = anchor.isoformat(timespec="seconds")
        return max(anchor, oldest)

    # ── Запросы ────────────────────────────────────────────────────────────

    async def _get(self, url: str) -> tuple[dict | None, str]:
        """GET с повтором временных ошибок → (JSON-объект, исход).

        Исход: ok · blocked (антибот) · network (повторы исчерпаны) ·
        error (404/422, битый JSON) · cap (кончился лимит запросов).
        """
        for attempt in range(len(RETRY_DELAYS) + 1):
            if self.requests >= OPEN_MAX_REQUESTS:
                return None, "cap"
            if self.requests:
                await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))
            self.requests += 1
            try:
                fetch_get = getattr(self.transport, "fetch_get", None)
                if fetch_get is not None:
                    raw = await fetch_get(url)
                else:
                    raw = await asyncio.to_thread(_fetch_get, url)
            except Exception as e:
                if (
                    isinstance(e, urllib.error.HTTPError)
                    and e.code in BLOCKED_HTTP_CODES
                ):
                    logger.warning(
                        f"ТЭК-Торг: {_error_code(e)} — площадка не пускает, "
                        f"обход остановлен без повторов"
                    )
                    return None, "blocked"
                temporary = _is_temporary_error(e)
                if not (temporary and attempt < len(RETRY_DELAYS)):
                    logger.warning(
                        f"ТЭК-Торг: ошибка загрузки {url} ({e}){geo_hint(e)}"
                    )
                    return None, "network" if temporary else "error"
                wait = _retry_after_seconds(e)
                wait = (
                    RETRY_DELAYS[attempt]
                    if wait is None
                    else min(wait, RETRY_AFTER_CAP)
                )
                logger.info(
                    f"ТЭК-Торг: {_error_code(e)} — повтор {attempt + 1} из "
                    f"{len(RETRY_DELAYS)} через {wait} с"
                )
                await asyncio.sleep(wait)
                continue
            if not _looks_like_json(raw or ""):
                logger.warning(
                    "ТЭК-Торг: вместо JSON пришла страница (антибот?) — обход остановлен"
                )
                return None, "blocked"
            try:
                data = json.loads(raw)
            except ValueError:
                return None, "error"
            if not isinstance(data, dict):
                return None, "error"
            return data, "ok"
        return None, "network"

    async def _fetch_card(self, pid: str) -> tuple[dict | None, str]:
        if self.cards >= OPEN_MAX_CARDS:
            return None, "cap"
        data, outcome = await self._get(f"{OPEN_API_URL}/procedures/{pid}")
        if outcome != "cap":
            self.cards += 1
        if outcome in ("ok", "error"):
            # Обрыв или антибот — карточку не получили, и обход должен
            # взять её снова, а не счесть увиденной.
            self._fetched.add(pid)
        if outcome == "ok" and not (data or {}).get("id"):
            return None, "error"
        return data, outcome

    def _reason_of(self, outcome: str) -> str:
        reason = {
            "blocked": REASON_BLOCKED,
            "cap": REASON_REQUEST_CAP,
            "network": REASON_NETWORK,
        }.get(outcome, REASON_FORMAT_CHANGED)
        if outcome in ("blocked", "cap"):
            self._stop_reason = reason
        return reason

    # ── Карточка: активность и отбор ───────────────────────────────────────

    def _card_active(self, card: dict) -> bool:
        status = str(card.get("statusName") or "").casefold()
        if any(m in status for m in DEAD_STATUS_MARKERS):
            return False
        lot_statuses = [str(s).casefold() for s in card.get("statusLots") or []]
        if lot_statuses and all(
            any(m in s for m in DEAD_STATUS_MARKERS) for s in lot_statuses
        ):
            return False
        deadline = parse_deadline((card.get("dates") or {}).get("dateEndRegistration"))
        if deadline is None:
            return status == ACTIVE_STATUS.casefold()
        return deadline > self.now

    def _organizer_selected(self, card: dict) -> bool:
        company = clean_field(str(card.get("organizerName") or ""))
        # Особый контроль (src/watch_clients.py): новый отбор обязан пропускать
        # заказчиков из watch_clients без остальных условий.
        if self._watched and is_watched(company, self._watched):
            return True
        if self.client_inns:
            return str(card.get("inn") or "").strip() in self.client_inns
        if not company:
            return False
        return any(TenderProParser._organizer_match(c, company) for c in self.companies)

    def _phrase_hit(self, card: dict) -> tuple[str, str]:
        """(совпавшая фраза, название для тендера) или ("", "").

        Совпадение только в предмете лота дописывается к названию: фильтр
        раннера сверяет фразу с name, и без предмета лот бы отбросился.
        """
        title = clean_field(str(card.get("title") or ""))
        hits = hits_prepared(name_words(title), self._terms)
        if hits:
            return hits[0], title
        for lot in card.get("lots") or []:
            if not isinstance(lot, dict):
                continue
            subject = clean_field(str(lot.get("subject") or ""))
            if not subject or subject == title:
                continue
            hits = hits_prepared(name_words(subject), self._terms)
            if hits:
                return hits[0], f"{title} — {subject}" if title else subject
        return "", ""

    def _process_card(self, card: dict, section: str) -> None:
        pid = str(card.get("id"))
        self.stats["cards"] += 1
        if not self._card_active(card):
            self._state["selected"].pop(pid, None)
            return
        self.stats["active"] += 1
        selected = False
        if self._organizer_selected(card):
            tender = _tender_from(card, section, True)
            if tender:
                self.results["section"].append(tender)
                self.stats["by_inn"] += 1
                selected = True
        if self._terms:
            phrase, name = self._phrase_hit(card)
            if phrase:
                tender = _tender_from(card, phrase, False, name=name)
                if tender:
                    self.results["phrase"].append(tender)
                    self.stats["by_phrase"] += 1
                    selected = True
        if selected:
            deadline = parse_deadline(
                (card.get("dates") or {}).get("dateEndRegistration")
            )
            self._state["selected"][pid] = {
                "deadline": deadline.isoformat(timespec="minutes") if deadline else "",
                "section": section,
            }
        else:
            self._state["selected"].pop(pid, None)

    # ── Обход ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Обойти секции один раз за прогон (повторный вызов — ничего)."""
        if self.ran:
            return
        self.ran = True
        self.search_dead = True
        self.now = self._now_override or now_msk()
        self._watched = watch_names()
        self._state = self._load_state()
        sections = list(PROFILE_SECTIONS)
        logger.warning(
            f"ТЭК-Торг: поиск площадки недоступен — обход через список лотов "
            f"({len(sections)} секций, отбор по {len(self.client_inns)} ИНН и "
            f"{len(self.phrases)} фразам на нашей стороне)"
        )
        try:
            await self._recheck_known()
        except Exception:
            logger.exception("ТЭК-Торг: сбой перепроверки известных процедур")
            self.unit_reports.append(
                failed_unit("section", RECHECK_LABEL, PLATFORM, REASON_CRASHED)
            )
        for section in sections:
            if self._stop_reason:
                self.unit_reports.append(
                    failed_unit("section", section, PLATFORM, self._stop_reason)
                )
                continue
            try:
                await self._walk_section(section)
            except Exception:
                logger.exception(f"ТЭК-Торг: сбой обхода секции '{section}'")
                self.unit_reports.append(
                    failed_unit("section", section, PLATFORM, REASON_CRASHED)
                )
        if self.phrases:
            # Поиска по площадке нет: фразы сверяются с названиями внутри
            # профильных секций. Объём такого «поиска» не известен — честное
            # unknown, а недобор секций виден в их собственных отчётах.
            self.unit_reports.append(
                build_unit_report(
                    "phrase",
                    f"{len(self.phrases)} фраз — отбор по названию внутри "
                    f"{len(sections)} профильных секций (поиск площадки недоступен)",
                    platform=PLATFORM,
                )
            )
        self._save_state()
        logger.info(
            f"ТЭК-Торг: обход списка — запросов {self.requests}, процедур в "
            f"списках {self.stats['procedures']}, карточек {self.stats['cards']} "
            f"(перепроверено {self.stats['rechecked']}), активных "
            f"{self.stats['active']}, отобрано по ИНН {self.stats['by_inn']}, "
            f"по фразам {self.stats['by_phrase']}"
        )

    async def _recheck_known(self) -> None:
        known: dict[str, tuple[str, str]] = {}
        if self.known_db:
            for pid, deadline in known_procedures(self.known_db).items():
                known[pid] = (deadline, "")
        selected = self._state["selected"]
        for pid, info in list(selected.items()):
            info = info if isinstance(info, dict) else {}
            known[pid] = (
                str(info.get("deadline") or ""),
                str(info.get("section") or ""),
            )
        due = []
        for pid, (deadline, section) in known.items():
            at = parse_deadline(deadline)
            if at is None or at <= self.now:
                selected.pop(pid, None)
                continue
            due.append((at, pid, section))
        if not due:
            return
        # Ближний срок — первым: упрёмся в потолок — потеряем дальние.
        due.sort()
        done = 0
        reason = ""
        for _at, pid, section in due:
            card, outcome = await self._fetch_card(pid)
            if outcome in ("blocked", "cap", "network"):
                reason = self._reason_of(outcome)
                break
            done += 1
            if outcome != "ok":
                selected.pop(pid, None)
                continue
            self.stats["rechecked"] += 1
            label = section or str(card.get("sectionAlias") or "")
            self._published[pid] = self._published_day(card)
            self._process_card(card, label)
        report = build_unit_report(
            "section",
            RECHECK_LABEL,
            platform=PLATFORM,
            expected_lots=len(due),
            collected_lots=done,
            reason=reason,
        )
        self.unit_reports.append(report)
        if report["status"] != "complete":
            logger.warning(
                f"ТЭК-Торг ⚠ перепроверено {done} из {len(due)} известных "
                f"процедур — {reason_text(report['reason'])}"
            )

    def _published_day(self, card: dict) -> str:
        at = parse_deadline((card.get("dates") or {}).get("datePublished"))
        return (at or self.now).date().isoformat()

    async def _walk_section(self, section: str) -> None:
        st = self._section_state(section)
        seen: dict = st["seen"]
        was_complete = bool(st.get("last_ok"))
        floor = self._floor(st)
        today = self.now.date().isoformat()
        pages = 0
        reason = ""
        done = False
        for page_no in range(1, OPEN_MAX_PAGES + 1):
            url = (
                f"{OPEN_API_URL}/lots?sectionsCodes[0]={section}&page={page_no}"
                f"&sort={OPEN_SORT}&limit={PAGE_LIMIT}"
            )
            data, outcome = await self._get(url)
            if outcome != "ok":
                reason = self._reason_of(outcome)
                break
            items = data.get("data")
            if not isinstance(items, list):
                # 422 и прочие ответы без списка: параметры или формат API
                # поменялись — повтор тем же кодом бесполезен.
                reason = REASON_FORMAT_CHANGED
                break
            pages += 1
            self.stats["pages"] += 1
            if not items:
                done = True
                break
            pids: list[str] = []
            for item in items:
                pid = str((item or {}).get("procedure_id") or "")
                if pid and pid not in pids:
                    pids.append(pid)
            self.stats["procedures"] += len(pids)
            fresh = []
            for pid in pids:
                if pid in seen:
                    continue
                if pid in self._fetched:
                    # Карточку уже взяли перепроверкой — запоминаем и не
                    # качаем второй раз.
                    seen[pid] = self._published.get(pid, today)
                    continue
                fresh.append(pid)
            numeric = [int(p) for p in pids if p.isdigit()]
            if numeric:
                st["max_id"] = max([st.get("max_id") or 0, *numeric])
            if not fresh and was_complete:
                # Вся страница знакома, а прошлый обход дошёл до конца —
                # дальше только то, что уже видели.
                done = True
                break
            reached_floor = False
            for pid in fresh:
                card, outcome = await self._fetch_card(pid)
                if outcome in ("blocked", "cap", "network"):
                    reason = self._reason_of(outcome)
                    break
                if outcome != "ok":
                    seen[pid] = today
                    continue
                seen[pid] = self._published[pid] = self._published_day(card)
                self._process_card(card, section)
                published = parse_deadline(
                    (card.get("dates") or {}).get("datePublished")
                )
                if published is not None and published < floor:
                    reached_floor = True
                    break
            if reason:
                break
            if reached_floor:
                done = True
                break
            total_pages = data.get("totalPages")
            if isinstance(total_pages, int) and page_no >= total_pages:
                done = True
                break
        else:
            reason = REASON_PAGE_CAP
        st["last_ok"] = done and not reason
        if st["last_ok"]:
            st["complete_at"] = self.now.isoformat(timespec="seconds")
        report = build_unit_report(
            "section",
            section,
            platform=PLATFORM,
            expected_pages=pages if st["last_ok"] else cap_expected_pages(None, pages),
            collected_pages=pages,
            reason=reason,
            visited=pages > 0,
        )
        self.unit_reports.append(report)
        if report["status"] != "complete":
            logger.warning(
                f"ТЭК-Торг ⚠ секция '{section}': страниц {pages} — "
                f"{reason_text(report['reason'])}"
            )


class TektorgParser:
    """Парсер JSON API ТЭК-Торга.

    Два режима (взаимоисключающие, выбираются в parse()):
      • keyword — задан keyword_phrases: по каждой фразе поиск (name=фраза)
        по профильным секциям; отбор только по статусу «Приём заявок»;
      • секционный — фраз нет: обход профильных секций без name «пока есть
        активные», с отбором по заказчику (client_inns по полю inn, иначе
        по совпадению organizerName с companies).

    Поиск мёртв (заглушка на первом же запросе) — оба режима берут свой
    отбор из TektorgOpenWalk: общего (open_walk от раннера) или своего.

    Интерфейс повторяет ZakupkiParser/BidzaarParser (browser_manager,
    days_back, публичный async-метод parse → список словарей тендеров).
    """

    def __init__(
        self,
        browser_manager,
        companies: list[str],
        days_back: int = 1,
        keyword_phrases: list[str] | None = None,
        client_inns: list[str] | None = None,
        transport=None,
        open_walk: TektorgOpenWalk | None = None,
    ):
        self.browser_manager = browser_manager  # не используется: сеть через urllib
        # Окно браузера для обхода антибота Variti (tektorg_transport.py).
        # None — прямой urllib (тесты и запасной путь); с 20.09.2026 боевой
        # обход без окна не проходит.
        self.transport = transport
        self.companies = companies or []
        self.days_back = days_back
        self.platform = PLATFORM
        # ИНН целевых заказчиков для секционного отбора (фильтра по ИНН
        # на стороне площадки нет — см. грабли в шапке файла).
        self.client_inns = {
            str(i).strip() for i in (client_inns or []) if str(i).strip()
        }
        self.keyword_phrases: list[str] = []
        if keyword_phrases:
            seen = set()
            for ph in keyword_phrases:
                if not isinstance(ph, str):
                    continue
                ph_s = ph.strip()
                if not ph_s or ph_s.casefold() in seen:
                    continue
                seen.add(ph_s.casefold())
                self.keyword_phrases.append(ph_s)
        # Общий обход открытого API (раннер даёт один на оба режима).
        self.open_walk = open_walk
        # Поиск площадки отдал заглушку на первом запросе прогона (16.10.2026).
        self.search_dead = False
        self._probe_pending = True
        # Отчёты о полноте по каждому объекту обхода (см. completeness.py):
        # обрыв сети или потолок страниц обязаны быть видны наружу, а не
        # только в логе (ТЗ-8).
        self.unit_reports: list[dict] = []
        # Была ли временная ошибка при последнем _load_page (ТЗ-11): её
        # читает _walk, чтобы замедлить темп. Ошибка могла быть отменена
        # повтором, но объект её уже словил — замедляемся и в этом случае.
        self._temp_error_seen = False
        # Сторож формата ответа (16.10.2026): API перестал присылать поле
        # `data`, и пустой ответ выглядел как «закупок нет, собрано целиком» —
        # ночь прошла с нулём без единого предупреждения. Запоминаем, получил
        # ли хоть один объект обхода поле `data`, и какие объекты его не видели.
        self._data_key_seen = False
        self._no_data_units: list[int] = []

    # ── Тело запроса ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_payload(
        sections: list[str], page: int, phrase: str | None = None
    ) -> dict:
        """params для getProcedures: секции, страница, sort=actual, limit≤50.

        Ключи секций передаются плоским списком sectionsCodes[0..N] —
        именно такой формат ожидает API (проверено живьём 31.08.2026).
        """
        params: dict = {}
        if phrase:
            params["name"] = phrase
        for i, code in enumerate(sections):
            params[f"sectionsCodes[{i}]"] = code
        params["page"] = page
        # sort строго "actual", limit строго ≤50 — другие значения ломают
        # ответ (total=null, data пуст). Не параметризируем сознательно.
        params["sort"] = SORT_ACTUAL
        params["limit"] = PAGE_LIMIT
        return {"params": params}

    # ── Цена и даты ─────────────────────────────────────────────────────────────

    _parse_price = staticmethod(_parse_price)
    _fmt_date = staticmethod(_fmt_date)
    _fmt_deadline = staticmethod(_fmt_deadline)

    # ── Отбор по заказчику (фильтра на площадке нет — см. шапку) ───────────────

    def _organizer_selected(self, lot: dict) -> bool:
        """Прошёл ли лот отбор по заказчику (ИНН либо название).

        ИНН надёжнее (в секции nornikel организаторы — десятки дочерних
        юрлиц с похожими названиями), поэтому при заданном client_inns
        отбор идёт только по нему.
        """
        if self.client_inns:
            return str(lot.get("inn") or "").strip() in self.client_inns
        organizer = clean_field(str(lot.get("organizerName") or ""))
        if not organizer:
            return False
        return any(
            TenderProParser._organizer_match(company, organizer)
            for company in self.companies
            if company
        )

    # ── Элемент data[] → словарь тендера ───────────────────────────────────────

    def _lot_to_tender(
        self, lot: dict, search_key: str, organizer_match: bool
    ) -> dict | None:
        return _tender_from(lot, search_key, organizer_match)

    # ── Загрузка страницы ───────────────────────────────────────────────────────

    async def _load_page(self, payload: dict) -> dict | None:
        """POST и разбор JSON с повтором ВРЕМЕННЫХ ошибок (ТЗ-11).

        Временная ошибка (429/5xx, таймаут, обрыв сети) повторяется по
        RETRY_DELAYS: одиночный 503 у ТЭК-Торга — троттлинг, а не падение,
        и не должен стоить целой секции за ночь. Постоянная (HTTP 4xx,
        битый JSON) не повторяется — повтор ничего не изменит.

        Прогон не роняем: что уже собрано по объекту обхода — вернём.
        Пометка self._temp_error_seen («объект словил временную ошибку»,
        даже отменённую повтором) читается _walk для замедления темпа.
        """
        self._temp_error_seen = False
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                if self.transport is not None:
                    raw = await self.transport.fetch(payload)
                else:
                    raw = await asyncio.to_thread(_fetch_procedures, payload)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("ответ не является JSON-объектом")
                if attempt:
                    logger.info(
                        f"ТЭК-Торг: повтор удался — страница загружена "
                        f"с попытки {attempt + 1}"
                    )
                return data
            except Exception as e:
                temporary = _is_temporary_error(e)
                if temporary:
                    self._temp_error_seen = True
                if not (temporary and attempt < len(RETRY_DELAYS)):
                    # Исчерпали повторы (или ошибка постоянная) — прежний
                    # warning с причиной: полнота считается как раньше.
                    logger.warning(
                        f"ТЭК-Торг: ошибка загрузки/разбора страницы ({e}){geo_hint(e)}"
                    )
                    return None
                # Retry-After надёжнее наших пауз — площадка сама говорит,
                # когда будет готова; но не дольше RETRY_AFTER_CAP.
                wait = _retry_after_seconds(e)
                if wait is None:
                    wait = RETRY_DELAYS[attempt]
                else:
                    wait = min(wait, RETRY_AFTER_CAP)
                logger.info(
                    f"ТЭК-Торг: {_error_code(e)} — повтор {attempt + 1} из "
                    f"{len(RETRY_DELAYS)} через {wait} с"
                )
                # Метод асинхронный: ждём именно asyncio.sleep, цикл
                # событий блокировать нельзя.
                await asyncio.sleep(wait)
        return None

    # ── Обход объекта (фраза или секция) ────────────────────────────────────────

    async def _walk(
        self,
        label: str,
        phrase: str | None,
        section: str | None,
        *,
        require_active: bool,
        select_organizer: bool,
    ) -> list[dict]:
        """Обход страниц одного объекта.

        label — что писать в лог/search_key (фраза либо код секции).
        require_active — секционный режим: стоп на первой странице без
        активных лотов (sort=actual не сортирует по дате — см. шапку).
        select_organizer — секционный режим: отбор по заказчику.
        """
        sections = [section] if section else list(PROFILE_SECTIONS)
        results: list[dict] = []
        seen_ids: set[str] = set()
        # Метрики полноты объекта (правила — в completeness.py): totalPages
        # ответа даёт ожидаемое число страниц, полнота считается по страницам.
        collected_pages = 0
        expected_pages: int | None = None
        reason = ""
        visited = True
        # После первой временной ошибки (даже отменённой повтором) листаем
        # этот объект вдвое медленнее до конца обхода: площадка троттлит
        # именно частые запросы, и прежний темп — напрашиваться на второй
        # 503. Флаг локальный, другие объекты не затрагиваются (ТЗ-11).
        slow_after_error = False
        no_data_key = False
        for page_no in range(1, MAX_PAGES + 1):
            if page_no > 1:
                # Не долбим площадку: пауза между страницами.
                pause = random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX)
                if slow_after_error:
                    pause *= 2.0
                await asyncio.sleep(pause)
            payload = self._build_payload(sections, page_no, phrase)
            logger.debug(f"ТЭК-Торг: '{label}' стр.{page_no}")
            resp = await self._load_page(payload)
            if self._temp_error_seen:
                slow_after_error = True
            if resp is None:
                # Сеть/битый JSON — прекращаем пагинацию объекта,
                # собранное не теряем. Ни одной страницы — до объекта не
                # дошли (skipped), иначе — partial с причиной network.
                if collected_pages == 0:
                    visited = False
                reason = REASON_NETWORK
                break
            if self._probe_pending:
                # Первый ответ прогона — проба поиска (16.10.2026): заглушка
                # без `data` и `total` значит «поиск мёртв», и обход уходит
                # в список лотов, не тратя запросы на остальные объекты.
                self._probe_pending = False
                if "data" not in resp and "total" not in resp:
                    self.search_dead = True
                    return []
            total_pages = resp.get("totalPages")
            if isinstance(total_pages, int) and total_pages >= 0:
                expected_pages = total_pages
            if "data" in resp:
                self._data_key_seen = True
            elif page_no == 1:
                no_data_key = True
            lots = resp.get("data") or []
            if not lots:
                # Пустая страница: площадка отдаёт всё, что есть, — значит,
                # обещанное число страниц здесь неактуально (пустая первая
                # страница с totalPages=1 — это «объект пуст», а не неполнота).
                expected_pages = collected_pages
                break
            collected_pages += 1
            active_on_page = 0
            added = 0
            for lot in lots:
                if str(lot.get("statusName") or "") != ACTIVE_STATUS:
                    continue
                active_on_page += 1
                if select_organizer and not self._organizer_selected(lot):
                    continue
                key = str(lot.get("id") or "")
                if key and key in seen_ids:
                    continue
                tender = self._lot_to_tender(
                    lot, search_key=label, organizer_match=select_organizer
                )
                if not tender:
                    continue
                if key:
                    seen_ids.add(key)
                results.append(tender)
                added += 1
            logger.debug(
                f"ТЭК-Торг: '{label}' стр.{page_no}: лотов {len(lots)}, "
                f"активных {active_on_page}, новых {added}"
            )
            # Секционный режим: страница без активных лотов — конец
            # интересных данных (sort=actual держит активные впереди).
            # Дальше только архив, который мы не собираем: по задаче
            # обхода всё собрано, totalPages всей секции тут ни при чём.
            if require_active and active_on_page == 0:
                expected_pages = collected_pages
                break
            # Выдача закончилась (keyword-режим: фраза даёт 1-2 страницы).
            if isinstance(total_pages, int) and page_no >= total_pages:
                break
        else:
            # for-else: исчерпали MAX_PAGES без естественной остановки —
            # в выдаче ещё могли остаться лоты. Это неполнота с причиной
            # page_cap, и она обязана быть видна наружу, а не только в логе.
            reason = REASON_PAGE_CAP
        report = build_unit_report(
            "section" if section else "phrase",
            label,
            platform=self.platform,
            expected_pages=expected_pages,
            collected_pages=collected_pages,
            reason=reason,
            visited=visited,
        )
        self.unit_reports.append(report)
        if no_data_key:
            self._no_data_units.append(len(self.unit_reports) - 1)
        if report["status"] == "partial":
            logger.warning(
                f"ТЭК-Торг ⚠ '{label}': собрано страниц {collected_pages} "
                f"из {expected_pages} — {reason_text(report['reason'])}"
            )
        return results

    def _flag_format_change(self) -> None:
        """Ни один объект не получил поле `data` — площадка сменила формат.

        Поодиночке ответ без лотов неотличим от пустой выдачи по фразе, но
        если так ответили ВСЕ объекты, включая секции, где лоты есть всегда,
        это не «закупок нет», а несовместимый ответ. Такие объекты помечаются
        «не обойдены» с причиной — сводка полноты и Excel покажут проблему,
        а не «собрано целиком».
        """
        if self._data_key_seen or not self._no_data_units:
            return
        for index in self._no_data_units:
            old = self.unit_reports[index]
            self.unit_reports[index] = build_unit_report(
                old["kind"],
                old["label"],
                platform=self.platform,
                expected_pages=None,
                collected_pages=0,
                reason=REASON_FORMAT_CHANGED,
                visited=False,
            )
        logger.warning(
            f"ТЭК-Торг ⚠ ответ API без списка лотов у всех "
            f"{len(self._no_data_units)} объектов обхода — площадка изменила "
            f"формат ответа, лоты не получены"
        )

    # ── Обход списка лотов, когда поиск мёртв ──────────────────────────────────

    async def _parse_open(self) -> list[dict]:
        """Свой отбор из обхода открытого API (общего или собственного)."""
        walk = self.open_walk
        own = walk is None
        if own:
            keyword = bool(self.keyword_phrases)
            walk = TektorgOpenWalk(
                phrases=self.keyword_phrases,
                client_inns=() if keyword else self.client_inns,
                companies=() if keyword else self.companies,
                transport=self.transport,
            )
        walk.search_dead = True
        await walk.run()
        if own:
            # Общий обход отчитывается раннеру сам — иначе отчёты задвоятся.
            self.unit_reports.extend(walk.unit_reports)
        mode = "phrase" if self.keyword_phrases else "section"
        return self._dedup(walk.take(mode))

    # ── Дедупликация (конвенция TenderProParser._dedup) ────────────────────────

    @staticmethod
    def _dedup(tenders: list[dict]) -> list[dict]:
        seen, out = set(), []
        for t in tenders:
            key = t.get("lot_id") or t.get("href", "").split("#")[0]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    # ── Точка входа ────────────────────────────────────────────────────────────

    async def parse(self) -> list[dict]:
        if self.open_walk is not None and self.open_walk.search_dead:
            # Другой режим этого прогона уже выяснил, что поиск мёртв.
            if self.keyword_phrases or self.client_inns or self.companies:
                return await self._parse_open()
            return []
        if self.keyword_phrases:
            # Режим keyword: фраза + профильные секции, отбор только по
            # статусу (organizer_match=False — искали по фразе, а не по
            # организатору, как в keyword-режимах остальных площадок).
            logger.info(
                f"ТЭК-Торг: keyword-режим, {len(self.keyword_phrases)} фраз "
                f"по {len(PROFILE_SECTIONS)} профильным секциям"
            )
            all_tenders: list[dict] = []
            for phrase in self.keyword_phrases:
                logger.info(f"ТЭК-Торг → фраза: '{phrase}'")
                try:
                    res = await self._walk(
                        phrase,
                        phrase=phrase,
                        section=None,
                        require_active=False,
                        select_organizer=False,
                    )
                    if res:
                        logger.info(f"ТЭК-Торг: фраза '{phrase}' → {len(res)} тендеров")
                    all_tenders.extend(res)
                except Exception as e:
                    logger.warning(f"ТЭК-Торг '{phrase}': {e}")
                if self.search_dead:
                    return await self._parse_open()
            self._flag_format_change()
            deduped = self._dedup(all_tenders)
            logger.info(
                f"ТЭК-Торг: итого {len(deduped)} (до дедупа {len(all_tenders)})"
            )
            return deduped

        # Секционный режим: отбор по заказчику на нашей стороне (фильтра
        # по ИНН/заказчику в API нет — см. грабли в шапке файла).
        if self.client_inns:
            logger.info(
                f"ТЭК-Торг: секционный режим, отбор по {len(self.client_inns)} ИНН"
            )
        elif self.companies:
            logger.info(
                f"ТЭК-Торг: секционный режим, отбор по {len(self.companies)} "
                f"названиям компаний"
            )
        else:
            logger.warning(
                "ТЭК-Торг: нет фраз, ИНН и компаний для отбора — этап пропущен "
                "(без отбора секция отдаёт всю ленту, фильтра по заказчику "
                "на площадке нет)"
            )
            return []
        all_tenders = []
        for section in PROFILE_SECTIONS:
            try:
                res = await self._walk(
                    section,
                    phrase=None,
                    section=section,
                    require_active=True,
                    select_organizer=True,
                )
                if res:
                    logger.info(f"ТЭК-Торг: секция '{section}' → {len(res)} тендеров")
                all_tenders.extend(res)
            except Exception as e:
                logger.warning(f"ТЭК-Торг секция '{section}': {e}")
            if self.search_dead:
                return await self._parse_open()
        self._flag_format_change()
        deduped = self._dedup(all_tenders)
        logger.info(f"ТЭК-Торг: итого {len(deduped)} (до дедупа {len(all_tenders)})")
        return deduped
