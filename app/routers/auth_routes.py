# -*- coding: utf-8 -*-
"""
Ямастер Чек — аутентификация (ООО «Ямастер», ymaster.ru).

Модель доступа:
  * Регистрация — ТОЛЬКО по приглашению от администратора (ссылка с токеном);
    роль присваивается приглашением сразу: «бухгалтер» или «пользователь».
  * Администратор всегда ОДИН; передача прав — через /api/v1/users/{id}/promote-admin.
  * Перебор паролей блокируется: 5 неудач за 15 минут → блок 15 минут (LoginGuard).
"""
from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..auth import (ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_USER, act_admin_id,
                    client_ip, create_access_token, decode_token,
                    get_current_user, hash_password, raw_bearer_token,
                    verify_password)
from ..database import get_db
from ..models import Company, Invite, User
from ..schemas import LoginRequest, PasswordChange, RegisterRequest
from ..security import login_guard
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/auth", tags=["Аутентификация"])


@router.post("/login", summary="Вход (получение JWT-токена)")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    key = (ip, body.username.strip().lower())
    login_guard.check(key)  # бросает 429, если перебирают пароль

    user = db.query(User).filter(User.username == body.username.strip()).first()
    if not user or not verify_password(body.password, user.password_hash):
        login_guard.record_fail(key)
        log_action(None, "login_failed", details={"username": body.username, "ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Учётная запись отключена")

    login_guard.record_ok(key)
    user.last_login_at = dt.datetime.utcnow()
    db.commit()
    log_action(user, "login", details={"ip": ip})
    token = create_access_token(user)
    resp = JSONResponse({
        "access_token": token,
        "token_type": "bearer",
        "user": user.to_dict(),
    })
    # Дублируем токен в HttpOnly-cookie: браузер передаёт её автоматически,
    # поэтому «вход успешен → следующий запрос 401» невозможен в принципе.
    resp.set_cookie(
        "ymaster_token", token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True, samesite="lax", path="/",
    )
    return resp


# --------------------------------------------------------------------------
#  Приглашения
# --------------------------------------------------------------------------
@router.get("/invite-info", summary="Информация о приглашении (для страницы регистрации)")
def invite_info(token: str, db: Session = Depends(get_db)):
    invite = db.query(Invite).filter(Invite.token == token.strip()).first()
    if not invite or not invite.is_valid:
        return {"valid": False, "message": "Приглашение недействительно или уже использовано"}
    comp = db.get(Company, invite.company_id) if invite.company_id else None
    return {
        "valid": True,
        "role": invite.role,
        "note": invite.note,
        "company_id": invite.company_id,           # v1.11.0
        "company_name": comp.name if comp else None,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }


@router.post("/register", summary="Регистрация по приглашению (роль выдаёт приглашение)")
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    invite = db.query(Invite).filter(Invite.token == body.token.strip()).first()
    if not invite or not invite.is_valid:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Приглашение недействительно, отозвано или уже использовано. "
                            "Запросите новую ссылку у администратора.")
    if invite.role not in (ROLE_ACCOUNTANT, ROLE_USER):
        # Администратор не назначается через приглашения — только передача прав
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недопустимая роль в приглашении")

    username = body.username.strip()
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Логин уже занят")

    user = User(
        username=username,
        full_name=body.full_name.strip() or username,
        organization=invite.note or "",
        password_hash=hash_password(body.password),
        role=invite.role,               # роль из приглашения — сразу, без путаницы
        company_id=invite.company_id,   # v1.11.0: пространство приглашённого
        must_change_password=False,
    )
    invite.used_count += 1
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(user, "register", "user", user.id,
               {"role": user.role, "invite": invite.id, "ip": client_ip(request)})
    token = create_access_token(user)
    resp = JSONResponse({"access_token": token, "token_type": "bearer",
                         "user": user.to_dict()})
    resp.set_cookie(
        "ymaster_token", token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True, samesite="lax", path="/",
    )
    return resp


@router.get("/me", summary="Текущий пользователь")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db),
       token: str | None = Depends(raw_bearer_token)):
    d = user.to_dict()
    # v1.11.0: название своей компании — для шапки интерфейса
    d["company_name"] = (db.get(Company, user.company_id).name
                         if user.company_id else None)
    d["is_platform_admin"] = (user.role == "admin")
    # v1.16.0: Telegram-привязка для личного кабинета настроек
    d["telegram_bound"] = bool(user.telegram_chat_id)
    # v1.17.0: реквизиты своей компании — для блока «Ваша компания»
    if user.company_id:
        c = db.get(Company, user.company_id)
        if c:
            d["company_inn"] = c.inn or ""
    # v1.18.0: режим просмотра — клиенту нужно знать, чьими глазами смотрят
    if token:
        admin_id = act_admin_id(decode_token(token))
        if admin_id:
            admin = db.get(User, admin_id)
            d["viewing_as"] = {
                "admin_id": admin_id,
                "admin_username": admin.username if admin else "?",
                "admin_name": (admin.full_name or admin.username) if admin else "?",
            }
    return d


@router.post("/change-password", summary="Смена собственного пароля")
def change_password(body: PasswordChange, request: Request,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Старый пароль неверен")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    log_action(user, "password_changed", details={"ip": client_ip(request)})
    # Перевыпускаем токен и cookie (старый JWT содержит прежние claims)
    token = create_access_token(user)
    resp = JSONResponse({"ok": True, "message": "Пароль изменён",
                         "access_token": token, "token_type": "bearer"})
    resp.set_cookie(
        "ymaster_token", token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True, samesite="lax", path="/",
    )
    return resp


@router.post("/logout", summary="Выход (очистка cookie сессии)")
def logout(response: Response):
    response.delete_cookie("ymaster_token", path="/")
    return {"ok": True, "message": "Вы вышли из системы"}
