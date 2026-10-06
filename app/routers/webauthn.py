# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — быстрый вход (WebAuthn / passkey): v1.43.0.
# Отпечаток пальца, распознавание лица или PIN устройства вместо пароля.
# Биометрия НЕ покидает устройство (проверяется в защищённом чипе) —
# на сервере хранится только публичный ключ ключа-пароля (152-ФЗ).
# Каждый ключ привязан к устройству: одно устройство — один ключ,
# разных устройств может быть несколько (Настройки → «Быстрый вход»).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from webauthn import (generate_authentication_options,
                      generate_registration_options,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import options_to_json_dict
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

from ..auth import client_ip, create_access_token, get_current_user
from ..database import get_db
from ..models import User, WebauthnChallenge, WebauthnCredential
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/auth", tags=["Аутентификация"])

CHALLENGE_TTL = dt.timedelta(minutes=3)   # срок жизни одноразового challenge
MAX_KEYS_PER_USER = 10


# --------------------------------------------------------------------------
#  Привязка к домену: RP ID = хост браузера без порта, origin — схема+хост.
#  Работает везде одинаково: localhost, IP:8000 и chek.ymaster.ru (за
#  reverse-proxy схема берётся из X-Forwarded-Proto, как в client_ip).
# --------------------------------------------------------------------------
def _rp_id(request: Request) -> str:
    host = (request.headers.get("host") or request.url.netloc or "")
    return host.split(":")[0].strip().lower() or "localhost"


def _origin(request: Request) -> str:
    host = (request.headers.get("host") or request.url.netloc or "").strip()
    scheme = (request.headers.get("x-forwarded-proto")
              or request.url.scheme or "http").split(",")[0].strip()
    return f"{scheme}://{host}"


def _take_challenge(db: Session, raw_hex: str | None, purpose: str,
                    user_id: str | None) -> bytes:
    """Одноразовый challenge: нашёл → забрал (повторное использование — 400)."""
    if not raw_hex:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Церемония не начата — получите challenge")
    row = (db.query(WebauthnChallenge)
             .filter(WebauthnChallenge.challenge == raw_hex,
                     WebauthnChallenge.purpose == purpose)
             .order_by(WebauthnChallenge.created_at.desc())
             .first())
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Challenge не найден или уже использован")
    db.delete(row)
    db.commit()
    if row.expires_at and row.expires_at < dt.datetime.utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Церемония истекла — начните заново")
    if purpose == "register" and row.user_id and row.user_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Challenge выдан другому пользователю")
    return bytes.fromhex(raw_hex)


class CredentialBody(BaseModel):
    """Ответ аутентификатора (как пришёл из navigator.credentials)."""
    id: str = ""
    rawId: str = ""
    type: str = "public-key"
    response: dict = Field(default_factory=dict)


class LoginOptionsBody(BaseModel):
    username: str | None = None   # необязательно: без него — вход по любому
                                  # сохранённому ключу этого сайта


class RegisterVerifyBody(CredentialBody):
    label: str = Field(default="", max_length=100)


@router.post("/passkey/register/options",
             summary="Быстрый вход: начать привязку устройства (нужен вход)")
def register_options(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    existing = (db.query(WebauthnCredential)
                  .filter(WebauthnCredential.user_id == user.id).all())
    opts = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name="Ямастер Чек",
        user_name=user.username,
        user_id=user.id.encode(),
        user_display_name=(user.full_name or user.username),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c.id.encode())
                             for c in existing],
    )
    challenge = opts.challenge
    data = options_to_json_dict(opts)      # challenge уже внутри (b64url)
    db.add(WebauthnChallenge(
        challenge=challenge.hex(), purpose="register", user_id=user.id,
        expires_at=dt.datetime.utcnow() + CHALLENGE_TTL))
    db.commit()
    data["challenge_hex"] = challenge.hex()   # клиент вернёт как есть
    return data


@router.post("/passkey/register/verify",
             summary="Быстрый вход: проверить и сохранить ключ устройства")
def register_verify(body: RegisterVerifyBody, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if (db.query(WebauthnCredential)
          .filter(WebauthnCredential.user_id == user.id).count()
            >= MAX_KEYS_PER_USER):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Не больше 10 устройств на пользователя — "
                            "удалите неиспользуемые в Настройках")
    challenge = _take_challenge(db, body.response.get("challenge_hex"),
                                "register", user.id)
    try:
        v = verify_registration_response(
            credential={"id": body.id, "rawId": body.rawId, "type": body.type,
                        "response": body.response},
            expected_challenge=challenge,
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            require_user_verification=True,
        )
    except WebAuthnException as e:   # любая ошибка церемонии → 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Устройство не прошло проверку: {e}")
    import base64
    cred_id_b64 = base64.urlsafe_b64encode(v.credential_id).decode().rstrip("=")
    if db.get(WebauthnCredential, cred_id_b64) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ключ уже привязан")
    label = (body.label or "Это устройство").strip()[:100]
    db.add(WebauthnCredential(
        id=cred_id_b64, user_id=user.id, label=label,
        public_key=base64.urlsafe_b64encode(
            v.credential_public_key).decode().rstrip("="),
        sign_count=int(v.sign_count or 0)))
    db.commit()                       # аудит отдельной сессией — коммитим заранее
    log_action(user, "passkey_added", details={"label": label})
    return {"ok": True, "id": cred_id_b64, "label": label,
            "message": f"Устройство «{label}» привязано — вход без пароля включён"}


