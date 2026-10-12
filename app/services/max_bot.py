# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — ассистент MAX (v1.66.0, Фаза 1 концепции, п.6).
# MAX Bot API: https://platform-api2.max.ru (сервер Минцифры — без
# прокси). Токен — ТОЛЬКО в заголовке Authorization (query запрещён).
# Приём сообщений — long-poll GET /updates (marker), отправка —
# POST /messages?user_id=… (лимит 2 сообщения/сек на диалог — анти-флуд
# 0.4 с). Привязка как в Telegram: /start КОД из «Настроек».
# Напоминания «сдай чеки за сегодня» (18:00 по умолчанию) + утренний
# дайджест бухгалтеру/админу (08:00). Работает в ПАРЕ с Telegram или
# отдельно — выбор канала в настройках (telegram | max | both), общая
# настройка assistants_channel; воркеры независимы.
# Без геолокации (решение владельца, 152-ФЗ).
# ======================================================================
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

log = logging.getLogger("ymaster.max")

API_BASE = "https://platform-api2.max.ru"
BIND_TTL = 15 * 60                       # код привязки живёт 15 минут
POLL_TIMEOUT = 25                        # long-poll, сек (лимит API: 90)
ANTIFLOOD_SEC = 0.4                      # ≤2 сообщений/сек на диалог
SETT = "max_enabled"
SETT_TOKEN = "max_bot_token"             # секрет (appsettings.SECRET_KEYS)
SETT_CHANNEL = "assistants_channel"      # telegram | max | both (общий)
SETT_USERNAME = "max_bot_username"       # v1.70.1: фактический ник бота
SETT_TIME = "max_reminder_time"
SETT_TEXT = "max_reminder_text"
SETT_DIGEST = "digest_enabled"           # утренний дайджест (общий)
DEFAULT_REMIND_TIME = "18:00"
DEFAULT_REMIND_TEXT = ("👋 {name}, не забудьте сфотографировать чеки за сегодня — "
                       "откройте «Ямастер Чек» и сканируйте. Бухгалтер скажет спасибо!")

_username_cache: str = ""

_QR_HINT_RE = re.compile(r"fn=\d{8,}", re.IGNORECASE)


class MaxError(Exception):
    """Понятная ошибка для UI (без токена и внутренностей)."""


# --------------------------------------------------------------------------
#  Настройки
# --------------------------------------------------------------------------
def get_token(db: Session) -> str:
    from . import appsettings
    return appsettings.get_setting(db, SETT_TOKEN, "")


def get_channel(db: Session) -> str:
    """Канал ассистентов: telegram | max | both (общая настройка)."""
    from . import appsettings
    ch = (appsettings.get_setting(db, SETT_CHANNEL, "telegram") or "telegram").strip()
    return ch if ch in ("telegram", "max", "both") else "telegram"


def bot_settings(db: Session) -> dict:
    from . import appsettings
    if not _username_cache and not appsettings.get_setting(
            db, SETT_USERNAME, "") and get_token(db):
        try:
            refresh_username(db)             # v1.70.1: ник для диплинков
        except Exception:                    # noqa: BLE001
            pass
    tme = appsettings.get_setting(db, SETT_TIME, DEFAULT_REMIND_TIME)
    if not re.fullmatch(r"\d{2}:\d{2}", tme or ""):
        tme = DEFAULT_REMIND_TIME
    return {
        "enabled": appsettings.get_setting(db, SETT, "0") == "1",
        "channel": get_channel(db),
        "reminder_time": tme,
        "reminder_text": appsettings.get_setting(db, SETT_TEXT, DEFAULT_REMIND_TEXT),
        "digest_enabled": appsettings.get_setting(db, SETT_DIGEST, "1") == "1",
        "has_token": bool(get_token(db)),
        "bot_username": _username_cache,
    }


def worker_is_alive() -> bool:
    return _worker is not None and _worker.is_alive()


# --------------------------------------------------------------------------
#  HTTP-клиент
# --------------------------------------------------------------------------
def api(token: str, method: str, params: dict | None = None,
        payload: dict | None = None, timeout: int = 30) -> dict | list:
    """Вызов MAX Bot API. Токен — в заголовке Authorization (не в query!)."""
    url = f"{API_BASE}/{method.lstrip('/')}"
    headers = {"Authorization": token}
    try:
        if payload is not None:
            resp = httpx.post(url, params=params, json=payload,
                              headers=headers, timeout=timeout)
        else:
            resp = httpx.get(url, params=params, headers=headers,
                             timeout=timeout)
    except Exception as e:                              # noqa: BLE001
        raise MaxError(f"MAX недоступен: {e}") from e
    if resp.status_code == 401:
        raise MaxError("MAX: токен неверный или отозван")
    if resp.status_code == 429:
        raise MaxError("MAX: слишком много запросов — повторите позже")
    if resp.status_code >= 400:
        raise MaxError(f"MAX: ошибка {resp.status_code}")
    return resp.json()


