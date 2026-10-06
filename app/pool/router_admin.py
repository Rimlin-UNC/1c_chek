# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — админ-роуты «Чек-Пула» (v1.30.0, Этап 1).
# Настройка вкл/выкл + минимальный дашборд. Только администратор.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User

router = APIRouter(prefix="/api/v1/pool-admin", tags=["pool"])


@router.get("/overview", summary="Чек-Пул: настройка и счётчики (админ)")
def pool_overview(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import appsettings
    from . import ingest
    return {
        "enabled": ingest.pool_enabled(db),
        "offerta": ingest.OFFERTA_SHORT,
        "daily_limit": ingest.DAILY_LIMIT,
        "points_per_receipt": ingest.POINTS_PER_RECEIPT,
        **ingest.overview(db),
    }


@router.put("/settings", summary="Чек-Пул: включить/выключить приём чеков (админ)")
def pool_settings(body: dict, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    from ..services import appsettings
    from ..services.audit import log_action
    enabled = bool((body or {}).get("enabled"))
    appsettings.set_setting(db, "pool_enabled", "1" if enabled else "0")
    log_action(admin, "pool_settings_saved", details={"enabled": enabled})
    return {"ok": True, "enabled": enabled,
            "message": ("Приём чеков включён — форма на сайте принимает чеки"
                        " (бот — резервный канал)"
                        if enabled else "Приём чеков в пул выключен")}


@router.get("/receipts", summary="Чек-Пул: список чеков (админ)")
def pool_receipts(status: str = Query("", max_length=16),
                  page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from .models import PoolReceipt
    q = db.query(PoolReceipt)
    if status:
        q = q.filter(PoolReceipt.status == status)
    total = q.count()
    rows = (q.order_by(PoolReceipt.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "id": r.id, "status": r.status, "source": r.source,
            "fn": r.fn, "fd": r.fd, "fp": r.fp,
            "receipt_date": r.receipt_date.isoformat() if r.receipt_date else None,
            "total_sum": r.total_sum, "operation": r.operation,
            "merchant_name": r.merchant_name, "merchant_inn": r.merchant_inn,
            "full_data": r.full_data, "points_awarded": r.points_awarded,
            "status_message": r.status_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


# --------------------------------------------------------------------------
# v1.32.0: SMTP для писем кабинета (подтверждение e-mail, magic link)
# и адрес сайта для ссылок. Пароль — шифруемый секрет (appsettings).
# --------------------------------------------------------------------------
@router.get("/smtp", summary="Чек-Пул: настройки почты (админ)")
def get_smtp(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import appsettings
    from .mailer import SET_BASE_URL, SET_SMTP_FROM, SET_SMTP_HOST, SET_SMTP_PORT, SET_SMTP_TLS, SET_SMTP_USER
    g = appsettings.get_setting
    return {
        "host": g(db, SET_SMTP_HOST), "port": g(db, SET_SMTP_PORT, "587"),
        "user": g(db, SET_SMTP_USER), "has_password": bool(g(db, "smtp_pass")),
        "sender": g(db, SET_SMTP_FROM), "tls": g(db, SET_SMTP_TLS, "1") == "1",
        "base_url": g(db, SET_BASE_URL),
        "configured": bool(g(db, SET_SMTP_HOST).strip()),
    }


@router.put("/smtp", summary="Чек-Пул: сохранить настройки почты (админ)")
def put_smtp(body: dict, db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    from ..services import appsettings
    from ..services.audit import log_action
    from .mailer import SET_BASE_URL, SET_SMTP_FROM, SET_SMTP_HOST, SET_SMTP_PORT, SET_SMTP_TLS, SET_SMTP_USER
    b = body or {}
    appsettings.set_setting(db, SET_SMTP_HOST, (b.get("host") or "").strip())
    appsettings.set_setting(db, SET_SMTP_PORT, str(int(b.get("port") or 587)))
    appsettings.set_setting(db, SET_SMTP_USER, (b.get("user") or "").strip())
    if b.get("password") not in (None, ""):       # "" — не менять
        appsettings.set_setting(db, "smtp_pass", b["password"])
    appsettings.set_setting(db, SET_SMTP_FROM, (b.get("sender") or "").strip())
    appsettings.set_setting(db, SET_SMTP_TLS, "1" if b.get("tls") else "0")
    appsettings.set_setting(db, SET_BASE_URL, (b.get("base_url") or "").strip())
    log_action(admin, "pool_smtp_saved", details={"configured": bool((b.get("host") or "").strip())})
    return {"ok": True, "message": "Настройки почты сохранены"}


@router.post("/smtp-test", summary="Чек-Пул: тестовое письмо (админ)")
def smtp_test(body: dict, db: Session = Depends(get_db),
              admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .mailer import send_mail
    to = ((body or {}).get("email") or "").strip()
    ok, err = send_mail(db, to, "Ямастер Чек-Пул: тестовое письмо",
                        "SMTP настроен верно — письма кабинета будут доходить.\n\nООО «Ямастер»")
    log_action(admin, "pool_smtp_test", details={"ok": ok})
    return {"ok": ok, "message": ("Письмо отправлено" if ok else f"Не отправлено: {err}")}
