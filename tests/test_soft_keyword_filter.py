# -*- coding: utf-8 -*-
"""Мягкий keyword-фильтр (режим B): соответствие найденного нашей номенклатуре.

Почему: прежний жёсткий фильтр проверял вхождение фразы подстрокой и
применялся ко ВСЕМ лотам — терял наши позиции («Сорбционная колонна» не
совпадала с лотом «Колонны сорбционные, кислотной обработки») и выкашивал
лоты закреплённых заказчиков, найденные адресным обходом. Новый фильтр
мягкий: проверяет только лоты, найденные по фразе, и совпадение —
морфологическое (падежи, порядок слов — matches_nomenclature).

Все тесты офлайн: чистая функция, без сети и браузера.

Запуск:
    pytest tests/test_soft_keyword_filter.py -v
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.parser_runner import apply_keyword_filter  # noqa: E402


def _lot(search_key=None, name="Лот", company="ООО Ромашка"):
    """Лот в том виде, в каком его собирают парсеры."""
    return {
        "name": name,
        "company": company,
        "search_key": search_key,
        "sum": None,
        "platform": "Росэлторг",
    }


# ── 1. Морфология: падежи и порядок слов ─────────────────────────────────────


def test_падежи_и_порядок_слов_не_теряют_лот():
    """Лот «Колонны сорбционные…» обязан выжить при фразе «Сорбционная
    колонна» — реальный кейс Восток-Золота, из-за него фильтр и выключали."""
    lot = _lot(
        search_key="Сорбционная колонна",
        name="Колонны сорбционные, кислотной обработки",
    )
    res = apply_keyword_filter([lot], ["Сорбционная колонна"])
    assert len(res) == 1
    assert "сорбционные" in res[0]["name"]


def test_нефильтрованная_лента_поиска_отсекается():
    """Поиск «Насосы» тащит первой страницей и питание вахтового посёлка —
    это не наша номенклатура, лот отсекается."""
    lot = _lot(
        search_key="Насосы",
        name="Организация питания вахтового поселка",
    )
    res = apply_keyword_filter([lot], ["Насосы"])
    assert res == []


# ── 2. Лоты, которых фильтр не трогает вовсе ─────────────────────────────────


def test_адресный_обход_по_заказчику_не_проверяется():
    """search_key = имя фирмы (адресный обход) — лот закреплённого клиента,
    а не поисковый шум: остаётся при любом названии."""
    lot = _lot(
        search_key='АО ПК "ЮЖНАЯ"',
        name="Услуги прачечной на промплощадке",
    )
    res = apply_keyword_filter([lot], ["Сорбционная колонна", "Насосы"])
    assert len(res) == 1


def test_лот_закреплённого_заказчика_остаётся():
    """Лот найден по фразе и названием не совпал, но организатор — наш
    клиент из client_labels: ценен независимо от формулировки."""
    lot = _lot(
        search_key="Насосы",
        name="Организация питания вахтового поселка",
        company="ООО ПК Южная",
    )
    res = apply_keyword_filter([lot], ["Насосы"], client_labels={"ООО ПК Южная"})
    assert len(res) == 1


def test_кавычки_в_имени_заказчика_не_мешают():
    """Площадка пишет «…», companies.json — "...": сравнение нормализованное."""
    lot = _lot(
        search_key="Насосы",
        name="Организация питания вахтового поселка",
        company='АО «ПК «ЮЖНАЯ»',
    )
    res = apply_keyword_filter(
        [lot], ["Насосы"], client_labels={'АО ПК "ЮЖНАЯ"'}
    )
    assert len(res) == 1


# ── 3. Граничные случаи ──────────────────────────────────────────────────────


def test_пустые_фразы_не_режут_выдачу():
    """Нет включённых фраз — фильтр пропускается (warning): молча обнулить
    выдачу хуже, чем пропустить лишнее."""
    lots = [_lot(search_key="Насосы", name="Что угодно") for _ in range(3)]
    res = apply_keyword_filter(lots, [])
    assert res == lots
    assert len(res) == 3


def test_совпадение_с_другой_фразой_профиля_спасает_лот():
    """Искали по «Насосы», а лот называется «Сорбционная колонна» — совпадение
    с ЛЮБОЙ включённой фразой профиля достаточно: заказчик мог назвать
    позицию иначе, чем мы искали."""
    lot = _lot(search_key="Насосы", name="Сорбционная колонна")
    res = apply_keyword_filter([lot], ["Насосы", "Сорбционная колонна"])
    assert len(res) == 1


def test_выходные_данные_не_мутируются():
    """Фильтр возвращает новый список — исходные лоты не изменяются."""
    lots = [_lot(search_key="Насосы", name="Насос шестеренный")]
    original = [dict(l) for l in lots]
    apply_keyword_filter(lots, ["Насосы"])
    assert lots == original


# ── runner без pytest ───────────────────────────────────────────────────────


def _run_all():
    fns = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
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


class TestСтроительныйЛотПоСлучайномуСлову:
    """Живые лоты 26.08.2026: длинные СМР-названия ловятся случайным словом."""

    def test_смр_со_складом_реагентов_не_проходит(self):
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": (
                    'Выполнение работ СМР: "АБК ЦЭТЛ ПНН, "ОФ АБК. Насосная станция '
                    "перекачки. Корпус дробления. Корпус обогащения, склад реагентов."
                ),
                "company": 'ООО "Посторонняя"',
                "search_key": "Реагенты для обогащения",
            }
        ]
        assert apply_keyword_filter(лоты, ["Реагенты для обогащения"]) == []

    def test_усиление_металлоконструкций_до_узла_извести_не_проходит(self):
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": (
                    "Выполнение работ по усилению металлоконструкций эстакады, "
                    "строительство конвейерного тракта до узла извести"
                ),
                "company": 'ООО "Посторонняя"',
                "search_key": "Известь",
            }
        ]
        assert apply_keyword_filter(лоты, ["Известь"]) == []

    def test_настоящая_поставка_извести_проходит(self):
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": "Известь комовая 1 сорт, сода кальцинированная техническая",
                "company": 'ООО "Посторонняя"',
                "search_key": "Известь",
            }
        ]
        assert len(apply_keyword_filter(лоты, ["Известь"])) == 1

    def test_лот_нашего_заказчика_не_отсекается_даже_строительный(self):
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": "Строительство склада реагентов",
                "company": 'АО ПК "ЮЖНАЯ"',
                "search_key": "Реагенты для обогащения",
            }
        ]
        kept = apply_keyword_filter(
            лоты, ["Реагенты для обогащения"], client_labels={'АО ПК "ЮЖНАЯ"'}
        )
        assert len(kept) == 1

    def test_если_искали_стройку_строительный_лот_законен(self):
        """Строительные фразы можно включить обратно — их лоты не выбрасываем."""
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": "Строительство моста через реку",
                "company": 'ООО "Посторонняя"',
                "search_key": "строительство моста",
            }
        ]
        assert len(apply_keyword_filter(лоты, ["строительство моста"])) == 1


class TestВетоТолькоНаРаботы:
    """Вето по «работам» не должно задевать поставку товара.

    Первая версия использовала is_construction, который ищет маркеры
    подстрокой: «мост» нашёлся внутри другого слова, «прокладк» — в
    уплотнительной прокладке клапана, и живые лоты «Известь гашеная, пушонка»
    и «Клапан для пресс-фильтров Roxia» терялись (26.08.2026).
    """

    def test_поставка_извести_остаётся(self):
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": (
                    "ОДНОЭТАПНАЯ ПРОЦЕДУРА. Известь гашеная, пушонка 1сорт, ФАСОВКА "
                    "МКР 500кг! В стоимость товара включена упаковка"
                ),
                "company": 'ООО "Посторонняя"',
                "search_key": "Известь",
            }
        ]
        assert len(apply_keyword_filter(лоты, ["Известь"])) == 1

    def test_клапан_для_пресс_фильтров_остаётся(self):
        from src.core.parser_runner import apply_keyword_filter

        лоты = [
            {
                "name": (
                    "Клапан редукционный 111580 для пресс-фильтров Roxia, "
                    "прокладка уплотнительная"
                ),
                "company": 'ООО "Посторонняя"',
                "search_key": "Пресс-фильтр",
            }
        ]
        assert len(apply_keyword_filter(лоты, ["Пресс-фильтр"])) == 1

    def test_подряд_и_ремонт_отсекаются(self):
        from src.core.parser_runner import looks_like_works

        for name in (
            "Выполнение работ СМР по объекту",
            "Цех №31. Капитальный ремонт здания хлорной компрессорной",
            "Заключение договора подряда по ремонту перекрытия",
            "Услуги по монтажу охранной сигнализации",
        ):
            assert looks_like_works(name), name

    def test_поставка_работами_не_считается(self):
        from src.core.parser_runner import looks_like_works

        for name in (
            "Известь гашеная, пушонка 1 сорт",
            "Клапан редукционный для пресс-фильтров Roxia",
            "Поставка компаунда заливочного",
            "ЗИП для плунжерного дозатора",
        ):
            assert not looks_like_works(name), name