def get_me(token: str) -> dict:
    return api(token, "me")


def send_message(token: str, user_id: int | str, text: str) -> bool:
    api(token, "messages", params={"user_id": int(user_id), "notify": True},
        payload={"text": text[:4000]})
    time.sleep(ANTIFLOOD_SEC)                # лимит API: 2 сообщения/сек
    return True


def send_rich(token: str, user_id: int | str, text: str,
              buttons: list) -> bool:
    """Сообщение с inline-кнопками-ссылками (v1.70.0). Каждая кнопка —
    {"type": "link", "text": ..., "url": ...} (документация MAX Bot API:
    attachments → inline_keyboard → buttons)."""
    api(token, "messages", params={"user_id": int(user_id), "notify": True},
        payload={"text": text[:4000],
                 "attachments": [{"type": "inline_keyboard",
                                  "payload": {"buttons": [buttons]}}]})
    time.sleep(ANTIFLOOD_SEC)
    return True


def refresh_username(db: Session) -> str:
    """v1.70.1: username бота из MAX API — и сразу в настройки: от него
    зависят ВСЕ диплинки (вход с сайта, приглашения, реферальные)."""
    global _username_cache
    token = get_token(db)
    if not token:
        return ""
    try:
        me = get_me(token)
        _username_cache = me.get("username") or ""
    except MaxError:
        _username_cache = ""
    u = (_username_cache or "").strip()
    if u:
        from . import appsettings
        if appsettings.get_setting(db, SETT_USERNAME, "") != u:
            appsettings.set_setting(db, SETT_USERNAME, u)
    return _username_cache


def bot_username(db: Session, refresh: bool = False) -> str:
    """Ник бота для диплинков: настройка (сохранённая) → кэш get_me →
    (по запросу) MAX API → нейтральный запасной вариант. Публичные
    обработчики зовут с refresh=False — без сетевых вызовов."""
    from . import appsettings
    u = (appsettings.get_setting(db, SETT_USERNAME, "") or "").strip()
    if u:
        return u
    u = (_username_cache or "").strip()
    if u:
        return u
    if refresh:
        return refresh_username(db) or "chek.ymaster.bot"
    return "chek.ymaster.bot"


# --------------------------------------------------------------------------
#  Привязка чата (как в Telegram): /start КОД
# --------------------------------------------------------------------------
_bind_codes: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def new_bind_code(db: Session, user_id: str) -> str:
    del db
    import secrets as _s
    code = str(100000 + _s.randbelow(900000))
    with _lock:
        now = time.time()
        for k in [k for k, (_u, exp) in _bind_codes.items() if exp < now]:
            _bind_codes.pop(k, None)
        _bind_codes[code] = (str(user_id), now + BIND_TTL)
    return code


def consume_bind_code(code: str) -> str | None:
    with _lock:
        item = _bind_codes.pop((code or "").strip(), None)
    if not item or item[1] < time.time():
        return None
    return item[0]


def handle_update(db: Session, upd: dict) -> None:
    """Сообщение /start КОД → привязка max_user_id к пользователю."""
    msg = ((upd.get("update") or {}).get("message")
           or upd.get("message") or upd)
    text = (msg.get("body") or {}).get("text") or ""
    sender = msg.get("sender") or {}
    user_id = str(sender.get("user_id") or "")
    if not user_id or not text:
        return
    token = get_token(db)
    m = re.fullmatch(r"\s*/start\s+(\d{6})\s*", text)
    if m:
        uid = consume_bind_code(m.group(1))
        if not uid:
            send_message(token, user_id,
                         "⏳ Код не найден или устарел — получите новый в "
                         "«Настройках» приложения.")
            return
        from ..models import User
        user = db.get(User, uid)
        if user is None:
            return
        user.max_user_id = user_id[:32]
        db.commit()
        send_message(token, user_id,
                     "✅ Готово! Теперь сюда будут приходить напоминания "
                     "о чеках и уведомления «Ямастер Чек».")
        return
    m_login = re.search(r"login_[A-Za-z0-9_-]{16,120}", text)
    if m_login:
        # v1.70.0: подтверждение входа на сайт кодом из формы входа
        from . import max_login
        ok = max_login.confirm(db, m_login.group(0), user_id,
                               username=str(sender.get("username") or ""),
                               display=str(sender.get("first_name") or ""))
        send_message(token, user_id,
                     "✅ Вход на сайт подтверждён — вернитесь в браузер, "
                     "вход выполнится автоматически." if ok else
                     "⚠️ Код входа не найден или устарел — на сайте нажмите "
                     "«Войти через MAX» ещё раз.")
        return
    if _QR_HINT_RE.search(text):
        _pool_process_async(user_id, sender.get("username"), text)
        return
    send_message(token, user_id,
                 "Ямастер Чек: пришлите строку QR-кода чека (t=…&s=…&fn=…), "
                 "а для уведомлений привяжите чат кодом из «Настроек».")


