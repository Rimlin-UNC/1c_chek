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
import io
import secrets
from typing import Annotated

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import (ROLE_ACCOUNTANT, ROLE_ADMIN, ROLE_MANAGER, ROLE_USER,
                    get_current_user, require_admin)
from ..clients import service as clients_svc
from ..clients.models import AccountantClient
from ..database import get_db
from ..models import AppSetting, Company, Invite, User
from ..schemas import InviteCreate
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/invites", tags=["Приглашения"])
company_router = APIRouter(prefix="/api/v1", tags=["Команда компании"])


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
        role=body.role if body.role in (ROLE_ACCOUNTANT, ROLE_MANAGER,
                                        ROLE_USER) else ROLE_USER,
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


# ==========================================================================
#  v1.70.0: отправка приглашения получателю ботом в MAX
# ==========================================================================
from pydantic import BaseModel, Field


class SendMaxBody(BaseModel):
    max_login: str = Field(min_length=3, max_length=100)


@router.post("/{invite_id}/send-max",
             summary="Отправить приглашение получателю ботом в MAX")
def send_max(invite_id: str, body: SendMaxBody, request: Request,
             db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    """Резолвим MAX-логин → id (PoolUser мини-приложения, сотрудник с
    привязкой или цифровой id) и шлём сообщение с кнопками: открыть
    приглашение в MAX (мини-приложение) или на сайте."""
    inv = db.get(Invite, invite_id)
    if not inv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено")
    if not inv.is_valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Приглашение недействительно — создайте новое")
    from ..auth import client_ip
    from ..services import max_bot, max_login
    mid = max_login.resolve_max_id(db, body.max_login)
    if not mid:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Получатель не найден: пусть откроет вашего бота в MAX "
            "(или мини-приложение) — и отправьте ещё раз")
    token = max_bot.get_token(db)
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Бот MAX не настроен (Настройки → Ассистенты)")
    comp = db.get(Company, inv.company_id) if inv.company_id else None
    roles = {"accountant": "Бухгалтер", "manager": "Руководитель",
             "user": "Сотрудник"}
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "localhost")
    web_url = f"{proto}://{host}/#/register/{inv.token}"
    text = ("📩 Приглашение в «Ямастер Чек»\n"
            + (f"Компания: {comp.name}\n" if comp else "")
            + f"Роль: {roles.get(inv.role, inv.role)}\n"
            + (f"Действует до: "
               f"{inv.expires_at.strftime('%d.%m.%Y %H:%M')}\n"
               if inv.expires_at else "")
            + "\nОткройте ссылку — вход и регистрация займут минуту.")
    buttons = [
        {"type": "link", "text": "Открыть в MAX",
         "url": (f"https://max.ru/{max_bot.bot_username(db, refresh=True)}"
                 f"?startapp=inv_{inv.token}")},
        {"type": "link", "text": "Открыть на сайте", "url": web_url},
    ]
    try:
        max_bot.send_rich(token, mid, text, buttons)
    except max_bot.MaxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    log_action(admin, "invite_send_max", "invite", inv.id,
               {"max_id": mid, "ip": client_ip(request)})
    return {"sent": True, "max_id": mid}


# ==========================================================================
#  v1.73.12: Команда компании и приглашения для бухгалтера и руководителя
#  (с поддержкой нескольких компаний у бухгалтера и руководителя группы)
# ==========================================================================
def _manageable_companies(db: Session, user: User) -> list[Company]:
    """Активные компании, в которые пользователь вправе приглашать участников."""
    if user.role == ROLE_ADMIN:
        return (db.query(Company).filter(Company.is_active.is_(True))
                .order_by(Company.name).all())
    if user.role not in (ROLE_ACCOUNTANT, ROLE_MANAGER):
        return []
    home = clients_svc.home_company_id(user)
    ids: list[str] = []
    if home:
        ids.append(home)
    for cid in clients_svc.client_ids(db, user.id):
        if cid not in ids:
            ids.append(cid)
    if not ids:
        return []
    rows = (db.query(Company)
            .filter(Company.id.in_(ids), Company.is_active.is_(True))
            .all())
    by_id = {c.id: c for c in rows}
    return [by_id[cid] for cid in ids if cid in by_id]


def _invitable_roles_for(role: str) -> tuple[str, ...]:
    """Кого может приглашать роль внутри своих компаний."""
    if role == ROLE_ADMIN:
        return (ROLE_USER, ROLE_MANAGER, ROLE_ACCOUNTANT)
    if role == ROLE_ACCOUNTANT:
        # Бухгалтер приглашает сотрудников, руководителей и коллег-бухгалтеров в свои компании
        return (ROLE_USER, ROLE_MANAGER, ROLE_ACCOUNTANT)
    if role == ROLE_MANAGER:
        # Руководитель приглашает сотрудников и бухгалтера в свою компанию
        return (ROLE_USER, ROLE_ACCOUNTANT)
    return ()


