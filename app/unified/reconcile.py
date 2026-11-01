"""Сверка единого реестра с рабочими таблицами при старте приложения.

Зачем. Откат на версию без единого реестра (1.70.80 и раньше) и повторное
обновление — штатный сценарий («Последние рабочие версии»). Пока работает
старая версия, она меняет только таблицы реестра и пула: переносит чеки между
компаниями, удаляет их, снимает закрепление, удаляет компании. При следующем
старте новая версия сводит каноническую запись к фактическому состоянию.

Правила — только однозначные исправления:

* ссылка на удалённую строку реестра или пула снимается; документ без ссылок
  помечается удалённым — так же, как при обычном удалении чека;
* документ на разборе принадлежности не трогается: его решает администратор;
* если все живые копии указывают на одну компанию, она становится владельцем;
  у документа только из пула без закрепления владельца нет;
* копии расходятся между компаниями — документ не меняется (видимость всё
  равно определяют штатные фильтры реестра и пула), расхождение — в журнал.

Каждое исправление пишется событием ``reconcile`` в журнал документа. Сверка
идемпотентна и дешёвая, когда расхождений нет (один запрос-кандидат).
"""
from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from ..models import Receipt
from ..pool.models import PoolReceipt
from . import service
from .models import REVIEW_OWNERSHIP, UnifiedDocument, UnifiedOrigin, UnifiedOwnership

log = logging.getLogger("ymaster")


def _dangling_origins(db: Session) -> list[UnifiedOrigin]:
    reg = (db.query(UnifiedOrigin)
           .filter(UnifiedOrigin.source_kind == service.REGISTRY,
                   ~select(Receipt.id).where(Receipt.id == UnifiedOrigin.source_id).exists()))
    pool = (db.query(UnifiedOrigin)
            .filter(UnifiedOrigin.source_kind == service.POOL,
                    ~select(PoolReceipt.id).where(PoolReceipt.id == UnifiedOrigin.source_id).exists()))
    return reg.all() + pool.all()


def _linked(model, kind, *conds):
    origin = aliased(UnifiedOrigin)
    return (select(origin.id).join(model, model.id == origin.source_id)
            .where(origin.document_id == UnifiedDocument.id, origin.source_kind == kind, *conds)).exists()


def _drift_candidates(db: Session) -> list[str]:
    """Документы, у которых владелец или автор канона разошёлся с проекциями."""
    owner = UnifiedOwnership.company_id
    reg_company = _linked(Receipt, service.REGISTRY, Receipt.company_id.is_distinct_from(owner))
    reg_author = _linked(Receipt, service.REGISTRY, Receipt.created_by.isnot(None),
                         Receipt.created_by.is_distinct_from(UnifiedOwnership.created_by))
    pool_company = _linked(PoolReceipt, service.POOL, PoolReceipt.assigned_company_id.isnot(None),
                           PoolReceipt.assigned_company_id.is_distinct_from(owner))
    pool_released = (owner.isnot(None)
                     & ~_linked(Receipt, service.REGISTRY)
                     & ~_linked(PoolReceipt, service.POOL, PoolReceipt.assigned_company_id == owner))
    rows = (db.query(UnifiedDocument.id)
            .join(UnifiedOwnership, UnifiedOwnership.document_id == UnifiedDocument.id)
            .filter(UnifiedDocument.deleted_at.is_(None),
                    (UnifiedDocument.review_flags.op("&")(REVIEW_OWNERSHIP)) == 0,
                    or_(reg_company, reg_author, pool_company, pool_released))
            .order_by(UnifiedDocument.id).all())
    return [r[0] for r in rows]


def _legacy_owner(regs: list[Receipt], pools: list[PoolReceipt]) -> tuple[bool, str | None]:
    """(однозначно?, компания) по живым проекциям документа."""
    pool_companies = {p.assigned_company_id for p in pools if p.assigned_company_id}
    if regs:
        companies = {r.company_id for r in regs}
        if len(companies) != 1:
            return False, None
        owner = next(iter(companies))
        return (not (pool_companies - {owner})), owner
    if len(pool_companies) > 1:
        return False, None
    return True, next(iter(pool_companies), None)


