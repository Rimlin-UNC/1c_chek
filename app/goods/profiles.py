"""Отраслевые профили компании (T-69, v1.70.101, ADR-31).

Профиль переводит категорию товара в статью расходов и задаёт запреты политики по умолчанию — пока у
компании нет своей настройки запрещённых групп (T-43). Профиль «Общий» повторяет прежнее поведение:
запрещены алкоголь, табак, продукты и общепит — решение владельца 03.11.2026 (#28), стройка.

Статья — подсказка: программа её предлагает, но выбор бухгалтера не перезаписывает. Хранится профиль
в app_settings под ключом «goods_profile:<компания>» — без новых колонок в старых таблицах.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

KEY = "goods_profile:"
DEFAULT = "general"

_GENERAL_FORBIDDEN = ("Алкогольные напитки", "Табачные изделия", "Продукты питания", "Общепит")

# Статья по категории для профиля «Общий»; пусто — статьи нет (товар запрещён или не определён)
BASE_ARTICLES: dict[str, str] = {
    "build_base": "Материалы", "build_finish": "Материалы", "build_fasteners": "Материалы",
    "build_electric": "Материалы", "build_plumbing": "Материалы", "build_consumables": "Материалы",
    "build_tools": "Инструмент и инвентарь", "build_ppe": "Спецодежда и СИЗ",
    "fuel": "ГСМ", "auto": "Содержание автотранспорта",
    "office": "Канцтовары", "books": "Канцтовары", "computers": "Оргтехника", "gadgets": "Оргтехника",
    "appliances": "Хозяйственные расходы", "electro_small": "Хозяйственные расходы",
    "chemicals": "Хозяйственные расходы", "household": "Хозяйственные расходы",
    "hygiene": "Хозяйственные расходы", "furniture": "Мебель и инвентарь", "pharma": "Медикаменты",
    "telecom": "Связь", "transport": "Транспортные расходы", "delivery": "Доставка",
    "repair": "Ремонт и обслуживание", "lodging": "Командировочные расходы", "rent": "Аренда",
    "services_other": "Прочие расходы", "clothes": "Прочие расходы", "pets": "Прочие расходы",
    "kids": "Прочие расходы", "sport": "Прочие расходы", "jewelry": "Прочие расходы",
    "catering": "", "food": "", "alcohol": "", "tobacco": "", "other": "",
}

PROFILES: dict[str, dict] = {
    "general": {
        "title": "Общий",
        "hint": "Запрещены алкоголь, табак, продукты и общепит — как до выбора профиля",
        "forbidden": _GENERAL_FORBIDDEN, "articles": {}, "focus": (),
    },
    "construction": {
        "title": "Стройка и ремонт",
        "hint": "Стройматериалы — «Материалы», инструмент — «Инструмент и инвентарь», СИЗ — отдельной статьёй",
        "forbidden": _GENERAL_FORBIDDEN, "articles": {},
        "focus": ("build_base", "build_finish", "build_fasteners", "build_electric", "build_plumbing",
                  "build_consumables", "build_tools", "build_ppe"),
    },
    "catering": {
        "title": "Общепит",
        "hint": "Продукты — сырьё кухни, а не нарушение; запрещён только табак",
        "forbidden": ("Табачные изделия",),
        "articles": {"food": "Сырьё и продукты", "alcohol": "Сырьё и продукты", "catering": "Сырьё и продукты"},
        "focus": ("food", "catering", "household", "chemicals"),
    },
    "retail": {
        "title": "Торговля",
        "hint": "Запрещены алкоголь и табак; продукты и общепит — по решению компании",
        "forbidden": ("Алкогольные напитки", "Табачные изделия"),
        "articles": {"food": "Хозяйственные расходы"}, "focus": (),
    },
    "office": {
        "title": "Офис и услуги",
        "hint": "Чай, кофе и вода для офиса — хозяйственные расходы; запрещены алкоголь и табак",
        "forbidden": ("Алкогольные напитки", "Табачные изделия"),
        "articles": {"food": "Хозяйственные расходы", "catering": "Представительские расходы"},
        "focus": ("office", "computers", "household", "food"),
    },
    "transport": {
        "title": "Транспорт и логистика",
        "hint": "Топливо — ГСМ, запчасти и ремонт — содержание транспорта, платные дороги и стоянки",
        "forbidden": _GENERAL_FORBIDDEN,
        "articles": {"auto": "Запчасти и ремонт транспорта", "repair": "Запчасти и ремонт транспорта",
                     "transport": "Платные дороги и стоянки"},
        "focus": ("fuel", "auto", "repair", "transport"),
    },
}


def codes() -> list[str]:
    return list(PROFILES)


def catalog() -> list[dict]:
    """Профили для выпадающего списка настроек."""
    return [{"code": code, "title": p["title"], "hint": p["hint"], "forbidden": list(p["forbidden"])}
            for code, p in PROFILES.items()]


def get(db: Session, company_id: str | None) -> str:
    """Профиль компании; не выбран — «Общий»."""
    if not company_id:
        return DEFAULT
    from ..models import AppSetting
    row = db.get(AppSetting, KEY + company_id)
    value = (row.value or "").strip() if row is not None else ""
    return value if value in PROFILES else DEFAULT


def set_profile(db: Session, company_id: str, code: str) -> str:
    from ..models import AppSetting
    code = code if code in PROFILES else DEFAULT
    db.merge(AppSetting(key=KEY + company_id, value=code))
    return code


def default_forbidden(code: str) -> tuple[str, ...]:
    return tuple(PROFILES.get(code, PROFILES[DEFAULT])["forbidden"])


def focus(code: str) -> tuple[str, ...]:
    return tuple(PROFILES.get(code, PROFILES[DEFAULT])["focus"])


def article_for(code: str, category: str) -> str:
    """Статья расходов для категории в профиле (пусто — статьи нет)."""
    prof = PROFILES.get(code, PROFILES[DEFAULT])
    if category in prof["articles"]:
        return prof["articles"][category]
    return BASE_ARTICLES.get(category, "")


def article_hint(code: str, items: list[dict]) -> dict:
    """Подсказка статьи для чека: статья с наибольшей суммой позиций.

    items: [{"category": код, "total": сумма}]. Возвращает {} или {"article", "share", "reason"}.
    """
    sums: dict[str, float] = {}
    total = 0.0
    for it in items or []:
        amount = float(it.get("total") or 0)
        total += max(amount, 0.0)
        article = article_for(code, it.get("category") or "")
        if article and amount > 0:
            sums[article] = sums.get(article, 0.0) + amount
    if not sums or total <= 0:
        return {}
    article = max(sums, key=lambda a: sums[a])
    share = sums[article] / total
    if share < 0.5:
        return {}
    title = PROFILES.get(code, PROFILES[DEFAULT])["title"]
    return {"article": article, "share": round(share, 3),
            "reason": f"по товарам чека: {round(share * 100)} % суммы, профиль «{title}»"}
