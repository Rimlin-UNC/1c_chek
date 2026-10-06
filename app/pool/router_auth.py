# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — аутентификация кабинета «Чек-Пула» (v1.32.0, Этап 3).
# Регистрация e-mail+пароль, вход, magic link (если настроен SMTP),
# подтверждение e-mail. Контур пула: JWT typ=pool, пользователи ядра
# компании не затрагиваются. Защита: rate-limit по IP, единые сообщения
# об ошибках (без перечисления существующих e-mail в magic link).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ..database import get_db
from . import accounts, antifraud, ingest, mailer, referral

router = APIRouter(prefix="/api/v1/pool-auth", tags=["pool-auth"])

# IP-лимиты (в бою дублируются nginx): регистрация 5/час, вход 10/час,
# запрос magic link 5/час
_LIMITS = {"register": 5, "login": 10, "magic": 5}
_HITS: dict[str, deque] = {}


def _allow(kind: str, ip: str) -> bool:
    hits = _HITS.setdefault(f"{kind}:{ip}", deque())
    now = time.monotonic()
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= _LIMITS[kind]:
        return False
    hits.append(now)
    return True


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


class AuthBody(BaseModel):
    email: str
    password: str
    ref_code: str = ""                 # v1.35.0: код приглашения (необязателен)


class MagicBody(BaseModel):
    email: str


class TokenBody(BaseModel):
    token: str


def _user_payload(u) -> dict:
    return {
        "id": u.id, "email": u.email, "email_verified": bool(u.email_verified),
        "points": u.points or 0, "trust_level": u.trust_level or 0,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.post("/register", summary="Чек-Пул: регистрация участника (email+пароль)")
def register(body: AuthBody, request: Request, response: Response,
             db: Session = Depends(get_db)):
    if not ingest.pool_enabled(db):
        raise HTTPException(403, "Регистрация в Чек-Пуле сейчас закрыта")
    if not _allow("register", _ip(request)):
        raise HTTPException(429, "Слишком много попыток — попробуйте позже")
    user, err = accounts.register_user(db, body.email, body.password)
    if err:
        raise HTTPException(422, err)
    moved = accounts.merge_guest_into_account(
        db, user, accounts.vid_from_cookie(request))
    antifraud.after_auth(db, user, request, "register")   # v1.34.0: слои 1–2
    # v1.35.0: реферальная привязка (после журнала IP — для проверки подсети)
    linked = referral.attribute(db, user, body.ref_code or "", request)
    db.commit()
    if moved and mailer.smtp_configured(db):
        # приветственное письмо не критично: сбой не мешает регистрации
        mailer.send_mail(db, user.email, "Ямастер Чек-Пул: добро пожаловать",
                         "Здравствуйте!\n\nКабинет «Ямастер Чек-Пула» создан. "
                         f"Перенесено чеков из гостевого режима: {moved}.\n\n"
                         "ООО «Ямастер» · https://ymaster.ru")
    return {"token": accounts.issue_pool_token(user.id), "user": _user_payload(user),
            "merged_receipts": moved, "referred": linked}


@router.post("/login", summary="Чек-Пул: вход по e-mail и паролю")
def login(body: AuthBody, request: Request, response: Response,
          db: Session = Depends(get_db)):
    if not _allow("login", _ip(request)):
        raise HTTPException(429, "Слишком много попыток — попробуйте позже")
    user = accounts.authenticate(db, body.email, body.password)
    if user is None:
        raise HTTPException(401, "Неверный e-mail или пароль")
    moved = accounts.merge_guest_into_account(
        db, user, accounts.vid_from_cookie(request))
    antifraud.after_auth(db, user, request, "login")      # v1.34.0: слои 1–2
    db.commit()
    return {"token": accounts.issue_pool_token(user.id), "user": _user_payload(user),
            "merged_receipts": moved}


@router.post("/magic", summary="Чек-Пул: вход по одноразовой ссылке (email)")
def magic_request(body: MagicBody, request: Request,
                  db: Session = Depends(get_db)):
    if not _allow("magic", _ip(request)):
        raise HTTPException(429, "Слишком много попыток — попробуйте позже")
    if not mailer.smtp_configured(db):
        raise HTTPException(400, "Вход по ссылке недоступен — войдите паролем")
    email = (body.email or "").strip().lower()
    user = accounts.find_by_email(db, email)
    if user is not None and not user.is_blocked:
        link = accounts.magic_link(db, email)
        db.commit()
        mailer.send_magic_link(db, email, link)   # сбой почты не ломает ответ
    # анти-перечисление: ответ одинаковый, есть аккаунт или нет
    return {"ok": True,
            "message": "Если аккаунт существует, письмо со ссылкой отправлено"}


@router.post("/magic/consume", summary="Чек-Пул: вход по ссылке из письма")
def magic_consume(body: TokenBody, request: Request, response: Response,
                  db: Session = Depends(get_db)):
    user, err = accounts.consume_token(db, body.token, "login")
    if err:
        raise HTTPException(400, err)
    accounts.merge_guest_into_account(
        db, user, accounts.vid_from_cookie(request))
    antifraud.after_auth(db, user, request, "login")      # v1.34.0: слои 1–2
    db.commit()
    return {"token": accounts.issue_pool_token(user.id), "user": _user_payload(user)}


@router.post("/verify", summary="Чек-Пул: подтверждение e-mail по ссылке")
def verify_email(body: TokenBody, db: Session = Depends(get_db)):
    user, err = accounts.consume_token(db, body.token, "verify")
    if err:
        raise HTTPException(400, err)
    user.email_verified = True
    # v1.35.0: реферальный бонус за подтверждение (с задержкой ≥ 24 ч)
    referral.on_email_verified(db, user)
    db.commit()
    return {"ok": True, "email": user.email, "message": "E-mail подтверждён"}
