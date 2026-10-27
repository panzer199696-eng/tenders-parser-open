"""
ТЭК-Торг: обход через открытый API, когда поиск площадки мёртв (16.10.2026).

С ночи 16.10.2026 поиск getProcedures на любые параметры отдаёт заглушку
`{"totalPages":1,"currentPage":1,"limitPage":N,"hash":…}` — без `data` и
`total`. Работают список лотов секции (`/open-api/lots`, только id) и
карточка процедуры (`/open-api/procedures/{id}`). Парсер пробует старый путь
первым и при заглушке переключается на обход «список → карточки» в том же
прогоне.

Фикстуры — живые ответы разведки 16.10.2026 (tests/fixtures/tektorg_open_*):
  • tektorg_open_lots_nornikel.json — стр.1 списка лотов nornikel (50 шт.);
  • tektorg_open_card_nornikel.json — карточка Северметалла без etpLink;
  • tektorg_open_card_zakupki.json — карточка zakupki с etpLink и ценой;
  • tektorg_open_card_old2021.json — карточка 2021 года, статус всё ещё
    «Приём заявок», срок давно истёк;
  • tektorg_open_search_stub.json — заглушка мёртвого поиска;
  • tektorg_open_422.json — ответ API на неверные параметры.

Сеть подменяется: _fetch_procedures (старый поиск) и _fetch_get (открытый API).
"""

import asyncio
import copy
import json
import logging
import sqlite3
import urllib.error
from datetime import datetime
from email.message import Message
from pathlib import Path

import pytest

import src.parsers.tektorg_parser as mod
from src.parsers import completeness
from src.parsers.tektorg_parser import TektorgOpenWalk, TektorgParser

FIXTURES = Path(__file__).parent / "fixtures"

# «Сейчас» для всех тестов: карточки фикстур опубликованы 15–16.10.2026.
NOW = datetime(2026, 10, 16, 12, 0)

INN_NORNICKEL = "7700000001"
INN_OTHER = "5405975847"  # НРСК-Сибирь из карточки zakupki — не наш заказчик


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


STUB = _load("tektorg_open_search_stub.json")
CARD_NORNIKEL = json.loads(_load("tektorg_open_card_nornikel.json"))
CARD_ZAKUPKI = json.loads(_load("tektorg_open_card_zakupki.json"))
CARD_OLD = json.loads(_load("tektorg_open_card_old2021.json"))
LOTS_NORNIKEL = json.loads(_load("tektorg_open_lots_nornikel.json"))


def card(
    pid,
    *,
    title="Поставка запчастей",
    inn=INN_NORNICKEL,
    published="2026-10-16T09:00:00+03:00",
    deadline="2026-10-22T20:00:00+03:00",
    status="Приём заявок",
    subject=None,
    base=None,
):
    """Синтетическая карточка процедуры на основе живой карточки Северметалла."""
    c = copy.deepcopy(base or CARD_NORNIKEL)
    c["id"] = pid
    c["title"] = title
    c["inn"] = inn
    c["statusName"] = status
    c["statusLots"] = [status]
    c["dates"] = {"datePublished": published, "dateEndRegistration": deadline}
    c["lots"][0]["subject"] = subject if subject is not None else title
    c["lots"][0]["statusName"] = status
    return c


def page(pids, total_pages=99, total=None):
    """Страница списка лотов: только id лота и id процедуры, как у API."""
    return {
        "total": total if total is not None else total_pages * 50,
        "totalPages": total_pages,
        "currentPage": 1,
        "limitPage": 50,
        "data": [{"id": 30000000 + i, "procedure_id": p} for i, p in enumerate(pids)],
        "hash": "x",
    }


def http_error(code, retry_after=None):
    hdrs = Message()
    if retry_after:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://api.tektorg.ru", code, "x", hdrs, None)


