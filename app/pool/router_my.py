# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — кабинет участника «Чек-Пула» (v1.32.0, Этап 3).
# Сводка (баллы, до награды), свои чеки с позициями и пагинацией,
# экспорт CSV («сдал чек — забрал чек»), смена пароля/e-mail, удаление
# аккаунта (152-ФЗ: ПДн стираются, баллы сгорают, чеки остаются
# обезличенными по оферте). Доступ — только по JWT кабинета (typ=pool).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from . import accounts, ingest, mailer, referral
from .models import PoolItem, PoolPoint, PoolReceipt, PoolReferral, PoolUser

router = APIRouter(prefix="/api/v1/pool-my", tags=["pool-my"])


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class EmailBody(BaseModel):
    password: str
    email: str


class DeleteBody(BaseModel):
    password: str


@router.get("/summary", summary="Чек-Пул: сводка кабинета")
def summary(db: Session = Depends(get_db),
            user=Depends(accounts.require_pool_user)):
    from ..models import utcnow
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(PoolReceipt).filter(PoolReceipt.pool_user_id == user.id)
    return {
        "email": user.email, "email_verified": bool(user.email_verified),
        "points": user.points or 0, "receipts_total": q.count(),
        "verified": q.filter(PoolReceipt.status == "verified").count(),
        "pending": q.filter(PoolReceipt.status == "pending").count(),
        "today": q.filter(PoolReceipt.created_at >= day_start).count(),
        "daily_limit": ingest.DAILY_LIMIT,
        "points_per_receipt": ingest.POINTS_PER_RECEIPT,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/receipts", summary="Чек-Пул: мои чеки (пагинация)")
def receipts(status: str = Query("", max_length=16),
             page: int = Query(1, ge=1),
             page_size: int = Query(20, ge=1, le=100),
             db: Session = Depends(get_db),
             user=Depends(accounts.require_pool_user)):
    q = db.query(PoolReceipt).filter(PoolReceipt.pool_user_id == user.id)
    if status:
        q = q.filter(PoolReceipt.status == status)
    total = q.count()
    rows = (q.order_by(PoolReceipt.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    ids = [r.id for r in rows]
    counts = {}
    if ids:
        from sqlalchemy import func
        counts = dict(db.query(PoolItem.receipt_id, func.count(PoolItem.id))
                      .filter(PoolItem.receipt_id.in_(ids))
                      .group_by(PoolItem.receipt_id).all())
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "id": r.id, "status": r.status, "message": r.status_message,
            "fn": r.fn, "fd": r.fd, "fp": r.fp,
            "receipt_date": r.receipt_date.isoformat() if r.receipt_date else None,
            "total_sum": r.total_sum,
            "merchant_name": r.merchant_name, "merchant_inn": r.merchant_inn,
            "items_count": counts.get(r.id, 0),
            "points": r.points_awarded,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


@router.get("/receipt/{receipt_id}", summary="Чек-Пул: мой чек с позициями")
def receipt_detail(receipt_id: str, db: Session = Depends(get_db),
                   user=Depends(accounts.require_pool_user)):
    r = (db.query(PoolReceipt)
         .filter(PoolReceipt.id == receipt_id,
                 PoolReceipt.pool_user_id == user.id).first())
    if r is None:
        raise HTTPException(404, "Чек не найден")
    items = (db.query(PoolItem).filter(PoolItem.receipt_id == r.id)
             .order_by(PoolItem.total.desc()).all())
    return {
        "id": r.id, "status": r.status, "message": r.status_message,
        "fn": r.fn, "fd": r.fd, "fp": r.fp,
        "receipt_date": r.receipt_date.isoformat() if r.receipt_date else None,
        "total_sum": r.total_sum, "operation": r.operation,
        "merchant_name": r.merchant_name, "merchant_inn": r.merchant_inn,
        "merchant_address": r.merchant_address, "cashier": r.cashier,
        "points": r.points_awarded,
        "items": [{"name": i.name, "quantity": i.quantity, "price": i.price,
                   "total": i.total, "vat_rate": i.vat_rate,
                   "vat_sum": i.vat_sum} for i in items],
    }


@router.get("/export.csv", summary="Чек-Пул: экспорт моих чеков (CSV, Excel)")
def export_csv(db: Session = Depends(get_db),
               user=Depends(accounts.require_pool_user)):
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.pool_user_id == user.id)
            .order_by(PoolReceipt.created_at.desc()).limit(10000).all())
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Когда сдан", "Дата чека", "Магазин", "ИНН", "Сумма, ₽",
                "Статус", "Баллы", "ФН", "ФД", "ФП"])
    st_names = {"verified": "Принят в пул", "pending": "На ручной проверке",
                "rejected": "Не принят"}
    for r in rows:
        w.writerow([
            r.created_at.strftime("%d.%m.%Y %H:%M") if r.created_at else "",
            r.receipt_date.strftime("%d.%m.%Y %H:%M") if r.receipt_date else "",
            r.merchant_name or "", r.merchant_inn or "",
            f"{r.total_sum:.2f}".replace(".", ","),
            st_names.get(r.status, r.status),
            r.points_awarded or 0, r.fn, r.fd, r.fp])
    now = datetime.utcnow()
    content = "\ufeff" + buf.getvalue()          # BOM для Excel (как в ядре)
    filename = f"chek-pool-{now.strftime('%Y%m%d-%H%M')}.csv"
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@router.post("/password", summary="Чек-Пул: смена пароля")
def change_password(body: PasswordBody, db: Session = Depends(get_db),
                    user=Depends(accounts.require_pool_user)):
    from ..auth import hash_password, verify_password
    if not user.password_hash or not verify_password(body.old_password,
                                                     user.password_hash):
        raise HTTPException(403, "Текущий пароль неверен")
    err = accounts.validate_registration(user.email, body.new_password)
    if err:
        raise HTTPException(422, err)
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True, "message": "Пароль изменён"}


