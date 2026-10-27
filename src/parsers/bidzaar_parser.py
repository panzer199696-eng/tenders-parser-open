"""
Bidzaar (bidzaar.com) parser — агрегатор закупок России, открытый JSON API.

Bidzaar собирает закупки со всех площадок страны разом. Разведка
03.09.2026 по шестнадцати фразам номенклатуры дала 498 лотов, многих из
которых нет на корпоративных площадках. Один этот парсер даёт больше охвата,
чем все корпоративные площадки вместе: ни входа, ни капчи, ни браузера.

Рабочий путь (проверен живьём 03.09.2026, обычным urllib без куки/сессии):

    GET https://bidzaar.com/api/process-aggregator/tenders/filter
        ?paging.page=1
        &paging.size=100          (потолок size — 200)
        &search=<фраза, необязательно>
        &sorting.key=acceptanceStartDate
        &sorting.direction=desc

    Заголовки: User-Agent браузера (ОБЯЗАТЕЛЕН), Accept: application/json

ГРАБЛИ, проверенные живьём (не «починить», а обойти — API такой):
  • Параметр поиска называется именно `search`. Варианты searchText, query,
    filter.name, name молча игнорируются и возвращают всю базу целиком
    (105 тысяч лотов). Это опасная ловушка: опечатка в имени параметра
    даёт не ошибку, а мусор, — поэтому имя параметра зафиксировано
    константой SEARCH_PARAM и не параметризируется.
  • Фильтра по заказчику нет: customerIds, companyIds, filters.companyIds
    тоже молча игнорируются. Адресный обход по заказчику делается тем же
    `search` по названию заказчика (≈100 лотов на крупного) — работает.
  • Карточка лота живёт по адресу /app/process/light/<procedureId>, а НЕ по
    полю id. Проверено кликом по строке списка 04.09.2026: id ведёт на
    несуществующую страницу, и SPA отдаёт на неё 200 с пустым телом — вот
    почему подмена не заметна обычным запросом. procedureId заполнен у всех
    ста лотов свежей выдачи; если он вдруг пуст, ссылку не выдумываем
    (лучше пустая, чем ведущая в 404).
  • историческая заметка: procedureId в выдаче часто пуст и для ссылки НЕ годится: ссылка лота
    строится из id — https://bidzaar.com/app/requests/external/<id>.
  • Цены в выдаче нет — это нормально, профиль
    умеет работать без цены (include_no_price).
  • Поиск морфологический и нестрогий (фраза из двух слов приносит и
    лоты, где совпало одно). Отсев — работа профиля, парсер по смыслу не
    фильтрует.

Живая проверка 16.10.2026 (после того как с 13.10 новых лотов почти не стало):
  • формат и параметры прежние, парсер ни при чём;
  • выдача отдаёт только лоты с открытым приёмом (вся база — 31 тыс., а не
    105 тыс.; у всех status=1), сортировка — сперва свои лоты Bidzaar, потом
    чужие (isExternal), внутри — по acceptanceStartDate desc;
  • чужие лоты агрегатор почти перестал подтягивать: по «поставка» 11.10 —
    96, 12.10 — 7, 13.10 — 4, 14.10 — 3, 15.10 — 0; свежий лот другой
    площадки от 14.10 на Bidzaar не находится. Наши фразы ловят в основном
    чужие лоты — отсюда ноль новых. Сторож свежести (_guard_platform) теперь
    говорит об этом в отчёте, а не «собрано целиком».

Даты приходят в UTC (суффикс Z). Дедлайн обязательно переводится в
московское время: лот с дедлайном в 02:00 МСК иначе показывается
вчерашним днём и уезжает в «Срок истёк» раньше времени.

Сеть — urllib.request через asyncio.to_thread (как в tektorg_parser.py),
чтобы не блокировать цикл: браузер не нужен. browser_manager принимается
для единообразия с остальными парсерами, но не используется.

Словарь тендера совместим с остальными парсерами (platform, name,
tender_no, company, href, sum, сроки). Сверка организатора — общая _common.organizer_match
(по целым словам, свой подход не изобретаем).
"""