class FakeApi:
    """Открытый API: списки по (секция, страница), карточки по id.

    Значение — dict (отдаётся JSON), str (тело как есть), Exception
    (бросается) или список таких значений — сценарий по попыткам.
    Нет ключа — пустая страница / 404.
    """

    def __init__(self, lists=None, cards=None):
        self.lists = lists or {}
        self.cards = cards or {}
        self.calls: list[str] = []
        self._attempts: dict[str, int] = {}

    def _pick(self, key, value):
        if isinstance(value, list) and value and not isinstance(value[0], dict):
            n = self._attempts.get(key, 0)
            self._attempts[key] = n + 1
            value = value[min(n, len(value) - 1)]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def __call__(self, url):
        self.calls.append(url)
        if "/lots?" in url:
            sec = url.split("sectionsCodes[0]=")[1].split("&")[0]
            pg = int(url.split("page=")[1].split("&")[0])
            key = f"{sec}:{pg}"
            value = self.lists.get(sec, {}).get(pg, page([], total_pages=pg))
            return self._pick(key, value)
        pid = int(url.rsplit("/", 1)[1])
        if pid not in self.cards:
            raise http_error(404)
        return self._pick(str(pid), self.cards[pid])

    def card_calls(self):
        return [u for u in self.calls if "/procedures/" in u]

    def list_calls(self):
        return [u for u in self.calls if "/lots?" in u]


@pytest.fixture
def api(monkeypatch):
    """Подменить сеть: старый поиск — заглушка, открытый API — FakeApi."""
    fake = FakeApi()
    search_calls = {"n": 0}

    def dead_search(payload):
        search_calls["n"] += 1
        return STUB

    monkeypatch.setattr(mod, "_fetch_procedures", dead_search)
    monkeypatch.setattr(mod, "_fetch_get", fake)
    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    monkeypatch.setattr(mod, "PROFILE_SECTIONS", ("nornikel",))
    monkeypatch.setattr(mod, "watch_names", list)
    monkeypatch.setattr(mod, "now_msk", lambda: NOW)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    fake.search_calls = search_calls
    return fake


def _walk(**kw):
    kw.setdefault("phrases", [])
    kw.setdefault("client_inns", [INN_NORNICKEL])
    kw.setdefault("companies", [])
    kw.setdefault("now", NOW)
    return TektorgOpenWalk(**kw)


def _parser(open_walk=None, **kw):
    kw.setdefault("companies", [])
    return TektorgParser(None, days_back=1, open_walk=open_walk, **kw)


def _section_reports(walk):
    return {r["label"]: r for r in walk.unit_reports if r["kind"] == "section"}


# ── Переключение старый путь ↔ новый ─────────────────────────────────────────


def test_заглушка_поиска_переключает_на_обход_списка(api, caplog):
    api.lists = {"nornikel": {1: page([101, 102], total_pages=1)}}
    api.cards = {101: card(101), 102: card(102, inn=INN_OTHER)}
    parser = _parser(client_inns=[INN_NORNICKEL])
    with caplog.at_level(logging.INFO, logger="src.parsers.tektorg_parser"):
        tenders = asyncio.run(parser.parse())
    assert [t["lot_id"] for t in tenders] == ["101"]
    # Старый путь стоил один запрос — дальше не долбим мёртвый поиск.
    assert api.search_calls["n"] == 1
    assert any(
        "поиск площадки недоступен — обход через список лотов" in r.message
        for r in caplog.records
    )
    [report] = parser.unit_reports
    assert report["status"] == completeness.UNIT_COMPLETE


def test_живой_поиск_обход_списка_не_запускается(api, monkeypatch):
    """Площадка починила поиск — парсер сам остаётся на старом пути."""
    section = _load("tektorg_section_response.json")
    monkeypatch.setattr(mod, "_fetch_procedures", lambda payload: section)
    tenders = asyncio.run(_parser(client_inns=[INN_NORNICKEL]).parse())
    assert tenders
    assert api.calls == []


def test_один_обход_два_отбора(api):
    """Фразы и ИНН отбираются из ОДНОГО обхода: карточки не качаются дважды."""
    api.lists = {"nornikel": {1: page([1, 2, 3], total_pages=1)}}
    api.cards = {
        1: card(1, title="Антисептик калия бутиловый", inn=INN_OTHER),
        2: card(2, title="Услуги по уборке снега"),
        3: card(3, title="Герметик анионный", inn=INN_NORNICKEL),
    }
    walk = _walk(phrases=["Антисептик", "Герметик"])
    kw = _parser(open_walk=walk, keyword_phrases=["Антисептик", "Герметик"])
    sec = _parser(open_walk=walk, client_inns=[INN_NORNICKEL])
    by_phrase = asyncio.run(kw.parse())
    by_inn = asyncio.run(sec.parse())
    assert len(api.card_calls()) == 3
    assert api.search_calls["n"] == 1  # второй парсер знает, что поиск мёртв
    assert {t["lot_id"]: t["search_key"] for t in by_phrase} == {
        "1": "Антисептик",
        "3": "Герметик",
    }
    assert all(t["organizer_match"] is False for t in by_phrase)
    assert {t["lot_id"] for t in by_inn} == {"2", "3"}
    assert all(t["search_key"] == "nornikel" for t in by_inn)
    assert all(t["organizer_match"] is True for t in by_inn)
    # Отчёты общего обхода — у него самого (их забирает раннер), не у парсеров.
    assert kw.unit_reports == [] and sec.unit_reports == []
    assert _section_reports(walk)["nornikel"]["status"] == completeness.UNIT_COMPLETE


