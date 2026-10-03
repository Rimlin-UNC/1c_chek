# -*- coding: utf-8 -*-
"""
Ямастер Чек — приглашения для регистрации (ООО «Ямастер», ymaster.ru).

Администратор создаёт приглашение с ролью (бухгалтер/пользователь); ссылка вида
  /#/register/<TOKEN>
выдаётся сотруднику. Роль присваивается автоматически — путаницы нет.
Приглашение может быть одноразовым или многоразовым, с сроком действия.
"""
from __future__ import annotations

import datetime as dt
import secrets

import io

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import ROLE_ACCOUNTANT, ROLE_USER, require_admin
from ..database import get_db
from ..models import Company, Invite, User
from ..schemas import InviteCreate
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/invites", tags=["Приглашения"])


@router.get("", summary="Список приглашений")
def list_invites(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    comps = {c.id: c.name for c in db.query(Company).all()}
    out = []
    for i in db.query(Invite).order_by(Invite.created_at.desc()).limit(200).all():
        d = i.to_dict()
        d["company_name"] = comps.get(i.company_id)      # v1.11.0
        out.append(d)
    return out


@router.post("", summary="Создать приглашение (получить ссылку)")
def create_invite(body: InviteCreate, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    # v1.12.0: компания приглашения — company_id, либо company_name
    # (находится по названию без учёта регистра, при отсутствии создаётся)
    company_id = None
    if body.company_id:
        comp = db.get(Company, body.company_id)
        if not comp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
        company_id = comp.id
    elif (body.company_name or "").strip():
        name = body.company_name.strip()[:200]
        cf = name.casefold()
        comp = next((c for c in db.query(Company).all()
                     if c.name.casefold() == cf), None)
        if comp is None:
            comp = Company(name=name)
            db.add(comp)
            db.flush()
        company_id = comp.id
    invite = Invite(
        token=secrets.token_urlsafe(16),
        role=body.role if body.role in (ROLE_ACCOUNTANT, ROLE_USER) else ROLE_USER,
        note=body.note.strip(),
        company_id=company_id,               # v1.11.0: пространство приглашённого
        created_by=admin.id,
        expires_at=dt.datetime.utcnow() + dt.timedelta(hours=body.expires_hours),
        max_uses=body.max_uses,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    comp = db.get(Company, invite.company_id) if invite.company_id else None
    log_action(admin, "invite_created", "invite", invite.id,
               {"role": invite.role, "max_uses": invite.max_uses,
                "company_id": invite.company_id,
                "company": comp.name if comp else None})
    return {**invite.to_dict(), "company_name": comp.name if comp else None}


@router.get("/{invite_id}/qr", summary="QR-код ссылки-приглашения (PNG)")
def invite_qr(invite_id: str, request: Request,
              db: Session = Depends(get_db),
              admin: User = Depends(require_admin)):
    """v1.8.2: PNG с QR-кодом регистрации — сотрудник наводит камеру телефона
    и попадает на страницу приглашения без пересылки ссылки."""
    inv = db.get(Invite, invite_id)
    if not inv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено")
    if not inv.is_valid:                       # v1.8.2: у модели свойство is_valid
        if inv.revoked:
            why = "отозвано"
        elif inv.expires_at and inv.expires_at < dt.datetime.utcnow():
            why = "истёк срок действия"
        else:
            why = "исчерпан лимит использований"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Приглашение недоступно: {why}")
    # адрес собираем как его видит пользователь (за nginx — https и боевой домен)
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    url = f"{proto}://{host}/#/register/{inv.token}"
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    log_action(admin, "invite_qr", "invite", inv.id)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="invite-qr.png"',
                 "X-Invite-Url": url})


@router.post("/{invite_id}/revoke", summary="Отозвать приглашение")
def revoke_invite(invite_id: str, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    invite = db.get(Invite, invite_id)
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено")
    invite.revoked = True
    db.commit()
    log_action(admin, "invite_revoked", "invite", invite.id)
    return {"ok": True, "message": "Приглашение отозвано"}
