# -*- coding: utf-8 -*-
"""
Российский корневой сертификат в поставке (для ЕИС).

zakupki.gov.ru работает на сертификате НУЦ Минцифры, которого нет ни в certifi,
ни в хранилище Windows: и обычный ssl, и truststore падают с «обработка прервана
на корневом сертификате, у которого отсутствует отношение доверия» (проверено
27.08.2026). Корень лежит в репозитории и подключается явно — при этом проверка
цепочки остаётся включённой.

Сеть не используется.
"""


def test_сертификат_есть_в_поставке():
    from src.net_ssl import RU_ROOT_CA, russian_root_available

    assert russian_root_available(), f"нет файла {RU_ROOT_CA}"


def test_отпечаток_совпадает_с_ожидаемым():
    """Подмена или обновление корня должны быть замечены до поставки."""
    from src.net_ssl import RU_ROOT_CA_SHA256, russian_root_fingerprint

    assert russian_root_fingerprint() == RU_ROOT_CA_SHA256


def test_контекст_создаётся_и_проверяет_сертификаты():
    import ssl

    from src.net_ssl import ssl_context_ru

    ctx = ssl_context_ru()
    assert ctx.verify_mode == ssl.CERT_REQUIRED, "проверка сертификатов должна быть включена"
    assert ctx.check_hostname is True


def test_корень_подключается_к_контексту(monkeypatch):
    """get_ca_certs у truststore-контекста не работает, поэтому ловим сам вызов."""
    import ssl

    from src import net_ssl

    загружено = []
    original = ssl.SSLContext.load_verify_locations

    def перехват(self, cafile=None, capath=None, cadata=None):
        загружено.append(cafile)
        return original(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", перехват)
    net_ssl.ssl_context_ru()
    assert any(f and "russian_trusted_root_ca" in str(f) for f in загружено)


def test_без_файла_корня_проверка_не_отключается(monkeypatch, tmp_path):
    """Нет сертификата — запрос честно упадёт на TLS, а не пойдёт без проверки."""
    import ssl

    from src import net_ssl

    monkeypatch.setattr(net_ssl, "RU_ROOT_CA", tmp_path / "нет-такого.pem")
    ctx = net_ssl.ssl_context_ru()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