def test_фраза_в_предмете_лота_попадает_в_название(api):
    api.lists = {"nornikel": {1: page([7], total_pages=1)}}
    api.cards = {
        7: card(7, title="Закупка МТР для ОФ", subject="Известь негашёная комовая")
    }
    walk = _walk(phrases=["Известь"], client_inns=[])
    tenders = asyncio.run(_parser(open_walk=walk, keyword_phrases=["Известь"]).parse())
    [t] = tenders
    assert t["search_key"] == "Известь"
    # Фильтр раннера сверяет фразу с name: без предмета лот бы отбросился.
    assert "Известь негашёная" in t["name"]
    assert t["name"].startswith("Закупка МТР для ОФ")


def test_фразовый_отчёт_честно_без_объёма(api):
    """Поиска по площадке нет: отбор фраз внутри секций — объём не известен."""
    api.lists = {"nornikel": {1: page([], total_pages=1)}}
    walk = _walk(phrases=["Известь"], client_inns=[])
    asyncio.run(walk.run())
    [phrase] = [r for r in walk.unit_reports if r["kind"] == "phrase"]
    assert phrase["status"] == completeness.UNIT_UNKNOWN
    assert "поиск площадки недоступен" in phrase["label"]


# ── Карточка → тендер ─────────────────────────────────────────────────────────


def test_карточка_секции_маппинг(api):
    pid = CARD_NORNIKEL["id"]
    api.lists = {"nornikel": {1: page([pid], total_pages=1)}}
    api.cards = {pid: CARD_NORNIKEL}
    walk = _walk()
    asyncio.run(walk.run())
    [t] = walk.take("section")
    assert t["lot_id"] == "20064364"  # id процедуры — как в базе до 16.10
    assert t["tender_no"] == "20060372/1"
    assert t["platform"] == "ТЭК-Торг"
    assert t["name"].startswith("174091 Услуги по приему и размещению снега")
    assert t["company"] == 'ЗФ ПАО «ГМК «Северный металл"'
    assert t["inn"] == INN_NORNICKEL
    assert t["tender_type"] == "Запрос цен"
    assert t["date_published"] == "15.10.2026"
    assert t["date_deadline"] == "22.10.2026 20:00"
    assert t["date_to_display"] == "22.10.2026 20:00"
    assert t["sum"] is None  # «НМЦ не установлена»
    # applBtn Северметалла — общий вход в SRM, одинаковый у всех лотов: раннер
    # склеивает лоты по href, и все лоты секции слились бы в один.
    assert t["href"] == ""


def test_карточка_zakupki_ссылка_и_цена(api):
    pid = CARD_ZAKUPKI["id"]
    api.lists = {"nornikel": {1: page([pid], total_pages=1)}}
    api.cards = {pid: CARD_ZAKUPKI}
    walk = _walk(client_inns=[INN_OTHER])
    asyncio.run(walk.run())
    [t] = walk.take("section")
    assert t["href"] == (
        "https://zakupki.tektorg.ru/#com/procedure/view/procedure/234500"
    )
    assert t["sum"] == 188500.0
    assert t["tender_no"] == "ЗП6101445"
    assert t["date_deadline"] == "26.10.2026 08:00"


def test_адресная_ссылка_applBtn_берётся():
    c = card(5)
    c["lots"][0]["applBtn"] = "https://srm.example.ru/lot/20060372"
    t = TektorgParser._lot_to_tender(_parser(), c, "nornikel", True)
    assert t["href"] == "https://srm.example.ru/lot/20060372"


def test_цена_суммой_лотов_если_у_процедуры_нет():
    c = card(6)
    c["sumPrice"] = "НМЦ не установлена"
    lot2 = copy.deepcopy(c["lots"][0])
    c["lots"][0]["startPrice"] = "1000.50 ₽"
    lot2["startPrice"] = "2 000 ₽"
    c["lots"].append(lot2)
    t = TektorgParser._lot_to_tender(_parser(), c, "nornikel", True)
    assert t["sum"] == 3000.5


