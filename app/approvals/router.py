"""Согласование авансовых отчётов: API (T-46, v1.70.112, ADR-24, ADR-42).

Видят: сотрудник — свои отчёты; руководитель и бухгалтер — отчёты своей (у аутсорсера — выбранной) компании;
администратор — любой компании. Решают только на своём этапе (service.can_act). Чужие отчёты — 404, как у
чеков. Подпись и решения требуют подтверждения (confirm) и хеша содержимого, которое человек видел (doc_hash).
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..auth import ROLE_ADMIN, client_ip, get_current_user, require_admin
from ..database import get_db
from ..models import Company, User
from ..services.audit import log_action
from . import service
from .models import AdvanceApproval

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

COMPANY_ROLES = ("admin", "accountant", "manager", "user")


def require_member(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role not in COMPANY_ROLES:
        raise HTTPException(403, "Согласование — для сотрудников компании")
    return user


Member = Annotated[User, Depends(require_member)]
Admin = Annotated[User, Depends(require_admin)]
Db = Annotated[Session, Depends(get_db)]
CompanyId = Annotated[str | None, Query(max_length=36)]
Day = Annotated[str, Field(min_length=10, max_length=10)]
Hash = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")]


class SettingsIn(BaseModel):
    company_id: str = Field(..., min_length=1, max_length=36)
    enabled: bool = False
    timeout_hours: int = Field(72, ge=service.TIMEOUT_RANGE[0], le=service.TIMEOUT_RANGE[1])
    manager_limit: float = Field(0, ge=0, le=service.LIMIT_MAX)
    accountant_limit: float = Field(0, ge=0, le=service.LIMIT_MAX)
    channels: list[Literal["telegram", "max"]] = Field(default_factory=list, max_length=2)


class CreateIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)          # администратор — выбранная компания
    employee_id: str | None = Field(None, max_length=36)         # бухгалтер и администратор — за сотрудника
    date_from: Day
    date_to: Day
    purpose: str = Field("", max_length=500)
    receipt_ids: list[Annotated[str, Field(max_length=36)]] | None = Field(None, max_length=service.MAX_RECEIPTS)
    previous_id: str = Field("", max_length=36)


class SignIn(BaseModel):
    doc_hash: Hash
    confirm: Literal[True]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    doc_hash: Hash
    confirm: Literal[True]
    comment: str = Field("", max_length=2000)


class CancelIn(BaseModel):
    comment: str = Field("", max_length=2000)


def _meta(request: Request) -> dict:
    return {"ip": client_ip(request), "user_agent": request.headers.get("user-agent", "")}


def _company(db: Session, user: User, company_id: str | None) -> Company:
    """Компания запроса: у администратора — выбранная, у остальных — своя (у аутсорсера — активный клиент)."""
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


def _approval(db: Session, user: User, approval_id: str) -> AdvanceApproval:
    approval = db.get(AdvanceApproval, approval_id) if len(approval_id) <= 36 else None
    if approval is None or not service.can_view(user, approval):
        raise HTTPException(404, "Отчёт не найден")
    return approval


def _fail(exc: service.ApprovalError):
    raise HTTPException(exc.status, str(exc)) from exc


def _scope(db: Session, user: User, box: str, company_id: str | None):
    """Списки: «Ждут меня» — мой ход (подпись своего отчёта или решение на моём этапе); «Мои» — где я сотрудник
    или автор; «Все» — компания (сотруднику — только свои)."""
    q = db.query(AdvanceApproval)
    own = or_(AdvanceApproval.employee_id == user.id, AdvanceApproval.created_by == user.id)
    if box == "mine" or (box == "all" and user.role == "user"):
        return q.filter(own)
    if box == "todo":
        draft = and_(AdvanceApproval.status == "draft", AdvanceApproval.employee_id == user.id)
        stage = {"manager": "manager", "accountant": "accountant", ROLE_ADMIN: "accountant"}.get(user.role)
        if stage is None:
            return q.filter(draft)
        turn = and_(AdvanceApproval.status == "pending", AdvanceApproval.stage == stage,
                    AdvanceApproval.employee_id != user.id)
        if user.role != ROLE_ADMIN:
            turn = and_(turn, AdvanceApproval.company_id == user.company_id)
        elif company_id:
            turn = and_(turn, AdvanceApproval.company_id == company_id)
        return q.filter(or_(draft, turn))
    if user.role == ROLE_ADMIN:
        return q.filter(AdvanceApproval.company_id == company_id) if company_id else q
    return q.filter(AdvanceApproval.company_id == user.company_id)


@router.get("/settings")
def get_settings(user: Member, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    from ..docreq import service as docreq
    return {"company": {"id": company.id, "name": company.short_name or company.name},
            "settings": service.settings_dict(service.settings_of(db, company.id)),
            "channels": {k: v for k, v in docreq.channel_status(db).items() if k in service.CHANNELS},
            "can_edit": user.role == ROLE_ADMIN}


@router.put("/settings")
def put_settings(body: SettingsIn, user: Admin, db: Db) -> dict:
    company = _company(db, user, body.company_id)
    try:
        s = service.save_settings(db, company.id, body.model_dump(), user)
    except service.ApprovalError as exc:
        _fail(exc)
    log_action(user, "approvals_settings", "company", company.id, details=service.settings_dict(s))
    return {"settings": service.settings_dict(s)}


@router.get("/employees")
def employees(user: Member, db: Db, company_id: CompanyId = None) -> dict:
    """Кому можно собрать отчёт: сотрудник — себе; бухгалтер и администратор — любому сотруднику компании."""
    company = _company(db, user, company_id)
    if user.role in ("user", "manager"):
        people = [user]
    else:
        people = (db.query(User).filter(User.company_id == company.id, User.is_active.is_(True),
                                        User.role.in_(("user", "manager", "accountant")))
                  .order_by(User.full_name, User.username).all())
    return {"items": [{"id": u.id, "name": u.full_name or u.username, "role": u.role} for u in people]}


def _employee(db: Session, user: User, company: Company, employee_id: str | None) -> User:
    if not employee_id or employee_id == user.id:
        return user
    if user.role not in ("admin", "accountant"):
        raise HTTPException(403, "Отчёт за другого сотрудника собирает бухгалтер")
    employee = db.get(User, employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(404, "Сотрудник не найден в компании")
    return employee


@router.get("/preview")
def preview(user: Member, db: Db, date_from: Annotated[str, Query(max_length=10)],
            date_to: Annotated[str, Query(max_length=10)], employee_id: CompanyId = None,
            company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    employee = _employee(db, user, company, employee_id)
    try:
        receipts = service.employee_receipts(db, company.id, employee, date_from, date_to)
    except service.ApprovalError as exc:
        _fail(exc)
    items = [service._item(r) for r in receipts]
    return {"employee": {"id": employee.id, "name": employee.full_name or employee.username},
            "count": len(items), "total": round(sum(i["amount"] for i in items), 2), "items": items[:50],
            "enabled": service.is_enabled(db, company.id)}


@router.get("")
def list_approvals(user: Member, db: Db, box: Literal["todo", "mine", "all"] = "todo",
                   company_id: CompanyId = None, limit: Annotated[int, Query(ge=1, le=200)] = 100) -> dict:
    rows = (_scope(db, user, box, company_id).order_by(AdvanceApproval.created_at.desc()).limit(limit).all())
    counts = {name: _scope(db, user, name, company_id).count() for name in ("todo", "mine")}
    enabled = service.is_enabled(db, company_id if user.role == ROLE_ADMIN else user.company_id)
    return {"items": [service.brief(a) for a in rows], "counts": counts, "enabled": enabled}


@router.post("")
def create(body: CreateIn, request: Request, user: Member, db: Db) -> dict:
    company = _company(db, user, body.company_id)
    employee = _employee(db, user, company, body.employee_id)
    try:
        approval = service.create(db, user, company, employee, body.date_from, body.date_to, purpose=body.purpose,
                                  receipt_ids=body.receipt_ids, previous_id=body.previous_id, meta=_meta(request))
    except service.ApprovalError as exc:
        _fail(exc)
    log_action(user, "approval_created", "advance_approval", approval.id,
               details={"number": approval.number, "employee": approval.employee_name, "total": approval.total})
    return _detail(db, user, approval)


def _detail(db: Session, user: User, approval: AdvanceApproval) -> dict:
    events = service.events_of(db, approval.id)
    snap = service.snapshot_of(approval)
    s = service.settings_of(db, approval.company_id)
    out = service.brief(approval)
    out.update({
        "snapshot": snap, "changes": service.changes(db, approval),
        "integrity": {"content": service.sha256(approval.snapshot or "") == approval.doc_hash,
                      "journal": service.verify(events)},
        "events": [service.event_dict(e) for e in events],
        "approvers": [u.full_name or u.username for u in service.approvers(db, approval)]
        if approval.status in ("draft", "pending") else [],
        "timeout_hours": int(s.timeout_hours or 72),
        "can": {"sign": approval.status == "draft" and service.can_act(user, approval),
                "decide": approval.status == "pending" and service.can_act(user, approval),
                "cancel": service.can_cancel(user, approval),
                "resubmit": approval.status in ("rejected", "cancelled") and user.id in (
                    approval.employee_id, approval.created_by)},
    })
    return out


@router.get("/{approval_id}")
def detail(approval_id: str, user: Member, db: Db) -> dict:
    return _detail(db, user, _approval(db, user, approval_id))


def _write(db: Session, fn):
    try:
        return fn()
    except service.ApprovalError as exc:
        db.rollback()
        _fail(exc)
    except (StaleDataError, IntegrityError):
        db.rollback()
        raise HTTPException(409, "Отчёт уже изменился — обновите страницу") from None


@router.post("/{approval_id}/sign")
def sign(approval_id: str, body: SignIn, request: Request, user: Member, db: Db) -> dict:
    approval = _approval(db, user, approval_id)
    _write(db, lambda: service.sign(db, approval, user, body.doc_hash, meta=_meta(request)))
    log_action(user, "approval_signed", "advance_approval", approval.id, details={"number": approval.number})
    return _detail(db, user, approval)


@router.post("/{approval_id}/decision")
def decision(approval_id: str, body: DecisionIn, request: Request, user: Member, db: Db) -> dict:
    approval = _approval(db, user, approval_id)
    stage = approval.stage
    _write(db, lambda: service.decide(db, approval, user, body.decision, body.doc_hash, body.comment,
                                      meta=_meta(request)))
    log_action(user, "approval_approved" if body.decision == "approve" else "approval_rejected",
               "advance_approval", approval.id, details={"number": approval.number, "stage": stage})
    return _detail(db, user, approval)


@router.post("/{approval_id}/cancel")
def cancel(approval_id: str, body: CancelIn, request: Request, user: Member, db: Db) -> dict:
    approval = _approval(db, user, approval_id)
    _write(db, lambda: service.cancel(db, approval, user, body.comment, meta=_meta(request)))
    log_action(user, "approval_cancelled", "advance_approval", approval.id, details={"number": approval.number})
    return _detail(db, user, approval)
