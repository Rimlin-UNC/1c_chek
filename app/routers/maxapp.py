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

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..config import settings
from ..models import User
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
        raise HTTPException(401, "Подпись MAX не сошлась — откройте "
                                 "приложение из чата бота ещё раз")
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
    d["ref_link"] = (maxapp.ref_deep_link(user.tg_user_id, d["ref_code"])
                     if d["ref_code"] else "")
    d["app_version"] = settings.APP_VERSION
    return d


from ..pool.models import PoolUser as _PoolUserModel  # noqa: E402
