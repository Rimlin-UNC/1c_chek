# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — модерация контура B2B (v1.70.45, только администратор).
#
# Требование владельца: «Администратор может все это видеть, но с
# пометкой "это не видит ни кто"». Поэтому КАЖДЫЙ ответ этого роутера
# несёт поле marker = «Это не видит никто» — внутренние поверхности
# отличаются от клиентских на уровне данных, а не только подписью в
# интерфейсе.
#
# Здесь же переключаются режимы автоматизации: полная / частичная /
# отмена (pipeline.MODE_*). Действия администратора пишутся и в аудит
# контура (b2b_events), и в общий журнал ядра (log_action).
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User
from . import auth, content, letters, pipeline, verify
from .models import (
    ACTOR_ADMIN,
    AP_NEW,
    AP_PARKED,
    AP_REVOKED,
    APPLICATION_TITLES,
    CHECK_TITLES,
    COMPANY_STATUSES,
    PURPOSE_VERIFY,
    ST_BLOCKED,
    STATUS_TITLES,
    B2bApplication,
    B2bCompany,
    B2bEvent,
)
from .router_public import key_info

log = logging.getLogger("ymaster.b2b.admin")

router = APIRouter(prefix="/api/v1/b2b-admin", tags=["b2b-admin"],
                   dependencies=[Depends(require_admin)])

MARKER = content.ADMIN_MARKER
PAGE_LIMIT = 200


class ConfigBody(BaseModel):
    auto_mode: str = ""
    enabled: bool | None = None
    auto_issue_key: bool | None = None
    require_registry: bool | None = None
    allow_free_domains: bool | None = None
    notify_admin: bool | None = None
    notify_email: str = ""


class ReasonBody(BaseModel):
    reason: str = ""
    note: str = ""


class ApproveBody(BaseModel):
    rate_per_hour: int | None = None
    monthly_quota: int | None = None


def _company_or_404(db: Session, cid: str) -> B2bCompany:
    company = db.get(B2bCompany, cid)
    if company is None:
        raise HTTPException(404, "Компания не найдена")
    return company


def _application_or_404(db: Session, aid: str) -> B2bApplication:
    rec = db.get(B2bApplication, aid)
    if rec is None:
        raise HTTPException(404, "Заявка не найдена")
    return rec


def _checks(db: Session, cid: str) -> list[dict]:
    return verify.latest_checks(db, cid)


def _applications(db: Session, cid: str) -> list[dict]:
    rows = (db.query(B2bApplication)
            .filter(B2bApplication.company_id == cid)
            .order_by(B2bApplication.created_at.desc()).limit(50).all())
    return [r.to_dict() for r in rows]


def _events(db: Session, cid: str, limit: int = 50) -> list[dict]:
    rows = (db.query(B2bEvent)
            .filter(B2bEvent.company_id == cid)
            .order_by(B2bEvent.created_at.desc()).limit(limit).all())
    return [r.to_dict() for r in rows]


