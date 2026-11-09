"""API рабочего места аутсорсера (T-50, v1.70.100, ADR-30).

GET /api/v1/clients — сводка по всем доступным компаниям за отчётный месяц (бухгалтер, администратор);
GET /api/v1/clients/mine — компании для переключателя клиента;
GET/PUT /api/v1/clients/access/{user_id} — клиенты бухгалтера (только администратор);
POST /api/v1/clients/bulk — массовые действия по отмеченным клиентам (v1.72.0, T-89).
Активный клиент бухгалтера — заголовок X-Ym-Company; проверка — в auth.get_current_user.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_accountant, require_admin
from ..database import get_db
from ..models import User
from ..services.audit import log_action
from . import service
from .contract import ContractIn

router = APIRouter(prefix="/api/v1/clients", tags=["Рабочее место аутсорсера"])

Accountant = Annotated[User, Depends(require_accountant)]
Admin = Annotated[User, Depends(require_admin)]
Db = Annotated[Session, Depends(get_db)]


class AccessIn(BaseModel):
    company_ids: list[Annotated[str, Field(max_length=36)]] = Field(default_factory=list, max_length=service.MAX_CLIENTS)


class BulkIn(BaseModel):
    """v1.72.0 (T-89): пачка компаний и одно действие над всеми. Проверяется до работы — целиком."""
    company_ids: list[Annotated[str, Field(max_length=36)]] = Field(..., min_length=1,
                                                                   max_length=service.MAX_BULK)
    action: Literal["remind", "archive", "restore", "assign"]
    period: str | None = Field(None, max_length=7)
    user_id: str | None = Field(None, max_length=36)          # для «назначить сотрудника»


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


@router.post("/bulk", summary="Массовое действие по отмеченным клиентам")
def bulk(body: BulkIn, user: Accountant, db: Db) -> dict:
    """v1.72.0 (T-89): одно действие сразу по нескольким клиентам.

    Каждая компания обрабатывается отдельно: ошибка по одной не отменяет остальные, а в ответе видно, что
    получилось по каждой (`ok`, `error`). Права — как у одиночных действий (ADR-30); чужую компанию не
    пропускаем молча, а показываем причину отказа. Действия: `remind` (напомнить о документах),
    `archive`/`restore` (архив компании), `assign` (сотрудник переезжает в отмеченные компании).
    """
    if body.action == "assign" and not body.user_id:
        raise HTTPException(422, "Выберите сотрудника для назначения")
    companies, items = service.bulk_targets(db, user, body.company_ids)
    if not companies and not items:
        raise HTTPException(422, "Не выбрано ни одного клиента")
    employee = db.get(User, body.user_id) if body.user_id else None
    if body.action == "assign" and employee is None:
        raise HTTPException(404, "Сотрудник не найден")
    for company in companies:
        try:
            if body.action == "remind":
                from ..docreq import service as docreq
                if body.period:
                    docreq.parse_period(body.period)          # проверка формата ГГГГ-ММ (возвращает части, не строку)
                period = body.period or docreq.default_period()
                report = docreq.send_reminders(db, company.id, period, "manual", actor_id=user.id)
                items.append({"company_id": company.id, "name": company.short_name or company.name, "ok": True,
                              "note": report.get("note") or f"отправлено: {report.get('sent', 0)}"})
            elif body.action == "archive":
                company.is_active = False
                items.append({"company_id": company.id, "name": company.short_name or company.name, "ok": True})
            elif body.action == "restore":
                company.is_active = True
                items.append({"company_id": company.id, "name": company.short_name or company.name, "ok": True})
            else:
                employee.company_id = company.id
                items.append({"company_id": company.id, "name": company.short_name or company.name, "ok": True})
        except Exception as exc:                                  # noqa: BLE001 — одна компания не держит пачку
            db.rollback()
            items.append({"company_id": company.id, "name": company.short_name or company.name, "ok": False,
                          "error": str(exc)[:200]})
        else:
            db.commit()
    done = sum(1 for i in items if i.get("ok"))
    failed = len(items) - done
    log_action(user, "clients_bulk", "company", company_ids_csv(body.company_ids),
               {"action": body.action, "total": len(items), "done": done, "failed": failed,
                "user_id": body.user_id or ""})
    return {"action": body.action, "title": service.BULK_TITLES.get(body.action, body.action),
            "items": items, "done": done, "failed": failed}


def company_ids_csv(ids: list[str]) -> str:
    """Компании пачки одной строкой — для журнала действий (колонка entity_id короткая)."""
    return ",".join(dict.fromkeys(ids))[:64]


@router.get('/{company_id}/contract', summary='Договор и тариф клиента')
def get_contract(company_id: str, user: Accountant, db: Db) -> dict:
    from . import contract
    try:
        company = contract.company_for(db, user, company_id)
    except service.ClientsError as exc:
        raise _fail(exc) from exc
    return {'contract': contract.as_dict(company)}


@router.put('/{company_id}/contract', summary='Сохранить договор и тариф клиента')
def put_contract(company_id: str, body: ContractIn, user: Accountant, db: Db) -> dict:
    from . import contract
    try:
        company = contract.company_for(db, user, company_id)
    except service.ClientsError as exc:
        raise _fail(exc) from exc
    contract.save(company, body)
    db.commit()
    log_action(user, 'client_contract_updated', 'company', company_id, {})
    return {'contract': contract.as_dict(company)}
