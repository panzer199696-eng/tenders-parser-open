"""
Тесты единого справочника (T004–T006): источники → производные файлы.

Главное, что здесь проверяется, — не механика генерации, а обещание
«ручное решение сильнее пересчёта». До слияния справочники синхронизировал
скрипт, который человек запускал руками; теперь генерация автоматическая,
и она обязана не отменять переключения, сделанные в интерфейсе.

Тесты сети не трогают и пишут только в tmp_path.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.store import derive

NOMENCLATURE = {
    "groups": [
        {
            "id": "reagents",
            "name": "Насосы",
            "enabled": True,
            "tier": "daily",
            "phrases": ["Насос центробежный", "Насосный агрегат"],
        },
        {
            "id": "wide",
            "name": "Широкое оборудование",
            "enabled": True,
            "tier": "weekly",
            "phrases": ["Дробилка", "Грохот"],
        },
        {
            "id": "off",
            "name": "Отключённая группа",
            "enabled": False,
            "tier": "daily",
            "phrases": ["Канцтовары"],
        },
    ]
}

CLIENTS = {
    "clients": [
        {
            "id": "romashka",
            "name": "Ромашка",
            "level": "partial",
            "aliases": ["Ромашка", "ПК Ромашка"],
            "inns": [
                {"inn": "0000000001", "name": 'АО "ПК РОМАШКА"', "verified": False}
            ],
        },
        {
            "id": "severny",
            "name": "Северный рудник",
            "level": "full",
            "aliases": ["Северный рудник"],
            "inns": [
                {"inn": "0000000002", "name": "ООО «СЕВЕРНЫЙ РУДНИК»", "verified": False}
            ],
        },
        {
            "id": "operator",
            "name": "Оператор Закупок",
            "level": "market",
            "operator": True,
        },
    ]
}


# ── Фразы поиска ─────────────────────────────────────────────────────────────


def test_ежедневный_набор_только_профильные_группы():
    kw = derive.build_keywords(NOMENCLATURE, tier="daily")
    enabled = {k["phrase"] for k in kw["keywords"] if k["enabled"]}
    assert enabled == {"Насос центробежный", "Насосный агрегат"}
    assert "Дробилка" in {k["phrase"] for k in kw["keywords"]}, (
        "широкие фразы не теряются"
    )


def test_выключенная_группа_даёт_выключенные_фразы():
    kw = derive.build_keywords(NOMENCLATURE, tier="daily")
    row = next(k for k in kw["keywords"] if k["phrase"] == "Канцтовары")
    assert row["enabled"] is False


def test_ручное_включение_переживает_пересчёт():
    """Человек включил широкую фразу — генерация не смеет её выключить."""
    previous = {"keywords": [{"phrase": "Дробилка", "enabled": True, "manual": True}]}
    kw = derive.build_keywords(NOMENCLATURE, tier="daily", previous=previous)
    row = next(k for k in kw["keywords"] if k["phrase"] == "Дробилка")
    assert row["enabled"] is True
    assert row["manual"] is True


def test_ручное_выключение_тоже_переживает():
    """Обратный случай: профильную фразу выключили осознанно."""
    previous = {
        "keywords": [{"phrase": "Насос центробежный", "enabled": False, "manual": True}]
    }
    kw = derive.build_keywords(NOMENCLATURE, tier="daily", previous=previous)
    row = next(k for k in kw["keywords"] if k["phrase"] == "Насос центробежный")
    assert row["enabled"] is False


def test_без_пометки_состояние_пересчитывается():
    """Фраза, которую человек не трогал, следует за настройкой группы."""
    previous = {"keywords": [{"phrase": "Дробилка", "enabled": True}]}
    kw = derive.build_keywords(NOMENCLATURE, tier="daily", previous=previous)
    row = next(k for k in kw["keywords"] if k["phrase"] == "Дробилка")
    assert row["enabled"] is False


def test_расхождение_с_расчётом_признаётся_ручным():
    """T006: переход на единый справочник не должен отменить настройки.

    В рабочем файле уже есть решения человека, но признака manual нет —
    его проставляем по расхождению с расчётом.
    """
    existing = {
        "keywords": [
            {"phrase": "Дробилка", "enabled": True},  # расходится → ручное
            {"phrase": "Насос центробежный", "enabled": True},  # совпадает → расчётное
        ]
    }
    adopted = derive.adopt_manual_flags(existing, NOMENCLATURE, tier="daily")
    rows = {r["phrase"]: r for r in adopted["keywords"]}
    assert rows["Дробилка"].get("manual") is True
    assert "manual" not in rows["Насос центробежный"]


def test_принятые_пометки_держатся_при_следующей_генерации():
    existing = {"keywords": [{"phrase": "Грохот", "enabled": True}]}
    adopted = derive.adopt_manual_flags(existing, NOMENCLATURE, tier="daily")
    kw = derive.build_keywords(NOMENCLATURE, tier="daily", previous=adopted)
    row = next(k for k in kw["keywords"] if k["phrase"] == "Грохот")
    assert row["enabled"] is True


def test_дубли_фраз_схлопываются():
    nom = {
        "groups": [
            {
                "enabled": True,
                "tier": "daily",
                "phrases": ["Насос центробежный", "насос центробежный "],
            },
        ]
    }
    kw = derive.build_keywords(nom, tier="daily")
    assert len(kw["keywords"]) == 1


# ── Заказчики ────────────────────────────────────────────────────────────────


def test_закреплённые_заказчики_идут_первыми():
    """Порядок задаёт приоритет обхода: у full-клиентов он выше."""
    companies = derive.build_companies(CLIENTS)["companies"]
    assert companies[0] == "Северный рудник"
    assert companies.index("Ромашка") < companies.index("Оператор Закупок")


def test_названия_не_дублируются_по_регистру_и_ё():
    clients = {
        "clients": [
            {"id": "a", "name": "Северный Рудник", "level": "full"},
            {"id": "b", "name": "северный рудник", "level": "market"},
        ]
    }
    assert len(derive.build_companies(clients)["companies"]) == 1


def test_ИНН_выгружаются_с_привязкой_к_клиенту():
    inns = derive.build_client_inns(CLIENTS)["targets"]
    by_inn = {t["inn"]: t for t in inns}
    assert by_inn["0000000002"]["client"] == "severny"
    assert by_inn["0000000001"]["verified"] is False


def test_клиент_без_ИНН_не_даёт_пустых_записей():
    inns = derive.build_client_inns(CLIENTS)["targets"]
    assert all(t["inn"].strip() for t in inns)
    assert len(inns) == 2, "у оператора ИНН не задан"


# ── Каталог целиком ──────────────────────────────────────────────────────────


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Каталог, работающий в tmp_path вместо папки пользователя."""
    from src.store import catalog as cat

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cat, "DATA_DIR", data)
    monkeypatch.setattr(cat, "NOMENCLATURE_FILE", data / "nomenclature.json")
    monkeypatch.setattr(cat, "CLIENTS_FILE", data / "clients.json")
    monkeypatch.setattr(cat, "RULES_FILE", data / "rules.json")
    monkeypatch.setattr(cat, "KEYWORDS_FILE", tmp_path / "keywords.json")
    monkeypatch.setattr(cat, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(cat, "CLIENT_INNS_FILE", tmp_path / "client_inns.json")
    return cat


def test_сохранение_заказчика_сразу_пересобирает_обход(catalog, tmp_path):
    """Ради этого всё и затевалось: правка справочника не требует скрипта."""
    catalog.save_nomenclature(NOMENCLATURE)
    catalog.save_clients(CLIENTS)

    companies = json.loads((tmp_path / "companies.json").read_text(encoding="utf-8"))
    inns = json.loads((tmp_path / "client_inns.json").read_text(encoding="utf-8"))
    assert "Северный рудник" in companies["companies"]
    assert len(inns["targets"]) == 2

    # добавили нового заказчика — обход обновился сам
    updated = {
        "clients": CLIENTS["clients"]
        + [
            {
                "id": "primer",
                "name": "Пример-Энерго",
                "level": "market",
                "inns": [{"inn": "0000000003", "name": "ПАО Пример", "verified": True}],
            }
        ]
    }
    catalog.save_clients(updated)
    companies = json.loads((tmp_path / "companies.json").read_text(encoding="utf-8"))
    inns = json.loads((tmp_path / "client_inns.json").read_text(encoding="utf-8"))
    assert "Пример-Энерго" in companies["companies"]
    assert len(inns["targets"]) == 3


def test_повторная_генерация_ничего_не_меняет(catalog, tmp_path):
    catalog.save_nomenclature(NOMENCLATURE)
    catalog.save_clients(CLIENTS)
    first = (tmp_path / "keywords.json").read_text(encoding="utf-8")
    catalog.sync_derived()
    assert (tmp_path / "keywords.json").read_text(encoding="utf-8") == first


def test_ручное_переключение_переживает_сохранение_справочника(catalog, tmp_path):
    """Пользователь включил широкую фразу, потом правил номенклатуру."""
    catalog.save_nomenclature(NOMENCLATURE)
    kw_file = tmp_path / "keywords.json"
    data = json.loads(kw_file.read_text(encoding="utf-8"))
    for row in data["keywords"]:
        if row["phrase"] == "Дробилка":
            row["enabled"] = True
            row["manual"] = True
    kw_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    catalog.save_nomenclature(NOMENCLATURE)  # любая правка справочника

    data = json.loads(kw_file.read_text(encoding="utf-8"))
    row = next(r for r in data["keywords"] if r["phrase"] == "Дробилка")
    assert row["enabled"] is True, "генерация отменила решение пользователя"


def test_битый_справочник_не_роняет_программу(catalog, tmp_path):
    catalog.NOMENCLATURE_FILE.write_text("{ это не json", encoding="utf-8")
    assert catalog.load_nomenclature() == {"groups": []}
    summary = catalog.sync_derived()
    assert summary["keywords"] == 0


# ── Частичное сохранение из интерфейса ───────────────────────────────────────
#
# Интерфейс присылает только тот раздел, который правил: экран номенклатуры —
# «groups», экран заказчиков — «clients». Записать это как весь документ значит
# молча стереть остальное: стоп-слова номенклатуры и веса приоритетов
# заказчиков исчезали при первом же нажатии «Сохранить» (найдено 01.09.2026).


def test_сохранение_групп_не_стирает_стоп_слова(catalog, tmp_path):
    catalog.save_nomenclature(
        {
            "version": "1.0",
            "groups": NOMENCLATURE["groups"],
            "stop_words": ["канцтовар", "клининг"],
        }
    )

    # Интерфейс прислал только группы — так делает /api/catalog/nomenclature.
    catalog.save_nomenclature({"groups": NOMENCLATURE["groups"]})

    saved = catalog.load_nomenclature()
    assert saved["stop_words"] == ["канцтовар", "клининг"], (
        "стоп-слова пропали при сохранении групп из интерфейса"
    )
    assert saved["version"] == "1.0"


def test_сохранение_заказчиков_не_стирает_веса_приоритетов(catalog, tmp_path):
    catalog.save_clients(
        {
            "priority_weights": {"full": 40, "partial": 25, "market": 5},
            "active_managers": ["Менеджер 1", "Менеджер 2"],
            "clients": CLIENTS["clients"],
        }
    )

    catalog.save_clients({"clients": CLIENTS["clients"]})

    saved = catalog.load_clients()
    assert saved["priority_weights"]["full"] == 40, "веса приоритетов потеряны"
    assert saved["active_managers"] == ["Менеджер 1", "Менеджер 2"]


def test_частичное_сохранение_обновляет_переданное(catalog, tmp_path):
    catalog.save_nomenclature({"groups": NOMENCLATURE["groups"], "stop_words": ["а"]})
    catalog.save_nomenclature({"stop_words": ["б", "в"]})

    saved = catalog.load_nomenclature()
    assert saved["stop_words"] == ["б", "в"], "переданный раздел обязан обновиться"
    assert len(saved["groups"]) == len(NOMENCLATURE["groups"]), "группы потеряны"


def test_правила_сливаются_так_же(catalog, tmp_path):
    catalog.save_rules({"version": "1.0", "min_price": {"default": 300000}})
    catalog.save_rules({"min_price": {"default": 500000}})

    saved = catalog.load_rules()
    assert saved["version"] == "1.0"
    assert saved["min_price"]["default"] == 500000
