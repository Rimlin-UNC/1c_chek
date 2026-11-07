"""Интеграция 1С: токены компаний (T-57, v1.70.92).

Общий токен (настройка «onec_api_token» или ONEC_API_TOKEN) работает как раньше и
видит все компании — уже настроенные базы 1С не ломаются. Токен компании задаёт
область: выгрузка (pull, EnterpriseData), подтверждение (ack) и счётчик в ping —
только её чеки. Хранится только sha256 токена; сам токен показывается один раз при
выпуске. Схема базы не меняется (ADR-03) — две записи в app_settings:
«onec_token:<компания>» (когда выпущен и последние символы) и
«onec_token_hash:<sha256>» (по какому хешу какая компания).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppSetting, Company

META = "onec_token:"
INDEX = "onec_token_hash:"
GLOBAL = None          # область общего токена — все компании


class TokenError(Exception):
    """Токен не подошёл."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def status(db: Session, company_id: str) -> dict:
    """Есть ли у компании токен; без самого токена и его хеша."""
    row = db.get(AppSetting, META + company_id)
    if not row or not row.value:
        return {"has_token": False}
    meta = json.loads(row.value)
    return {"has_token": True, "created_at": meta.get("created_at"), "hint": meta.get("hint")}


def issue(db: Session, company_id: str) -> str:
    """Выпустить токен компании (прежний отзывается). Возвращается один раз."""
    revoke(db, company_id)
    token = "1c-" + secrets.token_urlsafe(32)
    digest = _digest(token)
    created = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    db.merge(AppSetting(key=INDEX + digest, value=company_id))
    db.merge(AppSetting(key=META + company_id, value=json.dumps(
        {"hash": digest, "created_at": created, "hint": token[-4:]}, ensure_ascii=False)))
    db.flush()
    return token


def revoke(db: Session, company_id: str) -> bool:
    """Отозвать токен компании. False — токена не было."""
    row = db.get(AppSetting, META + company_id)
    if not row or not row.value:
        return False
    index = db.get(AppSetting, INDEX + json.loads(row.value).get("hash", ""))
    if index is not None:
        db.delete(index)
    db.delete(row)
    db.flush()
    return True


def global_token(db: Session) -> str:
    """Общий токен — расшифрованный. Он в секретных настройках с 1.16.0 и лежит в базе
    как «enc:v1:…»; до 1.70.92 проверка сравнивала токен с этой строкой, и токен,
    заданный или перевыпущенный в «Настройках», 1С не принимала."""
    from .appsettings import get_setting
    return get_setting(db, "onec_api_token", settings.ONEC_API_TOKEN or "")


def resolve(db: Session, token: str | None) -> str | None:
    """Область токена: id компании или GLOBAL (общий токен). Не подошёл — TokenError."""
    if not token:
        raise TokenError
    index = db.get(AppSetting, INDEX + _digest(token))
    if index is not None and index.value and db.get(Company, index.value) is not None:
        return index.value
    expected = global_token(db)
    if expected and hmac.compare_digest(token, expected):
        return GLOBAL
    raise TokenError
