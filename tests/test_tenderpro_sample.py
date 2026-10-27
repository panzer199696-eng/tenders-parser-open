# -*- coding: utf-8 -*-
"""
Офлайн-тесты Tender.pro парсера на РЕАЛЬНОЙ вёрстке.

Проверяют (БЕЗ сети, БЕЗ браузера):
  • разбор строк таблицы выдачи (id/№, тип, название, организатор, дедлайн,
    дата публикации, href, organizer_href);
  • очистку названия от суффикса «(idNNN)»;
  • organizer_match (позитив/негатив) — для адресной выдачи все True;
  • дедуп по lot_id;
  • пагинацию: обнаружение следующих страниц и корректный URL (с &page=);
  • сборку URL поиска (_build_url);
  • is_active по дедлайну, is_construction на реальных именах;
  • скоринг непустой (без цены);
  • Excel: 3 листа + столбец «Площадка» заполнен «Tender.pro».

Фикстура: tests/fixtures/tenderpro_search_sample.html (захват 2026-06-29,
поиск company_name=Ромашка&filtered=1). Обновить: см. _capture_tenderpro.py.

Запуск:
    cd <проект>
    set PYTHONUTF8=1
    python tests/test_tenderpro_sample.py
    (или) python -m pytest tests/test_tenderpro_sample.py -q
"""

import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parsers.tender_pro_parser import TenderProParser  # noqa: E402
from src.utils.classifier import is_active, is_construction, classify  # noqa: E402
from src.core.excel_report import build_report  # noqa: E402
from src.db import connect, ensure_schema, save_lots  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "tenderpro_search_sample.html"
TODAY = date(2026, 6, 29)


def _parser(company="Ромашка"):
    return TenderProParser(browser_manager=None, companies=[company])


def _soup():
    return BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "lxml")


def _rows():
    return TenderProParser._select_rows(_soup())


def _parse_all(company="Ромашка"):
    p = _parser(company)
    out = []
    for tr in _rows():
        t = p._parse_row(tr, company)
        if t:
            out.append(t)
    return out


# ── Базовое ─────────────────────────────────────────────────────────────────


def test_fixture_exists():
    assert FIXTURE.exists(), f"нет фикстуры {FIXTURE}"


def test_build_url_search_by_organizer():
    p = _parser("Ромашка")
    url = p._build_url("Ромашка")
    assert "tender.pro/api/tenders/list" in url, url
    assert "company_name=" in url, url
    assert "filtered=1" in url, url
    # Старый (битый) URL больше не используется
    assert "/search?keywords=" not in url, url


def test_rows_recognized():
    rows = _rows()
    assert len(rows) == 25, f"ожидалось 25 строк-данных, получено {len(rows)}"


def test_all_rows_parse():
    parsed = _parse_all()
    assert len(parsed) == 25, f"распознано {len(parsed)}/25"


# ── Поля карточки ───────────────────────────────────────────────────────────


def test_clean_name_strips_id_suffix():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    name = by_id["1210718"]["name"]
    assert "(id1210718)" not in name, name
    assert name.startswith("Закупка №38 изделий"), name
    assert "\xa0" not in name


def test_lot_id_and_tender_no():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    assert by_id["1210718"]["lot_id"] == "1210718"
    assert by_id["1210718"]["tender_no"] == "1210718"


def test_tender_type_from_img_title():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    assert by_id["1210718"]["tender_type"] == "Открытый тендер на закупку"
    # последний — тендер на продажу
    assert parsed[-1]["tender_type"] == "Открытый тендер на продажу"


def test_href_full_url():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    href = by_id["1210718"]["href"]
    assert href == "https://www.tender.pro/api/tender/1210718/view_public", href
    assert all(t["href"].startswith("https://") for t in parsed)


def test_company_and_organizer_href():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    assert by_id["1210718"]["company"] == "Комбинат Ромашка"
    assert by_id["1210718"]["organizer_href"].startswith(
        "https://www.tender.pro/api/company/16537/view"
    ), by_id["1210718"]["organizer_href"]


def test_deadline_and_published():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    assert by_id["1210718"]["date_deadline"] == "03.07.2026 10:00"
    assert by_id["1210718"]["date_published"] == "29.06.2026"
    assert by_id["1210718"]["date_to_display"] == "03.07.2026 10:00"


def test_platform_field_set():
    parsed = _parse_all()
    assert all(t["platform"] == "Tender.pro" for t in parsed)
    assert all(t["sum"] is None for t in parsed)  # в списке цены нет


def test_search_key_field():
    parsed = _parse_all("Ромашка")
    assert all(t["search_key"] == "Ромашка" for t in parsed)


