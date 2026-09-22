# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Управление пользователями (только администраторы).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import hash_password, require_admin
from ..database import get_db
from ..models import User
from ..schemas import UserCreate, UserPatch
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/users", tags=["Пользователи"])


@router.get("", summary="Список пользователей")
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return [u.to_dict() for u in db.query(User).order_by(User.username).all()]


@router.post("", summary="Создать пользователя")
def create_user(body: UserCreate, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    if db.query(User).filter(User.username == body.username.strip()).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Логин уже занят")
    user = User(
        username=body.username.strip(),
        full_name=body.full_name or body.username,
        organization=body.organization or "",
        role=body.role,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(admin, "user_created", "user", user.id, {"username": user.username})
    return user.to_dict()


@router.patch("/{user_id}", summary="Изменить пользователя")
def patch_user(user_id: str, body: UserPatch, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.organization is not None:
        user.organization = body.organization
    if body.role is not None:
        if user.id == admin.id and body.role != "admin":
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Нельзя снять права администратора с себя")
        user.role = body.role
    if body.is_active is not None:
        if user.id == admin.id and not body.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя отключить себя")
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)
    db.commit()
    log_action(admin, "user_updated", "user", user.id, {"username": user.username})
    return user.to_dict()


@router.delete("/{user_id}", summary="Удалить пользователя")
def delete_user(user_id: str, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя удалить себя")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    user.is_active = False
    user.username = f"archived_{user.username}_{user.id[:6]}"
    db.commit()
    log_action(admin, "user_archived", "user", user_id, {"username": user.username})
    return {"ok": True, "message": "Пользователь архивирован (отключён)"}
