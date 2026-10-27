# -*- coding: utf-8 -*-
"""
Верификация TLS через системное хранилище сертификатов Windows (truststore).

Зачем: на корпоративных машинах (домен, SSL-инспекция прокси или неполная
цепочка сертификатов) стандартный python-ssl с certifi падает с
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`` на ВСЕХ
https-запросах ``urllib`` — ломается обход площадок через urllib, хотя
браузер на той же машине сайты открывает (кейс корпоративной машины,
2026-07-14).

``truststore.inject_into_ssl()`` подменяет ``ssl.SSLContext`` так, что цепочка
проверяется системным CryptoAPI Windows — как в Chrome: доверенные корпоративные
корни принимаются, недостающие промежуточные сертификаты Windows докачивает сам
(AIA). Проверка сертификатов при этом ОСТАЁТСЯ ВКЛЮЧЁННОЙ — это не «verify off».

Вызывать как можно раньше (main_ui.py — при старте). Повторный вызов
безопасен. На отсутствие/сбой truststore
приложение не жалуется — работает как раньше (страховка на случай экзотики).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_injected = False


def inject_truststore() -> bool:
    """Включить системную верификацию TLS. True — включена (или уже была)."""
    global _injected
    if _injected:
        return True
    try:
        import truststore

        truststore.inject_into_ssl()
        _injected = True
        return True
    except Exception as exc:
        logger.warning("truststore не активирован (%s) — остаётся certifi", exc)
        return False


# ── Российский корневой сертификат (для zakupki.gov.ru) ─────────────────────
#
# ЕИС работает на сертификате НУЦ Минцифры, которого нет ни в certifi, ни в
# хранилище Windows: и обычный ssl, и truststore падают с «обработка прервана на
# корневом сертификате, у которого отсутствует отношение доверия» (проверено
# 27.08.2026). Поэтому корень лежит в репозитории и подключается явно.
# Проверка сертификатов при этом ОСТАЁТСЯ ВКЛЮЧЁННОЙ: цепочка ЕИС реально
# проверяется этим корнем — это не «verify off».

RU_ROOT_CA = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"
# Отпечаток самого сертификата (SHA-256 от DER, как показывают браузеры), а не
# файла: перевод строк не должен ломать проверку. Если сертификат подменят или
# он обновится — тест на отпечаток упадёт, и это заметят до поставки.
RU_ROOT_CA_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"


def russian_root_fingerprint() -> str:
    """SHA-256 корневого сертификата из поставки (пустая строка, если нет файла)."""
    import hashlib
    import ssl

    if not RU_ROOT_CA.is_file():
        return ""
    try:
        der = ssl.PEM_cert_to_DER_cert(RU_ROOT_CA.read_text(encoding="ascii"))
    except (OSError, ValueError) as e:
        logger.warning("Российский корень не читается как PEM (%s)", e)
        return ""
    return hashlib.sha256(der).hexdigest()


def russian_root_available() -> bool:
    """Есть ли в поставке российский корневой сертификат."""
    return RU_ROOT_CA.is_file()


def ssl_context_ru() -> "ssl.SSLContext":
    """TLS-контекст, доверяющий системным корням И корню НУЦ Минцифры.

    Нужен для площадок на российских сертификатах (ЕИС). Если файла корня нет,
    возвращается обычный системный контекст — тогда запрос к таким площадкам
    честно упадёт с ошибкой TLS, а не пойдёт без проверки.
    """
    import ssl

    inject_truststore()
    context = ssl.create_default_context()
    if russian_root_available():
        try:
            context.load_verify_locations(cafile=str(RU_ROOT_CA))
        except OSError as e:
            logger.warning("Не удалось подключить российский корень (%s)", e)
    else:
        logger.warning(
            "Российский корневой сертификат не найден (%s) — запросы к ЕИС "
            "могут не пройти проверку TLS",
            RU_ROOT_CA,
        )
    return context
