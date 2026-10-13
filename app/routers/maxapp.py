# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — API мини-приложения MAX (v1.69.0).
# Контур Чек-Пула (PoolUser/PoolReceipt/баллы) — движок приёма не
# продублирован: precheck_ingest + фоновый ingest_parsed(source="max").
# Вход: initData (подпись MAX по доке) → PoolUser(tg_user_id = MAX-id)
# + пул-JWT (issue_pool_token, как в кабинете; 30 дней).
# Бонус: если логин сотрудника ядра совпал с username MAX — привязываем
# ассистента MAX (users.max_user_id): напоминания и дайджест сами.
# ======================================================================
from __future__ import annotations

import logging
import threading
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..config import settings
from ..auth import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_USER
from ..models import Company, Invite, User
from ..pool import ingest
from ..pool import referral as pool_referral
from ..pool.accounts import issue_pool_token, require_pool_user
from ..services import maxapp

log = logging.getLogger("ymaster.maxapp")

router = APIRouter(prefix="/api/v1/maxapp", tags=["Мини-приложение MAX"])


class SessionBody(BaseModel):
    init_data: str = Field(min_length=16, max_length=8192)


class ScanBody(BaseModel):
    qr: str = Field(min_length=10, max_length=4000)
    form_ms: int = Field(default=0, ge=0, le=3_600_000)


def _start_param(init_data: str) -> str:
    for chunk in init_data.split("&"):
        if chunk.startswith("start_param="):
            return unquote(chunk.split("=", 1)[1])[:512]
    return ""


def _bind_assistant(db: Session, max_username: str, max_id: str) -> None:
    """Если логин сотрудника ядра совпал с username MAX — привязать
    канал уведомлений (v1.69.0). Иначе — ничего (код из настроек)."""
    from ..services import max_bot as mx
    if not (max_username and mx.get_token(db)):
        return
    user = (db.query(User)
            .filter(User.username == max_username.strip().lower(),
                    User.is_active.is_(True)).first())
    if user is not None and not (user.max_user_id or "").strip():
        user.max_user_id = max_id[:32]
        db.commit()
        log.info("maxapp: ассистент привязан (%s)", user.username)


def _me_dict(db: Session, user) -> dict:
    code = (pool_referral.get_or_create_code(db, user)
            if not user.is_blocked else "")
    return {
        "max_id": user.tg_user_id or "",
        "name": user.tg_username or "",
        "points": int(user.points or 0),
        "blocked": bool(user.is_blocked),
        "trust_level": int(getattr(user, "trust_level", 0) or 0),
        "ref_code": code,
    }


def _last_receipts(db: Session, user, limit: int = 20) -> list:
    from ..pool.models import PoolReceipt
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.pool_user_id == user.id)
            .order_by(PoolReceipt.created_at.desc())
            .limit(limit).all())
    return [{"id": r.id, "fn": r.fn, "fd": r.fd, "fp": r.fp,
             "sum": float(r.total_sum or 0), "status": r.status,
             "created_at": r.created_at.strftime("%d.%m %H:%M")
             if r.created_at else ""} for r in rows]


@router.post("/session", summary="Вход/регистрация через MAX (initData)")
def session(body: SessionBody, db: Session = Depends(get_db)):
    user_raw, err = maxapp.validate_init_data(body.init_data,
                                              maxapp.bot_token(db))
    if err == "not_configured":
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Мини-приложение ещё не настроено")
    if err in ("format", "bad_hash"):
        from fastapi import HTTPException
        from ..services import max_bot as _mb
        tech = _mb.tech_username(db)
        detail = (f"Подпись MAX не совпала с сохранённым токеном бота "
                  f"«{tech}». Если вы открыли приложение из чата другого "
                  "вашего бота — вставьте в Настройках → Ассистент MAX "
                  "токен именно его и откройте приложение заново"
                  if tech else
                  "Подпись MAX не сошлась — проверьте токен бота "
                  "(Настройки → Ассистент MAX) и откройте приложение "
                  "из чата бота ещё раз")
        raise HTTPException(401, detail)
    if err == "expired":
        from fastapi import HTTPException
        raise HTTPException(401, "Сессия устарела — откройте приложение "
                                 "заново")
    assert user_raw is not None
    max_id = str(user_raw["id"])
    username = (user_raw.get("username") or "").strip()

    existing = (db.query(_PoolUserModel)
                .filter(_PoolUserModel.tg_user_id == max_id).first())
    is_new = existing is None
    user = ingest.get_or_create_user(
        db, tg_user_id=max_id, tg_username=username
        or (user_raw.get("first_name") or ""))
    if not user.is_blocked:
        ref = maxapp.parse_start_param(_start_param(body.init_data))
        if ref.get("ref"):
            pool_referral.attribute(db, user, ref["ref"], request=None)
    db.commit()

    _bind_assistant(db, username, max_id)
    return {"access_token": issue_pool_token(user.id),
            "token_type": "bearer", "is_new": is_new,
            "me": _me_dict(db, user)}


@router.get("/me", summary="Профиль мини-приложения (баллы, чеки)")
def me(db: Session = Depends(get_db), user=Depends(require_pool_user)):
    d = _me_dict(db, user)
    d["receipts"] = _last_receipts(db, user)
    return d