def reconcile(db: Session) -> dict:
    """Свести канон к рабочим таблицам. Не делает commit — решает вызывающий."""
    out = {"dangling_removed": 0, "documents_closed": 0, "owners_fixed": 0,
           "authors_fixed": 0, "ambiguous": 0}
    touched: set[str] = set()
    for origin in _dangling_origins(db):
        touched.add(origin.document_id)
        db.delete(origin)
        out["dangling_removed"] += 1
    db.flush()
    for doc_id in sorted(touched):
        doc = db.get(UnifiedDocument, doc_id)
        if doc is None or doc.deleted_at is not None:
            continue
        left = db.query(UnifiedOrigin).filter(UnifiedOrigin.document_id == doc_id).count()
        if left:
            continue
        own = service._ensure_ownership(db, doc)
        old = own.company_id
        doc.deleted_at = service._now()
        own.company_id, own.assigned_at, own.assigned_by = None, None, ""
        doc.version += 1
        service._add_event(db, doc, "reconcile", None, from_company=old,
                           details={"reason": "source_deleted"})
        out["documents_closed"] += 1
    db.flush()
    for doc_id in _drift_candidates(db):
        doc = (db.query(UnifiedDocument).populate_existing()
               .filter(UnifiedDocument.id == doc_id).with_for_update(of=UnifiedDocument).first())
        if doc is None or doc.deleted_at is not None or doc.review_flags & REVIEW_OWNERSHIP:
            continue
        origins = db.query(UnifiedOrigin).filter(UnifiedOrigin.document_id == doc_id).all()
        # populate_existing: решение принимается по строкам из БД, а не по
        # объектам, оставшимся в сессии с прошлых операций
        reg_ids = [o.source_id for o in origins if o.source_kind == service.REGISTRY]
        pool_ids = [o.source_id for o in origins if o.source_kind == service.POOL]
        regs = (db.query(Receipt).populate_existing().filter(Receipt.id.in_(reg_ids)).all()
                if reg_ids else [])
        pools = (db.query(PoolReceipt).populate_existing().filter(PoolReceipt.id.in_(pool_ids)).all()
                 if pool_ids else [])
        clear, owner = _legacy_owner(regs, pools)
        if not clear:
            out["ambiguous"] += 1
            log.warning("Единый реестр: копии документа %s принадлежат разным компаниям — "
                        "оставлено до разбора", doc_id)
            continue
        own = service._ensure_ownership(db, doc)
        changes: dict = {}
        if own.company_id != owner:
            changes["company"] = True
            old = own.company_id
            own.company_id = owner
            own.assigned_at = service._now() if owner else None
            own.assigned_by = "reconcile" if owner else ""
            out["owners_fixed"] += 1
        else:
            old = own.company_id
        authors = {r.created_by for r in regs if r.created_by}
        if len(regs) == 1 and len(authors) == 1 and own.created_by not in authors:
            own.created_by = next(iter(authors))
            changes["author"] = True
            out["authors_fixed"] += 1
        if changes:
            doc.version += 1
            service._add_event(db, doc, "reconcile", None, from_company=old, to_company=owner,
                               details={"reason": "projection_changed", **changes})
    db.flush()
    return out


def reconcile_on_startup() -> dict | None:
    """Сверка при старте: сбой не останавливает сервис, причина — в журнал."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        result = reconcile(db)
        db.commit()
        if any(result.values()):
            log.info("Единый реестр: сверка с рабочими таблицами — %s", result)
        return result
    except Exception:  # noqa: BLE001 — сверка не должна останавливать сервис
        db.rollback()
        log.warning("Единый реестр: сверка при старте пропущена", exc_info=True)
        return None
    finally:
        db.close()