def _advance_deadline_days(db: Session) -> int:
    row = db.get(AppSetting, "advance_deadline_days")
    if row and (row.value or "").strip().isdigit():
        return max(1, min(365, int(row.value.strip())))
    return 10


def _company_staff_summary(db: Session, cid: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Бухгалтеры (штатные + аутсорс), руководители и все активные участники компании."""
    direct = (db.query(User)
              .filter(User.company_id == cid, User.is_active.is_(True))
              .order_by(User.full_name, User.username).all())
    outsource_ids = [
        uid for (uid,) in db.query(AccountantClient.user_id)
        .filter(AccountantClient.company_id == cid).all()
    ]
    outsourcers = []
    if outsource_ids:
        outsourcers = (db.query(User)
                       .filter(User.id.in_(outsource_ids),
                               User.role == ROLE_ACCOUNTANT,
                               User.is_active.is_(True))
                       .order_by(User.full_name, User.username).all())
    seen_acc: set[str] = set()
    accountants: list[dict] = []
    for u in [*[x for x in direct if x.role == ROLE_ACCOUNTANT], *outsourcers]:
        if u.id in seen_acc:
            continue
        seen_acc.add(u.id)
        accountants.append({
            "id": u.id,
            "name": u.full_name or u.username,
            "username": u.username,
            "is_outsource": u.company_id != cid,
        })
    managers = [
        {"id": u.id, "name": u.full_name or u.username, "username": u.username}
        for u in direct if u.role == ROLE_MANAGER
    ]
    members = [
        {"id": u.id, "name": u.full_name or u.username,
         "username": u.username, "role": u.role}
        for u in direct if u.role in (ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_USER)
    ]
    return accountants, managers, members


@company_router.get("/company-team",
                    summary="Карточка моей компании, закреплённый бухгалтер и приглашения команды")
def get_company_team(db: Annotated[Session, Depends(get_db)],
                     user: Annotated[User, Depends(get_current_user)],
                     company_id: str = ""):
    manageable = _manageable_companies(db, user)
    manageable_ids = {c.id for c in manageable}
    inv_roles = list(_invitable_roles_for(user.role))
    can_invite = bool(inv_roles and manageable)

    target_cid = (company_id or "").strip() or (user.company_id or "")
    if company_id and target_cid not in manageable_ids and user.role != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Нет доступа к выбранной компании")
    if not target_cid and manageable:
        target_cid = manageable[0].id

    comp = db.get(Company, target_cid) if target_cid else None
    deadline = _advance_deadline_days(db)
    if comp is None:
        return {
            "company": None,
            "advance_deadline_days": deadline,
            "accountants": [],
            "managers": [],
            "members": [],
            "can_invite": can_invite,
            "invitable_roles": inv_roles,
            "manageable_companies": [
                {"id": c.id, "name": c.short_name or c.name, "full_name": c.name, "inn": c.inn or ""}
                for c in manageable
            ],
            "invites": [],
        }

    accountants, managers, members = _company_staff_summary(db, comp.id)
    invites_out: list[dict] = []
    if can_invite and (user.role == ROLE_ADMIN or comp.id in manageable_ids):
        rows = (db.query(Invite)
                .filter(Invite.company_id == comp.id)
                .order_by(Invite.created_at.desc())
                .limit(50).all())
        invites_out = [
            {**i.to_dict(), "company_name": comp.short_name or comp.name}
            for i in rows if i.is_valid
        ]

    return {
        "company": {
            "id": comp.id,
            "name": comp.short_name or comp.name,
            "full_name": comp.name,
            "inn": comp.inn or "",
        },
        "advance_deadline_days": deadline,
        "accountants": accountants,
        "managers": managers,
        "members": members if can_invite else [],
        "can_invite": can_invite,
        "invitable_roles": inv_roles,
        "manageable_companies": [
            {"id": c.id, "name": c.short_name or c.name, "full_name": c.name, "inn": c.inn or ""}
            for c in manageable
        ],
        "invites": invites_out,
    }


@company_router.post("/company-invites",
                     summary="Создать приглашение в свою или закреплённую компанию (бухгалтер / руководитель)")
def create_company_invite(body: InviteCreate,
                          db: Annotated[Session, Depends(get_db)],
                          user: Annotated[User, Depends(get_current_user)]):
    inv_roles = _invitable_roles_for(user.role)
    if not inv_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Приглашать участников могут бухгалтер, руководитель и администратор")
    if body.role not in inv_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Недопустимая роль для приглашения с вашими правами")

    manageable = _manageable_companies(db, user)
    manageable_map = {c.id: c for c in manageable}
    if not manageable_map:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "За вашим профилем не закреплена активная компания")

    target_cid = (body.company_id or "").strip() or (user.company_id or "")
    if target_cid not in manageable_map:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Можно приглашать участников только в свою или закреплённую за вами компанию")

    comp = manageable_map[target_cid]
    invite = Invite(
        token=secrets.token_urlsafe(16),
        role=body.role,
        note=(body.note or "").strip()[:200],
        company_id=comp.id,
        created_by=user.id,
        expires_at=dt.datetime.utcnow() + dt.timedelta(hours=body.expires_hours),
        max_uses=body.max_uses,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    log_action(user, "company_invite_created", "invite", invite.id,
               {"role": invite.role, "max_uses": invite.max_uses,
                "company_id": comp.id, "company": comp.name})
    return {**invite.to_dict(), "company_name": comp.short_name or comp.name}


def _require_company_invite_access(db: Session, user: User, invite_id: str) -> Invite:
    inv_roles = _invitable_roles_for(user.role)
    if not inv_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
    inv = db.get(Invite, invite_id)
    if not inv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено")
    if user.role != ROLE_ADMIN:
        allowed_ids = {c.id for c in _manageable_companies(db, user)}
        if not inv.company_id or inv.company_id not in allowed_ids:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Нет доступа к приглашению другой компании")
    return inv


@company_router.get("/company-invites/{invite_id}/qr",
                    summary="QR-код приглашения компании (PNG)")
def company_invite_qr(invite_id: str, request: Request,
                      db: Annotated[Session, Depends(get_db)],
                      user: Annotated[User, Depends(get_current_user)]):
    inv = _require_company_invite_access(db, user, invite_id)
    if not inv.is_valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Приглашение уже недействительно")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    url = f"{proto}://{host}/#/register/{inv.token}"
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    log_action(user, "company_invite_qr", "invite", inv.id)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="invite-qr.png"',
                 "X-Invite-Url": url})


@company_router.post("/company-invites/{invite_id}/revoke",
                     summary="Отозвать приглашение в свою компанию")
def revoke_company_invite(invite_id: str,
                          db: Annotated[Session, Depends(get_db)],
                          user: Annotated[User, Depends(get_current_user)]):
    inv = _require_company_invite_access(db, user, invite_id)
    inv.revoked = True
    db.commit()
    log_action(user, "company_invite_revoked", "invite", inv.id)
    return {"ok": True, "message": "Приглашение отозвано"}


@company_router.post("/company-invites/{invite_id}/send-max",
                     summary="Отправить приглашение компании получателю в MAX")
def send_company_invite_max(invite_id: str, body: SendMaxBody, request: Request,
                            db: Annotated[Session, Depends(get_db)],
                            user: Annotated[User, Depends(get_current_user)]):
    inv = _require_company_invite_access(db, user, invite_id)
    if not inv.is_valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Приглашение недействительно — создайте новое")
    from ..auth import client_ip
    from ..services import max_bot, max_login
    mid = max_login.resolve_max_id(db, body.max_login)
    if not mid:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Получатель не найден: пусть откроет бота в MAX и отправьте ещё раз")
    token = max_bot.get_token(db)
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Бот MAX не настроен администратором")
    comp = db.get(Company, inv.company_id) if inv.company_id else None
    roles = {"accountant": "Бухгалтер", "manager": "Руководитель",
             "user": "Сотрудник"}
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "localhost")
    web_url = f"{proto}://{host}/#/register/{inv.token}"
    text = ("📩 Приглашение в «Ямастер Чек»\n"
            + (f"Компания: {comp.name}\n" if comp else "")
            + f"Роль: {roles.get(inv.role, inv.role)}\n"
            + (f"Действует до: "
               f"{inv.expires_at.strftime('%d.%m.%Y %H:%M')}\n"
               if inv.expires_at else "")
            + "\nОткройте ссылку — вход и регистрация займут минуту.")
    buttons = [
        {"type": "link", "text": "Открыть в MAX",
         "url": (f"https://max.ru/{max_bot.bot_username(db, refresh=True)}"
                 f"?startapp=inv_{inv.token}")},
        {"type": "link", "text": "Открыть на сайте", "url": web_url},
    ]
    try:
        max_bot.send_rich(token, mid, text, buttons)
    except max_bot.MaxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    log_action(user, "company_invite_send_max", "invite", inv.id,
               {"max_id": mid, "ip": client_ip(request)})
    return {"sent": True, "max_id": mid}
