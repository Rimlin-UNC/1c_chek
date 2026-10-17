# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — аккаунты участников «Чек-Пула» (v1.32.0, Этап 3).
# Контур пула полностью отделён от корпоративных пользователей ядра:
# свой JWT (typ=pool), свои пароли (PBKDF2 ядра), своя таблица токенов.
# Гостевая история (cookie vid) при регистрации/входе ПРИСОЕДИНЯЕТСЯ
# к аккаунту — «сдал чеки без регистрации → зарегистрировался → всё на месте».
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password
from ..config import settings
from ..database import get_db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PASSWORD_MIN = 8
TOKEN_TTL_LOGIN = 30 * 24 * 3600      # пул-JWT: 30 дней
MAGIC_TTL_MIN = 15                    # magic link: 15 минут
VERIFY_TTL_H = 24                     # подтверждение e-mail: 24 часа


# --- JWT кабинета пула ------------------------------------------------------
def issue_pool_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"typ": "pool", "pu": user_id, "iat": now, "exp": now + TOKEN_TTL_LOGIN},
        settings.SECRET_KEY, algorithm="HS256")


def pool_user_from_token(db: Session, token: str):
    """Участник пула по JWT (или None). Токены ядра (typ≠pool) не подходят."""
    from .models import PoolUser
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "pool":
        return None
    user = db.get(PoolUser, str(payload.get("pu") or ""))
    return user if user is not None and not user.is_blocked else None


def require_pool_user(request: Request,
                      db: Session = Depends(get_db)):
    """FastAPI-зависимость: участник пула по Bearer-токену кабинета."""
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Войдите в кабинет Чек-Пула")
    user = pool_user_from_token(db, auth[7:].strip())
    if user is None:
        raise HTTPException(401, "Сессия истекла — войдите заново")
    return user


# --- cookie гостя (vid) — та же подпись, что в router_public -----------------
def sign_vid(vid: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), vid.encode(),
                    hashlib.sha256).hexdigest()[:16]


def vid_from_cookie(request: Request) -> str | None:
    """Подписанный vid гостя из cookie (без выдачи новой)."""
    raw = (request.cookies.get("pool_vid") or "").strip()
    if raw and "." in raw:
        vid, sig = raw.rsplit(".", 1)
        if hmac.compare_digest(sign_vid(vid), sig):
            return vid
    return None


# --- регистрация / вход ------------------------------------------------------
def validate_registration(email: str, password: str) -> str | None:
    """Строка ошибки или None (когда всё корректно)."""
    if not EMAIL_RE.match(email or "") or len(email) > 256:
        return "Укажите корректный e-mail"
    if not password or len(password) < PASSWORD_MIN:
        return f"Пароль — не короче {PASSWORD_MIN} символов"
    if len(password) > 128:
        return "Пароль слишком длинный"
    return None


def find_by_email(db: Session, email: str):
    from .models import PoolUser
    return (db.query(PoolUser)
            .filter(PoolUser.email == (email or "").strip().lower()).first())


def register_user(db: Session, email: str, password: str):
    """Создание аккаунта кабинета. Возвращает (user, ошибка-строка)."""
    from .models import PoolUser
    email = (email or "").strip().lower()
    err = validate_registration(email, password)
    if err:
        return None, err
    if find_by_email(db, email) is not None:
        return None, "Такой e-mail уже зарегистрирован"
    user = PoolUser(email=email, password_hash=hash_password(password),
                    email_verified=False)
    db.add(user)
    db.flush()
    return user, ""


def authenticate(db: Session, email: str, password: str):
    user = find_by_email(db, email)
    if user is None or not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return None if user.is_blocked else user


# --- слияние гостевой истории (cookie vid) с аккаунтом ------------------------
def merge_guest_into_account(db: Session, account, vid: str | None) -> int:
    """Перенос чеков/баллов гостя (по vid из cookie) в аккаунт.
    Возвращает число перенесённых чеков. Идемпотентно: без гостя — 0."""
    from .models import PoolPoint, PoolReceipt, PoolUser

    if not vid:
        return 0
    guest = db.query(PoolUser).filter(PoolUser.vid == vid).first()
    if guest is None or guest.id == account.id:
        return 0
    moved = (db.query(PoolReceipt)
             .filter(PoolReceipt.pool_user_id == guest.id)
             .update({PoolReceipt.pool_user_id: account.id},
                     synchronize_session=False))
    db.query(PoolPoint).filter(PoolPoint.user_id == guest.id).update(
        {PoolPoint.user_id: account.id}, synchronize_session=False)
    account.points = (account.points or 0) + (guest.points or 0)
    db.delete(guest)
    return moved


# --- одноразовые токены (magic link / подтверждение e-mail) -------------------
def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_token(db: Session, purpose: str, email: str = "",
               user_id: str | None = None, ttl_seconds: int = 0) -> str:
    from .models import PoolToken
    raw = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_seconds)
    db.add(PoolToken(token_hash=_hash_token(raw), purpose=purpose,
                     email=(email or "").lower(), user_id=user_id,
                     expires_at=exp))
    return raw


def consume_token(db: Session, raw: str, purpose: str):
    """(user/email, err). Токен одноразовый: отмечается использованным."""
    from .models import PoolToken, PoolUser
    if not raw:
        return None, "Ссылка недействительна"
    row = (db.query(PoolToken)
           .filter(PoolToken.token_hash == _hash_token(raw),
                   PoolToken.purpose == purpose).first())
    if row is None or row.used_at is not None:
        return None, "Ссылка уже использована или недействительна"
    if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        return None, "Срок действия ссылки истёк — запросите новую"
    row.used_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if purpose == "login":
        user = find_by_email(db, row.email) if row.email else None
        if user is None or user.is_blocked:
            return None, "Аккаунт не найден"
        return user, ""
    user = db.get(PoolUser, row.user_id) if row.user_id else None
    if user is None or user.is_blocked:
        return None, "Аккаунт не найден"
    return user, ""


def magic_link(db: Session, email: str) -> str:
    """Создаёт magic-токен и возвращает готовую ссылку (письмо шлёт роутер)."""
    from .mailer import base_url
    raw = _new_token(db, "login", email=email, ttl_seconds=MAGIC_TTL_MIN * 60)
    return base_url(db) + "/#/pool-magic/" + raw


def verify_link(db: Session, user) -> str:
    from .mailer import base_url
    raw = _new_token(db, "verify", email=user.email, user_id=user.id,
                     ttl_seconds=VERIFY_TTL_H * 3600)
    return base_url(db) + "/#/pool-verify/" + raw


# --- удаление аккаунта (152-ФЗ) ----------------------------------------------
def delete_account(db: Session, user) -> None:
    """ПДн удаляются: e-mail и пароль стираются, баллы сгорают (запись в
    журнале). Сданные чеки остаются в открытой базе обезличенными — это
    согласовано офертой (участник передал фискальные данные без ПДн)."""
    from . import ingest
    from .models import PoolReceipt
    if user.points:
        ingest.add_points(db, user, -user.points, "account_deleted")
    user.email = ""
    user.password_hash = ""
    user.email_verified = False
    db.query(PoolReceipt).filter(PoolReceipt.pool_user_id == user.id).update(
        {PoolReceipt.pool_user_id: None}, synchronize_session=False)
