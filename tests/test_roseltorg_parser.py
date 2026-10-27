"""
Офлайн-тесты RoseltorgParser (Росэлторг) — БЕЗ сети и браузера.

Ответы площадки берутся из фикстур (реальные, 31.08.2026):
  • roseltorg_inn_response.html   — выдача по ИНН 7700000001 (ПАО ГМК
    «Северный металл»), 10 карточек, среди них два лота одной процедуры;
  • roseltorg_empty_response.html — пустая выдача.

Проверяется прежде всего смысл, а не механика: ключ лота обязан различать
лоты одной процедуры, «НМЦ не указано» не должно превращаться в ноль,
дата публикации не выдумывается, а листание идёт только параметром page
(остальные площадка молча игнорирует, и обход крутился бы по кругу).
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers.roseltorg_parser import RoseltorgParser

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(name: str) -> str:
    return open(FIXTURES / name, encoding="utf-8", errors="replace").read()


@pytest.fixture
def inn_html():
    return _fixture("roseltorg_inn_response.html")


@pytest.fixture
def empty_html():
    return _fixture("roseltorg_empty_response.html")


def _parser(**kwargs):
    return RoseltorgParser(None, kwargs.pop("companies", []), **kwargs)


def _patch_pages(monkeypatch, pages: list[str | None]):
    """Подменить загрузку страниц заранее заданной последовательностью."""
    calls: list[str] = []
    queue = list(pages)

    async def fake_load(self, url):
        calls.append(url)
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(RoseltorgParser, "_load_page", fake_load)
    return calls


# ── Разбор карточки ──────────────────────────────────────────────────────────


def test_карточки_разбираются(inn_html):
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert len(rows) == 10
    first = rows[0]
    assert first["platform"] == "Росэлторг"
    assert first["tender_no"] == "B2808261449466"
    # Площадка пишет заказчика прописными — сравниваем без учёта регистра.
    assert "северный металл" in first["company"].lower()
    assert "юридическое сопровождение" not in first["company"].lower(), (
        "теги поиска не должны приклеиваться к названию заказчика"
    )


def test_лоты_одной_процедуры_не_дубли(inn_html):
    """Процедура с двумя лотами — это два лота, а не задвоение."""
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    same = [r for r in rows if r["tender_no"] == "B2808261449466"]
    assert len(same) == 2
    assert {r["lot_id"] for r in same} == {"B2808261449466-1", "B2808261449466-2"}


def test_слово_организатор_не_попадает_в_название_компании(inn_html):
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert not rows[0]["company"].startswith("Организатор")


def test_ссылка_абсолютная(inn_html):
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert rows[0]["href"].startswith("https://www.roseltorg.ru/procedure/")


def test_дедлайн_в_привычном_формате(inn_html):
    """«04.09.2026 в 14:00» → «04.09.2026 14:00»: время сохраняем (16.10.2026)."""
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert rows[0]["date_deadline"] == "04.09.2026 14:00"


def test_дата_публикации_не_выдумывается(inn_html):
    """Площадка её в выдаче не отдаёт — честнее сказать «не указана»."""
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert all(r["date_published"] == "Не указана" for r in rows)


def test_нмц_не_указано_даёт_None_а_не_ноль(inn_html):
    """Ноль означал бы «бесплатно» и увёл бы лот под любой ценовой порог."""
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert all(r["sum"] is None for r in rows)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("НМЦ не указано", None),
        ("НМЦ 1 858 650,00 ₽", 1858650.0),
        ("НМЦ 5000000 руб.", 5000000.0),
        ("", None),
    ],
)
def test_разбор_цены(text, expected):
    assert RoseltorgParser._parse_sum(text) == expected


def test_инн_подхватывается_из_маркера(inn_html):
    """В карточке есть «Найдено в ИНН …» — подтверждение адресного попадания."""
    rows = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    assert rows[0]["inn"] == "7700000001"


def test_адресный_режим_помечает_совпадение_организатора(inn_html):
    by_inn = _parser()._parse_page(inn_html, "7700000001", by_inn=True)
    by_phrase = _parser()._parse_page(inn_html, "компаунд", by_inn=False)
    assert all(r["organizer_match"] for r in by_inn)
    assert not any(r["organizer_match"] for r in by_phrase)


def test_пустая_выдача_не_ломает_разбор(empty_html):
    assert _parser()._parse_page(empty_html, "нет-такого", by_inn=False) == []


def test_битый_html_не_роняет_разбор():
    assert (
        _parser()._parse_page("<div class='search-results__item'", "x", by_inn=False)
        == []
    )


# ── Обход страниц ────────────────────────────────────────────────────────────


def test_листание_идёт_параметром_page(monkeypatch, inn_html, empty_html):
    """Только page=N: p, from и offset площадка молча игнорирует."""
    calls = _patch_pages(monkeypatch, [inn_html, inn_html, empty_html])
    rows = asyncio.run(_parser(client_inns=["7700000001"]).parse())

    assert "page=1" in calls[0] and "page=2" in calls[1]
    assert all("&from=" not in c and "&offset=" not in c for c in calls)
    # 10 лотов на страницу, две страницы — но ключи повторяются, дедуп оставит 10
    assert len(rows) == 10


def test_обход_останавливается_на_пустой_странице(monkeypatch, inn_html, empty_html):
    calls = _patch_pages(monkeypatch, [inn_html, empty_html, inn_html])
    asyncio.run(_parser(client_inns=["7700000001"]).parse())
    assert len(calls) == 2, "после пустой страницы обход обязан прекратиться"


def test_ошибка_сети_сохраняет_собранное(monkeypatch, inn_html):
    calls = _patch_pages(monkeypatch, [inn_html, None])
    rows = asyncio.run(_parser(client_inns=["7700000001"]).parse())
    assert len(calls) == 2
    assert len(rows) == 10, "первая страница не должна пропасть из-за сбоя второй"


def test_статусы_всегда_в_запросе(monkeypatch, empty_html):
    calls = _patch_pages(monkeypatch, [empty_html])
    asyncio.run(_parser(client_inns=["123"]).parse())
    assert "status%5B%5D=5" in calls[0]
    assert "currency=all" in calls[0]


def test_оба_режима_работают_вместе(monkeypatch, inn_html, empty_html):
    calls = _patch_pages(monkeypatch, [inn_html, empty_html, inn_html, empty_html])
    rows = asyncio.run(
        _parser(client_inns=["7700000001"], keyword_phrases=["антисептик"]).parse()
    )
    assert any("query_field=7700000001" in c for c in calls)
    assert any("query_field=" in c and "7700000001" not in c for c in calls)
    # один и тот же лот пришёл дважды — в итоге остаётся один
    assert len(rows) == 10


def test_пустые_ключи_поиска_отбрасываются():
    p = _parser(client_inns=["7700000001", "", "  ", "7700000001"])
    assert p.client_inns == ["7700000001"]


def test_без_ключей_поиска_сеть_не_дёргается(monkeypatch):
    calls = _patch_pages(monkeypatch, [])
    rows = asyncio.run(_parser().parse())
    assert rows == []
    assert calls == []


def test_фразовый_поиск_росэлторга_выключен_по_умолчанию():
    """Площадка отдаёт потолок выдачи на любой узкой марке.

    Живой прогон 01.09.2026: «НШ 1-15» → 200 лотов, «Полотно дозатора» → 200,
    «Компаунд заливочный» → 200 — пять фраз из восьми упёрлись в предел, 1074 лота
    почти сплошного шума. Ценность Росэлторга — адресный обход по ИНН.
    """
    from src.settings_store import DEFAULTS

    assert DEFAULTS["roseltorg_keyword_search"] is False


def test_без_настройки_фразы_на_росэлторг_не_уходят(monkeypatch):
    import asyncio

    from src.core.parser_runner import ParserRunner

    captured = {}

    class _FakeParser:
        def __init__(self, *a, **kw):
            captured.update(kw)

        async def parse(self):
            return []

    import src.core.parser_runner as pr

    monkeypatch.setattr(pr, "RoseltorgParser", _FakeParser)
    monkeypatch.setattr(pr, "load_client_inns", lambda: ["7700000000"], raising=False)

    runner = ParserRunner(None, settings={"roseltorg_keyword_search": False}, companies=[])
    asyncio.run(runner._run_roseltorg())
    assert captured.get("keyword_phrases") is None, "фразы не должны уходить на площадку"


# ── Отчёт о полноте (ТЗ-8) ────────────────────────────────────────────────────


def test_unit_report_full_walk_is_complete(monkeypatch, inn_html, empty_html):
    """После разбора фикстуры отчёт непуст: выдача кончилась пустой страницей —
    площадка отдала всё, что есть, статус complete.

    У Росэлторга нет счётчика результатов, а пагинация в фикстуре — «окном»
    (многоточие): конец выдачи узнаётся только пустой страницей.
    """
    _patch_pages(monkeypatch, [inn_html, empty_html])
    p = _parser(client_inns=["7700000001"])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["platform"] == "Росэлторг"
    assert report["kind"] == "inn"
    assert report["collected_pages"] == 1
    assert report["status"] == "complete"


def test_unit_report_page_cap_is_partial(monkeypatch, inn_html):
    """Упор в потолок страниц при «окне» пагинации: сколько осталось —
    неизвестно, но что НЕ собрано — известно точно. Обязан быть partial
    с причиной page_cap, а не молчаливое «обошли».

    Фейк повторяет страницу с карточками: пустой страницы не будет, обход
    упирается в MAX_PAGES.
    """
    import src.parsers.roseltorg_parser as mod

    monkeypatch.setattr(mod, "PAGE_DELAY_MIN", 0)
    monkeypatch.setattr(mod, "PAGE_DELAY_MAX", 0)
    monkeypatch.setattr(mod, "MAX_PAGES", 3)

    p = _parser(client_inns=["7700000001"])
    _patch_pages(monkeypatch, [inn_html, inn_html, inn_html])
    asyncio.run(p.parse())

    [report] = p.unit_reports
    assert report["status"] == "partial"
    assert report["reason"] == "page_cap"
    # Объём площадка не сообщила — минимальная честная оценка: за потолком
    # осталась хотя бы одна несобранная страница.
    assert report["expected_pages"] == report["collected_pages"] + 1
