"""«Что нужно от клиента»: API для бухгалтера и администратора (T-58, v1.70.97).

Бухгалтер работает со своей компанией, администратор — с выбранной (company_id обязателен). Чужие
запросы — 404, как у чеков и первички. Журнал только пополняется: запросы не удаляются, а отменяются.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, require_accountant
from ..database import get_db
from ..models import Company, User
from ..services.audit import log_action
from . import service
from .models import DocRequest, DocRequestEvent

router = APIRouter(prefix="/api/v1/doc-requests", tags=["doc-requests"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]
CompanyId = Annotated[str | None, Query(max_length=36)]
Period = Annotated[str | None, Query(max_length=7)]


class PeriodIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    period: str = Field(..., max_length=7)


class ManualIn(PeriodIn):
    title: str = Field(..., max_length=500)
    counterparty_name: str = Field("", max_length=500)
    counterparty_inn: str = Field("", max_length=20)
    amount: float | None = Field(None, ge=0, lt=1e13)


class StatusIn(BaseModel):
    status: Literal["received", "cancelled", "open"]
    note: str = Field("", max_length=500)


class SettingsIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    enabled: bool = False
    auto_detect: bool = True
    deadline_day: int = Field(5, ge=1, le=28)
    x_hour: str = Field("18:00", max_length=5)
    remind_days: list[int] = Field(default_factory=lambda: [3, 1, 0], max_length=5)
    repeat_every: int = Field(2, ge=1, le=14)
    repeat_max: int = Field(3, ge=0, le=10)
    channels: list[Literal["telegram", "max", "email"]] = Field(default_factory=list, max_length=3)
    roles: list[Literal["manager", "user"]] = Field(default_factory=list, max_length=2)
    contact_email: str = Field("", max_length=200)
    escalate_days: int = Field(3, ge=0, le=30)          # v1.70.99: эскалация руководителю, 0 — выключена


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


def _period(period: str | None) -> str:
    value = period or service.default_period()
    try:
        service.parse_period(value)
    except service.DocRequestError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return value


def _users(db: Session, ids: set[str]) -> dict[str, User]:
    ids = {i for i in ids if i}
    return {u.id: u for u in db.query(User).filter(User.id.in_(ids))} if ids else {}


def _overview(db: Session, company: Company, period: str) -> dict:
    s = service.settings_of(db, company.id)
    items = (db.query(DocRequest).filter(DocRequest.company_id == company.id, DocRequest.period == period)
             .order_by(DocRequest.status != "open", DocRequest.created_at).all())
    users = _users(db, {r.assignee_id or "" for r in items})
    counts = dict(db.query(DocRequest.status, func.count()).filter(
        DocRequest.company_id == company.id, DocRequest.period == period).group_by(DocRequest.status).all())
    due = service.deadline(period, s)
    now = service.now_msk()
    sent = service.sent_slots(db, company.id, period)
    return {
        "company": {"id": company.id, "name": company.short_name or company.name},
        "period": period, "period_title": service.period_title(period),
        "deadline": due.isoformat(), "deadline_human": service.human_moment(due), "overdue": now >= due,
        "settings": service.settings_dict(s), "channels": service.channel_status(db),
        "items": [service.brief(r, users) for r in items],
        "summary": {status: int(counts.get(status, 0)) for status in service.STATUSES},
        "schedule": [{"slot": slot, "at": at.isoformat(), "human": service.human_moment(at), "sent": slot in sent}
                     for slot, at in service.schedule(period, s)],
        "discipline": service.discipline(db, company.id, period),
    }


@router.get("")
def overview(user: Accountant, db: Db, company_id: CompanyId = None, period: Period = None) -> dict:
    company = _company(db, user, company_id)
    period = _period(period)
    service.refresh(db, company.id, period)            # пришедшие документы закрывают запросы сами
    return _overview(db, company, period)


@router.post("/detect")
def detect(user: Accountant, db: Db, body: PeriodIn) -> dict:
    company = _company(db, user, body.company_id)
    result = service.detect(db, company.id, _period(body.period), user.id)
    return {**result, "overview": _overview(db, company, body.period)}


@router.post("")
def add(user: Accountant, db: Db, body: ManualIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        req = service.add_manual(db, company.id, _period(body.period), body.model_dump(), user)
    except service.DocRequestError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return service.brief(req)


@router.post("/remind")
def remind(user: Accountant, db: Db, body: PeriodIn) -> dict:
    """Напомнить сейчас — по тем же каналам и с записью в журнал (метка «manual»)."""
    company = _company(db, user, body.company_id)
    period = _period(body.period)
    report = service.send_reminders(db, company.id, period, "manual", actor_id=user.id)
    log_action(user, "docreq_reminded", details={"company": company.name, "period": period, **report})
    return report


@router.get("/preview")
def preview(user: Accountant, db: Db, company_id: CompanyId = None, period: Period = None) -> dict:
    company = _company(db, user, company_id)
    return service.preview(db, company.id, _period(period))


@router.get("/journal")
def journal(user: Accountant, db: Db, company_id: CompanyId = None, period: Period = None,
            limit: Annotated[int, Query(ge=1, le=500)] = 200) -> dict:
    company = _company(db, user, company_id)
    query = db.query(DocRequestEvent).filter(DocRequestEvent.company_id == company.id)
    if period:
        query = query.filter(DocRequestEvent.period == _period(period))
    events = query.order_by(DocRequestEvent.at.desc(), DocRequestEvent.id).limit(limit).all()
    users = _users(db, {e.actor_id for e in events})
    return {"items": [service.event_dict(e, users) for e in events]}


@router.get("/settings")
def get_settings(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    return {"settings": service.settings_dict(service.settings_of(db, company.id)),
            "channels": service.channel_status(db)}


@router.put("/settings")
def put_settings(user: Accountant, db: Db, body: SettingsIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        saved = service.save_settings(db, company.id, body.model_dump(), user)
    except service.DocRequestError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    log_action(user, "docreq_settings_updated", details={"company": company.name, "enabled": saved.enabled,
                                                         "deadline_day": saved.deadline_day, "x_hour": saved.x_hour})
    return {"settings": service.settings_dict(saved), "channels": service.channel_status(db)}


@router.post("/{req_id}/status")
def set_status(req_id: str, user: Accountant, db: Db, body: StatusIn) -> dict:
    req = db.get(DocRequest, req_id[:36])
    if req is None or (user.role != ROLE_ADMIN and req.company_id != user.company_id):
        raise HTTPException(404, "Запрос не найден")
    service.set_status(db, req, body.status, user, body.note.strip())
    return service.brief(req, _users(db, {req.assignee_id or ""}))
