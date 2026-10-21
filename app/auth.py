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
ROLE_MANAGER = "manager"     # v1.64.0: руководитель — только просмотр своей компании
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


# v1.70.38 (аудит): потолок числа итераций в записи хэша. Значение берётся из
# самой записи, поэтому битая или подделанная строка вроде
# «pbkdf2$999999999999$..$..» заставляла бы процесс считать хэш часами —
# прожог ЦП без единого падения в журнал. Граница растёт вместе с рабочей
# константой, так что повышение итераций в будущем проверку не сломает.
_PBKDF2_ITERS_MAX = max(1_000_000, _PBKDF2_ITERATIONS * 4)


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, digest_hex = stored.split("$")
        iters = int(iters)
        if not 1000 <= iters <= _PBKDF2_ITERS_MAX:
            return False            # запись не похожа на нашу — не жжём ЦП
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), iters
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


# --------------------------------------------------------------------------
#  v1.18.0: Режим просмотра — администратор смотрит приложение глазами
#  сотрудника или бухгалтера. Токен выписывается НА целевого пользователя,
#  а личность администратора фиксируется стандартным claim-ом «act»
#  (RFC 8693 «Actor claim»): act.sub = id администратора. Пароль при
#  переключении НЕ запрашивается — admin-сессия не прерывается.
# --------------------------------------------------------------------------
def create_impersonation_token(admin: User, target: User) -> str:
    """Токен «просмотра от имени»: sub = целевой пользователь, act.sub = админ."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": target.id,
        "username": target.username,
        "role": target.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iss": "ymaster-check",
        "act": {"sub": admin.id, "username": admin.username},
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def act_admin_id(payload: dict) -> str | None:
    """v1.18.0: id администратора из токена просмотра (или None)."""
    act = payload.get("act")
    if isinstance(act, dict):
        return act.get("sub") or None
    return None


def raw_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str | None:
    """v1.18.0: сырой токен запроса (заголовок, иначе cookie) — для /stop."""
    if credentials and credentials.credentials:
        return credentials.credentials
    return request.cookies.get("ymaster_token")


def cookie_secure(request: Request) -> bool:
    """v1.70.11: Secure-флаг cookie — только когда соединение действительно
    HTTPS (схема или X-Forwarded-Proto от локального nginx; uvicorn слушает
    только 127.0.0.1, снаружи заголовок подделать нельзя). На http-превью
    и в локальной сети cookie работает как раньше."""
    if request.url.scheme == "https":
        return True
    return (request.headers.get("x-forwarded-proto") or "").lower() == "https"


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
    """IP клиента с учётом reverse-proxy (v1.67.1: только доверенный прокси)."""
    from .security import real_client_ip
    return real_client_ip(request)