def test_срок_с_временем_по_москве():
    assert (
        TektorgParser._fmt_deadline("2026-10-22T20:00:00+03:00") == "22.10.2026 20:00"
    )
    # Пояс площадки переводится в МСК, а не отбрасывается.
    assert (
        TektorgParser._fmt_deadline("2026-10-22T17:00:00+00:00") == "22.10.2026 20:00"
    )
    assert TektorgParser._fmt_deadline(None) == "Не указана"
    assert TektorgParser._fmt_deadline("завтра") == "Не указана"


# ── Активность: по сроку, а не по статусу ────────────────────────────────────


def test_старая_карточка_2021_с_приёмом_заявок_не_активна(api):
    """У Северметалла процедуры 2021 года висят «Приём заявок» — судим по сроку."""
    pid = CARD_OLD["id"]
    api.lists = {"nornikel": {1: page([pid], total_pages=1)}}
    api.cards = {pid: CARD_OLD}
    walk = _walk()
    asyncio.run(walk.run())
    assert walk.take("section") == []


@pytest.mark.parametrize("status", ["Отменена", "Отменён", "Архив", "Завершена"])
def test_отменённые_и_завершённые_отсеиваются(api, status):
    api.lists = {"nornikel": {1: page([8], total_pages=1)}}
    api.cards = {8: card(8, status=status)}
    walk = _walk()
    asyncio.run(walk.run())
    assert walk.take("section") == []


def test_истёкший_сегодня_срок_не_активен(api):
    api.lists = {"nornikel": {1: page([9], total_pages=1)}}
    api.cards = {9: card(9, deadline="2026-10-16T11:00:00+03:00")}
    walk = _walk()
    asyncio.run(walk.run())
    assert walk.take("section") == []


# ── Остановка обхода ──────────────────────────────────────────────────────────