def _pool_process_async(user_id: str, from_username: str | None,
                        text: str) -> None:
    """Сдача чека в Чек-Пул через MAX: ingest в фоне, ответ — в MAX."""
    def _run() -> None:
        db = _db_session()
        try:
            from .telegram_bot import process_pool_message
            reply = process_pool_message(db, user_id, user_id,
                                         from_username, text)
            if reply:
                send_message(get_token(db), user_id, reply)
        except Exception as e:                          # noqa: BLE001
            log.warning("max pool ingest %s: %s", user_id, e)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True,
                     name="ymaster-max-pool").start()


# --------------------------------------------------------------------------
#  Дайджест (08:00, бухгалтерам и админам — в MAX)
# --------------------------------------------------------------------------
def digest_text(db: Session) -> str:
    """Дайджест: чеки сегодняшнего и вчерашнего дня по сотрудникам."""
    from ..models import Receipt
    from sqlalchemy import func as _f
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yest = day_start - timedelta(days=1)
    rows = (db.query(Receipt.assignee, _f.count(Receipt.id),
                     _f.coalesce(_f.sum(Receipt.total_sum), 0.0))
            .filter(Receipt.created_at >= yest, Receipt.assignee != "")
            .group_by(Receipt.assignee)
            .order_by(_f.sum(Receipt.total_sum).desc())
            .limit(12).all())
    lines = [f"📊 Дайджест «Ямастер Чек» за {yest.strftime('%d.%m')}–{now.strftime('%d.%m')}:"]  # noqa: E501
    if not rows:
        lines.append("Пока нет чеков за сегодня и вчера.")
    for name, cnt, total in rows:
        lines.append(f"• {name}: {int(cnt)} чек(ов) на {float(total):.2f} ₽")
    from . import webhooks as _wh
    _stuck = _wh.stuck_count(db)                 # v1.68.0: контроль вебхуков
    if _stuck:
        lines.append(f"🪝 Вебхуки: {_stuck} недоставлено (исчерпаны "
                     "повторы) — проверьте приёмник в «Настройках»")
    lines.append("Хорошего дня! ✅")
    return "\n".join(lines)


def _receivers(db: Session) -> list:
    """Получатели дайджеста: активные бухгалтеры и админы с MAX."""
    from ..models import User
    return (db.query(User)
            .filter(User.is_active.is_(True),
                    User.max_user_id.isnot(None),
                    User.max_user_id != "",
                    User.role.in_(("admin", "accountant"))).all())


def _send_digest(db: Session) -> None:
    token = get_token(db)
    if not token:
        return
    text = digest_text(db)
    sent = 0
    for u in _receivers(db):
        try:
            send_message(token, u.max_user_id, text)
            sent += 1
        except Exception as e:                          # noqa: BLE001
            log.warning("max digest %s: %s", u.username, e)
    if sent:
        from . import appsettings
        appsettings.set_setting(db, "max_last_digest",
                                datetime.now().strftime("%Y-%m-%d"))
        log.info("max digest sent: %s", sent)


def _digest_due(db: Session) -> bool:
    from . import appsettings
    if appsettings.get_setting(db, SETT_DIGEST, "1") != "1":
        return False
    now = datetime.now()
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if not (target <= now < target + timedelta(minutes=10)):
        return False
    return appsettings.get_setting(db, "max_last_digest", "") != \
        now.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
#  Напоминания (свой список — по max_user_id)
# --------------------------------------------------------------------------
def _users_to_remind(db: Session) -> list:
    """Привязанные к MAX активные сотрудники/бухгалтеры без чеков сегодня."""
    from ..models import Receipt, User
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0,
                                          microsecond=0)
    users = (db.query(User)
             .filter(User.is_active.is_(True),
                     User.max_user_id.isnot(None),
                     User.max_user_id != "",
                     User.role.in_(("user", "accountant"))).all())
    out = []
    for u in users:
        has = (db.query(Receipt.id)
               .filter(Receipt.created_by == u.id,
                       Receipt.created_at >= day_start).first())
        if not has:
            out.append(u)
    return out


