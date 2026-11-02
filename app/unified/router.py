"""API единого фискального документа (канонический слой)."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_accountant, require_admin
from ..config import settings
from ..database import get_db
from ..models import User
from . import service
from .models import UnifiedDocument, UnifiedOwnership

router = APIRouter(prefix="/api/v1/unified", tags=["Единый реестр чеков"])
log = logging.getLogger("ymaster")


class MoveBody(BaseModel):
    company_id: str = Field(min_length=1, max_length=36)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)
    resolve_ownership: bool = False


class ResolveBody(BaseModel):
    company_id: str | None = Field(default=None, max_length=36)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


@router.get("/stats", summary="Единый реестр: счётчики и конфликты")
def stats(db: Annotated[Session, Depends(get_db)],
          user: Annotated[User, Depends(require_accountant)]):
    if user.role != "admin":
        raise HTTPException(403, "Только администратор")
    return service.stats(db)


@router.get("/documents", summary="Единый реестр: канонические чеки")
def documents(*, company_id: str = Query("", max_length=36),
              page: int = Query(1, ge=1, le=settings.PAGE_MAX), per_page: int = Query(20, ge=1, le=100),
              conflicts: bool = False,
              flag: Annotated[int, Query(ge=0, le=255,
                                         description="Битовая маска review_flags: только чеки с этими пометками")] = 0,
              db: Annotated[Session, Depends(get_db)],
              user: Annotated[User, Depends(get_current_user)]):
    query = db.query(UnifiedDocument).outerjoin(UnifiedOwnership)
    query = service.scope_documents(query, db, user, company_id or None)
    if conflicts:
        from .models import REVIEW_BLOCKING
        # v1.70.83: удалённый документ разобрать нельзя (resolve отвечает 404),
        # и счётчик stats его не считает — список конфликтов с ним совпадает.
        query = query.filter(UnifiedDocument.deleted_at.is_(None),
                             UnifiedDocument.review_flags.op("&")(REVIEW_BLOCKING) != 0)
    if flag:
        query = query.filter(UnifiedDocument.review_flags.op("&")(flag) != 0)
    total = query.count()
    rows = (query.order_by(UnifiedDocument.receipt_date.desc(), UnifiedDocument.id)
            .offset((page - 1) * per_page).limit(per_page).all())
    colleagues = None
    if user.role != "admin":
        # Авторы страницы: свои видны, из чужих компаний — скрыты (ADR-20).
        colleagues = service.visible_users(
            db, user, [doc.ownership.created_by for doc in rows if doc.ownership])
    return {"total": total, "page": page, "per_page": per_page,
            "items": [service.document_payload(db, doc, viewer=user, colleagues=colleagues)
                      for doc in rows]}


@router.get("/documents/{document_id}", summary="Единый реестр: карточка и журнал")
def document(document_id: str,
             db: Annotated[Session, Depends(get_db)],
             user: Annotated[User, Depends(get_current_user)]):
    doc = db.get(UnifiedDocument, document_id)
    if doc is None or not service.can_view_document(db, user, doc):
        raise HTTPException(404, "Чек не найден")
    return service.document_payload(db, doc, with_events=True, viewer=user)


@router.get("/documents/{document_id}/review",
            summary="Единый реестр: карточка разбора конфликта (администратор)")
def review(document_id: str,
           db: Annotated[Session, Depends(get_db)],
           _admin: Annotated[User, Depends(require_admin)]):
    """Копии чека по компаниям, кандидаты во владельцы и журнал с именами."""
    doc = db.get(UnifiedDocument, document_id)
    if doc is None:
        raise HTTPException(404, "Чек не найден")
    return service.review_payload(db, doc)


@router.post("/documents/{document_id}/move", summary="Единый реестр: переместить чек")
def move(document_id: str, body: MoveBody,
         db: Annotated[Session, Depends(get_db)],
         user: Annotated[User, Depends(require_admin)]):
    doc = service.move_document(db, document_id, body.company_id, user,
                                expected_version=body.expected_version,
                                idempotency_key=body.idempotency_key,
                                resolve_ownership=body.resolve_ownership)
    db.commit()
    return service.document_payload(db, doc)


@router.post("/documents/{document_id}/resolve-ownership",
             summary="Единый реестр: разрешить конфликт принадлежности")
def resolve(document_id: str, body: ResolveBody,
            db: Annotated[Session, Depends(get_db)],
            user: Annotated[User, Depends(require_admin)]):
    doc = service.resolve_ownership(db, document_id, body.company_id, user,
                                    expected_version=body.expected_version,
                                    reason=body.reason)
    db.commit()
    return service.document_payload(db, doc)


# --------------------------------------------------------------------------
#  v1.70.85 «Единая база», фазы 1–2 (T-34): заявки компаний на чужие чеки.
#  Всё — только администратор: штатные роли не видят ни списка, ни числа.
# --------------------------------------------------------------------------
class ClaimDecisionBody(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


@router.get("/claims", summary="Единый реестр: заявки компаний на чужие чеки (администратор)")
def claims(*, status: str = Query("open", max_length=16,
                                  description="open | declined | transferred | void | all"),
           page: int = Query(1, ge=1, le=settings.PAGE_MAX), per_page: int = Query(20, ge=1, le=100),
           db: Annotated[Session, Depends(get_db)],
           _admin: Annotated[User, Depends(require_admin)]):
    from . import links
    return links.claims_page(db, status=status, page=page, per_page=per_page)


@router.get("/documents/{document_id}/companies",
            summary="Единый реестр: сколько организаций учитывают чек и какие (администратор)")
def document_companies(document_id: str,
                       db: Annotated[Session, Depends(get_db)],
                       _admin: Annotated[User, Depends(require_admin)]):
    from . import links
    doc = db.get(UnifiedDocument, document_id)
    if doc is None:
        raise HTTPException(404, "Чек не найден")
    return links.companies_payload(db, doc)


@router.post("/links/{link_id}/{action}",
             summary="Единый реестр: решение по заявке — transfer, decline или void")
def decide_claim(link_id: str, action: str, body: ClaimDecisionBody,
                 db: Annotated[Session, Depends(get_db)],
                 user: Annotated[User, Depends(require_admin)]):
    from ..services.audit import log_action
    from . import links
    link, doc, moved = links.decide(db, link_id, action, user,
                                    expected_version=body.expected_version, reason=body.reason)
    if moved:
        # Компания сменилась — пересчёт политики и правил назначения, как при
        # обычном перемещении чеков (POST /api/v1/receipts/move). Сбой пересчёта
        # решение не отменяет: причина — в журнал приложения.
        try:
            from ..services import policy
            policy.refresh_receipts(db, moved)
        except Exception:                              # noqa: BLE001
            log.warning("Единый реестр: пересчёт политики после передачи чека пропущен", exc_info=True)
        try:
            from ..services import merchant_rules
            merchant_rules.apply_rules_many(db, moved)
        except Exception:                              # noqa: BLE001
            log.warning("Единый реестр: правила назначения после передачи чека пропущены", exc_info=True)
    db.commit()
    result = {"ok": True, "action": action, "status": link.status, "link_id": link.id,
              "document_id": doc.id, "version": doc.version, "company_id": link.company_id}
    log_action(user, f"unified_claim_{action}", "unified_document", doc.id,
               {"link_id": link.id, "company_id": link.company_id})
    if moved:
        from ..services.events import broadcast
        broadcast("receipts_moved", {"ids": [r.id for r in moved], "company_id": link.company_id})
    return result