@router.post("/scan", summary="Сдача чека из мини-приложения (нативный QR)")
def scan(body: ScanBody, db: Session = Depends(get_db),
         user=Depends(require_pool_user)):
    parsed, pre_err = ingest.precheck_ingest(db, body.qr, user)
    if pre_err is not None:
        return {**pre_err, "accepted": False}
    fast = (body.form_ms or 0) < 1200        # антифрод: слишком быстрое окно
    res = {"result": "pending", "message": "Чек принят — проверяем",
           "accepted": True, "fn": parsed.fn, "fd": parsed.fd,
           "fp": parsed.fp, "points": 0,
           "user_points": int(user.points or 0)}
    user_id, qr_raw = user.id, body.qr

    def _bg():
        from ..database import SessionLocal
        s = SessionLocal()
        try:
            u = s.get(_PoolUserModel, user_id)
            if u is not None:
                ingest.ingest_parsed(s, qr_raw, parsed, "max", u, fast=fast)
                s.commit()
        except Exception as e:                           # noqa: BLE001
            log.warning("maxapp ingest %s: %s", user_id, e)
        finally:
            s.close()

    threading.Thread(target=_bg, daemon=True,
                     name="ymaster-maxapp-ingest").start()
    return res


@router.get("/summary", summary="Сводка: баллы, чеки, код приглашения")
def summary(db: Session = Depends(get_db), user=Depends(require_pool_user)):
    d = _me_dict(db, user)
    d["receipts"] = _last_receipts(db, user)
    d["ref_link"] = (maxapp.ref_deep_link(db, d["ref_code"])
                     if d["ref_code"] else "")
    d["app_version"] = settings.APP_VERSION
    return d


from ..pool.models import PoolUser as _PoolUserModel  # noqa: E402


# ==========================================================================
#  v1.70.0: вход/регистрация на сайте через MAX + приглашения
# ==========================================================================
class ConfirmLoginBody(BaseModel):
    code: str = Field(min_length=16, max_length=160)


class InviteTokenBody(BaseModel):
    token: str = Field(min_length=8, max_length=64)


@router.post("/confirm-login",
             summary="Подтвердить вход на сайт (код из формы входа)")
def confirm_login(body: ConfirmLoginBody, db: Session = Depends(get_db),
                  user=Depends(require_pool_user)):
    """Вызывается мини-приложением, когда открыто по диплинку
    startapp=login_<код> с сайта: пул-JWT уже доказывает личность MAX."""
    from ..services import max_login
    ok = max_login.confirm(db, body.code, str(user.tg_user_id or ""),
                           username=user.tg_username or "",
                           display=user.tg_username or "")
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Код не найден, устарел или уже подтверждён")
    return {"status": "confirmed"}


def _invite_or_none(db: Session, token: str):
    inv = db.query(Invite).filter(
        Invite.token == (token or "").strip()).first()
    return inv if (inv is not None and inv.is_valid) else None


@router.get("/invite",
            summary="Информация о приглашении (для мини-приложения)")
def invite_preview(token: str = Query(default="", min_length=8,
                                      max_length=64),
                   db: Session = Depends(get_db)):
    inv = _invite_or_none(db, token)
    if inv is None:
        return {"valid": False,
                "message": "Приглашение недействительно или уже использовано"}
    comp = db.query(Company).filter(Company.id == inv.company_id).first() \
        if inv.company_id else None
    return {"valid": True, "role": inv.role,
            "company_name": comp.name if comp else "",
            "expires_at": inv.expires_at.strftime("%d.%m.%Y %H:%M")
            if inv.expires_at else ""}


@router.post("/invite/accept",
             summary="Принять приглашение: вход/компания + билет на сайт")
def invite_accept(body: InviteTokenBody, db: Session = Depends(get_db),
                  user=Depends(require_pool_user)):
    """Создаёт учётную запись (или присоединяет «личную» к компании)
    по приглашению и выдаёт одноразовый веб-билет — мини-приложение
    открывает сайт уже залогиненным."""
    from ..services import max_login
    inv = _invite_or_none(db, body.token)
    if inv is None or inv.role not in (ROLE_ACCOUNTANT, ROLE_MANAGER,
                                       ROLE_USER):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Приглашение недействительно или уже использовано")
    max_id = str(user.tg_user_id or "")
    existing = db.query(User).filter(User.max_user_id == max_id).first()
    comp_name = ""
    if inv.company_id:
        _c = db.query(Company).filter(Company.id == inv.company_id).first()
        comp_name = _c.name if _c else ""
    if existing is not None and existing.company_id:
        if inv.company_id and existing.company_id == inv.company_id:
            code = max_login.make_webticket(db, max_id,
                                            user.tg_username or "",
                                            user.tg_username or "")
            return {"status": "ok", "action": "already",
                    "username": existing.username,
                    "company_name": comp_name,
                    "web_link": f"{max_login.SITE_URL}/#/max-login/{code}"}
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Эта учётная запись уже привязана к другой "
                            "компании — войдите на сайт под своим логином")
    u = max_login.login_or_register(db, max_id, user.tg_username or "",
                                    user.tg_username or "", invite=inv)
    code = max_login.make_webticket(db, max_id, user.tg_username or "",
                                    user.tg_username or "")
    return {"status": "ok",
            "action": "upgraded" if existing is not None else "registered",
            "username": u.username, "company_name": comp_name,
            "web_link": f"{max_login.SITE_URL}/#/max-login/{code}"}
