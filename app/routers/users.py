# -*- coding: utf-8 -*-
"""
Ямастер Чек — управление пользователями (ООО «Ямастер», ymaster.ru).

Инвариант системы: АДМИНИСТРАТОР ВСЕГДА ОДИН.
  * Создать второго админа нельзя (ни приглашением, ни напрямую);
  * Роль admin не выдаётся и не снимается через обычное редактирование;
  * Передача прав: /users/{id}/promote-admin — целевой становится админом,
    текущий админ автоматически становится бухгалтером.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_USER, hash_password, require_admin
from ..database import get_db
from ..models import User
from ..schemas import UserCreate, UserPatch
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/users", tags=["Пользователи"])


@router.get("", summary="Список пользователей")
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return [u.to_dict() for u in db.query(User).order_by(User.username).all()]


@router.post("", summary="Создать пользователя (роль: бухгалтер или пользователь)")
def create_user(body: UserCreate, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    if body.role not in (ROLE_ACCOUNTANT, ROLE_USER):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Роль «администратор» не выдаётся при создании. "
                            "Администратор один — передача прав через promote-admin.")
    if db.query(User).filter(User.username == body.username.strip()).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Логин уже занят")
    user = User(
        username=body.username.strip(),
        full_name=body.full_name or body.username,
        organization=body.organization or "",
        role=body.role,
        password_hash=hash_password(body.password),
        must_change_password=True,     # новый пользователь обязан сменить пароль
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(admin, "user_created", "user", user.id,
               {"username": user.username, "role": user.role})
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
    if body.role is not None and body.role != user.role:
        # Единственного админа нельзя ни разжаловать, ни повысить через patch
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Изменение роли запрещено. Администратор один: передача "
                            "прав — кнопкой «Сделать администратором» у бухгалтера.")
    if body.is_active is not None:
        if user.id == admin.id and not body.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя отключить себя")
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)
        user.must_change_password = True   # после сброса — обязан сменить пароль
    db.commit()
    log_action(admin, "user_updated", "user", user.id, {"username": user.username})
    return user.to_dict()


@router.delete("/{user_id}", summary="Архивировать пользователя")
def delete_user(user_id: str, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя архивировать себя")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    user.is_active = False
    user.username = f"archived_{user.username}_{user.id[:6]}"
    db.commit()
    log_action(admin, "user_archived", "user", user_id, {"username": user.username})
    return {"ok": True, "message": "Пользователь архивирован (отключён)"}


@router.post("/{user_id}/promote-admin",
             summary="Передать права администратора (админ всегда один)")
def promote_admin(user_id: str, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Вы уже администратор")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    if target.role != ROLE_ACCOUNTANT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Передавать права можно только пользователю с ролью "
                            "«бухгалтер»")
    if not target.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Пользователь отключён")

    # Атомарная передача: целевой → админ, текущий → бухгалтер
    target.role = ROLE_ADMIN
    target.must_change_password = target.must_change_password
    admin.role = ROLE_ACCOUNTANT
    db.commit()
    log_action(admin, "admin_transferred", "user", target.id,
               {"from": admin.username, "to": target.username})
    return {
        "ok": True,
        "message": f"Права администратора переданы: {target.username}. "
                   f"Вы теперь бухгалтер.",
        "new_admin": target.to_dict(),
        "previous_admin": admin.to_dict(),
    }