# ── organizer_match ─────────────────────────────────────────────────────────


def test_organizer_match_positive():
    p = _parser("Ромашка")
    assert p._organizer_match("Ромашка", "Комбинат Ромашка") is True
    assert p._organizer_match("Ромашка", 'ПАО "Комбинат Ромашка"') is True
    # ё→е
    assert p._organizer_match("Восток-Золото", 'АО "Восток-Золото"') is True


def test_organizer_match_negative():
    p = _parser("Ромашка")
    assert p._organizer_match("Ромашка", "ООО Лютик") is False
    assert p._organizer_match("Ромашка", "") is False


def test_organizer_match_all_true_on_address_search():
    # Адресная выдача company_name=Ромашка → организатор везде содержит «Ромашка»
    parsed = _parse_all("Ромашка")
    matched = [t for t in parsed if t["organizer_match"]]
    assert len(matched) == len(parsed), (
        f"organizer_match должно быть True для всех {len(parsed)}, "
        f"а получилось {len(matched)}"
    )


# ── Дедуп ───────────────────────────────────────────────────────────────────


def test_dedup_by_lot_id():
    parsed = _parse_all()
    dup = dict(parsed[0])
    deduped = TenderProParser._dedup(parsed + [dup, dup])
    assert len(deduped) == len(parsed)
    ids = [t["lot_id"] for t in deduped]
    assert len(ids) == len(set(ids))


# ── Пагинация ───────────────────────────────────────────────────────────────


def test_pagination_discovered():
    soup = _soup()
    urls = TenderProParser._discover_page_urls(soup, current_index=0)
    assert len(urls) >= 2, f"ожидалась пагинация (>=2 стр.), получено {len(urls)}"
    # В URL обязан быть разделённый &page= (не «order=3page=1»)
    assert all("&page=" in u for u in urls), urls
    # URL абсолютные
    assert all(u.startswith("https://www.tender.pro/") for u in urls), urls


def test_pagination_none_when_no_links():
    # на пустом HTML пагинации нет
    empty = BeautifulSoup("<html><body></body></html>", "lxml")
    assert TenderProParser._discover_page_urls(empty, current_index=0) == []


# ── is_active / is_construction ─────────────────────────────────────────────


def test_is_active_on_sample():
    parsed = _parse_all()
    assert all(is_active(t, today=TODAY) for t in parsed)


def test_is_active_past_deadline():
    parsed = _parse_all()
    t = dict(parsed[0])
    t["date_deadline"] = "01.05.2026"
    assert is_active(t, today=TODAY) is False


def test_is_construction_on_sample():
    parsed = _parse_all()
    by_id = {t["lot_id"]: t for t in parsed}
    # закупка запасных частей / изделий к оборудованию — НЕ стройка
    assert is_construction(by_id["1210718"]["name"]) is False
    assert is_construction(by_id["1210456"]["name"]) is False
    # синтетическое строительное имя детектор ловит:
    assert is_construction("Строительство моста через р. Монни") is True


# ── Скоринг ─────────────────────────────────────────────────────────────────


def test_score_nonzero_without_price():
    parsed = _parse_all()
    classified = [classify(t) for t in parsed]
    scores = [c["score"] for c in classified]
    assert any(s > 0 for s in scores), "скоринг везде 0 — цена не единственный фактор"


# ── Образец → база → книга Excel ────────────────────────────────────────────


def test_образец_доходит_до_книги():
    """Разобранные строки образца проходят базу и ложатся в книгу Excel
    (build_report): каждый лот на листе «Все активные», с площадкой и
    кликабельной ссылкой. Прежде здесь проверялась книга старого
    excel_exporter, которую программа давно не строит."""
    from datetime import datetime
    from io import BytesIO

    from openpyxl import load_workbook

    conn = connect(":memory:")
    ensure_schema(conn)
    save_lots(conn, _parse_all("Ромашка"))
    stored = conn.execute("SELECT count(*) FROM lots").fetchone()[0]
    data = build_report(conn, now=datetime(2026, 6, 29, 7, 0), nomenclature={"groups": []})
    ws = load_workbook(BytesIO(data))["Все активные"]
    head = [c.value for c in ws[1]]
    platform = head.index("Площадка") + 1
    link = head.index("Ссылка") + 1
    rows = range(2, ws.max_row + 1)
    assert stored and len(rows) == stored
    assert {ws.cell(r, platform).value for r in rows} == {'Tender.pro'}
    assert all(ws.cell(r, link).hyperlink for r in rows)


# ── runner без pytest ───────────────────────────────────────────────────────


def _run_all():
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nИтого: {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
