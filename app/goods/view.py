"""Как показать позицию чека: читаемое название, категория и почему она такая (T-69, v1.70.101).

Без базы и без импорта моделей — модуль вызывается из ReceiptItem.to_dict и из карточки чека.
"""
from __future__ import annotations

from ..services import goods
from . import names

# Источник категории позиции → подпись для человека
SOURCES: dict[str, str] = {
    "manual": "исправлено вручную",
    "memory": "память компании",
    "fns": "признак ФНС",
    "dictionary": "словарь",
    "model": "модель",
    "context": "по чеку и продавцу",
    "none": "не определено",
    "legacy": "словарь, до 1.70.101",
}
# Источники, которым политика верит без порога уверенности
CERTAIN = frozenset({"manual", "memory", "fns", "dictionary", "legacy"})
# Уверенность модели или контекста, с которой позиция считается и для политики (нарушения)
POLICY_MIN = 0.9

# Категория по умолчанию внутри группы — когда группу дал контекст, а модель молчит
DEFAULT_IN_GROUP = {
    "Строительные материалы и инструмент": "build_base",
    "Электроника и бытовая техника": "gadgets",
    "Бытовая химия и хозтовары": "household",
    "Канцелярия и книги": "office",
    "Услуги": "services_other",
}


def default_category(group: str) -> str:
    if group in DEFAULT_IN_GROUP:
        return DEFAULT_IN_GROUP[group]
    inside = goods.group_categories(group)
    return inside[0].code if inside else goods.OTHER


def legacy_category(name: str, group: str) -> str:
    """Категория для позиции, разобранной до 1.70.101 (есть только группа)."""
    if not group or group == goods.DEFAULT_GROUP:
        return goods.OTHER
    found = goods.match(name or "", groups=(group,))
    return found[0].code if found else default_category(group)


def is_certain(source: str, score) -> bool:
    """Можно ли считать категорию позиции для политики (запрещённые товары)."""
    if source in CERTAIN or not source:
        return True
    try:
        return float(score or 0) >= POLICY_MIN
    except (TypeError, ValueError):
        return False


def item_view(row) -> dict:
    """Поля позиции для интерфейса: display, pack, category…, source, note, certain."""
    readable = names.readable(getattr(row, "name", "") or "")
    code = getattr(row, "goods_category", "") or ""
    group = getattr(row, "product_group", "") or ""
    source = getattr(row, "group_source", "") or ""
    note = getattr(row, "group_note", "") or ""
    if not code and group:
        code = legacy_category(getattr(row, "name", ""), group)
        source, note = "legacy", note or "по словарю до выпуска 1.70.101"
    elif not code:
        # позиция ещё не разобрана (например, пришла без документа ФНС) — подсказка словаря на лету
        found = goods.match(getattr(row, "name", "") or "")
        code = found[0].code if found else goods.OTHER
        source = "dictionary" if found else "none"
        note = f"по слову «{found[1]}»" if found else ""
    cat = goods.category(code)
    score = getattr(row, "group_score", None)
    return {
        "display_name": readable["display"],
        "pack": readable["pack"],
        "category": cat.code,
        "category_title": cat.title,
        "category_icon": cat.icon,
        "category_group": group or cat.group,
        "category_source": source,
        "category_source_title": SOURCES.get(source, source),
        "category_score": round(float(score), 3) if score is not None else None,
        "category_note": note,
        "category_certain": is_certain(source, score),
    }