@router.post("/passkey/login/options",
             summary="Быстрый вход: challenge для входа без пароля (публично)")
def login_options(body: LoginOptionsBody, request: Request,
                  db: Session = Depends(get_db)):
    allow = None
    if (body.username or "").strip():
        u = (db.query(User)
               .filter(User.username == body.username.strip()).first())
        if u is not None:
            allow = [PublicKeyCredentialDescriptor(id=c.id.encode())
                     for c in db.query(WebauthnCredential)
                                .filter(WebauthnCredential.user_id == u.id)
                                .all()]
            if not allow:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "Для этого логина быстрый вход не настроен")
    opts = generate_authentication_options(
        rp_id=_rp_id(request),
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED)
    challenge = opts.challenge
    data = options_to_json_dict(opts)      # challenge уже внутри (b64url)
    db.add(WebauthnChallenge(challenge=challenge.hex(), purpose="auth",
                             expires_at=dt.datetime.utcnow() + CHALLENGE_TTL))
    db.commit()
    data["challenge_hex"] = challenge.hex()
    return data


@router.post("/passkey/login/verify",
             summary="Быстрый вход: вход по отпечатку/лицу/PIN без пароля")
def login_verify(body: CredentialBody, request: Request,
                 db: Session = Depends(get_db)):
    ip = client_ip(request)
    challenge = _take_challenge(db, body.response.get("challenge_hex"),
                                "auth", None)
    import base64
    try:
        raw_id = base64.urlsafe_b64decode(body.rawId + "=" * (-len(body.rawId) % 4))
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Некорректный ключ")
    cred = db.get(WebauthnCredential, body.rawId) or None
    if cred is None:
        # на случай паддинга: ищем по нормализованному base64url
        norm = base64.urlsafe_b64encode(raw_id).decode().rstrip("=")
        cred = db.get(WebauthnCredential, norm)
    if cred is None:
        log_action(None, "login_passkey_failed", details={
            "reason": "ключ не найден", "ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Устройство не привязано — войдите по паролю")
    user = db.get(User, cred.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Учётная запись отключена")
    try:
        v = verify_authentication_response(
            credential={"id": body.id, "rawId": body.rawId, "type": body.type,
                        "response": body.response},
            expected_challenge=challenge,
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            credential_public_key=base64.urlsafe_b64decode(
                cred.public_key + "=" * (-len(cred.public_key) % 4)),
            credential_current_sign_count=int(cred.sign_count or 0),
            require_user_verification=True,
        )
    except WebAuthnException as e:
        log_action(None, "login_passkey_failed", details={
            "reason": str(e)[:120], "ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            f"Проверка не пройдена ({str(e)[:80]}) — "
                            "попробуйте ещё раз")
    if (v.new_sign_count or 0) > (cred.sign_count or 0):
        cred.sign_count = int(v.new_sign_count)   # антреклей-детект (py_webauthn)
    cred.last_used_at = dt.datetime.utcnow()
    user.last_login_at = dt.datetime.utcnow()
    db.commit()                       # аудит отдельной сессией — коммитим заранее
    log_action(user, "login_passkey", details={"label": cred.label, "ip": ip})
    token = create_access_token(user)
    return {"access_token": token, "token_type": "bearer",
            "user": user.to_dict(),
            "message": f"Вход по ключу устройства «{cred.label}»"}


@router.get("/passkeys", summary="Мои ключи устройств (быстрый вход)")
def my_keys(db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    rows = (db.query(WebauthnCredential)
              .filter(WebauthnCredential.user_id == user.id)
              .order_by(WebauthnCredential.created_at.desc()).all())
    return {"items": [c.to_dict() for c in rows]}


@router.delete("/passkeys/{kid}",
               summary="Отвязать устройство (свой ключ)")
def remove_key(kid: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    c = db.get(WebauthnCredential, kid)
    if c is None or c.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ключ не найден")
    label = c.label
    db.delete(c)
    db.commit()                       # аудит отдельной сессией — коммитим заранее
    log_action(user, "passkey_removed", details={"label": label})
    return {"ok": True, "message": f"Устройство «{label}» отвязано"}
