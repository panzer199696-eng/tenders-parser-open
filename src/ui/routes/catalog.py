"""
Раздел «Справочники»: номенклатура (темы и фразы) и заказчики.

Перенесено из main_ui.py без изменения поведения (рефакторинг 16.10.2026).
"""

import logging

from flask import Blueprint, jsonify, request


logger = logging.getLogger(__name__)

bp = Blueprint("catalog", __name__)


@bp.route("/api/catalog")
def api_catalog():
    """Справочники: номенклатура, заказчики, правила — как их правит человек.

    Отдаём источники истины, а не производные файлы: пользователь редактирует
    группы и заказчиков, а фразы поиска и объекты обхода программа пересобирает
    сама (см. src/store/catalog.py).
    """
    from src.store import load_clients, load_nomenclature, load_rules

    try:
        return jsonify(
            {
                "nomenclature": load_nomenclature(),
                "clients": load_clients(),
                "rules": load_rules(),
            }
        )
    except Exception as e:
        logger.exception("Справочники недоступны")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/catalog/nomenclature", methods=["POST"])
def api_catalog_nomenclature():
    """Сохранить номенклатуру. Фразы поиска пересобираются сразу.

    Ручные переключения фраз при этом сохраняются: решение человека сильнее
    пересчёта (см. derive.build_keywords).
    """
    from src.store import save_nomenclature, sync_derived
    from src.store.derive import validate_nomenclature

    payload = request.get_json(silent=True) or {}
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        return jsonify({"error": "нужен непустой список групп"}), 400
    # Валидация до сохранения: дубль id или общее слово в strong ломают
    # поиск молча — лучше вернуть человека к экрану правки, чем потерять
    # находки до следующей ручной проверки справочника.
    problems = validate_nomenclature({"groups": groups})
    if problems:
        return jsonify({"error": "; ".join(problems)}), 400
    try:
        save_nomenclature({"groups": groups})
        return jsonify({"ok": True, "derived": sync_derived()})
    except Exception as e:
        logger.exception("Не удалось сохранить номенклатуру")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/catalog/clients", methods=["POST"])
def api_catalog_clients():
    """Сохранить заказчиков. Объекты обхода и ИНН пересобираются сразу."""
    from src.store import save_clients, sync_derived

    payload = request.get_json(silent=True) or {}
    clients = payload.get("clients")
    if not isinstance(clients, list) or not clients:
        return jsonify({"error": "нужен непустой список заказчиков"}), 400
    for client in clients:
        if not str(client.get("name", "")).strip():
            return jsonify({"error": "у заказчика должно быть название"}), 400
        if not str(client.get("id", "")).strip():
            return jsonify(
                {"error": f"у «{client.get('name')}» нет идентификатора"}
            ), 400
    try:
        current = payload.get("meta") or {}
        save_clients({**current, "clients": clients})
        return jsonify({"ok": True, "derived": sync_derived()})
    except Exception as e:
        logger.exception("Не удалось сохранить заказчиков")
        return jsonify({"error": str(e)}), 500
