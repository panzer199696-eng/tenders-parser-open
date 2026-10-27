"""
Профиль тендерной работы: почему лот в списке и насколько он наш.

Скоринг считается при СБОРЕ и сохраняется вместе с лотом (фича 002),
поэтому объяснение «почему лот в списке» одинаково в ленте, в отчёте и в
Excel, а не пересчитывается каждый раз заново.

Главное требование к результату — объяснимость. Менеджер должен видеть не
«скоринг 74», а «Насосы +30 · закреплённый клиент +40 · совпало
„насос центробежный“». Поэтому каждая оценка возвращает вклад по частям.

Источник правил — справочники `data/*.json`: номенклатура с весами,
клиенты с уровнем работы, профильные пороги цены. Ничего отраслевого не
зашито в код: новая позиция или заказчик — правка справочника.

ТЗ-4 (движок распознавания «наше / не наше»): голое вхождение подстроки
не отличало «Запасные части легкового транспорта» от запчастей к
оборудованию, а «Насос для системы пожаротушения» — от насосов, которые
вы поставляете. Поэтому у группы есть сильные термины, общие слова с
уточнениями, стоп-слова группы (veto) и отраслевой контекст заказчика.
Сопоставление слов — `nomenclature_hits` из utils/classifier (падежи,
«НШ 1-15» ≠ «НШ 1-16», «фильтр-пресс» ≠ «фильтр воздушный»).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

# Справочники — там же, где их правит пользователь: единый источник истины
# (фича 002). Переменная TENDERWORK_DATA переопределяет путь
# для тестов и переноса на другую машину.
from ..dates import now_msk, today_msk
from ..store.catalog import DATA_DIR as _CATALOG_DATA_DIR
from ..utils.classifier import (
    hits_prepared,
    name_words,
    nomenclature_hits,
    prepare_terms,
)

DATA_DIR = Path(os.environ.get("TENDERWORK_DATA") or _CATALOG_DATA_DIR)

# Экстренная закупка: дедлайн вплотную — короткие тендеры, на которые
# медленно реагируют, стоит видеть первыми.
URGENT_DAYS = 3
URGENT_BONUS = 15
# Бонус группам своего производства (флаг ``own_production`` в справочнике).
OWN_PRODUCTION_BONUS = 10
OURS_WITHOUT_CLIENT_BONUS = 10
# Общее слово без уточнения даёт лишь тень веса группы: лот «Насосное
# оборудование» больницы не должен стоять в списке выше точных находок.
WEAK_STRENGTH_MULT = 0.35
# Отрасль заказчика не распознана, а товар покупают все подряд — оставляем
# видимость, но с понижением и пометкой «проверить».
UNKNOWN_CONTEXT_MULT = 0.6
# Бонус за каждую дополнительную группу с точным совпадением: лот «насосы +
# запорная арматура» интереснее однокомпонентного. Потолок 10 — чтобы
# сборная солянка из пяти групп не обгоняла профильную находку у клиента.
EXTRA_GROUP_BONUS = 5
EXTRA_GROUP_BONUS_CAP = 10


def normalize(text: str) -> str:
    """Ключ сравнения: регистр, «ё» и кавычки не должны мешать совпадению.

    Площадка пишет организатора как «АО ПК "СЕВЕРНАЯ"», а в справочнике он
    записан «ПК Северная» — кавычка между словами разрывала подстроку, и
    лоты закреплённого клиента проходили мимо профиля. Дефис не трогаем:
    он значащий в «НШ 1-15».
    """
    if not text or not isinstance(text, str):
        return ""
    lowered = text.lower().replace("ё", "е")
    for quote in ('"', "«", "»", "„", "“", "”", "'", "`"):
        lowered = lowered.replace(quote, " ")
    return " ".join(lowered.split())


def _marker_re(key: str) -> re.Pattern:
    """Отраслевой маркер: короткая аббревиатура — только отдельным словом.

    Подстрока находила «гок» внутри «мноГОКвартирных», и Фонд капремонта
    Москвы получал «нашу отрасль» +10 (прогон 12.10.2026). Короткий маркер
    («гок», «тэц», «жкх», «гбуз») — аббревиатура: ищем его целым словом с
    падежным хвостом («ГОКа», «ТЭЦом»). Длинный маркер по-прежнему ищется
    подстрокой — так он находится и в слитных названиях («ТюменНЕФТЕГАЗ»),
    а случайно встретиться внутри чужого слова ему уже трудно.
    """
    if len(key) > 4:
        return re.compile(re.escape(key))
    return re.compile(
        r"(?<![0-9a-zа-я])"
        + re.escape(key)
        + r"(?:а|у|ом|е|ов|ам|ами|ах)?(?![0-9a-zа-я])"
    )


def _match_fields(raw: object) -> dict:
    """Разобрать поле ``match`` группы в сильные/общие/уточнения/стоп-слова.

    Обратная совместимость: старый формат (список подстрок, как во всех
    группах справочника на момент ТЗ-4) читается как сильные термины —
    иначе после обновления программы девять групп перестали бы работать
    до ручного перевода в новый вид.
    """
    if isinstance(raw, dict):
        return {
            "strong": prepare_terms(str(t) for t in raw.get("strong", [])),
            "weak": prepare_terms(str(t) for t in raw.get("weak", [])),
            "qualifiers": prepare_terms(str(t) for t in raw.get("qualifiers", [])),
            "exclude": prepare_terms(str(t) for t in raw.get("exclude", [])),
        }
    items = [str(t) for t in raw] if isinstance(raw, list) else []
    return {
        "strong": prepare_terms(items),
        "weak": [],
        "qualifiers": [],
        "exclude": [],
    }


@dataclass
class Verdict:
    """Разбор одного лота: оценка и её объяснение по частям."""

    score: int = 0
    groups: list[str] = field(default_factory=list)
    group_titles: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    client_id: str | None = None
    client_name: str | None = None
    client_level: str | None = None
    client_owner: str | None = None
    urgent: bool = False
    expired: bool = False
    core_profile: bool = False
    days_left: int | None = None
    below_threshold: bool = False
    off_profile: bool = False
    reasons: list[str] = field(default_factory=list)
    # ── ТЗ-4: сила совпадения, отрасль и пометки для ручной проверки ─────────
    strength: str = "none"  # "strong" | "weak" | "none"
    industry: str = "unknown"  # "client" | "ours" | "unknown" | "foreign"
    needs_review: bool = False
    off_industry: bool = False
    vetoed: list[tuple[str, str]] = field(default_factory=list)
    # Заготовка для обучения на решениях пользователя (следующий этап ТЗ):
    # какие именно термины сработали и насколько уверенно.
    matched_terms: list[dict] = field(default_factory=list)

    def explain(self) -> str:
        """Одна строка для списка лотов и для карточки."""
        if self.off_profile:
            return "не наш профиль: " + (
                self.reasons[0] if self.reasons else "стоп-слово"
            )
        # Чужая отрасль — та же «не наша» покупка, но другими словами:
        # «совпало „серная кислота“, но заказчик из чужой отрасли (ТЭЦ)».
        if self.off_industry:
            return "не наш профиль: " + (
                self.reasons[0] if self.reasons else "заказчик из чужой отрасли"
            )
        return " · ".join(self.reasons) if self.reasons else "нет совпадений с профилем"


class Profile:
    """Правила отбора и оценки, собранные из справочников."""

    def __init__(self, data_dir: Path | None = None):
        d = Path(data_dir or DATA_DIR)
        self.nomenclature = json.loads((d / "nomenclature.json").read_text("utf-8"))
        self.clients = json.loads((d / "clients.json").read_text("utf-8"))
        self.rules = json.loads((d / "rules.json").read_text("utf-8"))

        # Предкомпилируем термины: классификация тысячи лотов не должна
        # превращаться в тысячи разборов одного и того же справочника.
        self._groups = []
        for g in self.nomenclature["groups"]:
            terms = _match_fields(g.get("match", []))
            self._groups.append(
                {
                    "id": g["id"],
                    "title": g["title"],
                    "weight": int(g.get("weight", 0)),
                    "enabled": bool(g.get("enabled", True)),
                    "tier": g.get("tier", "weekly"),
                    # Насосы и кабель покупают все подряд — таким группам вес
                    # режется по отрасли заказчика. Узкопрофильное оборудование
                    # вне своей отрасли не покупает никто, поэтому по
                    # умолчанию группа отраслью не ограничивается.
                    "industry_sensitive": bool(g.get("industry_sensitive", False)),
                    # Своё производство — флаг справочника, а не id группы
                    # в коде: специфика живёт только в справочнике.
                    "own_production": bool(g.get("own_production", False))
                    or g.get("family") == "own_production",
                    **terms,
                }
            )
        self._stop = [normalize(s) for s in self.nomenclature.get("stop_words", [])]

        # Отраслевые маркеры: площадка массово пишет «…для нужд городской
        # больницы» прямо в название лота, а такого заказчика в справочнике нет —
        # поэтому ищем маркеры и в организаторе, и в названии.
        industry = self.nomenclature.get("industry", {}) or {}
        self._industry_foreign = [
            (_marker_re(normalize(m)), str(m))
            for m in industry.get("foreign", [])
            if normalize(m)
        ]
        self._industry_ours = [
            (_marker_re(normalize(m)), str(m))
            for m in industry.get("ours", [])
            if normalize(m)
        ]

        # Anti-группы — вето по форме закупки («это подряд, а не поставка»).
        # Товар при этом не выбрасывается: лот остаётся видимым с пометкой.
        self._anti_groups = []
        for ag in self.nomenclature.get("anti_groups", []) or []:
            self._anti_groups.append(
                {
                    "match": [str(t) for t in ag.get("match", [])],
                    "unless": [str(t) for t in ag.get("unless", [])],
                    "reason": str(
                        ag.get("reason") or ag.get("title") or "форма закупки не наша"
                    ),
                }
            )

        self._client_index: list[tuple[str, dict]] = []
        for c in self.clients["clients"]:
            for alias in [c["name"], *c.get("aliases", [])]:
                key = normalize(alias)
                if key:
                    self._client_index.append((key, c))
        # Длинные названия проверяем первыми: «Северный механический завод»
        # должен победить «Северный».
        self._client_index.sort(key=lambda kv: -len(kv[0]))
        self._level_weights = self.clients.get(
            "priority_weights", {"full": 40, "partial": 25, "market": 5}
        )
        self._min_price = self.rules.get("min_price", {})

    # ── номенклатура ────────────────────────────────────────────────────────
    def _match_all(self, name: str) -> tuple[list[dict], list[tuple[str, str]]]:
        """Разбор названия по всем включённым группам.

        Возвращает (сработавшие группы с силой совпадения, сработавшие
        exclude-вето). Сравнение слов — ``nomenclature_hits`` из
        utils/classifier: держит падежи («Задвижки клиновые» ↔
        «Клиновая задвижка»), требует точности для коротких слов и слов
        с цифрами («НШ 1-15» ≠ «НШ 1-16»). Писать здесь своё сравнение
        нельзя — эта функция отточена на живых данных.
        """
        words = name_words(name if isinstance(name, str) else "")
        if not words:
            return [], []
        fired: list[dict] = []
        vetoed: list[tuple[str, str]] = []
        for g in self._groups:
            if not g["enabled"]:
                continue
            # Вето проверяем первым и по всей группе: «Насос для системы
            # пожаротушения» не должен попадать в насосы ни сильным
            # термином, ни общим словом.
            exclude_hit = hits_prepared(words, g["exclude"])
            if exclude_hit:
                vetoed.append((g["id"], exclude_hit[0]))
                continue
            strong_terms = hits_prepared(words, g["strong"])
            if strong_terms:
                fired.append({**g, "found": strong_terms, "strength": "strong"})
                continue
            weak_terms = hits_prepared(words, g["weak"])
            if weak_terms:
                # Общее слово рядом с уточнением — уже уверенная находка:
                # «Кабель КГ 3х25» = кабель (общее) + КГ (уточнение).
                qualifiers = hits_prepared(words, g["qualifiers"])
                strength = "strong" if qualifiers else "weak"
                fired.append({**g, "found": weak_terms, "strength": strength})
        # Уверенная находка главнее догадки по общему слову, даже у более
        # тяжёлой группы: иначе сверху вставало общее слово тяжёлой группы,
        # и лот с точно распознанной позицией уходил «на проверку».
        # Внутри силы — по весу.
        fired.sort(key=lambda g: (g["strength"] != "strong", -g["weight"]))
        return fired, vetoed

    def classify(self, name: str) -> list[dict]:
        """Группы номенклатуры, к которым относится лот (по названию).

        У каждой группы поля ``found`` (совпавшие термины) и ``strength``
        ("strong"/"weak"). Сортировка по убыванию веса — сильнейшая первая.
        """
        fired, _vetoed = self._match_all(name)
        return fired

    def is_off_profile(self, name: str) -> str | None:
        """Стоп-слово, из-за которого лот заведомо не наш (или None)."""
        n = normalize(name)
        for s in self._stop:
            if s and s in n:
                return s
        return None

    def anti_group_reason(self, name: str) -> str | None:
        """Причина «это не поставка», если сработала anti-группа (или None).

        Лот «Поставка и монтаж насосного агрегата» подрядом НЕ считается: слово из
        ``unless`` гасит срабатывание — иначе теряли бы оборудование,
        которое просто монтируем на объекте.
        """
        for ag in self._anti_groups:
            if nomenclature_hits(name, ag["match"]) and not nomenclature_hits(
                name, ag["unless"]
            ):
                return ag["reason"]
        return None

    # ── отрасль заказчика ───────────────────────────────────────────────────
    def industry_of(self, company: str, name: str) -> str:
        """Отрасль закупки: "client" | "ours" | "foreign" | "unknown".

        Порядок жёсткий: клиент из справочника сильнее любых маркеров
        (закреплённый заказчик покупает и непрофильное), а чужая отрасль
        сильнее нашей — в названии бывает и «завод», и «больница», и тогда
        решает чужой маркер, потому что лот почти наверняка для чужой
        площадки (замер ТЗ-4: сотни лотов с нашей темой у чужих заказчиков).
        """
        if self.match_client(company):
            return "client"
        text = normalize(f"{company or ''} {name or ''}")
        if not text:
            return "unknown"
        for marker, _orig in self._industry_foreign:
            if marker.search(text):
                return "foreign"
        for marker, _orig in self._industry_ours:
            if marker.search(text):
                return "ours"
        return "unknown"

    def _foreign_marker(self, company: str, name: str) -> str:
        """Какой именно чужой маркер сработал — для объяснения словами."""
        text = normalize(f"{company or ''} {name or ''}")
        for marker, orig in self._industry_foreign:
            if marker.search(text):
                return orig
        return "чужая отрасль"

    # ── клиенты ─────────────────────────────────────────────────────────────
    def match_client(self, company: str) -> dict | None:
        """Наш заказчик по названию организатора."""
        n = normalize(company)
        if not n:
            return None
        for key, client in self._client_index:
            if key in n or n in key:
                return client
        return None

    # ── сроки ───────────────────────────────────────────────────────────────
    @staticmethod
    def deadline_at(deadline: str) -> datetime | None:
        """Момент окончания подачи. Без времени — конец дня (src/dates.py)."""
        from ..dates import parse_deadline

        return parse_deadline(deadline)

    @classmethod
    def days_until(cls, deadline: str, today: date | None = None) -> int | None:
        """Сколько дней до дедлайна подачи (None — дата не распознана)."""
        at = cls.deadline_at(deadline)
        if at is None:
            return None
        return (at.date() - (today or today_msk())).days

    @classmethod
    def is_expired(cls, deadline: str, now: datetime | None = None) -> bool:
        """Срок подачи уже прошёл (с точностью до времени, если оно указано)."""
        at = cls.deadline_at(deadline)
        if at is None:
            return False
        return at < (now or now_msk())

    # ── порог цены ──────────────────────────────────────────────────────────
    def threshold_for(self, group_ids: list[str]) -> float:
        """Профильный порог: берём самый мягкий среди совпавших групп."""
        default = float(self._min_price.get("default", 0))
        by_group = self._min_price.get("by_group", {})
        values = [float(by_group[g]) for g in group_ids if g in by_group]
        return min(values) if values else default

    # ── итоговая оценка ─────────────────────────────────────────────────────
    def evaluate(
        self, tender: dict, today: date | None = None, now: datetime | None = None
    ) -> Verdict:
        """Разбор лота: оценка, объяснение, метки «экстренный» и «ниже порога»."""
        name = tender.get("name") or ""
        company = tender.get("company") or ""
        v = Verdict()

        stop = self.is_off_profile(name)
        groups, vetoed = self._match_all(name)
        client = self.match_client(company)
        industry = self.industry_of(company, name)
        v.industry = industry
        v.vetoed = vetoed

        # Стоп-слово перекрывает всё, если нет явного попадания в номенклатуру:
        # у лота «организация питания на заводе» ничего нашего нет, а вот
        # «поставка насосов и продуктов питания» — уже спорный, оставляем.
        if stop and not groups:
            v.off_profile = True
            v.reasons = [f"стоп-слово «{stop}»"]
            return v

        # Anti-группа помечает лот, но не отменяет товарные группы: «насос
        # в подряде» всё равно насос — менеджер должен его видеть.
        anti_reason = self.anti_group_reason(name)
        if anti_reason:
            v.off_profile = True
            v.reasons.append(anti_reason)

        group_score = 0.0
        if groups:
            top = groups[0]
            v.groups = [g["id"] for g in groups]
            v.group_titles = [g["title"] for g in groups]
            v.matched = sorted({m for g in groups for m in g["found"]})
            v.matched_terms = [
                {"term": t, "group": g["id"], "strength": g["strength"]}
                for g in groups
                for t in g["found"]
            ]
            v.strength = top["strength"]
            strength_mult = 1.0 if v.strength == "strong" else WEAK_STRENGTH_MULT

            # Множитель отрасли — только для групп, чей товар покупают все
            # подряд. Решает сильнейшая группа: если сверху встала позиция
            # своего производства, чужой маркер в названии товара группой
            # ниже уже не топит лот.
            context_mult = 1.0
            if top["industry_sensitive"]:
                if industry == "foreign":
                    context_mult = 0.0
                    v.off_industry = True
                elif industry == "unknown":
                    context_mult = UNKNOWN_CONTEXT_MULT
                    v.needs_review = True

            group_score = top["weight"] * strength_mult * context_mult
            extra_strong = [g for g in groups[1:] if g["strength"] == "strong"]
            extra_bonus = min(
                EXTRA_GROUP_BONUS * len(extra_strong), EXTRA_GROUP_BONUS_CAP
            )
            v.score = group_score + extra_bonus

            if v.off_industry:
                # Чужая отрасль: объяснение одной строкой, каким термином
                # совпало и почему это не наша покупка.
                term = v.matched_terms[0]["term"] if v.matched_terms else ""
                v.reasons.append(
                    f"совпало «{term}», но заказчик из чужой отрасли "
                    f"({self._foreign_marker(company, name)})"
                )
            else:
                if v.strength == "weak":
                    # Общее слово без уточнения: вес группы почти обнулён, это
                    # видно и в числе, и в словах.
                    v.reasons.append(
                        f"{top['title']} +{round(group_score)} "
                        f"(общее слово «{top['found'][0]}», без уточнения)"
                    )
                else:
                    v.reasons.append(f"{top['title']} +{round(group_score)}")
                if len(groups) > 1:
                    also = "также: " + ", ".join(g["title"] for g in groups[1:])
                    if extra_bonus:
                        also += f" +{extra_bonus}"
                    v.reasons.append(also)
                if v.needs_review:
                    v.reasons.append(
                        "проверить: отрасль заказчика неясна — вес ×"
                        f"{UNKNOWN_CONTEXT_MULT}"
                    )

        if client:
            level = client.get("level", "market")
            weight = int(self._level_weights.get(level, 0))
            v.client_id = client["id"]
            v.client_name = client["name"]
            v.client_level = level
            v.client_owner = client.get("owner")
            v.score += weight
            label = {
                "full": "закреплённый клиент (полная работа)",
                "partial": "закреплённый клиент (совместно)",
                "market": "рынок",
            }.get(level, level)
            reason = f"{label} +{weight}"
            if level == "partial" and client.get("owner"):
                reason += f" — ведёт {client['owner']}"
            v.reasons.append(reason)

        # Маркер нашей отрасли без клиента в справочнике — тёплый лид:
        # завод из нашей отрасли покупает нашу позицию, хоть и не закреплён.
        if industry == "ours" and not client and not v.off_industry:
            v.score += OURS_WITHOUT_CLIENT_BONUS
            v.reasons.append(
                f"отрасль наша, заказчик вне справочника +{OURS_WITHOUT_CLIENT_BONUS}"
            )

        if any(g["own_production"] for g in groups):
            v.score += OWN_PRODUCTION_BONUS
            v.reasons.append(f"своё производство +{OWN_PRODUCTION_BONUS}")

        deadline = tender.get("date_deadline", "")
        days = self.days_until(deadline, today)
        v.days_left = days
        # Момент «сейчас» задаётся явно, иначе оценка зависит от времени запуска:
        # лот с дедлайном «26.08.2026 09:00» до девяти утра считался живым, а
        # после — истёкшим, и тесты плавали (найдено 27.08.2026). При заданной
        # дате без времени берём конец этого дня — так «вчерашний» срок всегда
        # истёк, а сегодняшний с временем сравнивается предсказуемо.
        if now is None:
            now = datetime.combine(today, time(23, 59)) if today else now_msk()
        v.expired = self.is_expired(deadline, now)
        # Основной профиль — группы ежедневного набора (tier = daily).
        # Остальное — рынок второго эшелона: нужен, но не должен вытеснять
        # основное из верхней части списка.
        v.core_profile = any(
            g["id"] in v.groups for g in self._groups if g.get("tier") == "daily"
        )
        if v.expired:
            v.reasons.append("срок подачи истёк")
        elif days is not None and 0 <= days <= URGENT_DAYS:
            # «Срочно — только по понятным»: +15 и подъём наверх получает
            # лот, про который ясно, что он наш, — заказчик из справочника или
            # наша отрасль, либо уверенная тема без сомнений по отрасли. Иначе
            # случайный «насос» от непонятной конторы
            # с дедлайном послезавтра обгоняла закреплённых клиентов. Чужой
            # отрасли, подряду, слабой теме и лоту без темы срок показываем
            # словами, но без бонуса.
            understood = (
                not v.off_industry
                and not v.off_profile
                and not v.needs_review
                and v.strength != "weak"
                and bool(v.groups or client or industry == "ours")
            )
            if understood:
                v.urgent = True
                v.score += URGENT_BONUS
                v.reasons.append(
                    f"🔥 экстренная закупка (дедлайн через {days} дн.) +{URGENT_BONUS}"
                )
            elif not v.off_industry:
                v.reasons.append(
                    f"дедлайн через {days} дн. (лот не разобран — без бонуса)"
                )

        price = tender.get("sum")
        if isinstance(price, (int, float)) and price > 0:
            threshold = self.threshold_for(v.groups)
            if price < threshold:
                v.below_threshold = True
                v.reasons.append(
                    f"ниже профильного порога ({price:,.0f} < {threshold:,.0f} ₽)".replace(
                        ",", " "
                    )
                )

        if stop and groups:
            v.reasons.append(f"внимание: есть стоп-слово «{stop}»")

        if v.matched and v.strength == "strong" and not v.off_industry:
            v.reasons.append(
                "совпало точно: " + ", ".join(f"«{m}»" for m in v.matched[:3])
            )

        if v.strength == "weak":
            # Слабое совпадение — тот же случай «проверить человеком», что и
            # незнакомая отрасль: без метки такие лоты терялись в «Прочем»,
            # теперь корзина «На проверку» (ТЗ-5) ловит их по одному правилу.
            v.needs_review = True
            v.reasons.append("проверить: тема угадана слабо")

        # Итог — целое число: десятые доли нечитаемы в списке лотов.
        v.score = int(round(v.score))
        return v

    def rank(
        self,
        tenders: list[dict],
        today: date | None = None,
        now: datetime | None = None,
    ) -> list[dict]:
        """Лоты с оценкой, отсортированные так, как их надо разбирать утром."""
        out = []
        for t in tenders:
            v = self.evaluate(t, today, now)
            out.append({**t, "verdict": v})
        out.sort(
            key=lambda row: (
                row["verdict"].off_profile,  # чужое — вниз
                row["verdict"].off_industry,  # чужая отрасль — туда же
                row["verdict"].expired,  # подавать некуда — тоже вниз
                -int(row["verdict"].urgent),  # горящее — вверх
                -int(row["verdict"].core_profile),  # основной профиль выше вала
                -row["verdict"].score,
                row["verdict"].days_left
                if row["verdict"].days_left is not None
                else 999,
            )
        )
        return out
