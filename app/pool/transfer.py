"""Перенос выбранного чека пула в реестр без повторного запроса к ФНС."""
from __future__ import annotations

import json

from fastapi import HTTPException

from ..models import Receipt, ReceiptItem
from ..services import goods, receipt_card
from .models import PoolItem


def copy_to_registry(db, pool, company_id: str, user) -> tuple[Receipt, bool]:
    """Вызывается под замком тройки; оставляет историю участника пула."""
    existing = db.query(Receipt).filter_by(fn=pool.fn, fd=pool.fd, fp=pool.fp).first()
    if existing:
        if existing.company_id != company_id:
            raise HTTPException(409, "Чек уже зарегистрирован в другой организации")
        return existing, False
    if pool.status != "verified" or pool.assigned_company_id != company_id:
        raise HTTPException(409, "Чек недоступен для переноса")
    if pool.receipt_date is None:
        raise HTTPException(409, "В чеке пула отсутствует дата: перенос невозможен")
    items = (db.query(PoolItem).filter(PoolItem.receipt_id == pool.id)
             .order_by(PoolItem.id).all())
    evidence = bool(pool.fns_confirmed)
    # В реестр другой компании нельзя копировать неограниченный сырой ответ:
    # в нём могут быть контакты покупателя и иные персональные данные.
    raw = {"pool_transfer": {"pool_receipt_id": pool.id, "fns_confirmed": evidence}}
    try:
        source_extra = json.loads(pool.extra_json or "{}")
    except ValueError:
        source_extra = {}
    allowed = ("retail_place", "kkt_reg_id", "shift_number", "request_number", "ffd_version")
    safe_extra = ({key: source_extra[key] for key in allowed if key in source_extra}
                  if isinstance(source_extra, dict) else {})
    from .geo import region_name
    receipt = Receipt(
        fn=pool.fn, fd=pool.fd, fp=pool.fp, qr_data=pool.qr_data or "",
        receipt_date=pool.receipt_date, total_sum=pool.total_sum, operation=pool.operation,
        company_id=company_id, created_by=user.id, source="pool",
        status="verified" if evidence else "pending",
        fns_status="valid" if evidence else "unknown",
        fns_checked_at=pool.fns_checked_at if evidence else None,
        fns_message=("Подтверждение ФНС сохранено при приёме в пул" if evidence else
                     "Одобрен в пуле; подтверждение ФНС в сохранённых данных не установлено"),
        full_data=bool(pool.full_data and items), details_source="pool",
        merchant_name=pool.merchant_name or "", merchant_inn=pool.merchant_inn or "",
        merchant_address=pool.merchant_address or "", cashier=pool.cashier or "",
        region_code=pool.region_code or "", region_name=region_name(pool.region_code) or "",
        region_city=(pool.city or "")[:120], ext_json=json.dumps(safe_extra, ensure_ascii=False),
        raw_data=json.dumps(raw, ensure_ascii=False),
    )
    for pos, it in enumerate(items):
        receipt.items.append(ReceiptItem(name=it.name, quantity=it.quantity, price=it.price,
            total=it.total, vat_rate=it.vat_rate, vat_sum=it.vat_sum, position=pos))
    receipt_card.enrich_items(receipt)
    calc = goods.classify_items([{"name": it.name, "total": it.total} for it in receipt.items])
    receipt.industry = (calc.get("industry") or "")[:100]
    receipt.industry_lc = receipt.industry.casefold()[:100]
    receipt.product_group = (calc.get("group") or "")[:100]
    db.add(receipt)
    db.flush()
    from ..services import merchant_rules, policy
    policy.refresh_receipt(db, receipt)
    merchant_rules.apply_rules(db, receipt)
    return receipt, True
