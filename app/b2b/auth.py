# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — авторизация контура B2B (v1.70.45).
#
# Третья цепочка доверия: токен ядра (typ=core) и токен Чек-Пула
# (typ=pool) в контур НЕ проходят, b2b-токен не открывает ни кабинет
# пула, ни внутренние разделы ядра. Проверка typ — первое действие.
#
# Пароли хешируются функцией ядра (PBKDF2), одноразовые ссылки хранятся
# хэшем SHA-256 с назначением и сроком — как в pool_tokens.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password
from ..config import settings
from ..database import get_db
from .models import (
    PURPOSE_RESET,
    PURPOSE_VERIFY,
    ST_BLOCKED,
    B2bCompany,
    B2bToken,
    _utcnow,
)

log = logging.getLogger("ymaster.b2b.auth")

TOKEN_TTL = 30 * 24 * 3600      # сессия кабинета: 30 дней
VERIFY_TTL_H = 24               # подтверждение корпоративной почты: 24 часа
RESET_TTL_MIN = 60              # ссылка входа: 1 час

# Ограничение попыток входа (защита от перебора паролей)
LOGIN_WINDOW = 900              # окно наблюдения: 15 минут
LOGIN_MAX = 6                   # неудач в окне до блокировки
LOGIN_WARN_AT = 3               # на какой неудаче писать событие в журнал
_ATTEMPTS: dict[str, list[float]] = {}

# Ограничение регистраций и повторных писем по IP
IP_WINDOW = 3600
IP_MAX = 5


def _reset_attempts() -> None:
    """Сброс счётчиков (используется тестами)."""
    _ATTEMPTS.clear()


def _hits(key: str) -> list[float]:
    now = time.time()
    seen = [t for t in _ATTEMPTS.get(key, []) if now - t < LOGIN_WINDOW]
    _ATTEMPTS[key] = seen
    return seen


def login_blocked(key: str) -> bool:
    """Заблокирован ли вход для ключа (почта или IP)."""
    return len(_hits(key)) >= LOGIN_MAX


def note_failure(key: str) -> int:
    """Отмечает неудачу и возвращает число неудач в окне наблюдения.

    Счётчик нужен не только ограничителю: на третьей неудаче подряд контур
    пишет событие в журнал компании, чтобы администратор видел попытку входа
    в чужой кабинет (перебор уже ограничен, поэтому журнал не забивается).
    """
    hits = _hits(key)
    hits.append(time.time())
    return len(hits)


def note_success(key: str) -> None:
    _ATTEMPTS.pop(key, None)


def ip_blocked(ip: str) -> bool:
    now = time.time()
    seen = [t for t in _ATTEMPTS.get("ip:" + ip, []) if now - t < IP_WINDOW]
    _ATTEMPTS["ip:" + ip] = seen
    return len(seen) >= IP_MAX


def note_ip(ip: str) -> None:
    key = "ip:" + ip
    now = time.time()
    seen = [t for t in _ATTEMPTS.get(key, []) if now - t < IP_WINDOW]
    seen.append(now)
    _ATTEMPTS[key] = seen


# --------------------------------------------------------------------------
# JWT кабинета компании
# --------------------------------------------------------------------------
def issue_b2b_token(company_id: str, epoch: int = 0) -> str:
    """Токен кабинета компании.

    `ep` — эпоха сессий из карточки компании: токен перестаёт действовать,
    как только эпоху увеличили (смена пароля, блокировка), не дожидаясь
    истечения срока.
    """
    now = int(time.time())
    return jwt.encode(
        {"typ": "b2b", "bc": company_id, "ep": int(epoch or 0),
         "iat": now, "exp": now + TOKEN_TTL},
        settings.SECRET_KEY, algorithm="HS256")