@router.post("/email", summary="Чек-Пул: смена e-mail")
def change_email(body: EmailBody, db: Session = Depends(get_db),
                 user=Depends(accounts.require_pool_user)):
    from ..auth import verify_password
    if not user.password_hash or not verify_password(body.password,
                                                     user.password_hash):
        raise HTTPException(403, "Пароль неверен")
    email = (body.email or "").strip().lower()
    err = accounts.validate_registration(email, "x" * 12)
    if err and "e-mail" in err:
        raise HTTPException(422, err)
    if accounts.find_by_email(db, email) is not None:
        raise HTTPException(409, "Такой e-mail уже зарегистрирован")
    user.email = email
    user.email_verified = False
    link = ""
    if mailer.smtp_configured(db):
        link = accounts.verify_link(db, user)
        db.commit()
        mailer.send_verify_email(db, email, link)
        return {"ok": True, "message": "E-mail изменён — отправлено письмо для подтверждения"}
    db.commit()
    return {"ok": True, "message": "E-mail изменён (подтверждение недоступно — SMTP не настроен)"}


@router.post("/resend-verification", summary="Чек-Пул: повторно прислать подтверждение")
def resend_verification(db: Session = Depends(get_db),
                        user=Depends(accounts.require_pool_user)):
    if user.email_verified:
        return {"ok": True, "message": "E-mail уже подтверждён"}
    if not mailer.smtp_configured(db):
        raise HTTPException(400, "Отправка писем не настроена")
    link = accounts.verify_link(db, user)
    db.commit()
    ok, err = mailer.send_verify_email(db, user.email, link)
    if not ok:
        raise HTTPException(502, f"Письмо не отправлено: {err}")
    return {"ok": True, "message": "Письмо отправлено"}


@router.post("/delete", summary="Чек-Пул: удаление аккаунта (152-ФЗ)")
def delete_account(body: DeleteBody, db: Session = Depends(get_db),
                   user=Depends(accounts.require_pool_user)):
    from ..auth import verify_password
    if not user.password_hash or not verify_password(body.password,
                                                     user.password_hash):
        raise HTTPException(403, "Пароль неверен")
    accounts.delete_account(db, user)
    db.commit()
    return {"ok": True, "message": "Аккаунт удалён: e-mail и пароль стёрты, баллы сгорели; сданные чеки остались в пуле обезличенными"}


@router.get("/referrals", summary="Чек-Пул: приглашения — код, статистика, список")
def referrals(db: Session = Depends(get_db),
              user=Depends(accounts.require_pool_user)):
    if not ingest.pool_enabled(db):
        raise HTTPException(403, "Приём чеков выключен — приглашения недоступны")
    code = referral.get_or_create_code(db, user)
    rows = (db.query(PoolReferral, PoolUser)
            .join(PoolUser, PoolUser.id == PoolReferral.referred_id)
            .filter(PoolReferral.referrer_id == user.id)
            .order_by(PoolReferral.created_at.desc()).limit(100).all())
    items = []
    earned_total = 0
    for ref, ru in rows:
        bonuses = (db.query(PoolPoint)
                   .filter(PoolPoint.user_id == user.id,
                           PoolPoint.ref_id == ref.id,
                           PoolPoint.reason.like("R_%")).all())
        got = sum(b.delta for b in bonuses)
        earned_total += got
        email = (ru.email or "").strip()
        if email:
            name, _, domain = email.partition("@")
            label = (name[:1] + "***@" + domain) if len(name) > 1 else email
        else:
            label = f"гость {(ru.vid or ru.id)[:8]}"
        items.append({
            "label": label,
            "at": ref.created_at.isoformat() if ref.created_at else None,
            "email_verified": bool(ru.email_verified),
            "receipts_verified": (db.query(PoolReceipt)
                                  .filter_by(pool_user_id=ru.id,
                                             status="verified").count()),
            "earned": got,
            "risk": ru.risk_score,
            "quarantined": bool(ru.quarantined_at),
        })
    return {"code": code,
            "link": f"{mailer.base_url(db)}/#/r/{code}" if code else "",
            "invited": len(items), "earned_total": earned_total,
            "limit": referral.REFERRAL_LIMIT,
            "next_bonus": referral.hint_for(db, user),
            "items": items}