import asyncio
import json
import logging
import math
import random
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from ._common import clean_field, looks_blocked, organizer_match, stale_since
from .completeness import (
    KIND_PLATFORM,
    REASON_BLOCKED,
    REASON_CRASHED,
    REASON_FORMAT_CHANGED,
    REASON_NETWORK,
    REASON_PAGE_CAP,
    REASON_STALE,
    build_unit_report,
    failed_unit,
    platform_unit,
    reason_text,
)
from .tender_pro_parser import geo_hint

from ..dates import today_msk
from ..net_ssl import ssl_context_ru

logger = logging.getLogger(__name__)

PLATFORM = "Bidzaar"
API_URL = "https://bidzaar.com/api/process-aggregator/tenders/filter"

# Имя параметра поиска — строго "search" (см. грабли в шапке файла):
# опечатка здесь не падает, а молча возвращает всю базу в 105 тысяч лотов.
SEARCH_PARAM = "search"

# Потолок страниц на один ключ поиска: 20 × 100 = 2000 лотов. По фразам
# номенклатуры и названию заказчика выдача заметно меньше, потолок нужен
# как предохранитель от зависшей пагинации.
MAX_PAGES = 20
# Размер страницы: площадка принимает до 200, но 100 держит стабильно и
# не выглядит для неё как скрейпинг всей базы.
PAGE_SIZE = 100

# Пауза между запросами страниц: не долбим площадку (лимиты вежливости).
PAGE_DELAY_MIN = 1.0
PAGE_DELAY_MAX = 2.0

REQUEST_TIMEOUT = 30

# Обычный UA браузера: без него площадка не отвечает (проверено живьём).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Карточка лота: /app/process/light/<procedureId>. Через id страница
# отдаёт 404 (проверено 04.09.2026 — ссылки в выгрузке не открывались).
LOT_HREF = "https://bidzaar.com/app/process/light/"

# Московское время — фиксированное смещение +3 (у Москвы нет перехода на
# летнее время), переводим даты UTC именно в него: дедлайны показываются
# и сравниваются по МСК.
MSK_TZ = timezone(timedelta(hours=3))

# Статус лота в выдаче. Живьём 16.10.2026 все ~1000 просмотренных лотов
# (свои и чужие, по фразам и без) были со status=1 и пустым finishDate, а
# выдача отдаёт только лоты с ещё открытым приёмом заявок. Берём только
# status=1 без finishDate: остальное — завершённое или отменённое. Если
# площадка поменяет коды и отсев съест всё — это смена формата (см. parse).
ACTIVE_STATUS = 1

# Коды ответа «не пускаем»: запрет доступа и ограничение частоты.
BLOCKED_HTTP_CODES = (401, 403, 429)


