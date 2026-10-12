# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — вход/регистрация через MAX на сайте (v1.70.0).
# Device-code поток: сайт создаёт одноразовый код (10 минут, в БД —
# только хэш), человек подтверждает его в MAX — в мини-приложении
# (подпись initData → пул-JWT) или сообщением боту. Подтверждённый код
# → обычная сессия сайта (create_access_token + HttpOnly-cookie), как
# при входе по паролю. Регистрация — если MAX-id ещё не привязан к
# учётной записи; выключается настройкой max_web_register=0.
# ======================================================================
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import time
from collections import deque

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import ROLE_USER, create_access_token, hash_password
from ..models import MaxLoginCode, User, utcnow

CODE_TTL_S = 10 * 60                     # жизнь кода на подтверждение
TICKET_TTL_S = 15 * 60                   # веб-билет из мини-приложения
RATE_MAX = 10                            # кодов с одного IP в час
RATE_WINDOW_S = 3600
SITE_URL = "https://chek.ymaster.ru"     # боевой домен (как диплинк бота)
BOT_LINK = "https://max.ru/chek.ymaster.bot"

_ip_times: dict[str, deque] = {}
_UNSAFE = re.compile(r"[^a-z0-9_]")


def _code_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").strip().encode()).hexdigest()


def create_code(db: Session, ip: str = "") -> dict:
    """Новый одноразовый код входа: {code, expires_in, max_link}."""
    now = time.monotonic()
    dq = _ip_times.setdefault((ip or "?")[:64], deque())
    while dq and dq[0] <= now - RATE_WINDOW_S:
        dq.popleft()
    if len(dq) >= RATE_MAX:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Слишком много запросов кода — попробуйте позже")
    dq.append(now)
    raw = "login_" + secrets.token_urlsafe(24)   # 32 симв. A-Za-z0-9_-
    db.add(MaxLoginCode(code_hash=_code_hash(raw), purpose="web",
                        ip=(ip or "")[:64],
                        expires_at=utcnow() + dt.timedelta(seconds=CODE_TTL_S)))
    db.commit()
    return {"code": raw, "expires_in": CODE_TTL_S,
            "max_link": f"{BOT_LINK}?startapp={raw}"}


def _live_row(db: Session, raw: str) -> MaxLoginCode | None:
    row = (db.query(MaxLoginCode)
           .filter(MaxLoginCode.code_hash == _code_hash(raw)).first())
    if row is None or row.used_at is not None:
        return None
    if row.expires_at and row.expires_at < utcnow():
        return None
    return row


def confirm(db: Session, raw: str, max_id: str, username: str = "",
            display: str = "") -> bool:
    """Подтверждение кода MAX-личностью (мини-приложение или бот)."""
    row = _live_row(db, raw)
    if row is None or row.max_user_id:
        return False
    row.max_user_id = str(max_id)[:32]
    row.max_username = (username or "")[:100]
    row.max_name = (display or "")[:200]
    db.commit()
    return True


def poll(db: Session, raw: str) -> dict:
    """Статус кода: pending | expired | ok (ok — код погашен, сессия готова)."""
    row = _live_row(db, raw)
    if row is None:
        return {"status": "expired"}
    if not row.max_user_id:
        return {"status": "pending"}
    row.used_at = utcnow()
    db.commit()
    user = login_or_register(db, row.max_user_id, row.max_username,
                             row.max_name or row.max_username)
    return {"status": "ok", "access_token": create_access_token(user),
            "user": user.to_dict()}


def make_webticket(db: Session, max_id: str, username: str = "",
                   display: str = "") -> str:
    """Подтверждённый одноразовый билет «открыть сайт уже залогиненным»
    (мини-приложение показывает ссылку после принятия приглашения)."""
    raw = "login_" + secrets.token_urlsafe(24)
    db.add(MaxLoginCode(code_hash=_code_hash(raw), purpose="web",
                        max_user_id=str(max_id)[:32],
                        max_username=(username or "")[:100],
                        max_name=(display or "")[:200],
                        expires_at=utcnow()
                        + dt.timedelta(seconds=TICKET_TTL_S)))
    db.commit()
    return raw


def resolve_max_id(db: Session, login_raw: str) -> str | None:
    """MAX-логин → id: цифры — как есть; иначе PoolUser (мини-приложение)
    или сотрудник с привязанным MAX (логин ядра == username MAX)."""
    s = (login_raw or "").strip().lstrip("@").lower()
    if not s:
        return None
    if s.isdigit() and len(s) >= 4:
        return s
    from ..pool.models import PoolUser
    pu = (db.query(PoolUser)
          .filter(func.lower(PoolUser.tg_username) == s).first())
    if pu is not None and (pu.tg_user_id or "").strip():
        return str(pu.tg_user_id)
    u = (db.query(User)
         .filter(func.lower(User.username) == s,
                 User.max_user_id.isnot(None)).first())
    return u.max_user_id if u is not None else None


def _unique_username(db: Session, max_id: str, username: str) -> str:
    base = _UNSAFE.sub("", (username or "").lower())[:30]
    if len(base) < 3:
        base = f"max{str(max_id)[-8:]}" or "maxuser"
    cand = base
    while db.query(User).filter(User.username == cand).first() is not None:
        cand = f"{base}_{secrets.token_hex(2)}"
    return cand


def login_or_register(db: Session, max_id: str, username: str = "",
                      display: str = "", invite=None) -> User:
    """Учётная запись по MAX-id: существующая → вход; иначе регистрация
    (роль/компания — из приглашения, если передано; без приглашения —
    личный аккаунт «пользователь», если саморегистрация разрешена)."""
    user = (db.query(User)
            .filter(User.max_user_id == str(max_id)[:32]).first())
    if user is not None and not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Учётная запись отключена")
    if user is None:
        from . import appsettings
        if invite is None and appsettings.get_setting(
                db, "max_web_register", "1") != "1":
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Регистрация через MAX выключена "
                                "администратором — вход только по логину")
        user = User(
            username=_unique_username(db, str(max_id), username),
            full_name=(display or username or "Сотрудник")[:200],
            organization=(invite.note or "") if invite is not None else "",
            password_hash=hash_password(secrets.token_urlsafe(24)),
            role=(invite.role if invite is not None else ROLE_USER),
            company_id=(invite.company_id if invite is not None else None),
            max_user_id=str(max_id)[:32],
        )
        if invite is not None:
            invite.used_count += 1
        db.add(user)
        db.commit()
        db.refresh(user)
    elif (invite is not None and not user.company_id
          and user.role in (ROLE_USER, "accountant", "manager")):
        # существующий «личный» аккаунт — присоединяем к компании приглашения
        user.role = invite.role
        user.company_id = invite.company_id
        invite.used_count += 1
        db.commit()
        db.refresh(user)
    user.last_login_at = dt.datetime.utcnow()
    db.commit()
    return user
