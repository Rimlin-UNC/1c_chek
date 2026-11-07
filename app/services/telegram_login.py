# ======================================================================
# Ямастер Чек — вход/регистрация через Telegram на сайте (v1.70.69).
# Зеркало потока MAX (v1.70.0): сайт создаёт одноразовый код (10 минут,
# в БД — только хэш), человек подтверждает его сообщением боту в
# Telegram — по deep-link «/start tglogin_…» или просто кодом текстом.
# Подтверждённый код → обычная сессия сайта (create_access_token +
# HttpOnly-cookie), как при входе по паролю. Регистрация — если чат ещё
# не привязан к учётной записи; выключается настройкой
# telegram_web_register=0. Короткие 6-значные коды привязки
# уведомлений не затрагиваются: у кода входа свой префикс и длина.
# ======================================================================
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import threading
import time
from collections import deque

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..auth import ROLE_USER, create_access_token, hash_password
from ..models import TelegramLoginCode, User, utcnow

CODE_TTL_S = 10 * 60                     # жизнь кода на подтверждение
RATE_MAX = 10                            # кодов с одного IP в час
RATE_WINDOW_S = 3600

MAX_KEYS = 5000                          # предел словаря адресов (память)
_ip_times: dict[str, deque] = {}
_UNSAFE = re.compile(r"[^a-z0-9_]")
# Один вход за раз: два одновременных опроса одного кода не должны создать
# две учётные записи на один чат (вкладка + мини-приложение, двойной клик).
_login_lock = threading.Lock()


def _code_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").strip().encode()).hexdigest()


def create_code(db: Session, ip: str = "") -> dict:
    """Новый одноразовый код входа: {code, expires_in[, tg_link]}.

    Без токена бота поток мёртв (подтверждать некому) — честный 409.
    Ссылка t.me добавляется, только если имя бота уже известно
    (воркер получил его через getMe); иначе человек вводит код руками.
    """
    now = time.monotonic()
    if len(_ip_times) > MAX_KEYS:        # страховка памяти: чужие адреса
        for k in [k for k, v in _ip_times.items()
                  if not v or v[-1] <= now - RATE_WINDOW_S]:
            _ip_times.pop(k, None)
    dq = _ip_times.setdefault((ip or "?")[:64], deque())
    while dq and dq[0] <= now - RATE_WINDOW_S:
        dq.popleft()
    if len(dq) >= RATE_MAX:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Слишком много запросов кода — попробуйте позже")
    dq.append(now)
    from . import telegram_bot
    if not telegram_bot.get_token(db):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Telegram-бот ещё не настроен — укажите токен "
                            "в Настройки → Telegram")
    # 8 + 32 = 40 символов [A-Za-z0-9_-] — запас start-параметра
    # Telegram (до 64) и отличим от 6-значных кодов привязки.
    raw = "tglogin_" + secrets.token_urlsafe(24)
    db.add(TelegramLoginCode(code_hash=_code_hash(raw), purpose="web",
                             ip=(ip or "")[:64],
                             expires_at=utcnow()
                             + dt.timedelta(seconds=CODE_TTL_S)))
    db.commit()
    out: dict = {"code": raw, "expires_in": CODE_TTL_S}
    username = (telegram_bot.bot_settings(db).get("bot_username") or "").strip()
    if username:
        out["tg_link"] = f"https://t.me/{username}?start={raw}"
    return out


def _live_row(db: Session, raw: str) -> TelegramLoginCode | None:
    row = (db.query(TelegramLoginCode)
           .filter(TelegramLoginCode.code_hash == _code_hash(raw)).first())
    if row is None or row.used_at is not None:
        return None
    if row.expires_at and row.expires_at < utcnow():
        return None
    return row


def confirm(db: Session, raw: str, chat_id: str, username: str = "",
            display: str = "") -> bool:
    """Подтверждение кода telegram-личностью (сообщение боту)."""
    row = _live_row(db, raw)
    if row is None or row.tg_chat_id:
        return False
    row.tg_chat_id = str(chat_id)[:32]
    row.tg_username = (username or "")[:100]
    row.tg_name = (display or "")[:200]
    db.commit()
    return True


def poll(db: Session, raw: str) -> dict:
    """Статус кода: pending | expired | ok (ok — код погашен, сессия
    готова). Как в v1.70.5: код гасим ТОЛЬКО после успешного входа или
    регистрации — если регистрация не прошла (например, выключена), код
    можно использовать повторно после устранения причины."""
    row = _live_row(db, raw)
    if row is None:
        return {"status": "expired"}
    if not row.tg_chat_id:
        return {"status": "pending"}
    # v1.70.69: вход и гашение кода — под замком, с перечитыванием строки:
    # второй параллельный опрос увидит погашенный код, а не создаст
    # вторую учётную запись на тот же чат.
    with _login_lock:
        db.expire_all()
        row = _live_row(db, raw)
        if row is None:
            return {"status": "expired"}
        user = login_or_register(db, row.tg_chat_id, row.tg_username,
                                 row.tg_name or row.tg_username)
        row.used_at = utcnow()
        db.commit()
    return {"status": "ok", "access_token": create_access_token(user),
            "user": user.to_dict()}


def _unique_username(db: Session, chat_id: str, username: str) -> str:
    base = _UNSAFE.sub("", (username or "").lower())[:30]
    if len(base) < 3:
        tail = str(chat_id or "").strip()[-8:]
        base = f"tg{tail}" if tail else "tguser"
    cand = base
    while db.query(User).filter(User.username == cand).first() is not None:
        cand = f"{base}_{secrets.token_hex(2)}"
    return cand


def login_or_register(db: Session, chat_id: str, username: str = "",
                      display: str = "") -> User:
    """Учётная запись по telegram_chat_id: существующая → вход; иначе
    регистрация — личный аккаунт «пользователь», если саморегистрация
    разрешена настройкой telegram_web_register (по умолчанию «1»).
    Ключ тот же, что у привязки уведомлений: человек, уже привязавший
    чат коротким кодом, входит без создания второго аккаунта."""
    cid = str(chat_id)[:32]
    user = db.query(User).filter(User.telegram_chat_id == cid).first()
    if user is not None and not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Учётная запись отключена")
    if user is None:
        from . import appsettings
        if appsettings.get_setting(
                db, "telegram_web_register", "1") != "1":
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Регистрация через Telegram выключена "
                                "администратором — вход только по логину")
        user = User(
            username=_unique_username(db, cid, username),
            full_name=(display or username or "Сотрудник")[:200],
            organization="",
            password_hash=hash_password(secrets.token_urlsafe(24)),
            role=ROLE_USER,
            telegram_chat_id=cid,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    user.last_login_at = utcnow()
    db.commit()
    return user
