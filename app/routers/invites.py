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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import ROLE_ACCOUNTANT, ROLE_USER, require_admin
from ..database import get_db
from ..models import Invite, User
from ..schemas import InviteCreate
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/invites", tags=["Приглашения"])


@router.get("", summary="Список приглашений")
def list_invites(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(Invite).order_by(Invite.created_at.desc()).limit(200).all()
    return [i.to_dict() for i in rows]


@router.post("", summary="Создать приглашение (получить ссылку)")
def create_invite(body: InviteCreate, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    invite = Invite(
        token=secrets.token_urlsafe(16),
        role=body.role if body.role in (ROLE_ACCOUNTANT, ROLE_USER) else ROLE_USER,
        note=body.note.strip(),
        created_by=admin.id,
        expires_at=dt.datetime.utcnow() + dt.timedelta(hours=body.expires_hours),
        max_uses=body.max_uses,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    log_action(admin, "invite_created", "invite", invite.id,
               {"role": invite.role, "max_uses": invite.max_uses})
    return invite.to_dict()


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
