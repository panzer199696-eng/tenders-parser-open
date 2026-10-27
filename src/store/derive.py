"""
Производные справочники: номенклатура и заказчики → рабочие файлы программы.

Раньше эти функции жили в отдельном скрипте и запускались руками
(`--apply`). Из-за этого справочники двух программ расходились, а
пользователь узнавал об этом по пропавшим лотам. Теперь генерация происходит
при сохранении справочника (см. `catalog.sync_derived`), а ручной скрипт
не нужен.

Что из чего получается:

    nomenclature.json → keywords.json      (фразы поиска)
    clients.json      → companies.json     (объекты обхода)
                      → client_inns.json   (адресный обход по ИНН: ТЭК-Торг)

**Главное правило: ручное решение пользователя сильнее пересчёта.** Если
человек включил или выключил фразу в интерфейсе, регенерация обязана это
сохранить. Иначе очередное сохранение справочника молча отменяло бы его
настройку — ровно тот класс «глюков», ради которого затевалось слияние.
"""

from datetime import datetime

# Группы с таким tier не попадают в ежедневный прогон: одна фраза стоит
# около 40 секунд, поэтому широкие группы гоняются отдельно, а их лоты и так
# приходят обходом по заказчикам.
TIER_DAILY = "daily"
TIER_OFF = "off"


def _term_key(term: object) -> str:
    """Ключ сравнения терминов: регистр и «ё» не создают ложных различий.

    «Фильтр-пресс» и «фильтр-пресс» — один термин; дефис сохраняем, он
    значащий («фильтр-пресс» ≠ «фильтр пресс»).
    """
    return " ".join(str(term or "").lower().replace("ё", "е").split())


def _too_short_for_strong(term: str, key: str) -> bool:
    """Короткий термин, которому нельзя доверять полный вес группы.

    Общее слово в «сильных» («насос», «бак») отдаёт весь вес группы любому
    лоту — от этого и страхуемся. Аббревиатуры так не работают: «МШЦ»
    (мельница шаровая с центральной разгрузкой), «НКУ», «ПДМ», «КСД» —
    точные обозначения, а короткое слово сравнивается только на полное
    равенство, так что ложных попаданий они не дают.

    Отличаем их по тому, как термин записан в справочнике: аббревиатура
    пишется ЗАГЛАВНЫМИ. Это правило видно человеку, который правит справочник
    руками, и не требует словаря русского языка.
    """
    if " " in key or any(c.isdigit() for c in key):
        return False
    if len(key) >= 4:
        return False
    return not term.isupper()


