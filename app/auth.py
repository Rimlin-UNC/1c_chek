# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Аутентификация и авторизация: JWT (HS256), PBKDF2-хеширование паролей, RBAC.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User

# Роли системы: администратор всегда один (передача прав — отдельной процедурой)
ROLE_ADMIN = "admin"
ROLE_ACCOUNTANT = "accountant"
ROLE_USER = "user"

bearer_scheme = HTTPBearer(auto_error=False)

# Количество итераций PBKDF2 (OWASP-рекомендация для SHA-256)
_PBKDF2_ITERATIONS = 260_000


# --------------------------------------------------------------------------
#  Пароли (PBKDF2-HMAC-SHA256, без внешних зависимостей)
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


# --------------------------------------------------------------------------
#  JWT-токены
# --------------------------------------------------------------------------
def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iss": "ymaster-check",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Токен истёк, войдите заново")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недействительный токен")


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    # Приоритет: заголовок Authorization; запасной вариант — HttpOnly-cookie
    # (гарантирует работу даже если JS не смог передать заголовок).
    token = credentials.credentials if credentials and credentials.credentials else None
    if not token:
        token = request.cookies.get("ymaster_token")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    payload = decode_token(token)
    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Пользователь не найден или отключён")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Требуются права администратора")
    return user


def require_role(*roles: str):
    """Фабрика зависимостей: доступ только для перечисленных ролей."""
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Недостаточно прав для этого действия")
        return user
    return dep


# Бухгалтер + администратор: проверка ФНС, выгрузка в 1С, редактирование чеков
require_accountant = require_role(ROLE_ADMIN, ROLE_ACCOUNTANT)


def client_ip(request: Request) -> str:
    """IP клиента с учётом reverse-proxy."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
