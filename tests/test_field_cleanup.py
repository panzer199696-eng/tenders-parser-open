"""
Очистка текстовых полей лота от HTML-мусора.

Площадка отдаёт названия с мнемониками и тегами подсветки совпадения — без
очистки этот мусор доезжает до Excel. Примеры — типичные ответы площадок:
`НАО &quot;РОМАШКА&quot;`, `Запчасти к <mark>насосам</mark>`,
`&laquo;Открытый запрос предложений&raquo;`.

Сеть не используется.
"""

import pytest

from src.parsers._common import clean_field


class TestCleanField:
    def test_мнемоники_кавычек_разворачиваются(self):
        assert clean_field("НАО &quot;РОМАШКА&quot;") == 'НАО "РОМАШКА"'

    def test_теги_подсветки_удаляются(self):
        assert clean_field("Запчасти к <mark>насосам</mark>") == "Запчасти к насосам"

    def test_типографские_кавычки(self):
        assert clean_field("&laquo;Тест&raquo;") == "«Тест»"

    def test_двойное_экранирование(self):
        assert clean_field("&amp;quot;Двойное&amp;quot;") == '"Двойное"'

    def test_пробелы_сжимаются(self):
        assert clean_field("  Насос   ЦНС\n\tсекция ") == "Насос ЦНС секция"

    def test_неразрывный_пробел_как_мнемоника(self):
        assert clean_field("Поставка&nbsp;насосов") == "Поставка насосов"

    @pytest.mark.parametrize("value", [None, "", "   ", 42, [], {}])
    def test_пустое_и_не_строки_дают_пустую_строку(self, value):
        assert clean_field(value) == ""

    def test_чистый_текст_не_меняется(self):
        assert clean_field("Насосы для нужд ООО «Ромашка»") == (
            "Насосы для нужд ООО «Ромашка»"
        )