def validate_nomenclature(data: dict) -> list[str]:
    """Проблемы справочника номенклатуры человеческим языком.

    Пустой список — всё в порядке. Проверяется перед сохранением из
    интерфейса (main_ui), чтобы опечатка человека не выкинула находки из
    поиска молча: дубль id перезаписывает группу, общее слово в strong
    возвращает вал из тысяч лотов (см. ТЗ-4, замер: «запасн част» ловил
    запчасти легкового транспорта).
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    strong_owner: dict[str, str] = {}
    for g in data.get("groups", []):
        gid = str(g.get("id") or "без id")
        if gid in seen_ids:
            problems.append(f"повторяющийся id группы: «{gid}»")
        seen_ids.add(gid)
        if not str(g.get("title") or "").strip():
            problems.append(f"группа «{gid}» без названия")
        if g.get("weight") is None:
            problems.append(f"группа «{gid}» без веса")

        raw = g.get("match", [])
        new_format = isinstance(raw, dict)
        strong = raw.get("strong", []) if new_format else raw
        weak = raw.get("weak", []) if new_format else []
        qualifiers = raw.get("qualifiers", []) if new_format else []

        for term in strong or []:
            key = _term_key(term)
            if not key:
                continue
            # Короткий strong — страховка от возврата общих слов («насос»,
            # «кабел») в сильные: такой термин даёт полный вес группы любому
            # лоту. Проверяем только новый объектный формат: старые списки —
            # выверенные данные, где «мшц»/«мшр» — точные марки мельниц
            # длиной 3 символа, и трогать их нельзя.
            if new_format and _too_short_for_strong(term, key):
                problems.append(
                    f"слишком короткий strong-термин «{term}» в группе «{gid}»: "
                    "добавьте уточнение или перенесите в weak"
                )
            # Один термин в strong двух групп — неоднозначность: движок молча
            # выберет группу с большим весом, а человек даже не узнает.
            if key in strong_owner and strong_owner[key] != gid:
                problems.append(
                    f"термин «{term}» в strong двух групп: "
                    f"«{strong_owner[key]}» и «{gid}»"
                )
            else:
                strong_owner.setdefault(key, gid)

        # weak без qualifiers бессмыслен: общее слово навсегда останется
        # слабым совпадением, группу нельзя будет усилить уточнением.
        if weak and not qualifiers:
            problems.append(
                f"в группе «{gid}» есть weak-термины, но нет ни одного qualifier"
            )
    return problems


def norm_name(text: str) -> str:
    """Ключ сравнения названий: регистр и «ё» не создают дублей.

    «Северный Рудник» и «северный рудник» — одна компания, но в прогоне
    это два объекта обхода по ~40 секунд каждый.
    """
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def build_keywords(
    nomenclature: dict, tier: str = TIER_DAILY, previous: dict | None = None
) -> dict:
    """Фразы поиска из номенклатуры, с уважением к ручным переключениям.

    `previous` — предыдущий `keywords.json`. Фраза, помеченная в нём как
    `manual`, сохраняет своё состояние: пользователь так решил. Остальные
    вычисляются по tier группы.
    """
    manual: dict[str, bool] = {}
    for row in (previous or {}).get("keywords", []):
        if row.get("manual"):
            manual[str(row.get("phrase", "")).strip().lower()] = bool(
                row.get("enabled")
            )

    out, seen = [], set()
    for group in nomenclature.get("groups", []):
        group_tier = group.get("tier", "weekly")
        if tier == "all":
            active = bool(group.get("enabled")) and group_tier != TIER_OFF
        else:
            active = bool(group.get("enabled")) and group_tier == tier
        for phrase in group.get("phrases", []):
            text = str(phrase).strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            row = {"phrase": text, "enabled": active}
            if key in manual:
                row["enabled"] = manual[key]
                row["manual"] = True
            out.append(row)
    return {"keywords": out}


def adopt_manual_flags(
    existing: dict | None, nomenclature: dict, tier: str = TIER_DAILY
) -> dict:
    """Признать текущее состояние фраз ручным там, где оно расходится с расчётом.

    Нужно один раз, при переходе на единый справочник: в рабочем
    `keywords.json` уже есть переключения, сделанные человеком (31.08.2026
    были включены 12 широких фраз — дробилка, грохот, гидроциклон и другие),
    но признака `manual` в файле нет. Без этого шага первая же генерация
    отменила бы их.
    """
    if not existing:
        return build_keywords(nomenclature, tier)

    computed = {
        row["phrase"].strip().lower(): row["enabled"]
        for row in build_keywords(nomenclature, tier).get("keywords", [])
    }
    out = []
    for row in existing.get("keywords", []):
        phrase = str(row.get("phrase", "")).strip()
        key = phrase.lower()
        enabled = bool(row.get("enabled"))
        item = {"phrase": phrase, "enabled": enabled}
        # Расхождение с расчётом означает, что состояние выставил человек.
        if row.get("manual") or (key in computed and computed[key] != enabled):
            item["manual"] = True
        out.append(item)
    return {"keywords": out}


def build_companies(clients: dict) -> dict:
    """Объекты обхода: по одному названию на заказчика, закреплённые первыми.

    Только основное название, без алиасов: каждая строка — отдельный объект
    обхода ценой около 40 секунд, а алиасы нужны только для сопоставления
    организатора и берутся прямо из `clients.json`.
    """
    order = {"full": 0, "partial": 1, "market": 2}
    rows = sorted(
        clients.get("clients", []), key=lambda c: order.get(c.get("level"), 9)
    )
    names, seen = [], set()
    for c in rows:
        name = str(c.get("name", "")).strip()
        key = norm_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return {"companies": names}


def build_client_inns(clients: dict) -> dict:
    """ИНН заказчиков для площадок, ищущих по ИНН (ТЭК-Торг и далее).

    Отбор по названию на таких площадках теряет лоты: организаторы пишутся
    не как у нас («ООО ГРК "Северное"» против «Северная горная»).
    Флаг `verified` переносится как есть — он не влияет на отбор, это пометка
    «совпадение по названию не сверено с ЕГРЮЛ».
    """
    targets = []
    for c in clients.get("clients", []):
        for item in c.get("inns", []):
            inn = str(item.get("inn", "")).strip()
            if not inn:
                continue
            targets.append(
                {
                    "inn": inn,
                    "name": item.get("name", c.get("name")),
                    "client": c.get("id"),
                    "verified": bool(item.get("verified")),
                }
            )
    return {
        "_comment": (
            "ИНН заказчиков для площадок, которые ищут по ИНН (ТЭК-Торг, "
            "Росэлторг). Сгенерирован из справочника заказчиков "
            f"{datetime.now():%Y-%m-%d}. verified=false — совпадение по названию "
            "не сверено с ЕГРЮЛ."
        ),
        "updated": f"{datetime.now():%Y-%m-%d}",
        "targets": targets,
    }