def company_from_token(db: Session, token: str) -> B2bCompany | None:
    """Компания по JWT контура. Токены ядра и пула (typ≠b2b) не подходят."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "b2b":
        return None
    company = db.get(B2bCompany, str(payload.get("bc") or ""))
    if company is None or company.status == ST_BLOCKED:
        return None
    # эпоха сессий: токен, выданный до смены пароля или блокировки, мёртв
    try:
        if int(payload.get("ep") or 0) != int(company.token_epoch or 0):
            return None
    except (TypeError, ValueError):
        return None
    return company


def bearer_token(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def require_company(request: Request, db: Session = Depends(get_db)) -> B2bCompany:
    """FastAPI-зависимость: компания по Bearer-токену кабинета B2B."""
    token = bearer_token(request)
    if not token:
        raise HTTPException(401, "Войдите в кабинет API для бизнеса")
    company = company_from_token(db, token)
    if company is None:
        raise HTTPException(401, "Сессия истекла — войдите заново")
    return company


def require_verified(company: B2bCompany = Depends(require_company)) -> B2bCompany:
    """Доступ к содержимому раздела: только подтверждённый статус компании."""
    from .models import ST_VERIFIED
    if company.status != ST_VERIFIED:
        raise HTTPException(403, "Раздел откроется после подтверждения статуса "
                                "компании: подтвердите корпоративную почту по "
                                "ссылке из письма")
    return company


# --------------------------------------------------------------------------
# Одноразовые токены (ссылки в письмах)
# --------------------------------------------------------------------------
def _hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def issue_one_time(db: Session, company: B2bCompany,
                   purpose: str = PURPOSE_VERIFY) -> str:
    """Новый одноразовый токен. В БД — только хэш, наружу — исходная строка."""
    raw = secrets.token_urlsafe(32)
    if purpose == PURPOSE_RESET:
        expires = _utcnow() + timedelta(minutes=RESET_TTL_MIN)
    else:
        expires = _utcnow() + timedelta(hours=VERIFY_TTL_H)
    # прежние токены того же назначения гасим: ссылка всегда одна
    for old in (db.query(B2bToken)
                .filter(B2bToken.company_id == company.id,
                        B2bToken.purpose == purpose,
                        B2bToken.used_at.is_(None)).all()):
        old.used_at = _utcnow()
    db.add(B2bToken(token_hash=_hash_token(raw), purpose=purpose,
                    email=company.email, company_id=company.id,
                    expires_at=expires, created_at=_utcnow()))
    db.commit()
    return raw


def consume_one_time(db: Session, raw: str, purpose: str) -> B2bCompany | None:
    """Проверяет и гасит одноразовый токен. Возвращает компанию или None."""
    token = (db.query(B2bToken)
             .filter(B2bToken.token_hash == _hash_token(raw or ""),
                     B2bToken.purpose == purpose).first())
    if token is None:
        return None
    if token.used_at is not None:
        return None
    now = _utcnow()
    if token.expires_at is not None and token.expires_at < now:
        return None
    token.used_at = now
    company = db.get(B2bCompany, token.company_id)
    db.commit()
    return company


def purge_expired(db: Session) -> int:
    """Чистка просроченных одноразовых токенов (возвращает число удалённых)."""
    rows = (db.query(B2bToken)
            .filter(B2bToken.expires_at < _utcnow()).all())
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


# --------------------------------------------------------------------------
# Пароли
# --------------------------------------------------------------------------
PASSWORD_MIN = 8


def password_error(password: str) -> str:
    """Текст ошибки пароля или пустая строка."""
    if not password or len(password) < PASSWORD_MIN:
        return f"Пароль — не короче {PASSWORD_MIN} символов"
    if len(password) > 128:
        return "Пароль слишком длинный"
    if password.isdigit():
        return "Пароль из одних цифр слишком прост"
    return ""


def hash_secret(password: str) -> str:
    return hash_password(password)


def check_secret(password: str, stored: str) -> bool:
    return bool(stored) and verify_password(password, stored)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def token_ttl_left(token: B2bToken) -> int:
    """Сколько секунд осталось у одноразового токена (для диагностики)."""
    if token.expires_at is None:
        return 0
    delta = token.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, int(delta.total_seconds()))
