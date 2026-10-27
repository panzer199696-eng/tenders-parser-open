"""
Офлайн-тесты справочника ИНН заказчиков (src/client_inns_store.py) и его
доставки в адресный режим ТЭК-Торга.

Зачем этот справочник: у ТЭК-Торга нет фильтра по заказчику на входе, отбор
идёт по выдаче, а названия организаторов там не совпадают с companies.json
(«ООО ГРК "Северное"» против «Северная горная»). ИНН совпадает точно,
поэтому адресный обход должен идти по нему.

Сеть и файловая система пользователя не трогаются: файлы создаются в tmp_path,
парсер подменяется fake-классом.

Запуск:
    pytest tests/test_client_inns.py -v
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.client_inns_store import load_client_inns


def _write(tmp_path: Path, payload) -> Path:
    p = tmp_path / "client_inns.json"
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ── Чтение справочника ───────────────────────────────────────────────────────


def test_ИНН_читаются_в_порядке_файла(tmp_path):
    """Порядок targets[] сохраняется: он отражает приоритет заказчиков."""
    p = _write(
        tmp_path,
        {
            "targets": [
                {"inn": "7700000001", "name": "Северметалл"},
                {"inn": "7700000002", "name": "Северное"},
            ]
        },
    )
    assert load_client_inns(p) == ["7700000001", "7700000002"]


def test_дубли_схлопываются(tmp_path):
    """Один ИНН может стоять у клиента и у его актива — берём один раз."""
    p = _write(
        tmp_path,
        {
            "targets": [
                {"inn": "7700000001"},
                {"inn": "7700000001"},
                {"inn": "7700000002"},
            ]
        },
    )
    assert load_client_inns(p) == ["7700000001", "7700000002"]


def test_ИНН_числом_приводится_к_строке(tmp_path):
    """Файл правят руками: ИНН легко записать без кавычек."""
    p = _write(tmp_path, {"targets": [{"inn": 7700000001}]})
    assert load_client_inns(p) == ["7700000001"]


def test_пустые_и_мусорные_записи_пропускаются(tmp_path):
    p = _write(
        tmp_path,
        {
            "targets": [
                {"inn": ""},
                {"inn": "   "},
                {"name": "без ИНН"},
                "строка вместо объекта",
                {"inn": True},
                {"inn": "7700000001"},
            ]
        },
    )
    assert load_client_inns(p) == ["7700000001"]


def test_файла_нет_пустой_список(tmp_path):
    """Справочника у пользователя может не быть — прогон не должен падать."""
    assert load_client_inns(tmp_path / "нет-такого.json") == []


def test_битый_json_пустой_список(tmp_path):
    p = _write(tmp_path, "{ это не json")
    assert load_client_inns(p) == []


def test_без_ключа_targets_пустой_список(tmp_path):
    p = _write(tmp_path, {"updated": "2026-08-31"})
    assert load_client_inns(p) == []


def test_верхний_уровень_не_объект(tmp_path):
    p = _write(tmp_path, ["7700000001"])
    assert load_client_inns(p) == []


# ── Доставка ИНН в адресный режим ТЭК-Торга ──────────────────────────────────


class _FakeTektorg:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def parse(self):
        return []

    @staticmethod
    def _dedup(tenders):
        return tenders


def _runner(monkeypatch, inns, companies=("Северный металл",), phrases=()):
    _FakeTektorg.instances = []
    monkeypatch.setattr("src.core.parser_runner.TektorgParser", _FakeTektorg)
    monkeypatch.setattr("src.keywords_store.enabled_phrases", lambda: list(phrases))
    monkeypatch.setattr(
        "src.client_inns_store.load_client_inns", lambda *a, **kw: list(inns)
    )

    from src.core.parser_runner import ParserRunner

    runner = ParserRunner.__new__(ParserRunner)
    runner.browser_manager = None
    runner.companies = list(companies)
    runner.days_back = 1
    runner.settings = {"parse_tektorg": True}
    return runner


def test_ИНН_доезжают_до_парсера(monkeypatch):
    """Загруженные ИНН должны попасть в client_inns адресного режима."""
    runner = _runner(monkeypatch, ["7700000001", "7700000002"])
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 1
    assert _FakeTektorg.instances[0].kwargs.get("client_inns") == [
        "7700000001",
        "7700000002",
    ]


def test_без_ИНН_отбор_остается_по_названиям(monkeypatch):
    """Справочника нет — адресный обход всё равно идёт, но по companies."""
    runner = _runner(monkeypatch, [])
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 1
    assert _FakeTektorg.instances[0].kwargs.get("client_inns") is None


def test_адресный_обход_идет_даже_без_справочника_компаний(monkeypatch):
    """ИНН есть, companies пуст — обход должен состояться: ИНН самодостаточны."""
    runner = _runner(monkeypatch, ["7700000001"], companies=())
    asyncio.run(runner._run_tektorg())

    assert len(_FakeTektorg.instances) == 1
    assert _FakeTektorg.instances[0].kwargs.get("client_inns") == ["7700000001"]


def test_справочник_из_поставки_читается():
    """client_inns.json в корне репозитория валиден (в примере ИНН не заданы)."""
    inns = load_client_inns(ROOT / "client_inns.json")
    assert isinstance(inns, list)
    assert all(i.isdigit() for i in inns)
