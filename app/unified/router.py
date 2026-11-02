"""API единого фискального документа (канонический слой)."""
from __future__ import annotations

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
    return {"total": total, "page": page, "per_page": per_page,
            "items": [service.document_payload(db, doc) for doc in rows]}


@router.get("/documents/{document_id}", summary="Единый реестр: карточка и журнал")
def document(document_id: str,
             db: Annotated[Session, Depends(get_db)],
             user: Annotated[User, Depends(get_current_user)]):
    doc = db.get(UnifiedDocument, document_id)
    if doc is None or not service.can_view_document(db, user, doc):
        raise HTTPException(404, "Чек не найден")
    return service.document_payload(db, doc, with_events=True)


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