def test_страница_из_известных_id_останавливает_обход(api, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "sections": {
                    "nornikel": {
                        "seen": {"11": "2026-10-15", "12": "2026-10-15"},
                        "complete_at": "2026-10-16T03:00:00",
                        "last_ok": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    api.lists = {
        "nornikel": {
            1: page([21, 22, 11]),
            2: page([12, 11]),
            3: page([5, 4]),
        }
    }
    api.cards = {21: card(21), 22: card(22)}
    walk = _walk(state_path=state)
    asyncio.run(walk.run())
    assert len(api.list_calls()) == 2, "на странице из известных id обход — стоп"
    assert len(api.card_calls()) == 2  # только новые процедуры
    assert _section_reports(walk)["nornikel"]["status"] == completeness.UNIT_COMPLETE
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert {"21", "22", "11", "12"} <= set(saved["sections"]["nornikel"]["seen"])


def test_первый_прогон_останавливается_на_горизонте(api):
    api.lists = {"nornikel": {1: page([31, 32, 33, 34]), 2: page([35])}}
    api.cards = {
        31: card(31, published="2026-10-16T08:00:00+03:00"),
        32: card(32, published="2026-10-14T08:00:00+03:00"),
        33: card(33, published="2026-10-01T08:00:00+03:00"),  # за горизонтом
        34: card(34, published="2026-09-30T08:00:00+03:00"),
    }
    walk = _walk()
    asyncio.run(walk.run())
    assert len(api.card_calls()) == 3
    assert len(api.list_calls()) == 1
    assert _section_reports(walk)["nornikel"]["status"] == completeness.UNIT_COMPLETE


def test_потолок_карточек_partial_и_догон_следующим_прогоном(
    api, monkeypatch, tmp_path
):
    monkeypatch.setattr(mod, "OPEN_MAX_CARDS", 2)
    state = tmp_path / "state.json"
    api.lists = {"nornikel": {1: page([41, 42, 43, 44], total_pages=1)}}
    api.cards = {p: card(p) for p in (41, 42, 43, 44)}
    walk = _walk(state_path=state)
    asyncio.run(walk.run())
    report = _section_reports(walk)["nornikel"]
    assert report["status"] == completeness.UNIT_PARTIAL
    assert report["reason"] == completeness.REASON_REQUEST_CAP
    assert len(walk.take("section")) == 2

    # Следующая ночь: 41 и 42 — уже отобранные, их карточки перепроверяются
    # один раз, обход их не качает повторно; хвост 43–44 догоняется.
    monkeypatch.setattr(mod, "OPEN_MAX_CARDS", 4)
    api.calls.clear()
    walk2 = _walk(state_path=state)
    asyncio.run(walk2.run())
    fetched = [int(u.rsplit("/", 1)[1]) for u in api.card_calls()]
    assert sorted(fetched) == [41, 42, 43, 44]
    assert _section_reports(walk2)["nornikel"]["status"] == completeness.UNIT_COMPLETE
    assert {t["lot_id"] for t in walk2.take("section")} == {"41", "42", "43", "44"}


def test_бюджет_запросов_за_прогон(api, monkeypatch):
    monkeypatch.setattr(mod, "OPEN_MAX_REQUESTS", 3)
    monkeypatch.setattr(mod, "PROFILE_SECTIONS", ("nornikel", "market"))
    api.lists = {"nornikel": {1: page([51, 52, 53, 54], total_pages=1)}}
    api.cards = {p: card(p) for p in (51, 52, 53, 54)}
    walk = _walk()
    asyncio.run(walk.run())
    assert len(api.calls) == 3
    reports = _section_reports(walk)
    assert reports["nornikel"]["reason"] == completeness.REASON_REQUEST_CAP
    assert reports["market"]["status"] == completeness.UNIT_SKIPPED
    assert reports["market"]["reason"] == completeness.REASON_REQUEST_CAP


def test_предохранитель_страниц(api, monkeypatch):
    monkeypatch.setattr(mod, "OPEN_MAX_PAGES", 2)
    api.lists = {"nornikel": {1: page([61]), 2: page([62]), 3: page([63])}}
    api.cards = {p: card(p) for p in (61, 62, 63)}
    walk = _walk()
    asyncio.run(walk.run())
    report = _section_reports(walk)["nornikel"]
    assert report["reason"] == completeness.REASON_PAGE_CAP
    assert len(api.list_calls()) == 2


# ── Защита: антибот, ошибки ──────────────────────────────────────────────────


def test_html_антибота_останавливает_обход_blocked(api, monkeypatch):
    monkeypatch.setattr(mod, "PROFILE_SECTIONS", ("nornikel", "market"))
    api.lists = {"nornikel": {1: "<!DOCTYPE html><html>проверка</html>"}}
    walk = _walk()
    asyncio.run(walk.run())
    assert len(api.calls) == 1, "антибот — не долбим площадку дальше"
    reports = _section_reports(walk)
    assert reports["nornikel"]["reason"] == completeness.REASON_BLOCKED
    assert reports["market"]["reason"] == completeness.REASON_BLOCKED


@pytest.mark.parametrize("code", [403, 429, 307])
def test_403_429_307_это_blocked_без_повторов(api, code):
    api.lists = {"nornikel": {1: page([71, 72], total_pages=1)}}
    api.cards = {71: http_error(code), 72: card(72)}
    walk = _walk()
    asyncio.run(walk.run())
    assert len(api.card_calls()) == 1
    report = _section_reports(walk)["nornikel"]
    assert report["status"] == completeness.UNIT_PARTIAL
    assert report["reason"] == completeness.REASON_BLOCKED


def test_одиночный_503_повторяется(api):
    api.lists = {"nornikel": {1: page([81], total_pages=1)}}
    api.cards = {81: [http_error(503), card(81)]}
    walk = _walk()
    asyncio.run(walk.run())
    assert [t["lot_id"] for t in walk.take("section")] == ["81"]
    assert len(api.card_calls()) == 2


def test_карточка_404_пропускается_обход_идёт_дальше(api):
    api.lists = {"nornikel": {1: page([91, 92], total_pages=1)}}
    api.cards = {92: card(92)}  # 91 — 404
    walk = _walk()
    asyncio.run(walk.run())
    assert [t["lot_id"] for t in walk.take("section")] == ["92"]
    assert _section_reports(walk)["nornikel"]["status"] == completeness.UNIT_COMPLETE


def test_422_на_списке_это_смена_формата(api):
    api.lists = {"nornikel": {1: json.loads(_load("tektorg_open_422.json"))}}
    walk = _walk()
    asyncio.run(walk.run())
    report = _section_reports(walk)["nornikel"]
    assert report["status"] == completeness.UNIT_SKIPPED
    assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_исключение_в_секции_даёт_failed_unit(api, monkeypatch):
    monkeypatch.setattr(mod, "PROFILE_SECTIONS", ("nornikel", "market"))

    async def boom(self, section):
        if section == "nornikel":
            raise RuntimeError("сломалось")

    monkeypatch.setattr(TektorgOpenWalk, "_walk_section", boom)
    walk = _walk()
    asyncio.run(walk.run())
    report = _section_reports(walk)["nornikel"]
    assert report["status"] == completeness.UNIT_SKIPPED
    assert report["reason"] == completeness.REASON_CRASHED


# ── Перепроверка известных активных ─────────────────────────────────────────


def test_известные_активные_перепроверяются_по_карточке(api, tmp_path):
    """Обход идёт до первой известной страницы, а лоты прошлых ночей должны
    приходить каждую ночь (как при старом поиске) — иначе они «пропадут»."""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "sections": {
                    "nornikel": {
                        "seen": {"101": "2026-10-10"},
                        "complete_at": "2026-10-16T03:00:00",
                        "last_ok": True,
                    }
                },
                "selected": {
                    "101": {"deadline": "2026-10-20T10:00:00", "section": "nornikel"},
                    "102": {"deadline": "2026-10-10T10:00:00", "section": "nornikel"},
                },
            }
        ),
        encoding="utf-8",
    )
    api.lists = {"nornikel": {1: page([101], total_pages=1)}}
    api.cards = {101: card(101, published="2026-10-10T09:00:00+03:00")}
    walk = _walk(state_path=state)
    asyncio.run(walk.run())
    assert [t["lot_id"] for t in walk.take("section")] == ["101"]
    # Истёкшая 102 не перепроверяется — лишний запрос.
    assert [u.rsplit("/", 1)[1] for u in api.card_calls()] == ["101"]


