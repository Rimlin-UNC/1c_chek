"""API рабочего места аутсорсера (T-50, v1.70.100, ADR-30).

GET /api/v1/clients — сводка по всем доступным компаниям за отчётный месяц (бухгалтер, администратор);
GET /api/v1/clients/mine — компании для переключателя клиента;
GET/PUT /api/v1/clients/access/{user_id} — клиенты бухгалтера (только администратор).
Активный клиент бухгалтера — заголовок X-Ym-Company; проверка — в auth.get_current_user.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_accountant, require_admin
from ..database import get_db
from ..models import User
from ..services.audit import log_action
from . import service

router = APIRouter(prefix="/api/v1/clients", tags=["Рабочее место аутсорсера"])

Accountant = Annotated[User, Depends(require_accountant)]
Admin = Annotated[User, Depends(require_admin)]
Db = Annotated[Session, Depends(get_db)]


class AccessIn(BaseModel):
    company_ids: list[Annotated[str, Field(max_length=36)]] = Field(default_factory=list, max_length=service.MAX_CLIENTS)


def _fail(exc: service.ClientsError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("", summary="Все клиенты в одном окне: что требует внимания по каждой компании")
def overview(user: Accountant, db: Db, period: Annotated[str | None, Query(max_length=7)] = None) -> dict:
    try:
        data = service.overview(db, user, period)
    except service.ClientsError as exc:
        raise _fail(exc) from exc
    data["periods"] = service.period_choices()
    return data


@router.get("/mine", summary="Компании для переключателя клиента (своя — первой)")
def mine(user: Accountant, db: Db) -> dict:
    return {"clients": service.clients_of(db, user), "home_company_id": service.home_company_id(user),
            "active_company_id": user.company_id}


def _accountant(db: Session, user_id: str) -> User:
    target = db.get(User, user_id[:36])
    if target is None:
        raise HTTPException(404, "Пользователь не найден")
    return target


@router.get("/access/{user_id}", summary="Клиенты бухгалтера (администратор)",
            dependencies=[Depends(require_admin)])
def get_access(user_id: str, db: Db) -> dict:
    target = _accountant(db, user_id)
    return {"user_id": target.id, "role": target.role, "home_company_id": target.company_id,
            "company_ids": service.access_of(db, target)}


@router.put("/access/{user_id}", summary="Назначить клиентов бухгалтеру (администратор)")
def put_access(user_id: str, body: AccessIn, admin: Admin, db: Db) -> dict:
    target = _accountant(db, user_id)
    try:
        result = service.set_access(db, admin, target, body.company_ids)
    except service.ClientsError as exc:
        raise _fail(exc) from exc
    log_action(admin, "clients_access_set", "user", target.id,
               {"username": target.username, "total": len(result["company_ids"]),
                "added": result["added"], "removed": result["removed"]})
    return result