def _reminders_due(db: Session) -> bool:
    from . import appsettings
    tme = appsettings.get_setting(db, SETT_TIME, DEFAULT_REMIND_TIME)
    if not re.fullmatch(r"\d{2}:\d{2}", tme or ""):
        tme = DEFAULT_REMIND_TIME
    now = datetime.now()
    hh, mm = (int(x) for x in tme.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (target <= now < target + timedelta(minutes=2)):
        return False
    return appsettings.get_setting(db, "max_last_reminder", "") != \
        now.strftime("%Y-%m-%d")


def _send_reminders(db: Session) -> None:
    from . import appsettings
    token = get_token(db)
    if not token:
        return
    text_tpl = appsettings.get_setting(db, SETT_TEXT, DEFAULT_REMIND_TEXT)
    sent = 0
    for u in _users_to_remind(db):
        try:
            name = (u.full_name or u.username or "").split()[0]
            send_message(token, u.max_user_id, text_tpl.format(name=name))
            sent += 1
        except Exception as e:                          # noqa: BLE001
            log.warning("max remind %s: %s", u.username, e)
    if sent:
        appsettings.set_setting(db, "max_last_reminder",
                                datetime.now().strftime("%Y-%m-%d"))
        log.info("max reminders sent: %s", sent)


# --------------------------------------------------------------------------
#  Воркер (long-poll), отдельный поток — как у Telegram
# --------------------------------------------------------------------------
_worker: threading.Thread | None = None
_stop = threading.Event()


def _db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _poll_once(marker: list) -> None:
    """v1.67.1 (аудит/стресс): сессия БД не держится во время long-poll."""
    db = _db_session()
    try:
        token = get_token(db)
    finally:
        db.close()
    if not token:
        _stop.wait(5)
        return
    try:
        result = api(token, "updates",
                     params={"marker": marker[0], "timeout": POLL_TIMEOUT,
                             "types": "message_created"},
                     timeout=POLL_TIMEOUT + 10)
    except MaxError as e:
        log.warning("max poll: %s", str(e)[:120])
        _stop.wait(30 if "401" in str(e) else 15)
        return
    marker[0] = result.get("marker") or marker[0]
    for upd in result.get("updates") or []:
        db = _db_session()
        try:
            handle_update(db, upd)
        except Exception as e:                          # noqa: BLE001
            log.warning("max handle: %s", e)
        finally:
            db.close()


def _should_run(db: Session) -> bool:
    """v1.70.1: воркер живёт по «Включён + токен» — независимо от канала
    уведомлений (канал решает лишь, куда слать дайджесты/напоминания).
    Иначе при канале «telegram» (по умолчанию) бот MAX молчал: не читал
    /start-коды, подтверждения входа и QR-строки."""
    from . import appsettings
    return (appsettings.get_setting(db, SETT, "0") == "1") and bool(get_token(db))


def _worker_loop() -> None:
    marker = [None]
    last_check = 0.0
    try:                          # v1.70.1: ник бота — для всех диплинков
        _db0 = _db_session()
        try:
            refresh_username(_db0)
        finally:
            _db0.close()
    except Exception:                                  # noqa: BLE001
        pass
    while not _stop.is_set():
        db = _db_session()
        try:
            # выключили бота или выбрали «только Telegram» — поток завершается
            if not _should_run(db):
                log.info("max worker: бот выключен — воркер остановлен")
                return
            db.close()                                   # v1.67.1: БД не держим
            _poll_once(marker)
            db = _db_session()
            if time.time() - last_check > 30:   # проверка не чаще 2х/мин
                last_check = time.time()
                if _digest_due(db):
                    _send_digest(db)
                if _reminders_due(db):
                    _send_reminders(db)
        except Exception:                               # noqa: BLE001
            log.exception("max worker loop")
            _stop.wait(10)
        finally:
            db.close()
        _stop.wait(0.2)


def start_worker() -> bool:
    """Запустить поток MAX-бота (если включён и токен задан)."""
    global _worker
    db = _db_session()
    try:
        ok = _should_run(db)
    finally:
        db.close()
    if not ok:
        return False
    if _worker is not None and _worker.is_alive():
        return True
    _stop.clear()
    _worker = threading.Thread(target=_worker_loop, daemon=True,
                               name="ymaster-max-bot")
    _worker.start()
    log.info("max worker started")
    return True


def stop_worker() -> None:
    _stop.set()


def restart_worker() -> bool:
    global _worker
    stop_worker()
    if _worker is not None:
        _worker.join(timeout=3)
    _worker = None
    return start_worker()
