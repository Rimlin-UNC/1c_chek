"""Перенос выбранного чека пула в реестр без повторного запроса к ФНС."""
from __future__ import annotations

import json

from fastapi import HTTPException

from ..models import Receipt, ReceiptItem
from .models import PoolItem


def copy_to_registry(db, pool, company_id: str, user) -> tuple[Receipt, bool]:
    """Вызывается под замком тройки; оставляет историю участника пула."""
    # Доступ к источнику проверяется до идемпотентного возврата: между выборкой
    # и получением замка назначение или статус могли измениться.
    if pool.status != "verified" or pool.assigned_company_id != company_id:
        raise HTTPException(409, "Чек недоступен для переноса")
    from ..unified import service as unified
    existing = db.query(Receipt).filter_by(fn=pool.fn, fd=pool.fd, fp=pool.fp).first()
    if existing and existing.company_id != company_id:
        # v1.70.85: проверка до регистрации пуловой копии — иначе строгая
        # регистрация отвечала бы «принадлежит другой организации» без тройки,
        # и заявка администратору не записалась бы (T-34).
        from ..unified.links import ForeignOwnerConflict
        raise ForeignOwnerConflict("Чек уже зарегистрирован в другой организации",
                                   pool.fn, pool.fd, pool.fp, company_id)
    unified.register_source(db, unified.POOL, pool, user, strict=True, event_type="transfer_source")
    if existing:
        unified.register_source(db, unified.REGISTRY, existing, user,
                                strict=True, event_type="transfer_link")
        return existing, False
    if pool.receipt_date is None:
        raise HTTPException(409, "В чеке пула отсутствует дата: перенос невозможен")
    items = (db.query(PoolItem).filter(PoolItem.receipt_id == pool.id)
             .order_by(PoolItem.id).all())
    # Сам по себе исторический флаг не доказывает, когда прошла проверка.
    evidence = bool(pool.fns_confirmed and pool.fns_checked_at is not None)
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
    # v1.70.101 (T-69): категории позиций и отрасль чека — каскадом понимания чеков
    from ..goods import service as goods_service
    goods_service.classify_receipt(db, receipt)
    db.add(receipt)
    db.flush()
    unified.register_source(db, unified.REGISTRY, receipt, user,
                            strict=True, event_type="transfer")
    from ..services import merchant_rules, policy
    policy.refresh_receipt(db, receipt)
    merchant_rules.apply_rules(db, receipt)
    return receipt, True