def _fetch_tenders(params: dict) -> str:
    """GET к JSON API Bidzaar (синхронная; вызывается через to_thread)."""
    req = urllib.request.Request(
        f"{API_URL}?{urlencode(params)}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    # У Bidzaar зарубежный CDN, но сертификат может быть подписан и через
    # российский НУЦ — контекст из net_ssl доверяет системным корням И
    # российским, проверка цепочки остаётся включённой (как у ТЭК-Торга).
    ctx = ssl_context_ru()
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")



# Как устроены ссылки Bidzaar (разобрано 04.09.2026 по жалобе «все ссылки
# дают 404»). Агрегатор смешивает два вида лотов:
#
#   • свои — размещённые на самом Bidzaar. У них заполнен procedureId, и
#     карточка живёт по адресу /app/process/light/<procedureId>. Таких мало:
#     на наших запросах около одного процента.
#   • чужие (isExternal=true) — собранные агрегатором с других площадок. У
#     них procedureId пуст, своей карточки на Bidzaar нет вовсе, а в поле
#     externalId лежит номер процедуры ПЕРВОИСТОЧНИКА. Именно туда и надо
#     вести человека: там документация и подача заявки.
#
# Первоисточник узнаём по виду номера:
#   6–8 цифр           → B2B Center (проверено: карточка открывается)
#   19 цифр            → ЕИС, госзакупки
#   остальное          → площадка неизвестна, ссылку не выдумываем
B2B_VIEW = "https://www.b2b-center.ru/market/view.html?id="
EIS_SEARCH = (
    "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString="
)


def build_lot_href(procedure_id: str, external_id: str) -> str:
    """Ссылка на лот: своя карточка Bidzaar либо первоисточник по номеру."""
    if procedure_id:
        return f"{LOT_HREF}{procedure_id}"
    number = (external_id or "").strip()
    if number.isdigit() and 6 <= len(number) <= 8:
        return f"{B2B_VIEW}{number}"
    if number.isdigit() and len(number) == 19:
        return f"{EIS_SEARCH}{number}"
    # Площадку по номеру не опознали. Пустая ссылка честнее ведущей в 404:
    # номер процедуры в выгрузке есть, по нему лот ищется руками.
    return ""

class BidzaarParser:
    """Парсер JSON API Bidzaar.

    Два режима, работают ОБА СРАЗУ (как на ТЭК-Торге и Росэлторге, а не
    «или-или»), потому что дополняют друг друга:
      • фразы номенклатуры — search=<фраза> для каждой включённой фразы;
      • адресный обход по заказчикам — search=<название клиента>: фильтра
        по заказчику у API нет (см. шапку), поэтому выдача помечается
        search_key=<название> и organizer_match=True только если клиент
        действительно встречается в company.name (сверка по целым словам
        через _common.organizer_match — поиск-то нестрогий и тянет чужие лоты,
        где заказчик лишь упомянут в названии).

    Интерфейс повторяет остальные парсеры (browser_manager, companies,
    days_back, публичный async-метод parse → список словарей тендеров).
    """

    def __init__(
        self,
        browser_manager,
        companies: list[str],
        days_back: int = 1,
        keyword_phrases: list[str] | None = None,
        client_names: list[str] | None = None,
        today: date | None = None,
    ):
        self.browser_manager = browser_manager  # не используется: сеть через urllib
        # «Сегодня» для сторожа свежести ленты (тесты подставляют дату
        # фикстуры); по умолчанию — московская дата прогона.
        self.today = today
        self.companies = companies or []
        self.days_back = days_back
        self.platform = PLATFORM
        self.keyword_phrases = self._clean_list(keyword_phrases)
        # Названия заказчиков для адресного обхода. Если не переданы явно
        # — используем companies (тот же список закреплённых клиентов).
        if client_names is None:
            client_names = self.companies
        self.client_names = self._clean_list(client_names)
        # Отчёты о полноте по каждому ключу поиска (см. completeness.py):
        # упор в потолок страниц или обрыв сети обязаны быть видны наружу,
        # а не только в логе (ТЗ-8).
        self.unit_reports: list[dict] = []
        # Сторож площадки (В6, 16.10.2026): с 13.10 агрегатор перестал
        # подтягивать лоты других площадок, а отчёт каждую ночь писал
        # «собрано целиком» на ~600 старых лотах. Копим за прогон: свежайшую
        # дату публикации и сколько лотов отсеял статус.
        self._newest_published: datetime | None = None
        self._lots_seen = 0
        self._lots_inactive = 0

    @staticmethod
    def _clean_list(values: list[str] | None) -> list[str]:
        """Убрать пустые и повторы, сохранив порядок (он задаёт приоритет)."""
        out, seen = [], set()
        for value in values or []:
            if not isinstance(value, str):
                continue
            text = value.strip()
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    # ── Тело запроса ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_params(search: str | None, page: int) -> dict:
        """Параметры запроса: поиск, страница, сортировка по дате публикации.

        Имя параметра поиска — строго SEARCH_PARAM="search": варианты
        searchText/query/name молча игнорируются (см. шапку файла).
        Сортировка acceptanceStartDate desc — свежие лоты первыми, чтобы
        при срезе MAX_PAGES терялись старые, а не новые.
        """
        params: dict = {
            "paging.page": page,
            "paging.size": PAGE_SIZE,
            "sorting.key": "acceptanceStartDate",
            "sorting.direction": "desc",
        }
        if search:
            params[SEARCH_PARAM] = search
        return params

    # ── Даты: UTC → московское время ───────────────────────────────────────────

    @staticmethod
    def _parse_utc(iso_text) -> datetime | None:
        """ISO 8601 UTC («2026-09-04T09:00:00Z») → datetime в МСК (или None).

        Без таймзоны считаем UTC: площадка всегда отдаёт Z (проверено
        живьём), а «наивное» сравнение с локальным временем ПК увезло бы
        дедлайн на часовой пояс машины пользователя.
        """
        if not iso_text or not isinstance(iso_text, str):
            return None
        try:
            # fromisoformat в Python < 3.11 не понимает суффикс Z —
            # заменяем на явное смещение UTC.
            dt = datetime.fromisoformat(iso_text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK_TZ)

    @classmethod
    def _fmt_published(cls, iso_text) -> str:
        """acceptanceStartDate → «ДД.ММ.ГГГГ» (московское время).

        Нет поля / не разобралось → «Не указана», не выдумываем дату.
        Время отбрасываем: у даты публикации в конвенции проекта времени
        нет, важен только день.
        """
        dt = cls._parse_utc(iso_text)
        return dt.strftime("%d.%m.%Y") if dt else "Не указана"

    @classmethod
    def _fmt_deadline(cls, iso_text) -> str:
        """acceptanceEndDate → «ДД.ММ.ГГГГ ЧЧ:ММ» по Москве (или «Не указана»).

        Время здесь обязательно: дедлайн «2026-09-04T09:00:00Z» — это
        12:00 по Москве, и без перевода лот с дедлайном в 02:00 МСК
        показался бы вчерашним днём и уехал в «Срок истёк» раньше срока.
        """
        dt = cls._parse_utc(iso_text)
        return dt.strftime("%d.%m.%Y %H:%M") if dt else "Не указана"

    # ── Элемент items[] → словарь тендера ──────────────────────────────────────

    def _lot_to_tender(
        self, lot: dict, search_key: str, organizer_match: bool
    ) -> dict | None:
        try:
            lot_id = str(lot.get("id") or "")
            procedure_id = str(lot.get("procedureId") or "").strip()
            company = str((lot.get("company") or {}).get("name") or "")
            return {
                "platform": self.platform,
                "name": clean_field(str(lot.get("name") or "")) or "Без названия",
                "tender_no": str(lot.get("externalId") or ""),
                "tender_type": "",  # в выдаче агрегатора типа процедуры нет
                "lot_id": lot_id,
                "company": clean_field(company),
                "search_key": search_key,
                "organizer_href": None,
                "organizer_match": organizer_match,
                # Своя карточка — по procedureId, у агрегированных лотов —
                # первоисточник по номеру процедуры (см. build_lot_href).
                "href": build_lot_href(procedure_id, str(lot.get("externalId") or "")),
                "sum": None,  # цену площадка не отдаёт — профиль умеет без неё
                "date_published": self._fmt_published(lot.get("acceptanceStartDate")),
                "date_deadline": self._fmt_deadline(lot.get("acceptanceEndDate")),
                "date_to_display": self._fmt_deadline(lot.get("acceptanceEndDate")),
            }
        except Exception as e:
            logger.debug(f"Bidzaar: разбор лота не удался: {e}")
            return None

    # ── Загрузка страницы ───────────────────────────────────────────────────────

    async def _load_page(self, params: dict) -> tuple[dict | None, str]:
        """GET и разбор JSON → (ответ, причина отказа).

        Причина различает три беды, которые раньше все были «обрывом связи»:
        сеть не ответила (повтор поможет), площадка не пускает (403/429,
        страница капчи — нужен человек) и ответ не того вида (пустой 200,
        не JSON, нет items/totalCount — нужна правка программы). Прогон не
        роняем: что уже собрано по ключу поиска — вернём.
        """
        try:
            raw = await asyncio.to_thread(_fetch_tenders, params)
        except urllib.error.HTTPError as e:
            reason = REASON_BLOCKED if e.code in BLOCKED_HTTP_CODES else REASON_NETWORK
            logger.warning(f"Bidzaar: ошибка загрузки/разбора страницы (HTTP {e.code})")
            return None, reason
        except Exception as e:
            logger.warning(f"Bidzaar: ошибка загрузки/разбора страницы ({e}){geo_hint(e)}")
            return None, REASON_NETWORK
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("ответ не является JSON-объектом")
        except ValueError as e:
            reason = REASON_BLOCKED if looks_blocked(raw) else REASON_FORMAT_CHANGED
            logger.warning(
                f"Bidzaar: ошибка загрузки/разбора страницы ({e}) — "
                f"{reason_text(reason)}"
            )
            return None, reason
        if not isinstance(data.get("items"), list) or not isinstance(
            data.get("totalCount"), int
        ):
            logger.warning(
                f"Bidzaar: в ответе нет items/totalCount (ключи: "
                f"{sorted(data)[:10]}) — {reason_text(REASON_FORMAT_CHANGED)}"
            )
            return None, REASON_FORMAT_CHANGED
        return data, ""

    def _is_active(self, lot: dict) -> bool:
        """Лот принимает заявки: status=1 и не завершён (см. ACTIVE_STATUS)."""
        return lot.get("status") == ACTIVE_STATUS and not lot.get("finishDate")

    def _note_published(self, lot: dict) -> None:
        """Запомнить дату публикации для сторожа свежести.

        Даты из будущего не считаем: живьём у чужих лотов встречалось
        acceptanceStartDate 31.12.2026 — это не публикация.
        """
        at = self._parse_utc(lot.get("acceptanceStartDate"))
        if at is None or at > datetime.now(MSK_TZ):
            return
        if self._newest_published is None or at > self._newest_published:
            self._newest_published = at

    # ── Обход одного ключа поиска (фраза или название заказчика) ────────────────

    async def _walk(
        self, label: str, search: str, organizer_match_mode: bool
    ) -> list[dict]:
        """Обход страниц одного ключа поиска.

        label — что писать в search_key (фраза либо название заказчика).
        organizer_match_mode — режим заказчика: organizer_match считается
        честной сверкой label с company.name (поиск нестрогий и может
        привести чужой лот, где клиент лишь упомянут в названии).
        """
        results: list[dict] = []
        # Метрики полноты для этого ключа (правила — в completeness.py):
        # полнота считается по страницам, totalCount даёт ожидаемое число.
        collected_pages = 0
        expected_pages: int | None = None
        reason = ""
        visited = True
        for page_no in range(1, MAX_PAGES + 1):
            if page_no > 1:
                # Не долбим площадку: пауза между страницами.
                await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))
            params = self._build_params(search, page_no)
            logger.debug(f"Bidzaar: '{label}' стр.{page_no}")
            resp, failure = await self._load_page(params)
            if resp is None:
                # Отказ — прекращаем пагинацию ключа, собранное не теряем.
                # Ни одной страницы — до объекта не дошли вовсе (skipped),
                # иначе — partial; причина — какой именно был отказ.
                if collected_pages == 0:
                    visited = False
                reason = failure
                break
            total = resp["totalCount"]
            if total >= 0:
                # Площадка обещает totalCount лотов: ожидаемое число страниц —
                # ceil(total / PAGE_SIZE); при total=0 это 0 (обошли всё).
                expected_pages = math.ceil(total / PAGE_SIZE)
            items = resp["items"]
            if not items:
                break
            recognized = 0
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                recognized += 1
                self._lots_seen += 1
                self._note_published(item)
                if not self._is_active(item):
                    self._lots_inactive += 1
                    continue
                # Сверка с ТЕМ клиентом, по которому нашли лот: у нестрогого
                # поиска чужие лоты обязаны получить честный False.
                org_match_item = False
                if organizer_match_mode:
                    # Сверка с ТЕМ клиентом, по которому нашли лот: у
                    # нестрогого поиска чужие лоты обязаны получить
                    # честный False.
                    org_match_item = organizer_match(
                        label, str((item.get("company") or {}).get("name") or "")
                    )
                tender = self._lot_to_tender(
                    item, search_key=label, organizer_match=org_match_item
                )
                if tender:
                    results.append(tender)
            if not recognized:
                # Лоты на странице есть, а узнать ни один не вышло — площадка
                # переименовала поля, а не «закупок нет».
                logger.warning(
                    f"Bidzaar: '{label}' стр.{page_no}: лотов {len(items)}, "
                    f"распознано 0 — {reason_text(REASON_FORMAT_CHANGED)}"
                )
                if collected_pages == 0:
                    visited = False
                reason = REASON_FORMAT_CHANGED
                break
            collected_pages += 1
            logger.debug(
                f"Bidzaar: '{label}' стр.{page_no}: лотов {len(items)}, всего {total}"
            )
            # Выдача закончилась по счётчику площадки.
            if isinstance(total, int) and page_no * PAGE_SIZE >= total:
                break
        else:
            # for-else: цикл исчерпал MAX_PAGES без естественной остановки —
            # в выдаче ещё могли остаться лоты. Молча терять нельзя: это
            # неполнота с причиной page_cap, и она обязана быть видна наружу.
            reason = REASON_PAGE_CAP
            logger.warning(
                f"Bidzaar: '{label}': достигнут потолок обхода "
                f"({MAX_PAGES} стр. × {PAGE_SIZE} лотов) — возможно, часть "
                f"выдачи не собрана"
            )
        report = build_unit_report(
            "company" if organizer_match_mode else "phrase",
            label,
            platform=self.platform,
            expected_pages=expected_pages,
            collected_pages=collected_pages,
            reason=reason,
            visited=visited,
        )
        self.unit_reports.append(report)
        if report["status"] == "partial":
            logger.warning(
                f"Bidzaar ⚠ '{label}': собрано страниц {collected_pages} "
                f"из {expected_pages} — {reason_text(report['reason'])}"
            )
        return results

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

    # ── Сторож площадки ────────────────────────────────────────────────────────

    def _guard_platform(self) -> None:
        """Отчёты о площадке целиком: формат сменился / лента застыла.

        • все обойдённые ключи отказали по формату — площадка отвечает не тем;
        • лоты были, но отсев по статусу съел все — коды статуса сменились;
        • свежайшая публикация старше двух рабочих дней — лента не
          обновляется (в отчёте «собрано целиком» это было не видно).
        """
        own = [r for r in self.unit_reports if r.get("kind") != KIND_PLATFORM]
        if own and all(r.get("reason") == REASON_FORMAT_CHANGED for r in own):
            self.unit_reports.append(platform_unit(self.platform, REASON_FORMAT_CHANGED))
        elif self._lots_seen and self._lots_inactive == self._lots_seen:
            logger.warning(
                f"Bidzaar ⚠ все {self._lots_seen} лотов отсеяны по статусу — "
                f"{reason_text(REASON_FORMAT_CHANGED)}"
            )
            self.unit_reports.append(platform_unit(self.platform, REASON_FORMAT_CHANGED))
        if self._newest_published is None:
            return
        today = self.today or today_msk()
        newest = self._newest_published.date()
        logger.info(f"Bidzaar: свежайшая публикация в выдаче — {newest:%d.%m.%Y}")
        if newest < stale_since(today):
            logger.warning(
                f"Bidzaar ⚠ свежайший лот опубликован {newest:%d.%m.%Y} — "
                f"{reason_text(REASON_STALE)}"
            )
            self.unit_reports.append(platform_unit(self.platform, REASON_STALE))

    # ── Точка входа ────────────────────────────────────────────────────────────

    async def parse(self) -> list[dict]:
        if not self.keyword_phrases and not self.client_names:
            logger.warning(
                "Bidzaar: нет ни фраз номенклатуры, ни заказчиков — этап пропущен"
            )
            return []
        all_tenders: list[dict] = []

        # Режим 1: фразы номенклатуры. organizer_match=False — искали по
        # фразе, а не по организатору (как в keyword-режимах остальных
        # площадок); отсев нестрогих совпадений — работа профиля.
        for phrase in self.keyword_phrases:
            logger.info(f"Bidzaar → фраза: '{phrase}'")
            try:
                res = await self._walk(phrase, search=phrase, organizer_match_mode=False)
                if res:
                    logger.info(f"Bidzaar: фраза '{phrase}' → {len(res)} тендеров")
                all_tenders.extend(res)
            except Exception as e:
                # Объект без отчёта в сводке выглядит так, будто его и не было.
                logger.exception(f"Bidzaar '{phrase}': {e}")
                self.unit_reports.append(
                    failed_unit("phrase", phrase, self.platform, REASON_CRASHED)
                )

        # Режим 2: адресный обход по заказчикам. Фильтра по companyIds у API
        # нет (см. шапку), поэтому ищем названием через тот же search.
        for client in self.client_names:
            logger.info(f"Bidzaar → заказчик: '{client}'")
            try:
                res = await self._walk(client, search=client, organizer_match_mode=True)
                if res:
                    logger.info(f"Bidzaar: заказчик '{client}' → {len(res)} тендеров")
                all_tenders.extend(res)
            except Exception as e:
                logger.exception(f"Bidzaar '{client}': {e}")
                self.unit_reports.append(
                    failed_unit("company", client, self.platform, REASON_CRASHED)
                )

        self._guard_platform()
        deduped = self._dedup(all_tenders)
        logger.info(
            f"Bidzaar: итого {len(deduped)} (до дедупа {len(all_tenders)})"
        )
        return deduped
