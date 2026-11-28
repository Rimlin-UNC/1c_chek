"""REST API корпоративного биллинга, тарифов, модулей и счетов (v1.74.0).

Два контура доступа:
- ``/api/v1/billing/admin/*`` — строго ``require_admin`` (изменение прайс-листа,
  тарифов, политики Grandfathering, режима тарификации, промокодов, счетов и
  подписок организаций, просмотр MRR/ARR и журнала аудита).
- ``/api/v1/billing/my*``, ``/quote``, ``/request-invoice``, ``/invoices/{id}/print`` —
  для администратора, бухгалтера (по своим и назначенным компаниям) и
  руководителя (строго по своей компании).
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import (
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_MANAGER,
    create_print_ticket,
    current_user_optional,
    decode_print_ticket,
    get_current_user,
    require_admin,
)
from ..clients import service as clients_svc
from ..database import get_db
from ..models import User
from ..services.audit import log_action
from . import service

router = APIRouter(prefix="/api/v1/billing", tags=["Подписки и тарифы"])

AdminUser = Annotated[User, Depends(require_admin)]
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(current_user_optional)]
Db = Annotated[Session, Depends(get_db)]
CompanyIdQuery = Annotated[str | None, Query(max_length=36)]
TicketQuery = Annotated[str | None, Query(max_length=600)]


def _resolve_billing_company_id(db: Session, user: User, requested_id: str | None) -> str:
    """Проверить доступ пользователя к биллингу выбранной компании (мультикомпании бухгалтера)."""
    if user.role == ROLE_ADMIN:
        cid = (requested_id or user.company_id or "").strip()
        if not cid:
            raise HTTPException(400, "Выберите организацию")
        return cid
    if user.role not in (ROLE_ACCOUNTANT, ROLE_MANAGER):
        raise HTTPException(403, "Управление подпиской доступно руководителю и бухгалтеру организации")
    allowed = clients_svc.allowed_company_ids(db, user)
    if not allowed:
        raise HTTPException(400, "Профиль не привязан к организации")
    if requested_id and requested_id.strip():
        cid = requested_id.strip()
        if cid not in allowed:
            raise HTTPException(403, "Нет доступа к выбранной организации")
        return cid
    if user.company_id and user.company_id in allowed:
        return user.company_id
    return sorted(allowed)[0]


# --------------------------------------------------------------------------
#  Pydantic-схемы запросов
# --------------------------------------------------------------------------
class ConfigPatchIn(BaseModel):
    billing_mode: Literal["free_all", "soft_notice", "strict"] | None = None
    trial_days: int | None = Field(None, ge=0, le=365)
    grace_days: int | None = Field(None, ge=0, le=90)
    annual_discount_pct: int | None = Field(None, ge=0, le=60)
    vat_mode: Literal["none", "vat5", "vat7", "vat20"] | None = None
    price_change_policy: Literal["grandfather", "next_renewal"] | None = None
    seller_name: str | None = Field(None, max_length=300)
    seller_inn: str | None = Field(None, max_length=20)
    seller_kpp: str | None = Field(None, max_length=20)
    seller_address: str | None = Field(None, max_length=300)
    seller_bank: str | None = Field(None, max_length=200)
    seller_bik: str | None = Field(None, max_length=20)
    seller_account: str | None = Field(None, max_length=30)
    seller_corr_account: str | None = Field(None, max_length=30)
    invoice_prefix: str | None = Field(None, max_length=12)
    support_email: str | None = Field(None, max_length=120)


class ModulePriceIn(BaseModel):
    monthly_kop: int = Field(..., ge=0, le=service.MAX_KOP)
    yearly_kop: int = Field(..., ge=0, le=service.MAX_KOP)
    monthly_quota: int = Field(0, ge=0, le=10_000_000)
    available: bool = True
    apply_policy: Literal["grandfather", "next_renewal"] | None = None


class PlanLimitsIn(BaseModel):
    users: int = Field(0, ge=0, le=100_000)
    receipts_monthly: int = Field(0, ge=0, le=10_000_000)
    upd_monthly: int = Field(0, ge=0, le=10_000_000)
    counterparties_monthly: int = Field(0, ge=0, le=10_000_000)


class PlanUpsertIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    title: str = Field(..., min_length=1, max_length=120)
    badge: str = Field("", max_length=40)
    description: str = Field("", max_length=400)
    modules: list[str] = Field(default_factory=list, max_length=10)
    monthly_kop: int = Field(..., ge=0, le=service.MAX_KOP)
    quarterly_kop: int | None = Field(None, ge=0, le=service.MAX_KOP)
    yearly_kop: int | None = Field(None, ge=0, le=service.MAX_KOP)
    limits: PlanLimitsIn = Field(default_factory=PlanLimitsIn)
    active: bool = True
    sort_order: int | None = Field(None, ge=0, le=10_000)
    apply_policy: Literal["grandfather", "next_renewal"] | None = None


class CompanySubIn(BaseModel):
    status: Literal["free", "trial", "active", "grace", "expired", "partner", "suspended"] = "active"
    plan_code: str = Field("", max_length=40)
    billing_cycle: Literal["monthly", "quarterly", "yearly"] = "monthly"
    addon_modules: list[str] = Field(default_factory=list, max_length=10)
    locked_price_kop: int | None = Field(None, ge=0, le=service.MAX_KOP)
    price_locked: bool = True
    keep_locked_price: bool = False
    custom_discount_pct: int = Field(0, ge=0, le=100)
    custom_limits: dict[str, int] | None = None
    period_end: str | None = Field(None, max_length=40)
    duration_days: int | None = Field(None, ge=0, le=3650)
    contract_number: str = Field("", max_length=80)
    contract_date: str = Field("", max_length=20)
    admin_note: str = Field("", max_length=500)
    enforce_in_free_mode: bool = False


class ExtendSubIn(BaseModel):
    add_days: int = Field(30, ge=0, le=3650)
    target_date: str | None = Field(None, max_length=40)
    reason: str = Field("", max_length=400)


class PromoIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=32)
    title: str = Field("", max_length=120)
    discount_kind: Literal["percent", "fixed_kop"] = "percent"
    discount_value: int = Field(..., ge=0, le=service.MAX_KOP)
    bonus_trial_days: int = Field(0, ge=0, le=365)
    valid_until: str = Field("", max_length=30)
    max_uses: int = Field(0, ge=0, le=1_000_000)
    active: bool = True


class QuoteIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    plan_code: str = Field("", max_length=40)
    addon_modules: list[str] = Field(default_factory=list, max_length=10)
    billing_cycle: Literal["monthly", "quarterly", "yearly"] = "monthly"
    promo_code: str = Field("", max_length=32)


class CreateInvoiceAdminIn(QuoteIn):
    company_id: str = Field(..., max_length=36)
    custom_discount_pct: int | None = Field(None, ge=0, le=100)
    custom_price_kop: int | None = Field(None, ge=0, le=service.MAX_KOP)
    comment: str = Field("", max_length=400)


class RequestInvoiceClientIn(QuoteIn):
    comment: str = Field("", max_length=400)


class MarkPaidIn(BaseModel):
    payment_ref: str = Field("", max_length=120)


# --------------------------------------------------------------------------
#  Административный контур (/api/v1/billing/admin/*)
# --------------------------------------------------------------------------
@router.get("/admin/overview", summary="Сводка коммерции, прайс-лист, подписки компаний и аудит")
def admin_overview(admin: AdminUser, db: Db):
    return service.admin_overview(db)


@router.put("/admin/config", summary="Обновить глобальные настройки тарификации и реквизиты счёта")
def admin_update_config(body: ConfigPatchIn, admin: AdminUser, db: Db):
    patch = body.model_dump(exclude_none=True)
    try:
        old_cfg, new_cfg = service.update_config(db, patch)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    changed = {
        k: {"from": old_cfg.get(k), "to": new_cfg.get(k)}
        for k in patch
        if old_cfg.get(k) != new_cfg.get(k)
    }
    log_action(admin, "billing_config_updated", "billing", "config", changed)
    return {"ok": True, "config": new_cfg}


@router.put("/admin/modules/{code}", summary="Изменить цену, квоту и доступность модуля подписки")
def admin_update_module(code: str, body: ModulePriceIn, admin: AdminUser, db: Db):
    try:
        updated = service.update_module_price(
            db,
            code,
            monthly_kop=body.monthly_kop,
            yearly_kop=body.yearly_kop,
            monthly_quota=body.monthly_quota,
            available=body.available,
            apply_policy=body.apply_policy,
            actor=admin.username,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_module_price_updated", "billing", code, {
        "monthly_kop": body.monthly_kop,
        "yearly_kop": body.yearly_kop,
        "monthly_quota": body.monthly_quota,
        "available": body.available,
        "policy": body.apply_policy or service.get_config(db)["price_change_policy"],
    })
    return {"ok": True, "module": updated}


@router.post("/admin/plans", summary="Создать или обновить пакетный тарифный план")
def admin_upsert_plan(body: PlanUpsertIn, admin: AdminUser, db: Db):
    try:
        plan = service.upsert_plan(
            db,
            body.model_dump(),
            apply_policy=body.apply_policy,
            actor=admin.username,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_plan_saved", "billing", plan["code"], {
        "title": plan["title"],
        "monthly_kop": plan["monthly_kop"],
        "yearly_kop": plan["yearly_kop"],
        "modules": plan["modules"],
        "price_version": plan["price_version"],
        "policy": body.apply_policy or service.get_config(db)["price_change_policy"],
    })
    return {"ok": True, "plan": plan}


@router.put("/admin/plans/{plan_code}", summary="Обновить существующий тарифный план")
def admin_update_plan(plan_code: str, body: PlanUpsertIn, admin: AdminUser, db: Db):
    data = body.model_dump()
    data["code"] = plan_code
    try:
        plan = service.upsert_plan(
            db,
            data,
            apply_policy=body.apply_policy,
            actor=admin.username,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_plan_saved", "billing", plan["code"], {
        "title": plan["title"],
        "monthly_kop": plan["monthly_kop"],
        "yearly_kop": plan["yearly_kop"],
        "modules": plan["modules"],
        "price_version": plan["price_version"],
        "policy": body.apply_policy or service.get_config(db)["price_change_policy"],
    })
    return {"ok": True, "plan": plan}


@router.put("/admin/companies/{company_id}/subscription", summary="Назначить или изменить подписку компании")
def admin_set_company_sub(company_id: str, body: CompanySubIn, admin: AdminUser, db: Db):
    try:
        sub = service.assign_company_subscription(
            db, company_id, body.model_dump(), actor=admin.username
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_subscription_assigned", "billing", company_id, {
        "status": sub["status"],
        "plan_code": sub["plan_code"],
        "addon_modules": sub["addon_modules"],
        "locked_price_kop": sub["locked_price_kop"],
        "custom_discount_pct": sub["custom_discount_pct"],
        "period_end": sub["period_end"],
    })
    return {"ok": True, "subscription": sub}


@router.post("/admin/companies/{company_id}/extend", summary="Продлить подписку компании на дни или до даты")
def admin_extend_company_sub(company_id: str, body: ExtendSubIn, admin: AdminUser, db: Db):
    try:
        sub = service.extend_company_subscription(
            db,
            company_id,
            add_days=body.add_days,
            target_date=body.target_date,
            reason=body.reason,
            actor=admin.username,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_subscription_extended", "billing", company_id, {
        "add_days": body.add_days,
        "target_date": body.target_date,
        "period_end": sub["period_end"],
        "reason": body.reason,
    })
    return {"ok": True, "subscription": sub}


@router.post("/admin/promos", summary="Создать или обновить промокод")
def admin_save_promo(body: PromoIn, admin: AdminUser, db: Db):
    try:
        promo = service.create_or_update_promo(db, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_promo_saved", "billing", promo["code"], {
        "discount_kind": promo["discount_kind"],
        "discount_value": promo["discount_value"],
        "bonus_trial_days": promo["bonus_trial_days"],
    })
    return {"ok": True, "promo": promo}


@router.delete("/admin/promos/{code}", summary="Отключить промокод")
def admin_disable_promo(code: str, admin: AdminUser, db: Db):
    try:
        promo = service.deactivate_promo(db, code)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_promo_disabled", "billing", promo["code"], {})
    return {"ok": True, "promo": promo}


@router.post("/admin/invoices", summary="Выставить счёт компании от имени администратора")
def admin_create_invoice(body: CreateInvoiceAdminIn, admin: AdminUser, db: Db):
    try:
        inv = service.create_invoice(
            db,
            company_id=body.company_id,
            plan_code=body.plan_code or None,
            addon_modules=body.addon_modules,
            billing_cycle=body.billing_cycle,
            promo_code=body.promo_code or None,
            custom_discount_pct=body.custom_discount_pct,
            custom_price_kop=body.custom_price_kop,
            comment=body.comment,
            created_by=admin.username,
            origin="admin",
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_invoice_issued", "billing", inv["id"], {
        "number": inv["number"],
        "company_id": inv["company_id"],
        "total_kop": inv["total_kop"],
    })
    return {"ok": True, "invoice": inv}


@router.post("/admin/invoices/{invoice_id}/mark-paid", summary="Подтвердить оплату счёта и активировать подписку")
def admin_mark_invoice_paid(invoice_id: str, body: MarkPaidIn, admin: AdminUser, db: Db):
    try:
        inv, sub = service.mark_invoice_paid(
            db, invoice_id, payment_ref=body.payment_ref, actor=admin.username
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_invoice_paid", "billing", inv["id"], {
        "number": inv["number"],
        "company_id": inv["company_id"],
        "total_kop": inv["total_kop"],
        "payment_ref": body.payment_ref,
        "period_end": sub["period_end"],
    })
    return {"ok": True, "invoice": inv, "subscription": sub}


@router.post("/admin/invoices/{invoice_id}/cancel", summary="Отменить неоплаченный счёт")
def admin_cancel_invoice(invoice_id: str, admin: AdminUser, db: Db):
    try:
        inv = service.cancel_invoice(db, invoice_id, actor=admin.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(admin, "billing_invoice_cancelled", "billing", inv["id"], {
        "number": inv["number"],
        "company_id": inv["company_id"],
    })
    return {"ok": True, "invoice": inv}


# --------------------------------------------------------------------------
#  Клиентский контур для бухгалтера и руководителя (/api/v1/billing/my*)
# --------------------------------------------------------------------------
@router.get("/my", summary="Подписка, квоты и витрина тарифов организации")
def my_billing(user: CurrentUser, db: Db, company_id: CompanyIdQuery = None):
    cid = _resolve_billing_company_id(db, user, company_id)
    try:
        data = service.client_billing_view(db, cid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return data


@router.post("/quote", summary="Предварительный расчёт стоимости тарифа, модулей и промокода")
def calculate_quote(body: QuoteIn, user: CurrentUser, db: Db):
    cid = _resolve_billing_company_id(db, user, body.company_id)
    try:
        q = service.quote_subscription(
            db,
            company_id=cid,
            plan_code=body.plan_code or None,
            addon_modules=body.addon_modules,
            billing_cycle=body.billing_cycle,
            promo_code=body.promo_code or None,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return q


@router.post("/request-invoice", summary="Сформировать счёт/заявку на подключение тарифа или модулей")
def request_invoice(body: RequestInvoiceClientIn, user: CurrentUser, db: Db):
    cid = _resolve_billing_company_id(db, user, body.company_id)
    try:
        inv = service.create_invoice(
            db,
            company_id=cid,
            plan_code=body.plan_code or None,
            addon_modules=body.addon_modules,
            billing_cycle=body.billing_cycle,
            promo_code=body.promo_code or None,
            comment=body.comment,
            created_by=user.username,
            origin="client",
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    log_action(user, "billing_invoice_requested", "billing", inv["id"], {
        "number": inv["number"],
        "company_id": cid,
        "plan_code": inv["plan_code"],
        "addon_modules": inv["addon_modules"],
        "total_kop": inv["total_kop"],
    })
    return {"ok": True, "invoice": inv}


@router.post("/invoices/{invoice_id}/print-ticket", summary="Одноразовый билет печати счёта (для телефона)")
def invoice_print_ticket(invoice_id: str, user: CurrentUser, db: Db):
    inv = service.get_invoice(db, invoice_id)
    if inv is None:
        raise HTTPException(404, "Счёт не найден")
    if user.role != ROLE_ADMIN:
        allowed = clients_svc.allowed_company_ids(db, user)
        if user.role not in (ROLE_ACCOUNTANT, ROLE_MANAGER) or inv.get("company_id") not in allowed:
            raise HTTPException(403, "Нет доступа к счёту другой организации")
    ticket = create_print_ticket(user, f"inv:{invoice_id}")
    return {"ticket": ticket}


@router.get("/invoices/{invoice_id}/print", response_class=HTMLResponse, summary="Печатная форма счёта на оплату")
def print_invoice(invoice_id: str, user: OptionalUser, db: Db, ticket: TicketQuery = None):
    inv = service.get_invoice(db, invoice_id)
    if inv is None:
        raise HTTPException(404, "Счёт не найден")
    eff_user = None
    if ticket:
        uid_from_ticket = decode_print_ticket(ticket, f"inv:{invoice_id}")
        if uid_from_ticket:
            eff_user = db.get(User, uid_from_ticket)
    if eff_user is None:
        eff_user = user
    if eff_user is None or not eff_user.is_active:
        raise HTTPException(401, "Требуется авторизация")
    if eff_user.role != ROLE_ADMIN:
        allowed = clients_svc.allowed_company_ids(db, eff_user)
        if eff_user.role not in (ROLE_ACCOUNTANT, ROLE_MANAGER) or inv.get("company_id") not in allowed:
            raise HTTPException(403, "Нет доступа к счёту другой организации")
    return HTMLResponse(
        service.render_invoice_html(inv),
        headers={"Cache-Control": "no-store"},
    )
