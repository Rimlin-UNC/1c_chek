"""Транзакционные операции единого фискального документа.

Функции не делают commit и не вызывают ФНС.  Вызывающий маршрут держит одну
транзакцию для канонического документа и совместимых проекций.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Company, Receipt, ReceiptItem, User
from ..pool.models import PoolItem, PoolReceipt
from ..services.receipt_identity import lock_identity
from .models import (
    REVIEW_AMOUNT,
    REVIEW_BLOCKING,
    REVIEW_DATE,
    REVIEW_INCOMPLETE_IDENTITY,
    REVIEW_ITEMS,
    REVIEW_MERCHANT,
    REVIEW_OPERATION,
    REVIEW_OWNERSHIP,
    REVIEW_VERIFICATION,
    UnifiedDocument,
    UnifiedEvent,
    UnifiedItem,
    UnifiedOrigin,
    UnifiedOwnership,
)

REGISTRY = "registry"
POOL = "pool"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def identity_key(fn: str | None, fd: str | None, fp: str | None) -> str | None:
    """Ключ совместим с ReceiptIdentityLock; неполные тройки не склеиваем."""
    values = [fn or "", fd or "", fp or ""]
    if not all(v.strip() for v in values):
        return None
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _lock_values(db: Session, fn: str, fd: str, fp: str) -> None:
    if identity_key(fn, fd, fp) is not None:
        lock_identity(db, fn, fd, fp)


def _source(db: Session, kind: str, source_id: str):
    model = Receipt if kind == REGISTRY else PoolReceipt
    return db.get(model, source_id)


def _core(row: Any, kind: str) -> dict:
    return {
        "fn": row.fn or "",
        "fd": row.fd or "",
        "fp": row.fp or "",
        "receipt_date": row.receipt_date,
        "total_sum": float(row.total_sum or 0.0),
        "operation": int(row.operation or 1),
        "merchant_name": row.merchant_name or "",
        "merchant_inn": row.merchant_inn or "",
        "merchant_address": row.merchant_address or "",
        "cashier": row.cashier or "",
        "region_code": row.region_code or "",
        "region_city": (row.region_city if kind == REGISTRY else row.city) or "",
        "industry": row.industry or "",
    }


def _money(value: float) -> float:
    return round(float(value or 0.0), 2)


def _item_core(item: Any) -> tuple:
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    return (
        (get("name", "") or "").strip(),
        round(float(get("quantity", 0.0) or 0.0), 6),
        _money(get("price", 0.0)),
        _money(get("total", 0.0)),
        str(get("vat_rate", "none") or "none"),
        _money(get("vat_sum", 0.0)),
    )


def _items(db: Session, row: Any, kind: str) -> list[dict]:
    if kind == REGISTRY:
        rows = (db.query(ReceiptItem).filter(ReceiptItem.receipt_id == row.id)
                .order_by(ReceiptItem.position, ReceiptItem.id).all())
        return [{
            "position": pos,
            "name": it.name or "",
            "quantity": float(it.quantity or 0.0),
            "price": float(it.price or 0.0),
            "total": float(it.total or 0.0),
            "vat_rate": it.vat_rate or "none",
            "vat_sum": float(it.vat_sum or 0.0),
            "product_type": it.product_type,
            "payment_type": it.payment_type,
            "unit": it.unit or "",
            "product_group": it.product_group or "",
            "industry": it.industry or "",
            "extra_json": it.extra_json or "",
        } for pos, it in enumerate(rows)]
    rows = (db.query(PoolItem).filter(PoolItem.receipt_id == row.id)
            .order_by(PoolItem.id).all())
    return [{
        "position": pos,
        "name": it.name or "",
        "quantity": float(it.quantity or 0.0),
        "price": float(it.price or 0.0),
        "total": float(it.total or 0.0),
        "vat_rate": it.vat_rate or "none",
        "vat_sum": float(it.vat_sum or 0.0),
        "product_type": None,
        "payment_type": None,
        "unit": "",
        "product_group": "",
        "industry": "",
        "extra_json": "",
    } for pos, it in enumerate(rows)]


def _set_items(db: Session, doc: UnifiedDocument, items: list[dict]) -> None:
    for old in list(doc.items):
        db.delete(old)
    db.flush()
    for data in items:
        db.add(UnifiedItem(document_id=doc.id, **data))
    db.flush()


def _add_event(db: Session, doc: UnifiedDocument, event_type: str, actor: User | None,
               *, from_company: str | None = None, to_company: str | None = None,
               idempotency_key: str | None = None, details: dict | None = None) -> UnifiedEvent:
    event = UnifiedEvent(
        document_id=doc.id, event_type=event_type,
        actor_id=actor.id if actor else None,
        from_company_id=from_company, to_company_id=to_company,
        idempotency_key=idempotency_key,
        details=json.dumps(details or {}, ensure_ascii=False),
    )
    db.add(event)
    return event


def _company_id(row: Any, kind: str) -> str | None:
    return row.company_id if kind == REGISTRY else row.assigned_company_id


def _fns_evidence(row: Any, kind: str) -> tuple[str, datetime | None]:
    if kind == REGISTRY:
        status = row.fns_status or "unknown"
        checked = row.fns_checked_at
        if status in ("valid", "invalid", "not_found") and checked:
            return status, checked
        return "unknown", None
    if row.fns_confirmed and row.fns_checked_at:
        return "valid", row.fns_checked_at
    return "unknown", None


def _merge_fns(doc: UnifiedDocument, status: str, checked: datetime | None) -> None:
    if status == "unknown" or checked is None:
        return
    if doc.fns_status == "unknown":
        doc.fns_status, doc.fns_checked_at = status, checked
        return
    if doc.fns_status != status:
        doc.review_flags |= REVIEW_VERIFICATION
        return
    if checked and (doc.fns_checked_at is None or checked > doc.fns_checked_at):
        doc.fns_checked_at = checked


def _compare_core(doc: UnifiedDocument, data: dict) -> int:
    flags = 0
    if doc.receipt_date and data["receipt_date"] and doc.receipt_date != data["receipt_date"]:
        flags |= REVIEW_DATE
    if _money(doc.total_sum) != _money(data["total_sum"]):
        flags |= REVIEW_AMOUNT
    if doc.operation != data["operation"]:
        flags |= REVIEW_OPERATION
    for field in ("merchant_name", "merchant_inn", "merchant_address"):
        old, new = getattr(doc, field), data[field]
        if old and new and old.strip().casefold() != new.strip().casefold():
            flags |= REVIEW_MERCHANT
    return flags


def _apply_core(doc: UnifiedDocument, data: dict) -> None:
    for field, value in data.items():
        setattr(doc, field, value)


def _ensure_ownership(db: Session, doc: UnifiedDocument) -> UnifiedOwnership:
    if doc.ownership is None:
        doc.ownership = UnifiedOwnership(document_id=doc.id)
        db.add(doc.ownership)
        db.flush()
    return doc.ownership


def register_source(db: Session, kind: str, row: Any, actor: User | None = None,
                    *, strict: bool = False, event_type: str = "register") -> UnifiedDocument:
    """Создать/дополнить канонический документ ссылкой на легаси-проекцию.

    ``strict=True`` используется живыми операциями: расхождение не должно
    молча создать второй смысл одного чека.  Backfill вызывает с strict=False
    и отправляет конфликтующие документы на разбор.
    """
    if kind not in (REGISTRY, POOL):
        raise ValueError("unknown source kind")
    _lock_values(db, row.fn or "", row.fd or "", row.fp or "")
    origin = (db.query(UnifiedOrigin)
              .filter(UnifiedOrigin.source_kind == kind, UnifiedOrigin.source_id == row.id)
              .first())
    if origin:
        return origin.document
    key = identity_key(row.fn, row.fd, row.fp)
    doc = None
    if key is not None:
        doc = (db.query(UnifiedDocument).populate_existing()
               .filter(UnifiedDocument.identity_key == key).with_for_update(of=UnifiedDocument).first())
    data = _core(row, kind)
    items = _items(db, row, kind)
    created = doc is None
    if created:
        doc = UnifiedDocument(identity_key=key, **data)
        if key is None:
            doc.review_flags |= REVIEW_INCOMPLETE_IDENTITY
        db.add(doc)
        db.flush()
        _ensure_ownership(db, doc)
        _set_items(db, doc, items)
    else:
        assert doc is not None
        flags = _compare_core(doc, data)
        if items and doc.items:
            if [_item_core(i) for i in items] != [_item_core(i) for i in doc.items]:
                flags |= REVIEW_ITEMS
        elif items and not doc.items:
            _set_items(db, doc, items)
        if flags and strict:
            raise HTTPException(409, "Данные чека расходятся с единой записью; требуется разбор")
        doc.review_flags |= flags
        if key is None:
            doc.review_flags |= REVIEW_INCOMPLETE_IDENTITY
    own = _ensure_ownership(db, doc)
    company_id = _company_id(row, kind)
    if company_id:
        if own.company_id is None and not (doc.review_flags & REVIEW_OWNERSHIP):
            own.company_id = company_id
            own.assigned_at = _now()
            own.assigned_by = actor.id if actor else ""
        elif own.company_id != company_id:
            if strict:
                raise HTTPException(409, "Чек уже принадлежит другой организации")
            doc.review_flags |= REVIEW_OWNERSHIP
            own.company_id = None
    if kind == REGISTRY and row.created_by and not own.created_by:
        own.created_by = row.created_by
    status, checked = _fns_evidence(row, kind)
    _merge_fns(doc, status, checked)
    db.add(UnifiedOrigin(document_id=doc.id, source_kind=kind, source_id=row.id))
    if doc.deleted_at is not None:
        doc.deleted_at = None
        if not any(o.source_kind == POOL for o in doc.origins):
            own.company_id = None
            own.created_by = None
            own.assigned_at = None
            own.assigned_by = ""
    doc.version += 1
    _add_event(db, doc, event_type, actor, to_company=own.company_id,
               details={"source_kind": kind, "created": created})
    db.flush()
    return doc


def document_for_source(db: Session, kind: str, source_id: str) -> UnifiedDocument | None:
    origin = (db.query(UnifiedOrigin)
              .filter(UnifiedOrigin.source_kind == kind, UnifiedOrigin.source_id == source_id)
              .first())
    return origin.document if origin else None


def _project_pool(db: Session, doc: UnifiedDocument) -> None:
    """Обновить общую часть пуловой проекции; raw/баллы/согласия не трогаем."""
    origins = db.query(UnifiedOrigin).filter(UnifiedOrigin.document_id == doc.id).all()
    items = (db.query(UnifiedItem).filter(UnifiedItem.document_id == doc.id)
             .order_by(UnifiedItem.position, UnifiedItem.id).all())
    for origin in origins:
        if origin.source_kind != POOL:
            continue
        pool = db.get(PoolReceipt, origin.source_id)
        if pool is None:
            continue
        for field in ("fn", "fd", "fp", "receipt_date", "total_sum", "operation",
                      "merchant_name", "merchant_inn", "merchant_address", "cashier",
                      "region_code", "industry"):
            setattr(pool, field, getattr(doc, field))
        pool.city = doc.region_city
        old_items = db.query(PoolItem).filter(PoolItem.receipt_id == pool.id).all()
        for old in old_items:
            db.delete(old)
        db.flush()
        for item in items:
            db.add(PoolItem(
                receipt_id=pool.id, name=item.name, quantity=item.quantity,
                price=item.price, total=item.total, vat_rate=item.vat_rate,
                vat_sum=item.vat_sum,
            ))


def sync_registry(db: Session, receipt: Receipt, actor: User | None = None) -> UnifiedDocument:
    """После правки/обогащения реестра обновить каноническую запись и пул."""
    db.flush()
    doc = document_for_source(db, REGISTRY, receipt.id)
    if doc is None:
        return register_source(db, REGISTRY, receipt, actor, strict=True, event_type="sync")
    _lock_values(db, receipt.fn, receipt.fd, receipt.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == doc.id).with_for_update(of=UnifiedDocument).first())
    key = identity_key(receipt.fn, receipt.fd, receipt.fp)
    if key != doc.identity_key:
        if key is not None:
            clash = (db.query(UnifiedDocument.id)
                     .filter(UnifiedDocument.identity_key == key,
                             UnifiedDocument.id != doc.id).first())
            if clash:
                raise HTTPException(409, "Чек с такими ФН/ФД/ФП уже существует")
        doc.identity_key = key
        if key is None:
            doc.review_flags |= REVIEW_INCOMPLETE_IDENTITY
        else:
            doc.review_flags &= ~REVIEW_INCOMPLETE_IDENTITY
    data = _core(receipt, REGISTRY)
    doc.review_flags &= ~_compare_core(doc, data)
    _apply_core(doc, data)
    items = _items(db, receipt, REGISTRY)
    if items or not doc.items:
        _set_items(db, doc, items)
        doc.review_flags &= ~REVIEW_ITEMS
    status, checked = _fns_evidence(receipt, REGISTRY)
    _merge_fns(doc, status, checked)
    own = _ensure_ownership(db, doc)
    if receipt.created_by and not own.created_by:
        own.created_by = receipt.created_by
    doc.version += 1
    _add_event(db, doc, "sync", actor, details={"source_kind": REGISTRY})
    _project_pool(db, doc)
    db.flush()
    return doc


def _recheck_actor(db: Session, actor: User, roles: tuple[str, ...],
                   company_id: str | None = None) -> User:
    fresh = (db.query(User).populate_existing().filter(User.id == actor.id)
             .with_for_update().first())
    if (not fresh or not fresh.is_active or fresh.role not in roles
            or (company_id and fresh.role != "admin" and fresh.company_id != company_id)):
        raise HTTPException(403, "Права изменились — обновите страницу")
    return fresh


def _recheck_company(db: Session, company_id: str) -> Company:
    comp = (db.query(Company).populate_existing().filter(Company.id == company_id)
            .with_for_update().first())
    if not comp or not comp.is_active:
        raise HTTPException(409, "Компания назначения не найдена или отключена")
    return comp


def claim_pool(db: Session, pool: PoolReceipt, company: Company, actor: User,
               *, event_type: str = "claim") -> UnifiedDocument:
    """Закрепить пуловой чек за компанией без второго канонического документа."""
    doc = document_for_source(db, POOL, pool.id)
    if doc is None:
        doc = register_source(db, POOL, pool, actor, strict=True, event_type="register")
    _lock_values(db, pool.fn, pool.fd, pool.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == doc.id).with_for_update(of=UnifiedDocument).first())
    actor = _recheck_actor(db, actor, ("admin", "accountant"), company.id)
    company = _recheck_company(db, company.id)
    if doc.deleted_at is not None or doc.review_flags & REVIEW_BLOCKING:
        raise HTTPException(409, "Чек требует разбора перед закреплением")
    own = _ensure_ownership(db, doc)
    if own.company_id not in (None, company.id):
        raise HTTPException(409, "Чек уже принадлежит другой организации")
    if own.company_id != company.id:
        own.company_id = company.id
        own.assigned_at = _now()
        own.assigned_by = actor.id
        doc.version += 1
        _add_event(db, doc, event_type, actor, to_company=company.id)
    pool = (db.query(PoolReceipt).populate_existing().filter(PoolReceipt.id == pool.id)
            .with_for_update().first())
    if pool is None or pool.status != "verified":
        raise HTTPException(409, "Чек недоступен для закрепления")
    if pool.assigned_company_id not in (None, company.id):
        raise HTTPException(409, "Чек уже закреплён за другой организацией")
    pool.assigned_company_id = company.id
    pool.assigned_at = pool.assigned_at or _now()
    pool.assigned_by = pool.assigned_by or actor.id
    db.flush()
    return doc


def release_pool(db: Session, pool: PoolReceipt, actor: User) -> UnifiedDocument:
    doc = document_for_source(db, POOL, pool.id)
    if doc is None:
        doc = register_source(db, POOL, pool, actor, strict=True)
    _lock_values(db, pool.fn, pool.fd, pool.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == doc.id).with_for_update(of=UnifiedDocument).first())
    own = _ensure_ownership(db, doc)
    if any(o.source_kind == REGISTRY for o in doc.origins):
        raise HTTPException(409, "Чек уже находится в реестре: сначала решите вопрос с его учётом")
    old = own.company_id
    if old is not None:
        actor = _recheck_actor(db, actor, ("admin", "accountant"), old)
        own.company_id = None
        own.assigned_at = None
        own.assigned_by = ""
        doc.version += 1
        _add_event(db, doc, "release", actor, from_company=old)
    pool.assigned_company_id = None
    pool.assigned_at = None
    pool.assigned_by = ""
    db.flush()
    return doc


def _legacy_projections(db: Session, doc: UnifiedDocument) -> tuple[list[Receipt], list[PoolReceipt]]:
    registries, pools = [], []
    for origin in list(doc.origins):
        row = _source(db, origin.source_kind, origin.source_id)
        if row is None:
            db.delete(origin)
            continue
        if origin.source_kind == REGISTRY:
            registries.append(row)
        else:
            pools.append(row)
    return registries, pools


def move_document(db: Session, document_id: str, target_company_id: str, actor: User,
                  *, expected_version: int | None = None,
                  idempotency_key: str | None = None,
                  resolve_ownership: bool = False) -> UnifiedDocument:
    """Атомарно сменить владельца канонического документа и всех проекций."""
    doc = db.get(UnifiedDocument, document_id)
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(404, "Чек не найден")
    _lock_values(db, doc.fn, doc.fd, doc.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == document_id).with_for_update(of=UnifiedDocument).first())
    actor = _recheck_actor(db, actor, ("admin",))
    if idempotency_key:
        prior = (db.query(UnifiedEvent)
                 .filter(UnifiedEvent.actor_id == actor.id,
                         UnifiedEvent.idempotency_key == idempotency_key).first())
        if prior:
            if prior.document_id != doc.id or prior.to_company_id != target_company_id:
                raise HTTPException(409, "Ключ идемпотентности уже использован для другой операции")
            return doc
    target = _recheck_company(db, target_company_id)
    if expected_version is not None and doc.version != expected_version:
        raise HTTPException(409, "Версия чека изменилась — обновите страницу")
    blocking = doc.review_flags & REVIEW_BLOCKING
    if blocking:
        if resolve_ownership and blocking == REVIEW_OWNERSHIP:
            doc.review_flags &= ~REVIEW_OWNERSHIP
        else:
            raise HTTPException(409, "Чек требует разбора перед перемещением")
    own = _ensure_ownership(db, doc)
    old = own.company_id
    if old == target.id and not resolve_ownership:
        return doc
    registries, pools = _legacy_projections(db, doc)
    own.company_id = target.id
    own.assigned_at = _now()
    own.assigned_by = actor.id
    for receipt in registries:
        receipt.company_id = target.id
        receipt.exported = False
        receipt.exported_at = None
    for pool in pools:
        pool.assigned_company_id = target.id
        pool.assigned_at = _now()
        pool.assigned_by = actor.id
    doc.version += 1
    _add_event(db, doc, "move", actor, from_company=old, to_company=target.id,
               idempotency_key=idempotency_key,
               details={"resolved_ownership": bool(resolve_ownership)})
    db.flush()
    return doc


def detach_registry(db: Session, receipt: Receipt, actor: User | None = None) -> None:
    """Удаление учётной проекции не стирает пуловую историю и канонический чек."""
    doc = document_for_source(db, REGISTRY, receipt.id)
    if doc is None:
        return
    _lock_values(db, receipt.fn, receipt.fd, receipt.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == doc.id).with_for_update(of=UnifiedDocument).first())
    origin = (db.query(UnifiedOrigin)
              .filter(UnifiedOrigin.source_kind == REGISTRY,
                      UnifiedOrigin.source_id == receipt.id).first())
    if origin:
        db.delete(origin)
        db.flush()
    remaining = [o for o in doc.origins if o.source_kind != REGISTRY or o.source_id != receipt.id]
    _add_event(db, doc, "detach_registry", actor, details={"remaining_origins": len(remaining)})
    if not remaining:
        doc.deleted_at = _now()
        own = _ensure_ownership(db, doc)
        own.company_id = None
    doc.version += 1
    db.flush()


def resolve_ownership(db: Session, document_id: str, company_id: str | None, actor: User,
                      *, expected_version: int, reason: str) -> UnifiedDocument:
    doc = db.get(UnifiedDocument, document_id)
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(404, "Чек не найден")
    _lock_values(db, doc.fn, doc.fd, doc.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == document_id).with_for_update(of=UnifiedDocument).first())
    actor = _recheck_actor(db, actor, ("admin",))
    if doc.version != expected_version:
        raise HTTPException(409, "Версия чека изменилась — обновите страницу")
    if not (doc.review_flags & REVIEW_OWNERSHIP):
        raise HTTPException(409, "У чека нет конфликта принадлежности")
    if company_id:
        _recheck_company(db, company_id)
    own = _ensure_ownership(db, doc)
    old = own.company_id
    own.company_id = company_id
    own.assigned_at = _now() if company_id else None
    own.assigned_by = actor.id if company_id else ""
    registries, pools = _legacy_projections(db, doc)
    for receipt in registries:
        receipt.company_id = company_id
        receipt.exported = False
        receipt.exported_at = None
    for pool in pools:
        pool.assigned_company_id = company_id
        pool.assigned_at = _now() if company_id else None
        pool.assigned_by = actor.id if company_id else ""
    doc.review_flags &= ~REVIEW_OWNERSHIP
    doc.version += 1
    _add_event(db, doc, "resolve_ownership", actor, from_company=old, to_company=company_id,
               details={"reason": reason[:500]})
    db.flush()
    return doc


def retarget_company(db: Session, from_company_id: str, to_company_id: str | None,
                     actor: User, *, wipe: bool = False) -> dict:
    """Перенос/очистка компании через каноническое владение и все проекции."""
    actor = _recheck_actor(db, actor, ("admin",))
    if to_company_id:
        _recheck_company(db, to_company_id)
    docs = (db.query(UnifiedDocument).join(UnifiedOwnership)
            .filter(UnifiedOwnership.company_id == from_company_id,
                    UnifiedDocument.deleted_at.is_(None))
            .order_by(UnifiedDocument.fn, UnifiedDocument.fd,
                      UnifiedDocument.fp, UnifiedDocument.id).all())
    moved = deleted = 0
    for doc in docs:
        _lock_values(db, doc.fn, doc.fd, doc.fp)
        doc = (db.query(UnifiedDocument).populate_existing()
               .filter(UnifiedDocument.id == doc.id).with_for_update(of=UnifiedDocument).first())
        registries, pools = _legacy_projections(db, doc)
        own = _ensure_ownership(db, doc)
        if wipe:
            for receipt in registries:
                detach_registry(db, receipt, actor)
                db.delete(receipt)
                deleted += 1
            for pool in pools:
                pool.assigned_company_id = None
                pool.assigned_at = None
                pool.assigned_by = ""
            if not any(o.source_kind == POOL for o in doc.origins):
                doc.deleted_at = _now()
            own.company_id = None
            own.assigned_at = None
            own.assigned_by = ""
            doc.version += 1
            _add_event(db, doc, "company_wipe", actor, from_company=from_company_id)
        else:
            own.company_id = to_company_id
            own.assigned_at = _now()
            own.assigned_by = actor.id
            for receipt in registries:
                receipt.company_id = to_company_id
                receipt.exported = False
                receipt.exported_at = None
            for pool in pools:
                pool.assigned_company_id = to_company_id
                pool.assigned_at = _now()
                pool.assigned_by = actor.id
            doc.version += 1
            _add_event(db, doc, "move", actor, from_company=from_company_id,
                       to_company=to_company_id)
            moved += 1
        db.flush()
    return {"moved": moved, "deleted": deleted}


def can_view_document(db: Session, user: User, doc: UnifiedDocument,
                      company_id: str | None = None) -> bool:
    """Повторная проверка прав по текущему состоянию БД, не по снимку токена."""
    fresh = db.get(User, user.id)
    if fresh is None or not fresh.is_active:
        return False
    if doc.deleted_at is not None:
        return fresh.role == "admin"
    own = doc.ownership
    if fresh.role == "admin":
        return company_id is None or (own is not None and own.company_id == company_id)
    if own is None or doc.review_flags & REVIEW_OWNERSHIP:
        return False
    if own.company_id != fresh.company_id:
        return False
    if fresh.company_id:
        comp = db.get(Company, fresh.company_id)
        if comp is None or not comp.is_active:
            return False
    if fresh.role == "user":
        return own.created_by == fresh.id
    return fresh.role in ("accountant", "manager")


def scope_documents(query, db: Session, user: User, company_id: str | None = None):
    """Область списка канонических документов для штатного аккаунта."""
    fresh = db.get(User, user.id)
    if fresh is None or not fresh.is_active:
        return query.filter(False)
    if fresh.role == "admin":
        if company_id:
            query = query.filter(UnifiedOwnership.company_id == company_id)
        return query
    query = query.filter(UnifiedDocument.deleted_at.is_(None),
                         UnifiedOwnership.company_id == fresh.company_id,
                         (UnifiedDocument.review_flags.op("&")(REVIEW_OWNERSHIP)) == 0)
    if fresh.role == "user":
        query = query.filter(UnifiedOwnership.created_by == fresh.id)
    elif fresh.role not in ("accountant", "manager"):
        query = query.filter(False)
    return query


def stats(db: Session) -> dict:
    from sqlalchemy import func
    total = db.query(func.count(UnifiedDocument.id)).filter(UnifiedDocument.deleted_at.is_(None)).scalar() or 0
    conflicts = (db.query(func.count(UnifiedDocument.id))
                 .filter(UnifiedDocument.deleted_at.is_(None),
                         UnifiedDocument.review_flags.op("&")(REVIEW_BLOCKING) != 0).scalar() or 0)
    ownership_conflicts = (db.query(func.count(UnifiedDocument.id))
                           .filter(UnifiedDocument.deleted_at.is_(None),
                                   UnifiedDocument.review_flags.op("&")(REVIEW_OWNERSHIP) != 0).scalar() or 0)
    origins = db.query(func.count(UnifiedOrigin.id)).scalar() or 0
    return {"documents": total, "conflicts": conflicts,
            "ownership_conflicts": ownership_conflicts, "origins": origins}


def document_payload(db: Session, doc: UnifiedDocument, *, with_events: bool = False) -> dict:
    own = doc.ownership
    company_name = None
    if own and own.company_id:
        comp = db.get(Company, own.company_id)
        company_name = comp.name if comp else None
    payload = {
        "id": doc.id,
        "fn": doc.fn, "fd": doc.fd, "fp": doc.fp,
        "receipt_date": doc.receipt_date.isoformat() if doc.receipt_date else None,
        "total_sum": doc.total_sum,
        "operation": doc.operation,
        "merchant_name": doc.merchant_name,
        "merchant_inn": doc.merchant_inn,
        "merchant_address": doc.merchant_address,
        "cashier": doc.cashier,
        "region_code": doc.region_code,
        "region_city": doc.region_city,
        "industry": doc.industry,
        "fns_status": doc.fns_status,
        "fns_checked_at": doc.fns_checked_at.isoformat() + "Z" if doc.fns_checked_at else None,
        "company_id": own.company_id if own else None,
        "company_name": company_name,
        "created_by": own.created_by if own else None,
        "review_flags": doc.review_flags,
        "version": doc.version,
        "items_count": len(doc.items),
        "origins": [{"kind": o.source_kind, "id": o.source_id} for o in doc.origins],
        "items": [{
            "id": it.id, "position": it.position, "name": it.name,
            "quantity": it.quantity, "price": it.price, "total": it.total,
            "vat_rate": it.vat_rate, "vat_sum": it.vat_sum,
            "product_type": it.product_type, "payment_type": it.payment_type,
            "unit": it.unit, "product_group": it.product_group,
            "industry": it.industry,
        } for it in doc.items],
    }
    if with_events:
        events = (db.query(UnifiedEvent).filter(UnifiedEvent.document_id == doc.id)
                  .order_by(UnifiedEvent.created_at, UnifiedEvent.id).all())
        payload["events"] = [{
            "id": e.id, "type": e.event_type,
            "actor_id": e.actor_id,
            "from_company_id": e.from_company_id,
            "to_company_id": e.to_company_id,
            "details": json.loads(e.details or "{}"),
            "created_at": e.created_at.isoformat() + "Z",
        } for e in events]
    return payload