# --------------------------------------------------------------------------
# Сводка и режим автоматизации
# --------------------------------------------------------------------------
@router.get("/overview", summary="B2B-модерация: сводка")
def overview(db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    cfg = pipeline.get_config(db)
    counters = pipeline.counters(db)
    queue = (db.query(B2bApplication)
             .filter(B2bApplication.status.in_((AP_NEW, AP_PARKED)))
             .order_by(B2bApplication.created_at.asc()).limit(20).all())
    queue_ids = list({a.company_id for a in queue})
    companies = ({c.id: c for c in db.query(B2bCompany)
                  .filter(B2bCompany.id.in_(queue_ids)).all()}
                 if queue_ids else {})
    return {
        "ok": True, "marker": MARKER,
        "counters": counters,
        "status_titles": dict(STATUS_TITLES),
        "config": cfg,
        "modes": [{"code": m, "title": pipeline.MODE_TITLES[m],
                   "note": pipeline.MODE_NOTES[m]} for m in pipeline.MODES],
        "queue": [{**a.to_dict(),
                   "company": (companies.get(a.company_id).company_name
                               if companies.get(a.company_id) else "—"),
                   "email": (companies.get(a.company_id).email
                             if companies.get(a.company_id) else ""),
                   "inn": (companies.get(a.company_id).inn
                           if companies.get(a.company_id) else ""),
                   "risk_score": (companies.get(a.company_id).risk_score
                                  if companies.get(a.company_id) else 0),
                   "tariff_title": content.TARIFF_TITLES.get(a.tariff, a.tariff),
                   # лимиты тарифа — те же цифры, что в списке заявок: админ
                   # видит, что именно выдаст одобрение в один клик
                   "tariff_rate": pipeline.tariff_limits(a.tariff)[0],
                   "tariff_quota": pipeline.tariff_limits(a.tariff)[1]}
                  for a in queue],
        "tariffs": content.tariffs(),
        "admin": admin.username,
    }


@router.put("/config", summary="B2B-модерация: режим автоматизации")
def put_config(body: ConfigBody, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    """Полная / частичная автоматизация либо отмена + переключатели."""
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    cfg, error = pipeline.set_config(db, payload)
    if error:
        raise HTTPException(422, error)
    from ..services.audit import log_action
    log_action(admin, "b2b_config_changed", details=payload)
    return {"ok": True, "marker": MARKER, "config": cfg,
            "message": f"Режим: {cfg['auto_mode_title']}. {cfg['auto_mode_note']}"}


# --------------------------------------------------------------------------
# Компании
# --------------------------------------------------------------------------
@router.get("/companies", summary="B2B-модерация: список компаний")
def companies_list(status: str = "", q: str = "",
                   db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    query = db.query(B2bCompany)
    if status and status in COMPANY_STATUSES:
        query = query.filter(B2bCompany.status == status)
    needle = (q or "").strip()
    if needle:
        # LIKE в SQLite регистронезависим только для ASCII, поэтому кириллицу
        # ищем по колонке search_key (заполняется str.lower в Python), а почту
        # и ИНН дублируем обычным сравнением — для строк без ключа.
        low = needle.lower()
        query = query.filter(
            B2bCompany.search_key.like(f"%{low}%")
            | B2bCompany.email.like(f"%{low}%")
            | B2bCompany.inn.like(f"%{needle}%"))
    rows = query.order_by(B2bCompany.created_at.desc()).limit(PAGE_LIMIT).all()
    # число заявок на каждую компанию — одним запросом (без N+1)
    ids = [c.id for c in rows]
    counts: dict[str, int] = {}
    if ids:
        from sqlalchemy import func
        for cid, n in (db.query(B2bApplication.company_id,
                                func.count(B2bApplication.id))
                       .filter(B2bApplication.company_id.in_(ids))
                       .group_by(B2bApplication.company_id).all()):
            counts[cid] = int(n)
    return {
        "ok": True, "marker": MARKER,
        "items": [{
            "id": c.id, "email": c.email, "company_name": c.company_name,
            "inn": c.inn, "kind": c.kind, "status": c.status,
            "status_title": STATUS_TITLES.get(c.status, c.status),
            "risk_score": c.risk_score,
            "applications": counts.get(c.id, 0),
            "created_at": c.created_at.strftime("%d.%m.%Y %H:%M")
            if c.created_at else "",
        } for c in rows],
        "statuses": [{"code": s, "title": STATUS_TITLES[s]}
                     for s in COMPANY_STATUSES],
    }


@router.get("/companies/{cid}", summary="B2B-модерация: карточка компании")
def company_card(cid: str, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    company = _company_or_404(db, cid)
    return {
        "ok": True, "marker": MARKER,
        "company": company.to_dict(with_private=True),
        "checks": _checks(db, cid),
        "check_titles": dict(CHECK_TITLES),
        "applications": _applications(db, cid),
        "events": _events(db, cid),
        "key": key_info(db, company),
        "config": pipeline.get_config(db),
    }


@router.post("/companies/{cid}/verify",
             summary="B2B-модерация: подтвердить статус вручную")
def company_verify(cid: str, body: ReasonBody, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    company = _company_or_404(db, cid)
    result = pipeline.verify_manually(db, company, admin, body.note)
    return {"ok": result["ok"], "marker": MARKER, **result}


@router.post("/companies/{cid}/reject",
             summary="B2B-модерация: отклонить компанию")
def company_reject(cid: str, body: ReasonBody, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    company = _company_or_404(db, cid)
    result = pipeline.reject_manually(db, company, admin, body.reason)
    if not result["ok"]:
        raise HTTPException(422, result["error"])
    return {"ok": True, "marker": MARKER, **result}


@router.post("/companies/{cid}/block",
             summary="B2B-модерация: закрыть доступ")
def company_block(cid: str, body: ReasonBody, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    company = _company_or_404(db, cid)
    result = pipeline.set_blocked(db, company, admin, True, body.reason)
    return {"ok": result["ok"], "marker": MARKER, **result}


@router.post("/companies/{cid}/unblock",
             summary="B2B-модерация: восстановить доступ")
def company_unblock(cid: str, db: Session = Depends(get_db),
                    admin: User = Depends(require_admin)):
    company = _company_or_404(db, cid)
    result = pipeline.set_blocked(db, company, admin, False)
    return {"ok": result["ok"], "marker": MARKER, **result}


@router.post("/companies/{cid}/recheck",
             summary="B2B-модерация: запустить проверки")
def company_recheck(cid: str, db: Session = Depends(get_db),
                    admin: User = Depends(require_admin)):
    """Нужно в режиме «отмена» и после исправления данных компанией."""
    company = _company_or_404(db, cid)
    result = pipeline.recheck(db, company, actor=ACTOR_ADMIN, actor_id=admin.id)
    return {"ok": True, "marker": MARKER, **result}


@router.post("/companies/{cid}/resend",
             summary="B2B-модерация: выслать письмо повторно")
def company_resend(cid: str, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    company = _company_or_404(db, cid)
    token = auth.issue_one_time(db, company, PURPOSE_VERIFY)
    sent = letters.send_verify(db, company, token)
    from ..services.audit import log_action
    # details — именованным аргументом: третья позиция в сигнатуре log_action
    # занята entity_type, и словарь на её месте ломал привязку параметров
    # SQLite, из-за чего запись аудита молча не создавалась.
    log_action(admin, "b2b_letter_resent", entity_type="b2b_company",
               entity_id=company.id,
               details={"company": company.email, "sent": sent})
    return {"ok": True, "marker": MARKER, "sent": sent,
            "message": "Письмо отправлено" if sent
            else "Почта сервиса не настроена — письмо не отправлено"}


# --------------------------------------------------------------------------
# Заявки
# --------------------------------------------------------------------------
@router.get("/applications", summary="B2B-модерация: очередь заявок")
def applications_list(status: str = "", db: Session = Depends(get_db),
                      admin: User = Depends(require_admin)):
    query = db.query(B2bApplication)
    if status and status in APPLICATION_TITLES:
        query = query.filter(B2bApplication.status == status)
    rows = (query.order_by(B2bApplication.created_at.desc())
            .limit(PAGE_LIMIT).all())
    ids = list({a.company_id for a in rows})
    companies = ({c.id: c for c in db.query(B2bCompany)
                  .filter(B2bCompany.id.in_(ids)).all()} if ids else {})
    checks_map = verify.latest_checks_many(db, ids)
    items = []
    for a in rows:
        company = companies.get(a.company_id)
        rate, quota = pipeline.tariff_limits(a.tariff)
        items.append({
            **a.to_dict(),
            "company": company.company_name if company else "—",
            "email": company.email if company else "",
            "inn": company.inn if company else "",
            "company_status": company.status if company else "",
            "risk_score": company.risk_score if company else 0,
            "tariff_title": content.TARIFF_TITLES.get(a.tariff, a.tariff),
            "tariff_rate": rate, "tariff_quota": quota,
            "checks": checks_map.get(a.company_id, []),
        })
    return {"ok": True, "marker": MARKER, "items": items,
            "statuses": [{"code": s, "title": APPLICATION_TITLES[s]}
                         for s in APPLICATION_TITLES],
            "queue": pipeline.queue_size(db)}


@router.post("/applications/{aid}/approve",
             summary="B2B-модерация: одобрить заявку и выдать ключ")
def application_approve(aid: str, body: ApproveBody,
                        db: Session = Depends(get_db),
                        admin: User = Depends(require_admin)):
    """Одобрение в один клик: ключ выдаётся по лимитам тарифа.

    Полный ключ возвращается ОДИН раз — в базе только SHA-256 хэш.
    """
    rec = _application_or_404(db, aid)
    if rec.status == AP_REVOKED:
        raise HTTPException(409, "Ключ по этой заявке отозван — примите "
                                 "новую заявку")
    result = pipeline.approve_application(db, rec, admin,
                                          rate=body.rate_per_hour,
                                          quota=body.monthly_quota)
    if not result["ok"]:
        raise HTTPException(422, result["error"])
    return {"ok": True, "marker": MARKER, **result}


@router.post("/applications/{aid}/reject",
             summary="B2B-модерация: отклонить заявку")
def application_reject(aid: str, body: ReasonBody,
                       db: Session = Depends(get_db),
                       admin: User = Depends(require_admin)):
    rec = _application_or_404(db, aid)
    result = pipeline.reject_application(db, rec, admin, body.reason)
    if not result["ok"]:
        raise HTTPException(422, result["error"])
    return {"ok": True, "marker": MARKER, **result}


@router.post("/applications/{aid}/revoke",
             summary="B2B-модерация: отозвать выданный ключ")
def application_revoke(aid: str, db: Session = Depends(get_db),
                       admin: User = Depends(require_admin)):
    rec = _application_or_404(db, aid)
    result = pipeline.revoke_key(db, rec, admin)
    if not result["ok"]:
        raise HTTPException(422, result["error"])
    return {"ok": True, "marker": MARKER, **result}


# --------------------------------------------------------------------------
# Журнал контура
# --------------------------------------------------------------------------
@router.get("/events", summary="B2B-модерация: журнал контура")
def events(limit: int = 50, db: Session = Depends(get_db),
           admin: User = Depends(require_admin)):
    since = max(1, min(int(limit or 50), PAGE_LIMIT))
    rows = (db.query(B2bEvent).order_by(B2bEvent.created_at.desc())
            .limit(since).all())
    return {"ok": True, "marker": MARKER, "items": [r.to_dict() for r in rows],
            "note": "Журнал контура: решения автоматики, компании и "
                    "администратора"}


@router.get("/blocked", summary="B2B-модерация: закрытые кабинеты")
def blocked_list(db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    rows = (db.query(B2bCompany).filter(B2bCompany.status == ST_BLOCKED)
            .order_by(B2bCompany.created_at.desc()).limit(PAGE_LIMIT).all())
    return {"ok": True, "marker": MARKER,
            "items": [c.to_dict(with_private=True) for c in rows]}
