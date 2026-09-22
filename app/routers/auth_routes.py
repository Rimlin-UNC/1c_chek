# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Роутер аутентификации: вход, регистрация, профиль.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..auth import (client_ip, create_access_token, get_current_user,
                    hash_password, verify_password)
from ..database import get_db
from ..models import User
from ..schemas import LoginRequest, PasswordChange, UserCreate
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/auth", tags=["Аутентификация"])


@router.post("/login", summary="Вход (получение JWT-токена)")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username.strip()).first()
    if not user or not verify_password(body.password, user.password_hash):
        log_action(None, "login_failed", details={"username": body.username,
                                                  "ip": client_ip(request)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Учётная запись отключена")

    import datetime as _dt
    user.last_login_at = _dt.datetime.utcnow()
    db.commit()
    log_action(user, "login", details={"ip": client_ip(request)})
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": user.to_dict(),
    }


@router.post("/register", summary="Регистрация (первый пользователь становится админом)")
def register(body: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).count() > 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Регистрация закрыта: пользователей создаёт администратор")
    user = User(
        username=body.username.strip(),
        full_name=body.full_name or body.username,
        password_hash=hash_password(body.password),
        role="admin",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(user, "register", "user", user.id)
    return {"access_token": create_access_token(user), "token_type": "bearer",
            "user": user.to_dict()}


@router.get("/me", summary="Текущий пользователь")
def me(user: User = Depends(get_current_user)):
    return user.to_dict()


@router.post("/change-password", summary="Смена собственного пароля")
def change_password(body: PasswordChange, request: Request,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Старый пароль неверен")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    log_action(user, "password_changed", details={"ip": client_ip(request)})
    return {"ok": True, "message": "Пароль изменён"}
