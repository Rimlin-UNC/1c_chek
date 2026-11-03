"""Транзакционные операции единого фискального документа.

Функции не делают commit и не вызывают ФНС.  Вызывающий маршрут держит одну
транзакцию для канонического документа и совместимых проекций.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert, or_, select
from sqlalchemy.orm import Session, aliased, lazyload

from ..models import Company, Receipt, ReceiptItem, User, uid
from ..pool.models import PoolItem, PoolReceipt
from ..services.receipt_identity import lock_identities, lock_identity
from . import links
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

log = logging.getLogger("ymaster")


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


def _registry_item(pos: int, it: ReceiptItem) -> dict:
    return {
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
    }


def _items(db: Session, row: Any, kind: str) -> list[dict]:
    if kind == REGISTRY:
        rows = (db.query(ReceiptItem).filter(ReceiptItem.receipt_id == row.id)
                .order_by(ReceiptItem.position, ReceiptItem.id).all())
        return [_registry_item(pos, it) for pos, it in enumerate(rows)]
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
        # Значения по умолчанию колонок применяются только при INSERT: до flush
        # review_flags/version у нового объекта — None. Задаём их явно (иначе
        # чек из архива без ФП падал с TypeError при первой правке).
        doc = UnifiedDocument(identity_key=key, version=1,
                              review_flags=REVIEW_INCOMPLETE_IDENTITY if key is None else 0, **data)
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


def _register_lazily(db: Session, receipt: Receipt, actor: User | None) -> UnifiedDocument | None:
    """Чек из архива (до переноса) попадает в единый реестр при первой правке.

    Конфликт с уже существующей единой записью не блокирует работу с чеком:
    регистрация откатывается в своей точке сохранения, чек остаётся в прежнем
    режиме, а расхождение покажет перенос архива (``app.unified.backfill``).
    """
    try:
        with db.begin_nested():
            return register_source(db, REGISTRY, receipt, actor, strict=True, event_type="sync")
    except HTTPException as exc:
        log.warning("Единый реестр: чек %s оставлен в прежнем режиме — %s", receipt.id, exc.detail)
        return None
    except Exception:  # noqa: BLE001 — регистрация необязательна, правка чека важнее
        log.error("Единый реестр: чек %s оставлен в прежнем режиме — сбой регистрации",
                  receipt.id, exc_info=True)
        return None


def sync_registry(db: Session, receipt: Receipt, actor: User | None = None) -> UnifiedDocument | None:
    """После правки/обогащения реестра обновить каноническую запись и пул."""
    db.flush()
    doc = document_for_source(db, REGISTRY, receipt.id)
    if doc is None:
        return _register_lazily(db, receipt, actor)
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


def sync_registry_safely(db: Session, receipt: Receipt, actor: User | None = None) -> bool:
    """Синхронизация для фоновых путей обогащения (ФНС, внешние источники).

    Ответ ФНС уже оплачен суточной квотой и важнее канонической копии: сбой
    единого реестра откатывает только собственную точку сохранения, данные
    чека сохраняются как раньше, причина уходит в журнал. Каноническая запись
    догонит реестр при следующей правке или переносе архива.
    """
    try:
        with db.begin_nested():
            sync_registry(db, receipt, actor)
        return True
    except Exception as exc:  # noqa: BLE001 — защитный контур: проверка чека важнее
        log.warning("Единый реестр: синхронизация чека %s пропущена — %s",
                    receipt.id, getattr(exc, "detail", exc))
        return False


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
    # «Единая база» (T-34): прежний владелец — в список компаний чека,
    # заявка компании-получателя исполнена.
    links.after_move(db, [(doc.id, old)], target.id, actor.id, _now())
    db.flush()
    return doc


# --------------------------------------------------------------------------
#  Пакетный перенос (T-33, ADR-20). Результат — как у прежнего цикла по чекам
#  document_for_source → register_source(strict=True) → move_document, но
#  тройки блокируются одной вставкой, записи выбираются и обновляются пачками,
#  а права и компания назначения проверяются один раз.
# --------------------------------------------------------------------------
CHUNK = 500


def _chunks(values: list, size: int = CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _lock_many(db: Session, triples) -> int:
    """Пакетный аналог _lock_values: неполные тройки не блокируются.

    Полнота — то же правило, что у identity_key (ни одно значение не пустое
    после strip), но без лишнего хеширования: ключ считает lock_identities.
    """
    complete = []
    for fn, fd, fp in triples:
        values = (fn or "", fd or "", fp or "")
        if all(v.strip() for v in values):
            complete.append(values)
    return lock_identities(db, complete)


def _bulk_documents(db: Session):
    """Выборка единых записей для пакетных операций: позиции и источники не
    подгружаются заранее (у связей lazy="selectin"), а читаются по требованию."""
    return db.query(UnifiedDocument).options(lazyload(UnifiedDocument.items),
                                             lazyload(UnifiedDocument.origins))


def _insert_rows(db: Session, model, rows: list[dict]) -> None:
    """Вставка новых строк пачками без отслеживания объектов в сессии."""
    for chunk in _chunks(rows):
        db.execute(insert(model), chunk)


def _event_row(document_id: str, event_type: str, actor: User | None, when: datetime, *,
               from_company: str | None = None, to_company: str | None = None,
               details: dict | None = None) -> dict:
    """Строка unified_events с теми же полями, что пишет _add_event."""
    return {"id": uid(), "document_id": document_id, "event_type": event_type,
            "actor_id": actor.id if actor else None,
            "from_company_id": from_company, "to_company_id": to_company,
            "idempotency_key": None,
            "details": json.dumps(details or {}, ensure_ascii=False),
            "created_at": when}


def _registry_documents(db: Session, receipt_ids: list[str]) -> dict[str, str]:
    """{id чека реестра: id единой записи} одним запросом на пачку."""
    found: dict[str, str] = {}
    for chunk in _chunks(receipt_ids):
        found.update(db.query(UnifiedOrigin.source_id, UnifiedOrigin.document_id)
                     .filter(UnifiedOrigin.source_kind == REGISTRY,
                             UnifiedOrigin.source_id.in_(chunk)).all())
    return found


def _registry_items_many(db: Session, receipt_ids: list[str]) -> dict[str, list[dict]]:
    """Позиции нескольких чеков реестра — те же словари и порядок, что у _items."""
    grouped: dict[str, list[ReceiptItem]] = defaultdict(list)
    for chunk in _chunks(receipt_ids):
        for it in (db.query(ReceiptItem).filter(ReceiptItem.receipt_id.in_(chunk))
                   .order_by(ReceiptItem.receipt_id, ReceiptItem.position, ReceiptItem.id)):
            grouped[it.receipt_id].append(it)
    return {rid: [_registry_item(pos, it) for pos, it in enumerate(rows)]
            for rid, rows in grouped.items()}


def _register_registry_many(db: Session, receipts: list[Receipt],
                            actor: User | None) -> dict[str, str]:
    """Регистрация чеков реестра в едином слое пачкой (strict).

    Быстрый путь — только полная тройка, у которой единой записи ещё нет: для
    неё результат совпадает с register_source(strict=True) для нового
    документа. Неполная тройка и уже существующая запись (из пула, удалённая)
    идут прежним register_source по одному чеку — со всеми его проверками.
    Тройки должен заранее заблокировать вызывающий (_lock_many).
    Возвращает {id чека: id единой записи}.
    """
    keys = {r.id: identity_key(r.fn, r.fd, r.fp) for r in receipts}
    existing: set[str] = set()
    complete = [k for k in keys.values() if k]
    for chunk in _chunks(complete):
        existing.update(k for (k,) in db.query(UnifiedDocument.identity_key)
                        .filter(UnifiedDocument.identity_key.in_(chunk)))
    fresh = [r for r in receipts if keys[r.id] and keys[r.id] not in existing]
    result: dict[str, str] = {}
    if fresh:
        items = _registry_items_many(db, [r.id for r in fresh])
        now = _now()
        docs, owners, item_rows, origins, events = [], [], [], [], []
        for r in fresh:
            doc_id = uid()
            # Как register_source для нового документа: version 1 при создании
            # и +1 за регистрацию; review_flags 0 — тройка полная; статус ФНС —
            # тем же _merge_fns от «unknown».
            fns = SimpleNamespace(fns_status="unknown", fns_checked_at=None, review_flags=0)
            _merge_fns(fns, *_fns_evidence(r, REGISTRY))
            docs.append({"id": doc_id, "identity_key": keys[r.id], **_core(r, REGISTRY),
                         "fns_status": fns.fns_status, "fns_checked_at": fns.fns_checked_at,
                         "review_flags": fns.review_flags, "version": 2, "deleted_at": None,
                         "created_at": now, "updated_at": now})
            owner = r.company_id or None
            owners.append({"document_id": doc_id, "company_id": owner,
                           "created_by": r.created_by or None,
                           "assigned_at": now if owner else None,
                           "assigned_by": (actor.id if actor else "") if owner else ""})
            item_rows.extend({"id": uid(), "document_id": doc_id, **data} for data in items.get(r.id, []))
            origins.append({"id": uid(), "document_id": doc_id, "source_kind": REGISTRY,
                            "source_id": r.id, "created_at": now})
            events.append(_event_row(doc_id, "register", actor, now, to_company=owner,
                                     details={"source_kind": REGISTRY, "created": True}))
            result[r.id] = doc_id
        _insert_rows(db, UnifiedDocument, docs)
        _insert_rows(db, UnifiedOwnership, owners)
        _insert_rows(db, UnifiedItem, item_rows)
        _insert_rows(db, UnifiedOrigin, origins)
        _insert_rows(db, UnifiedEvent, events)
    fresh_ids = {r.id for r in fresh}
    for r in receipts:
        if r.id not in fresh_ids:
            result[r.id] = register_source(db, REGISTRY, r, actor, strict=True).id
    return result


class BlockedDocuments(HTTPException):
    """409: часть документов пачки ждёт разбора (Q-24, решение 03.11.2026).

    Пачка по умолчанию атомарна — не переносится ни один документ. Исключение
    несёт id документов на разборе и число тех, что можно перенести, чтобы
    вызывающий показал список и предложил «переместить остальные».
    """

    def __init__(self, document_ids: list[str], movable: int) -> None:
        super().__init__(409, "Чек требует разбора перед перемещением")
        self.document_ids = document_ids
        self.movable = movable


def move_documents(db: Session, document_ids: list[str], target_company_id: str,
                   actor: User, *, locked: bool = False,
                   skip_blocked: bool = False) -> tuple[list[str], list[str]]:
    """Пакетная смена владельца: для каждого документа — то же, что
    move_document без expected_version и ключа идемпотентности.

    Документы, которые уже у компании назначения, пропускаются до блокировок и
    проверки пометок — как в прежнем цикле переноса. Если хоть один документ не
    найден или удалён, исключение прерывает всю пачку (вызывающий откатывает
    транзакцию). Документ, требующий разбора, тоже прерывает пачку
    (BlockedDocuments) — если не задан ``skip_blocked``: тогда такие документы
    остаются у прежнего владельца, остальные переносятся (Q-24).
    ``locked=True`` — тройки уже заблокированы вызывающим в этой транзакции.
    Возвращает (id перенесённых, id пропущенных на разборе).
    """
    ids = list(dict.fromkeys(document_ids))
    owners: dict[str, UnifiedOwnership] = {}
    docs: dict[str, UnifiedDocument] = {}

    def load_owners(keys: list[str], fresh: bool) -> None:
        query = db.query(UnifiedOwnership)
        if fresh:
            query = query.populate_existing()
        for chunk in _chunks(keys):
            owners.update((o.document_id, o) for o in
                          query.filter(UnifiedOwnership.document_id.in_(chunk)))

    def load_docs(keys: list[str], for_update: bool) -> None:
        for chunk in _chunks(sorted(keys)):
            query = _bulk_documents(db).filter(UnifiedDocument.id.in_(chunk))
            if for_update:
                query = (query.populate_existing().order_by(UnifiedDocument.id)
                         .with_for_update(of=UnifiedDocument))
            docs.update((d.id, d) for d in query)

    # Тройки уже заблокированы вызывающим — достаточно одного чтения под
    # блокировкой строк; иначе сначала блокируем, потом перечитываем.
    load_owners(ids, fresh=locked)
    pending = [i for i in ids if not (i in owners and owners[i].company_id == target_company_id)]
    if not pending:
        return [], []
    load_docs(pending, for_update=locked)
    if any(docs.get(i) is None or docs[i].deleted_at is not None for i in pending):
        raise HTTPException(404, "Чек не найден")
    if not locked:
        _lock_many(db, [(docs[i].fn, docs[i].fd, docs[i].fp) for i in pending])
        load_docs(pending, for_update=True)
    actor = _recheck_actor(db, actor, ("admin",))
    target = _recheck_company(db, target_company_id)
    blocked = [i for i in pending if docs[i].review_flags & REVIEW_BLOCKING]
    if blocked and not skip_blocked:
        raise BlockedDocuments(blocked, len(pending) - len(blocked))
    if blocked:
        skip = set(blocked)
        pending = [i for i in pending if i not in skip]
        if not pending:
            return [], blocked
    if not locked:
        load_owners(pending, fresh=True)
    created = [UnifiedOwnership(document_id=i, company_id=None, created_by=None,
                                assigned_at=None, assigned_by="") for i in pending if i not in owners]
    if created:
        db.add_all(created)
        db.flush()
        owners.update((o.document_id, o) for o in created)
    origins: dict[str, list[UnifiedOrigin]] = defaultdict(list)
    for chunk in _chunks(pending):
        for origin in db.query(UnifiedOrigin).filter(UnifiedOrigin.document_id.in_(chunk)):
            origins[origin.document_id].append(origin)
    source_ids = {REGISTRY: [], POOL: []}
    for group in origins.values():
        for origin in group:
            source_ids[REGISTRY if origin.source_kind == REGISTRY else POOL].append(origin.source_id)
    registries: dict[str, Receipt] = {}
    pools: dict[str, PoolReceipt] = {}
    for chunk in _chunks(source_ids[REGISTRY]):
        # Позиции чека переносу не нужны — не загружаем их заранее (selectin).
        registries.update((r.id, r) for r in db.query(Receipt).options(lazyload("*"))
                          .filter(Receipt.id.in_(chunk)))
    for chunk in _chunks(source_ids[POOL]):
        pools.update((p.id, p) for p in db.query(PoolReceipt).options(lazyload("*"))
                     .filter(PoolReceipt.id.in_(chunk)))
    now = _now()
    moved: list[str] = []
    previous: dict[str, str | None] = {}
    events: list[dict] = []
    for i in pending:
        doc, own = docs[i], owners[i]
        old = own.company_id
        if old == target.id:
            continue
        previous[i] = old
        for origin in origins.get(i, []):
            if origin.source_kind == REGISTRY:
                receipt = registries.get(origin.source_id)
                if receipt is None:
                    db.delete(origin)
                    continue
                receipt.company_id = target.id
                receipt.exported = False
                receipt.exported_at = None
            else:
                pool = pools.get(origin.source_id)
                if pool is None:
                    db.delete(origin)
                    continue
                pool.assigned_company_id = target.id
                pool.assigned_at = now
                pool.assigned_by = actor.id
        own.company_id = target.id
        own.assigned_at = now
        own.assigned_by = actor.id
        doc.version += 1
        events.append(_event_row(i, "move", actor, now, from_company=old, to_company=target.id,
                                 details={"resolved_ownership": False}))
        moved.append(i)
    db.flush()
    _insert_rows(db, UnifiedEvent, events)
    links.after_move(db, [(i, previous[i]) for i in moved], target.id, actor.id, now)
    return moved, blocked


# Коды причин разбора в ответе перемещения — те же, что у экрана «Разбор конфликтов».
BLOCKING_REASONS = ((REVIEW_OWNERSHIP, "ownership"), (REVIEW_DATE, "date"),
                    (REVIEW_AMOUNT, "amount"), (REVIEW_OPERATION, "operation"),
                    (REVIEW_ITEMS, "items"), (REVIEW_MERCHANT, "merchant"))


def blocking_reasons(flags: int) -> list[str]:
    """Коды причин разбора — те же, что у экрана «Разбор конфликтов»."""
    return [code for bit, code in BLOCKING_REASONS if flags & bit]


def move_registry_receipts(db: Session, receipts: list[Receipt], target_company_id: str,
                           actor: User, *, skip_blocked: bool = False,
                           ) -> tuple[list[Receipt], int, list[dict]]:
    """Пакетный перенос чеков реестра в другую компанию (T-33, Q-24).

    Возвращает перенесённые чеки (в порядке ФН, ФД, ФП, id — как прежний цикл),
    число чеков, которые уже были у компании назначения, и пропущенные чеки на
    разборе ({receipt_id, reasons}) — только при ``skip_blocked``. Без него чек
    на разборе отменяет всю пачку: 409 со списком таких чеков и числом тех,
    что можно перенести («Переместить остальные»).
    """
    ordered = sorted(receipts, key=lambda r: (r.fn, r.fd, r.fp, r.id))
    _lock_many(db, [(r.fn, r.fd, r.fp) for r in ordered])
    doc_of = _registry_documents(db, [r.id for r in ordered])
    missing = [r for r in ordered if r.id not in doc_of]
    if missing:
        doc_of.update(_register_registry_many(db, missing, actor))

    def blocked_items(document_ids) -> list[dict]:
        wanted = set(document_ids)
        flags: dict[str, int] = {}
        for chunk in _chunks(sorted(wanted)):
            flags.update(db.query(UnifiedDocument.id, UnifiedDocument.review_flags)
                         .filter(UnifiedDocument.id.in_(chunk)).all())
        return [{"receipt_id": r.id, "reasons": blocking_reasons(flags.get(doc_of[r.id], 0))}
                for r in ordered if doc_of[r.id] in wanted]

    try:
        moved_ids, blocked_ids = move_documents(db, [doc_of[r.id] for r in ordered],
                                                target_company_id, actor, locked=True,
                                                skip_blocked=skip_blocked)
    except BlockedDocuments as exc:
        items = blocked_items(exc.document_ids)
        raise HTTPException(409, detail={
            "message": (f"Чек требует разбора перед перемещением: {len(items)} из {len(ordered)} "
                        "отмеченных ждут решения в «Разборе конфликтов». Не перемещён ни один чек."),
            "blocked": items,
            "movable": exc.movable,
        }) from None
    moved_docs = set(moved_ids)
    moved = [r for r in ordered if doc_of[r.id] in moved_docs]
    skipped = blocked_items(blocked_ids) if blocked_ids else []
    return moved, len(ordered) - len(moved) - len(skipped), skipped


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
    # Компании с копией чека до решения: проигравшие получат заявку «отклонена».
    contenders = ({r.company_id for r in registries if r.company_id}
                  | {p.assigned_company_id for p in pools if p.assigned_company_id})
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
    links.after_resolve(db, doc.id, contenders, company_id, actor.id, _now())
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
    moved_ids: list[str] = []
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
            moved_ids.append(doc.id)
        db.flush()
    # «Единая база» (T-34): компания удаляется — её заявки больше не учитываются;
    # чеки переехали к получателю — его заявки на них исполнены.
    links.close_company(db, from_company_id, _now())
    links.satisfy_claims(db, moved_ids, to_company_id, actor.id, _now())
    db.flush()
    return {"moved": moved, "deleted": deleted}


def _projections_agree(db: Session, doc: UnifiedDocument, user: User) -> bool:
    """Живые проекции подтверждают владение (защита от устаревшей копии).

    После отката на старую версию (она меняет только реестр и пул) владелец
    в каноне может отстать. Тогда штатный аккаунт не должен видеть чужой уже
    чек: каждая живая проекция обязана принадлежать его компании, а хотя бы
    одна — подтверждать доступ (для сотрудника — его собственный чек).
    """
    cid = user.company_id
    origins = db.query(UnifiedOrigin).filter(UnifiedOrigin.document_id == doc.id).all()
    regs = [r for r in (db.get(Receipt, o.source_id) for o in origins if o.source_kind == REGISTRY) if r]
    pools = [p for p in (db.get(PoolReceipt, o.source_id) for o in origins if o.source_kind == POOL) if p]
    if any(r.company_id != cid for r in regs):
        return False
    if any(p.assigned_company_id not in (None, cid) for p in pools):
        return False
    if user.role == "user":
        return any(r.created_by == user.id for r in regs)
    return bool(regs) or any(p.assigned_company_id == cid for p in pools)


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
    if not fresh.company_id or own.company_id != fresh.company_id:
        return False
    comp = db.get(Company, fresh.company_id)
    if comp is None or not comp.is_active:
        return False
    if fresh.role == "user" and own.created_by != fresh.id:
        return False
    if fresh.role not in ("user", "accountant", "manager"):
        return False
    return _projections_agree(db, doc, fresh)


def _projection_filters(user: User) -> list:
    """SQL-зеркало _projections_agree для списков единого реестра."""
    cid = user.company_id

    def linked(model, kind, *conds):
        origin = aliased(UnifiedOrigin)
        return (select(origin.id).join(model, model.id == origin.source_id)
                .where(origin.document_id == UnifiedDocument.id,
                       origin.source_kind == kind, *conds)).exists()

    foreign = [~linked(Receipt, REGISTRY, or_(Receipt.company_id.is_(None), Receipt.company_id != cid)),
               ~linked(PoolReceipt, POOL, PoolReceipt.assigned_company_id.isnot(None),
                       PoolReceipt.assigned_company_id != cid)]
    if user.role == "user":
        return [*foreign, linked(Receipt, REGISTRY, Receipt.company_id == cid,
                                 Receipt.created_by == user.id)]
    return [*foreign, or_(linked(Receipt, REGISTRY, Receipt.company_id == cid),
                          linked(PoolReceipt, POOL, PoolReceipt.assigned_company_id == cid))]


def scope_documents(query, db: Session, user: User, company_id: str | None = None):
    """Область списка канонических документов для штатного аккаунта."""
    fresh = db.get(User, user.id)
    if fresh is None or not fresh.is_active:
        return query.filter(False)
    if fresh.role == "admin":
        if company_id:
            query = query.filter(UnifiedOwnership.company_id == company_id)
        return query
    if not fresh.company_id or fresh.role not in ("user", "accountant", "manager"):
        return query.filter(False)
    comp = db.get(Company, fresh.company_id)
    if comp is None or not comp.is_active:
        return query.filter(False)
    query = query.filter(UnifiedDocument.deleted_at.is_(None),
                         UnifiedOwnership.company_id == fresh.company_id,
                         (UnifiedDocument.review_flags.op("&")(REVIEW_OWNERSHIP)) == 0,
                         *_projection_filters(fresh))
    if fresh.role == "user":
        query = query.filter(UnifiedOwnership.created_by == fresh.id)
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
            "ownership_conflicts": ownership_conflicts, "origins": origins,
            "claims_open": links.open_claims_count(db)}


def visible_users(db: Session, viewer: User, user_ids) -> set[str]:
    """Кого штатная роль видит по id: себя и сотрудников своей компании (ADR-20)."""
    visible = {viewer.id}
    wanted = sorted({u for u in user_ids if u and u != viewer.id})
    if viewer.company_id and wanted:
        for chunk in _chunks(wanted):
            visible.update(user_id for (user_id,) in db.query(User.id).filter(
                User.id.in_(chunk), User.company_id == viewer.company_id))
    return visible


def document_payload(db: Session, doc: UnifiedDocument, *, with_events: bool = False,
                     viewer: User | None = None, colleagues: set[str] | None = None) -> dict:
    """Карточка единой записи.

    ``viewer`` не администратор — сведения о других организациях скрыты:
    автор из чужой компании (после переноса) и журнал (см. _viewer_events).
    ``colleagues`` — заранее найденные visible_users для страницы списка.
    """
    own = doc.ownership
    staff = viewer is not None and viewer.role != "admin"
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
    events = []
    if with_events:
        events = (db.query(UnifiedEvent).filter(UnifiedEvent.document_id == doc.id)
                  .order_by(UnifiedEvent.created_at, UnifiedEvent.id).all())
    if staff:
        if colleagues is None:
            colleagues = visible_users(db, viewer, [payload["created_by"],
                                                    *(e.actor_id for e in events)])
        author = payload["created_by"]
        payload["created_by_hidden"] = bool(author) and author not in colleagues
        if payload["created_by_hidden"]:
            payload["created_by"] = None
    if with_events:
        if staff:
            payload["events"] = _viewer_events(events, viewer, colleagues)
        else:
            payload["events"] = [{
                "id": e.id, "type": e.event_type,
                "actor_id": e.actor_id,
                "from_company_id": e.from_company_id,
                "to_company_id": e.to_company_id,
                "details": json.loads(e.details or "{}"),
                "created_at": e.created_at.isoformat() + "Z",
            } for e in events]
    return payload


# Журнал карточки для штатных ролей (T-32, ADR-20): компания видит только себя.
# Ключи details без сведений о других организациях; свободный текст причины
# решения спора (resolve_ownership) сюда не входит, у сверки причина — код.
VIEWER_DETAIL_KEYS = frozenset({"source_kind", "created", "resolved_ownership",
                                "remaining_origins", "company", "author"})
VIEWER_REASON_CODES = frozenset({"source_deleted", "projection_changed"})


def _viewer_events(events: list[UnifiedEvent], viewer: User, colleagues: set[str]) -> list[dict]:
    """События без чужих компаний, чужих исполнителей и причины решения спора.

    Своя компания и сотрудники своей компании видны как есть; чужие заменены
    на null с признаком *_hidden («другая организация»). Администратор
    платформы для компании — тоже другая организация.
    """
    own_company = viewer.company_id

    def company(value: str | None) -> tuple[str | None, bool]:
        if value is None:
            return None, False
        if own_company and value == own_company:
            return value, False
        return None, True

    result = []
    for e in events:
        if e.event_type in links.ADMIN_ONLY_EVENTS:
            # решение по чужой заявке раскрыло бы, что чек пыталась учесть
            # другая организация (T-34, Q-22)
            continue
        details = json.loads(e.details or "{}")
        safe = {k: v for k, v in details.items() if k in VIEWER_DETAIL_KEYS}
        if e.event_type == "reconcile" and details.get("reason") in VIEWER_REASON_CODES:
            safe["reason"] = details["reason"]
        from_id, from_hidden = company(e.from_company_id)
        to_id, to_hidden = company(e.to_company_id)
        actor_visible = e.actor_id is None or e.actor_id in colleagues
        result.append({
            "id": e.id, "type": e.event_type,
            "actor_id": e.actor_id if actor_visible else None,
            "actor_hidden": not actor_visible,
            "from_company_id": from_id, "from_company_hidden": from_hidden,
            "to_company_id": to_id, "to_company_hidden": to_hidden,
            "details": safe,
            "created_at": e.created_at.isoformat() + "Z",
        })
    return result


def _live_copies(db: Session, doc: UnifiedDocument) -> list[tuple[str, Any]]:
    """Живые копии документа (реестр и пул) — только чтение.

    В отличие от _legacy_projections ничего не удаляет: экран разбора читает
    карточку GET-запросом, и висячая ссылка остаётся на сверку при старте.
    """
    copies = []
    for origin in doc.origins:
        row = _source(db, origin.source_kind, origin.source_id)
        if row is not None:
            copies.append((origin.source_kind, row))
    return copies


def review_payload(db: Session, doc: UnifiedDocument) -> dict:
    """Карточка для экрана разбора конфликтов (только администратор).

    К обычной карточке добавляются копии чека с компаниями, кандидаты во
    владельцы и журнал с именами сотрудников и компаний.  Штатным ролям это
    не отдаётся: их /documents/{id} — без сведений о других организациях
    (document_payload с viewer, ADR-20).
    """
    payload = document_payload(db, doc, with_events=True)
    copies = []
    for kind, row in _live_copies(db, doc):
        copies.append({"kind": kind, "id": row.id, "company_id": _company_id(row, kind)})
    company_ids = {c["company_id"] for c in copies if c["company_id"]}
    if payload["company_id"]:
        company_ids.add(payload["company_id"])
    for event in payload["events"]:
        company_ids.update(c for c in (event["from_company_id"], event["to_company_id"]) if c)
    names = ({c.id: c.name for c in db.query(Company).filter(Company.id.in_(company_ids))}
             if company_ids else {})
    actor_ids = {e["actor_id"] for e in payload["events"] if e["actor_id"]}
    actors = ({u.id: (u.full_name or u.username) for u in db.query(User).filter(User.id.in_(actor_ids))}
              if actor_ids else {})
    for copy in copies:
        copy["company_name"] = names.get(copy["company_id"]) if copy["company_id"] else None
    for event in payload["events"]:
        event["actor_name"] = actors.get(event["actor_id"]) if event["actor_id"] else None
        event["from_company_name"] = names.get(event["from_company_id"]) if event["from_company_id"] else None
        event["to_company_name"] = names.get(event["to_company_id"]) if event["to_company_id"] else None
    # Кандидаты во владельцы — компании, у которых есть копия чека (порядок копий).
    candidates: dict[str, dict] = {}
    for copy in copies:
        if not copy["company_id"]:
            continue
        cand = candidates.setdefault(copy["company_id"], {
            "company_id": copy["company_id"], "company_name": copy["company_name"], "sources": []})
        if copy["kind"] not in cand["sources"]:
            cand["sources"].append(copy["kind"])
    payload["copies"] = copies
    payload["candidates"] = list(candidates.values())
    payload["deleted"] = doc.deleted_at is not None
    payload["can_resolve"] = bool(doc.review_flags & REVIEW_OWNERSHIP) and doc.deleted_at is None
    return payload
