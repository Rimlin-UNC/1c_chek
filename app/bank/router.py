"""Банковская выписка: API для бухгалтера и администратора (T-45, v1.70.98).

Бухгалтер работает со своей компанией, администратор — с выбранной (company_id обязателен). Чужие выписки,
операции и чеки — 404, как у первички и «Что нужно от клиента». Файл выписки не хранится: только итоги
разбора и операции, которым нужны документы (ADR-28).
"""
from __future__ import annotations

import csv
import io
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, require_accountant
from ..database import get_db
from ..models import Company, User
from ..services.audit import log_action
from . import parser, service
from .models import BankOperation, BankStatement

router = APIRouter(prefix="/api/v1/bank", tags=["bank"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]
CompanyId = Annotated[str | None, Query(max_length=36)]


class CardIn(BaseModel):
    tail: str = Field(..., max_length=8)
    user_id: str = Field("", max_length=36)


class SettingsIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    retention_days: int = Field(service.DEFAULT_RETENTION, ge=1, le=100000)
    cards: list[CardIn] = Field(default_factory=list, max_length=service.MAX_CARDS + 1)


class OperationIn(BaseModel):
    action: Literal["ignore", "open", "link"]
    note: str = Field("", max_length=300)
    ref_type: Literal["receipt", "primary", ""] = ""
    ref_id: str = Field("", max_length=36)


class DecisionIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    status: Literal["paid", "declined", "pending"]
    note: str = Field("", max_length=300)


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


def _fail(exc: service.BankError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("")
def overview(user: Accountant, db: Db, company_id: CompanyId = None,
             status: Annotated[Literal["open", "matched", "ignored", "all"], Query()] = "open") -> dict:
    company = _company(db, user, company_id)
    return {"company": {"id": company.id, "name": company.short_name or company.name},
            **service.overview(db, company.id, status)}


@router.post("/statements")
async def upload(user: Accountant, db: Db, file: Annotated[UploadFile, File()],
                 company_id: Annotated[str | None, Form()] = None) -> dict:
    company = _company(db, user, company_id)
    raw = await file.read(parser.MAX_BYTES + 1)
    try:
        result = service.import_statement(db, company, raw, user)
    except service.BankError as exc:
        raise _fail(exc) from exc
    log_action(user, "bank_statement_uploaded", details={
        "company": company.name, "period": f"{result['statement']['date_from']} — {result['statement']['date_to']}",
        "operations": result["operations"], "saved": result["saved"], "matched": result["matched"]})
    return result


@router.delete("/statements/{statement_id}")
def remove(statement_id: str, user: Accountant, db: Db) -> dict:
    st = db.get(BankStatement, statement_id[:36])
    if st is None or (user.role != ROLE_ADMIN and st.company_id != user.company_id):
        raise HTTPException(404, "Выписка не найдена")
    company = db.get(Company, st.company_id)
    removed = service.delete_statement(db, st, user)
    log_action(user, "bank_statement_deleted", details={"company": company.name if company else "",
                                                        "operations": removed})
    return {"deleted": removed}


@router.post("/match")
def rematch(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    """Сопоставить открытые операции заново: пришли новые чеки и УПД."""
    company = _company(db, user, company_id)
    return {"matched": service.rematch(db, company.id)}


@router.post("/operations/{op_id}")
def operation(op_id: str, user: Accountant, db: Db, body: OperationIn) -> dict:
    op = db.get(BankOperation, op_id[:36])
    if op is None or (user.role != ROLE_ADMIN and op.company_id != user.company_id):
        raise HTTPException(404, "Операция не найдена")
    try:
        service.set_operation(db, op, body.action, user, body.note, body.ref_type, body.ref_id)
    except service.BankError as exc:
        raise _fail(exc) from exc
    if body.action == "ignore":
        log_action(user, "bank_operation_ignored", details={"amount": op.amount, "date": op.op_date.isoformat(),
                                                            "note": op.match_note})
    return service.op_dict(op)


@router.get("/reimburse")
def reimburse(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    return service.reimbursements(db, company.id)


@router.get("/reimburse.csv")
def reimburse_csv(user: Accountant, db: Db, company_id: CompanyId = None) -> Response:
    """К возмещению — таблица для авансового отчёта в 1С (точка с запятой, UTF-8 с меткой для Excel)."""
    company = _company(db, user, company_id)
    data = service.reimbursements(db, company.id)
    buf = io.StringIO()
    out = csv.writer(buf, delimiter=";")
    out.writerow(["Сотрудник", "Дата чека", "Продавец", "Оплата", "Сумма чека", "К возмещению", "Решение",
                  "Примечание"])
    titles = {"pending": "к возмещению", "paid": "возмещено", "declined": "не возмещать"}
    for group in data["employees"]:
        for item in group["items"]:
            out.writerow([group["name"], item["date"][:10], item["merchant"], item["payment"],
                          f"{item['total_sum']:.2f}".replace(".", ","), f"{item['amount']:.2f}".replace(".", ","),
                          titles.get(item["status"], item["status"]), item["note"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="k_vozmeshcheniyu.csv"'})


@router.post("/reimburse/{receipt_id}")
def decide(receipt_id: str, user: Accountant, db: Db, body: DecisionIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        result = service.decide(db, company.id, receipt_id[:36], body.status, user, body.note)
    except service.BankError as exc:
        raise _fail(exc) from exc
    if body.status != "pending":
        log_action(user, "bank_reimbursement_decided", details={"company": company.name, "status": body.status,
                                                                "note": result.get("note", "")})
    return result


@router.get("/settings")
def get_settings(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    return service.settings_dict(db, service.settings_of(db, company.id))


@router.put("/settings")
def put_settings(user: Accountant, db: Db, body: SettingsIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        saved = service.save_settings(db, company.id, body.model_dump(), user)
    except service.BankError as exc:
        raise _fail(exc) from exc
    log_action(user, "bank_settings_updated", details={"company": company.name, "retention_days": saved.retention_days,
                                                       "cards": len(service.cards_of(saved))})
    return service.settings_dict(db, saved)
