"""
Сторож формата Tender.pro (Д1, 16.10.2026).

Раньше любая страница без строк выдачи читалась как «у заказчика закупок
нет» — и капча, и вход в систему, и переименованная вёрстка. Теперь парсер
требует признак страницы поиска и различает причины. Фикстура — настоящая
выдача (tenderpro_search_sample.html); «сломанные» варианты получены из
неё правкой.
"""

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import src.parsers.tender_pro_parser as mod
from src.parsers import completeness
from src.parsers.tender_pro_parser import TenderProParser

FIXTURE = ROOT / "tests" / "fixtures" / "tenderpro_search_sample.html"
REAL = FIXTURE.read_text(encoding="utf-8")
# Страница поиска без единой строки — «у заказчика закупок нет».
EMPTY = re.sub(r"<tr[^>]*>(?:(?!</tr>).)*tender__id.*?</tr>", "", REAL, flags=re.DOTALL)
CAPTCHA = "<html><body><h1>Подтвердите, что вы не робот</h1></body></html>"


class _Resp:
    def __init__(self, status):
        self.status = status


class _Page:
    """Страница браузера: на каждый goto отдаёт следующий ответ из списка."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.url = ""
        self._html = ""
        self.visited = []

    async def goto(self, url, **kwargs):
        html, status, final_url = (
            self.answers.pop(0) if self.answers else (EMPTY, 200, None)
        )
        self.visited.append(url)
        self.url = final_url or url
        self._html = html
        return _Resp(status)

    async def wait_for_timeout(self, ms):
        return None

    async def content(self):
        return self._html

    async def close(self):
        return None


class _Browser:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(mod, "COMPANY_DELAY", 0)


def _run(answers, company="Ромашка"):
    p = TenderProParser(_Browser(_Page(answers)), [company], days_back=7)
    tenders = asyncio.run(p.parse())
    [report] = [r for r in p.unit_reports if r["label"] == company]
    return tenders, report


def test_настоящая_выдача_собрана_целиком():
    _tenders, report = _run([(REAL, 200, None)])
    assert report["status"] == completeness.UNIT_COMPLETE
    assert report["collected_pages"] >= 1


def test_пустая_страница_поиска_это_нет_закупок_а_не_поломка():
    tenders, report = _run([(EMPTY, 200, None)])
    assert tenders == []
    assert report["status"] == completeness.UNIT_COMPLETE


def test_капча_это_блокировка():
    tenders, report = _run([(CAPTCHA, 200, None)])
    assert tenders == []
    assert report["status"] == completeness.UNIT_SKIPPED
    assert report["reason"] == completeness.REASON_BLOCKED


def test_403_это_блокировка():
    _t, report = _run([(CAPTCHA, 403, None)])
    assert report["reason"] == completeness.REASON_BLOCKED


def test_увели_на_вход_это_не_выполнен_вход():
    _t, report = _run([(EMPTY, 200, "https://www.tender.pro/api/my/login")])
    assert report["reason"] == completeness.REASON_NOT_LOGGED_IN


def test_404_адрес_выдачи_сменился():
    """Июнь 2026: старый /search стал отдавать 404, и парсер месяц давал 0."""
    _t, report = _run([("<html>Not found</html>", 404, None)])
    assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_пустой_ответ_200_это_смена_формата():
    _t, report = _run([("", 200, None)])
    assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_нет_контейнера_выдачи_это_смена_формата():
    broken = REAL.replace("search-page", "lots-page")
    _t, report = _run([(broken, 200, None)])
    assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_строки_есть_распознано_ноль_это_смена_формата():
    """Переименовали класс ячейки номера — строки на месте, узнать нечего."""
    broken = REAL.replace("tender__id", "tender__num")
    tenders, report = _run([(broken, 200, None)])
    assert tenders == []
    assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_ссылки_лотов_переименованы_это_смена_формата():
    broken = REAL.replace("/api/tender/", "/api/lot/")
    tenders, report = _run([(broken, 200, None)])
    assert tenders == []
    assert report["reason"] == completeness.REASON_FORMAT_CHANGED


def test_браузер_не_открыл_страницу_все_заказчики_в_отчёте():
    class _Dead:
        async def new_page(self):
            raise RuntimeError("браузер умер")

    p = TenderProParser(_Dead(), ["Ромашка", "Восток-Золото"], days_back=1)
    assert asyncio.run(p.parse()) == []
    assert {r["label"] for r in p.unit_reports} == {"Ромашка", "Восток-Золото"}
    assert all(r["status"] == completeness.UNIT_SKIPPED for r in p.unit_reports)


def test_период_публикации_по_москве(monkeypatch):
    """Глубина из раннера: days_back=2 при «сегодня» 16.10 по Москве — с 14.10."""
    from datetime import date

    monkeypatch.setattr(mod, "today_msk", lambda: date(2026, 10, 16))
    p = TenderProParser(None, [], days_back=2)
    assert p._within_days_back("14.10.2026 09:00")
    assert not p._within_days_back("13.10.2026 23:59")


def test_503_на_входе_повторяется_и_выдача_собирается():
    # Живьём 16.10.2026: защита площадки отвечает 503 на первый запрос
    # свежего браузера и пропускает повторный.
    _tenders, report = _run([("", 503, None), (REAL, 200, None)])
    assert report["status"] == completeness.UNIT_COMPLETE
    assert report["collected_pages"] >= 1


def test_503_дважды_подряд_это_обрыв_связи():
    _t, report = _run([("", 503, None), ("", 503, None)])
    assert report["reason"] == completeness.REASON_NETWORK
