"""«Единая база», шаг 4в (T-39): чтение из единой записи.

Решение владельца Q-28 (03.11.2026) — вариант (в). Копия компании бывает беднее
единой записи: чек отсканирован по QR, а продавца, кассира и позиции единая запись
знает из Чек-Пула. Компания видит недостающие в её копии значения отдельным блоком
``unified_fill`` с пометкой «из единой записи»; поля самой копии не меняются,
поэтому выгрузка в 1С и другие выгрузки берут только данные копии компании.
Перенести значения в свой чек бухгалтер может только явно — кнопкой «Подставить»
в форме правки (обычный PATCH чека через единую точку записи).

Ничего не подставляется, если единой записи нет (чек читается из своей копии, как
раньше), она удалена или ждёт разбора расхождений или проверки — спорные данные
компании не показываются. Метки (регион, город, отрасль) не подставляются: это
классификация, а не факты чека. Модуль только читает базу.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy.orm import Session

from ..models import Receipt
from .models import REVIEW_BLOCKING, REVIEW_VERIFICATION, UnifiedDocument, UnifiedItem
from .service import _chunks, _registry_documents

# Поля продавца, которые копия по QR обычно не знает
FILL_FIELDS = ("merchant_name", "merchant_inn", "merchant_address", "cashier")
# Единая запись с такими пометками компании не показывается
HIDDEN_FLAGS = REVIEW_BLOCKING | REVIEW_VERIFICATION


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _item(it: UnifiedItem) -> dict:
    return {"name": it.name, "quantity": it.quantity, "price": it.price,
            "total": it.total, "vat_rate": it.vat_rate}


def fills_for_receipts(db: Session, receipts: Iterable[Receipt], *,
                       with_items: bool = True) -> dict[str, dict]:
    """{id чека: {"fields": {...}, "items": [...]}} — только непустые подстановки.

    Чеки уже отобраны по правам пользователя. Позиции подставляются, только если
    ``with_items`` и у копии их нет. Запросов — не больше трёх на пачку.
    """
    wanted: dict[str, tuple[list[str], bool]] = {}
    for r in receipts:
        fields = [f for f in FILL_FIELDS if _empty(getattr(r, f, None))]
        need_items = with_items and not r.items
        if fields or need_items:
            wanted[r.id] = (fields, need_items)
    if not wanted:
        return {}
    doc_of = _registry_documents(db, list(wanted))
    if not doc_of:
        return {}
    columns = (UnifiedDocument.id, UnifiedDocument.review_flags, UnifiedDocument.deleted_at,
               *(getattr(UnifiedDocument, f) for f in FILL_FIELDS))
    docs = {}
    for chunk in _chunks(sorted(set(doc_of.values()))):
        for row in db.query(*columns).filter(UnifiedDocument.id.in_(chunk)):
            if row.deleted_at is None and not (row.review_flags or 0) & HIDDEN_FLAGS:
                docs[row.id] = row
    item_docs = sorted({doc_of[rid] for rid, (_, need) in wanted.items()
                        if need and doc_of.get(rid) in docs})
    items_of: dict[str, list[dict]] = defaultdict(list)
    for chunk in _chunks(item_docs):
        for it in (db.query(UnifiedItem).filter(UnifiedItem.document_id.in_(chunk))
                   .order_by(UnifiedItem.document_id, UnifiedItem.position, UnifiedItem.id)):
            items_of[it.document_id].append(_item(it))
    out: dict[str, dict] = {}
    for rid, (fields, need_items) in wanted.items():
        doc = docs.get(doc_of.get(rid))
        if doc is None:
            continue
        fill: dict = {}
        values = {f: getattr(doc, f) for f in fields if not _empty(getattr(doc, f))}
        if values:
            fill["fields"] = values
        if need_items and items_of.get(doc.id):
            fill["items"] = items_of[doc.id]
        if fill:
            out[rid] = fill
    return out