def test_известные_из_базы_перепроверяются(api, tmp_path):
    db = tmp_path / "tenderwork.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE lots (lot_key TEXT PRIMARY KEY, platform TEXT, lot_id TEXT,"
        " date_deadline TEXT)"
    )
    conn.executemany(
        "INSERT INTO lots VALUES (?, ?, ?, ?)",
        [
            ("ТЭК-Торг|201", "ТЭК-Торг", "201", "20.10.2026"),
            ("ТЭК-Торг|202", "ТЭК-Торг", "202", "01.10.2026"),
            ("Росэлторг|203", "Росэлторг", "203", "20.10.2026"),
        ],
    )
    conn.commit()
    conn.close()
    api.lists = {"nornikel": {1: page([], total_pages=1)}}
    api.cards = {201: card(201, published="2026-10-05T09:00:00+03:00")}
    walk = _walk(known_db=db)
    asyncio.run(walk.run())
    assert [t["lot_id"] for t in walk.take("section")] == ["201"]
    assert [u.rsplit("/", 1)[1] for u in api.card_calls()] == ["201"]


def test_known_procedures_только_чтение_и_нет_файла(tmp_path):
    assert mod.known_procedures(tmp_path / "нет.db") == {}
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lots (platform TEXT, lot_id TEXT, date_deadline TEXT)")
    conn.execute("INSERT INTO lots VALUES ('ТЭК-Торг', '7', '20.10.2026 10:00')")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    assert mod.known_procedures(db) == {"7": "20.10.2026 10:00"}
    assert db.read_bytes() == before


# ── Конвейер ─────────────────────────────────────────────────────────────────


def test_раннер_общий_обход_отчёты_и_фразы(api, monkeypatch):
    """Раннер: один обход на оба режима, отчёты обхода — в сводке раннера."""
    import src.core.parser_runner as pr

    api.lists = {"nornikel": {1: page([1, 2], total_pages=1)}}
    api.cards = {
        1: card(1, title="Антисептик калия", inn=INN_OTHER),
        2: card(2, title="Уборка снега"),
    }
    monkeypatch.setattr(pr, "probe_direct", lambda fetch: "ok")
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: ["Антисептик"])
    monkeypatch.setattr(
        "src.client_inns_store.load_client_inns", lambda *a, **kw: [INN_NORNICKEL]
    )
    runner = pr.ParserRunner.__new__(pr.ParserRunner)
    runner.browser_manager = None
    runner.companies = []
    runner.days_back = 1
    # «Антисептик» — в исключениях старого поиска, но отбор на нашей
    # стороне ничего не стоит, и фраза в обходе списка работает.
    runner.settings = {"tektorg_skip_phrases": ["Антисептик"]}
    runner.unit_reports = []
    tenders = asyncio.run(runner._run_tektorg())
    assert {t["lot_id"] for t in tenders} == {"1", "2"}
    assert len(api.card_calls()) == 2
    labels = {r["label"] for r in runner.unit_reports}
    assert "nornikel" in labels
