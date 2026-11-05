"""«Моё дело» по API (T-71, v1.70.107, ADR-37): ключ в карточке компании и отправка актов покупок.

Доступ — бухгалтер (своя компания) и администратор (выбранная компания; company_id обязателен), как у банка
и почтового ящика. Документы чужих компаний — 404, как в первичке (primary.service.visible).

  GET/PUT/DELETE /connection — состояние, сохранить ключ (только зашифрованным), отключить;
  POST /connection/check — проверить ключ запросом на чтение;
  GET /documents/{id} — состояние документа: подключение, что уйдёт и что мешает, отметка об отправке;
  POST /documents/{id}/send — отправить актом покупки (as_services — бухгалтер подтвердил, что это услуги);
  DELETE /documents/{id}/link — забыть отправку (акт удалили в «Моём деле»);
  POST /send/check и POST /send — пакет: отмеченные документы или период; проверка базу не меняет.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, require_accountant
from ..database import get_db
from ..models import Company, User
from ..primary import service as primary_service
from ..primary.models import PrimaryDocument
from ..services.audit import log_action
from . import service
from .service import MoedeloServiceError

router = APIRouter(prefix="/api/v1/moedelo", tags=["moedelo"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]
CompanyId = Annotated[str | None, Query(max_length=36)]
DateType = date


class KeyIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    key: str = Field(..., min_length=1, max_length=300)


class CompanyIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)


class SendIn(BaseModel):
    as_services: bool = False


class BatchIn(BaseModel):
    ids: list[Annotated[str, Field(max_length=36)]] = Field(default_factory=list, max_length=500)
    company_id: str | None = Field(None, max_length=36)
    date_from: DateType | None = None
    date_to: DateType | None = None


def _company(db: Session, user: User, company_id: str | None) -> Company:
    """Компания запроса: у бухгалтера — своя, у администратора — выбранная."""
    if user.role == ROLE_ADMIN:
        if not company_id:
            raise HTTPException(400, "Выберите компанию")
        company = db.get(Company, company_id)
    else:
        if company_id and company_id != user.company_id:
            raise HTTPException(404, "Компания не найдена")
        company = db.get(Company, user.company_id) if user.company_id else None
    if company is None:
        raise HTTPException(404, "Компания не найдена")
    return company


def _document(db: Session, user: User, doc_id: str) -> PrimaryDocument:
    doc = db.get(PrimaryDocument, doc_id)
    if doc is None or not primary_service.visible(user, doc):
        raise HTTPException(404, "Документ не найден")
    return doc


def _fail(exc: MoedeloServiceError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _company_name(db: Session, company_id: str | None) -> str:
    company = db.get(Company, company_id) if company_id else None
    return company.name if company else "—"


@router.get("/connection")
def connection(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    return service.connection_dict(service.connection_of(db, company.id))


@router.put("/connection")
def save_connection(user: Accountant, db: Db, body: KeyIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        conn = service.save_key(db, company, body.key, user)
    except MoedeloServiceError as exc:
        raise _fail(exc) from exc
    log_action(user, "moedelo_key_saved", details={"company": company.name})
    return service.connection_dict(conn)


@router.post("/connection/check")
def check_connection(user: Accountant, db: Db, body: CompanyIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        conn = service.check(db, company, user)
    except MoedeloServiceError as exc:
        raise _fail(exc) from exc
    log_action(user, "moedelo_connected", details={"company": company.name})
    return service.connection_dict(conn)


@router.delete("/connection")
def remove_connection(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    removed = service.remove(db, company)
    if removed:
        log_action(user, "moedelo_disconnected", details={"company": company.name})
    return {"removed": removed}


@router.get("/documents/{doc_id}")
def document_state(doc_id: str, user: Accountant, db: Db) -> dict:
    return service.document_state(db, _document(db, user, doc_id))


@router.post("/documents/{doc_id}/send")
def send_document(doc_id: str, user: Accountant, db: Db, body: SendIn) -> dict:
    doc = _document(db, user, doc_id)
    try:
        link = service.send_document(db, user, doc, as_services=body.as_services)
    except MoedeloServiceError as exc:
        raise _fail(exc) from exc
    log_action(user, "moedelo_document_sent", details={
        "company": _company_name(db, doc.company_id), "document": primary_service.title(doc),
        "number": link.md_number, "status": service.LINK_TITLES.get(link.status, link.status),
        "kontragent_new": bool(link.kontragent_new)})
    return {"link": service.link_dict(link), "state": service.document_state(db, doc)}


@router.delete("/documents/{doc_id}/link")
def forget_link(doc_id: str, user: Accountant, db: Db) -> dict:
    doc = _document(db, user, doc_id)
    removed = service.unlink(db, doc)
    if removed:
        log_action(user, "moedelo_link_removed", details={
            "company": _company_name(db, doc.company_id), "document": primary_service.title(doc)})
    return {"removed": removed}


def _batch_docs(db: Session, user: User, body: BatchIn) -> list[PrimaryDocument]:
    if user.role == ROLE_ADMIN and not body.company_id and not body.ids:
        raise HTTPException(400, "Выберите компанию или отметьте документы")
    return service.selection(db, user, ids=body.ids or None, company_id=body.company_id,
                             date_from=body.date_from, date_to=body.date_to)


@router.post("/send/check")
def batch_check(user: Accountant, db: Db, body: BatchIn) -> dict:
    return service.batch_check(db, _batch_docs(db, user, body))


@router.post("/send")
def batch_send(user: Accountant, db: Db, body: BatchIn) -> dict:
    result = service.batch_send(db, user, _batch_docs(db, user, body))
    log_action(user, "moedelo_batch_sent", details={
        "company": _company_name(db, body.company_id or user.company_id), "sent": result["sent"],
        "found": result["found"], "mismatch": result["mismatch"], "failed": len(result["failed"]),
        "skipped": result["skipped"]})
    return result
