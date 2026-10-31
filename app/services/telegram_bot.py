# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — Telegram-бот (v1.16.0): уведомления и дисциплина
# подотчётников. Дизайн:
#   • входящие — long-polling getUpdates в фоновом потоке (не нужен
#     публичный webhook и белые IP: работает за NAT/прокси);
#   • привязка чата: пользователь получает одноразовый код в приложении
#     и отправляет боту «/start КОД» — chat_id сохраняется в профиле;
#   • токен бота хранится зашифрованным (appsettings.SECRET_KEYS →
#     secretbox), в логи не пишется, ошибки обезличены;
#   • напоминания: раз в день в заданное время сотрудникам, у которых
#     НЕСКОЛЬКО нет чеков за сегодня (собираемость чеков 60% → 90%+).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

log = logging.getLogger("ymaster.telegram")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
# v1.70.12: адрес Bot API можно заменить своим шлюзом/мостом (сервер в
# сети, где api.telegram.org недоступен; bridge: deploy/tg-bridge).
# Приоритет: настройка «Адрес API» (БД) -> env TELEGRAM_API_BASE -> официальный.
TG_API_BASE_DEFAULT = "https://api.telegram.org"
BIND_TTL = 15 * 60                       # код привязки живёт 15 минут
POLL_TIMEOUT = 25                        # long-poll, сек
SETT = "telegram_enabled"
SETT_TOKEN = "telegram_bot_token"
SETT_PROXY = "telegram_proxy"            # v1.29.0: socks5://… или http://…
PROXY_SCHEMES = ("socks5://", "socks5h://", "http://", "https://")
# v1.30.0: «Чек-Пул» — бот принимает строки QR от любого человека
_QR_HINT_RE = re.compile(r"fn=\d{8,}", re.IGNORECASE)
SETT_TIME = "telegram_reminder_time"     # "HH:MM"
SETT_API_BASE = "telegram_api_base"      # v1.70.12: сменный адрес Bot API (мост)
SETT_TEXT = "telegram_reminder_text"
SETT_DIGEST = "digest_enabled"           # v1.66.0: утренний дайджест (общий)
# v1.70.14: суточный пинг пути Telegram (AdGuard-автопилот)
SETT_PING_MS = "telegram_ping_ms"        # последний замер, целые мс
SETT_PING_AT = "telegram_ping_at"        # когда замеряли
SETT_PING_OK = "telegram_ping_ok"        # путь жив? 1/0
PING_TIME = (15, 0)                      # автозамер в 15:00 МСК, окно 20 мин
DIGEST_TIME = (8, 0)                     # v1.66.0: дайджест в 08:00
DEFAULT_REMIND_TIME = "18:00"
DEFAULT_REMIND_TEXT = ("👋 {name}, не забудьте сфотографировать чеки за сегодня — "
                       "откройте «Ямастер Чек» и сканируйте. Бухгалтер скажет спасибо!")


class TelegramError(Exception):
    """Понятная ошибка для UI (без токена и внутренностей)."""


# --------------------------------------------------------------------------
#  Настройки
# --------------------------------------------------------------------------
def get_token(db: Session) -> str:
    from . import appsettings
    return appsettings.get_setting(db, SETT_TOKEN, "")


def get_proxy(db: Session) -> str:
    """v1.29.0: прокси для Telegram (БД -> env TELEGRAM_PROXY).

    api.telegram.org из многих сетей РФ напрямую недоступен (блокировка
    на уровне TLS) - трафик бота идёт через прокси; остальные интеграции
    это не затрагивает."""
    from . import appsettings
    from ..config import settings
    return appsettings.get_setting(db, SETT_PROXY, "") or settings.TELEGRAM_PROXY


def get_api_base(db: Session) -> str:
    """v1.70.12: сменный адрес Bot API (настройка «Адрес API» или env).

    Урл без хвоста /bot…/method: https://api.telegram.org или адрес моста
    (deploy/tg-bridge). «-» в настройке возвращает официальный адрес."""
    from . import appsettings
    from ..config import settings
    val = (appsettings.get_setting(db, SETT_API_BASE, "") or "").strip()
    if val == "-":
        return TG_API_BASE_DEFAULT
    if val:
        return val
    env = (getattr(settings, "TELEGRAM_API_BASE", "") or "").strip()
    return env or TG_API_BASE_DEFAULT


def bot_settings(db: Session) -> dict:
    from . import appsettings
    token = get_token(db)
    enabled = appsettings.get_setting(db, SETT, "0") == "1"
    tme = appsettings.get_setting(db, SETT_TIME, DEFAULT_REMIND_TIME)
    if not re.fullmatch(r"\d{2}:\d{2}", tme or ""):
        tme = DEFAULT_REMIND_TIME
    return {
        "has_token": bool(token),
        "enabled": enabled,
        "reminder_time": tme,
        "reminder_text": appsettings.get_setting(db, SETT_TEXT, DEFAULT_REMIND_TEXT),
        "bot_username": (_bot_username or ""),
        "worker_running": _worker.is_alive() if _worker else False,
        "proxy_configured": bool(get_proxy(db)),           # v1.29.0
        "ping_ms": _ping_ms_or_none(db),                   # v1.70.14
        "ping_at": appsettings.get_setting(db, SETT_PING_AT, ""),
        "ping_ok": appsettings.get_setting(db, SETT_PING_OK, "") == "1",
    }


# --------------------------------------------------------------------------
#  Вызовы API (обезличенные ошибки)
# --------------------------------------------------------------------------
def api(token: str, method: str, payload: dict | None = None,
        timeout: float = 35.0, proxy: str = "",
        base: str = "") -> dict:
    if not token:
        raise TelegramError("Токен бота не задан — укажите его в настройках")
    origin = (base or "").rstrip("/") or API_BASE.rsplit("/bot", 1)[0]
    try:
        resp = httpx.post(origin + "/bot" + token + "/" + method,
                          json=payload or {}, timeout=timeout,
                          proxy=proxy or None)           # v1.29.0
    except Exception:                                    # noqa: BLE001
        raise TelegramError(
            "Telegram недоступен с сервера. Если сеть блокирует "
            "api.telegram.org — укажите прокси в Настройках -> Telegram "
            "(socks5://… или http://…)")
    try:
        data = resp.json()
    except Exception:                                    # noqa: BLE001
        raise TelegramError(f"Telegram: неожиданный ответ (HTTP {resp.status_code})")
    if not data.get("ok"):
        desc = str(data.get("description") or "")[:120]
        # токен в description не попадает, но на всякий случай вычищаем
        if token and token in desc:
            desc = desc.replace(token, "***")
        raise TelegramError(f"Telegram: {desc or f'HTTP {resp.status_code}'}")
    return data.get("result") or {}


def get_me(token: str, proxy: str = "", base: str = "") -> dict:
    me = api(token, "getMe", proxy=proxy, base=base)
    return {"username": (me.get("username") or ""), "name": (me.get("first_name") or "")}


def send_message(token: str, chat_id: str | int, text: str,
                 proxy: str = "", base: str = "") -> bool:
    api(token, "sendMessage", {"chat_id": chat_id, "text": text[:3500],
                               "parse_mode": "", "disable_web_page_preview": True},
        proxy=proxy, base=base)
    return True


# --------------------------------------------------------------------------
#  Коды привязки (в памяти процесса, TTL 15 минут)
# --------------------------------------------------------------------------
_bind_codes: dict[str, tuple[str, float]] = {}       # code → (user_id, expires)
_lock = threading.Lock()


def new_bind_code(db: Session, user_id: str) -> str:
    del db  # единообразие сигнатуры
    import secrets as _s
    code = str(100000 + _s.randbelow(900000))
    with _lock:
        # держим словарь компактным
        now = time.time()
        for k in [k for k, (_u, exp) in _bind_codes.items() if exp < now]:
            _bind_codes.pop(k, None)
        _bind_codes[code] = (user_id, now + BIND_TTL)
    return code


def consume_bind_code(code: str) -> str | None:
    """Одноразовость: вернули user_id и удалили код."""
    with _lock:
        item = _bind_codes.pop((code or "").strip(), None)
    if not item or item[1] < time.time():
        return None
    return item[0]


# --------------------------------------------------------------------------
#  Обработчики команд
# --------------------------------------------------------------------------
def _fmt_today_stats(db: Session, user) -> str:
    from ..models import Receipt
    from datetime import datetime as _dt
    day_start = _dt.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(Receipt).filter(Receipt.created_by == user.id,
                                 Receipt.created_at >= day_start)
    cnt = q.count()
    total = sum(r.total_sum or 0 for r in q.all())
    return (f"📊 Чеков за сегодня: {cnt}\n"
            f"💰 Сумма: {total:.2f} ₽\n"
            f"📈 В 1С выгружено: {sum(1 for r in q.all() if r.exported)}")


def handle_update(db: Session, upd: dict) -> None:
    """Одно входящее сообщение от Telegram."""
    msg = (upd or {}).get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    token = get_token(db)
    proxy = get_proxy(db)                                # v1.29.0
    _base = get_api_base(db)                             # v1.70.12

    def _send(txt: str) -> None:
        send_message(token, chat_id, txt, proxy=proxy, base=_base)

    from ..models import User
    text_low = text.lower()

    # --- v1.30.0: «Чек-Пул» — строка QR от любого человека ---
    if _QR_HINT_RE.search(text) or text_low.startswith("/qr"):
        frm = msg.get("from") or {}
        reply = process_pool_message(db, chat_id,
                                     str(frm.get("id") or "") or None,
                                     str(frm.get("username") or ""),
                                     text)
        if reply:
            _send(reply)
        return
    if msg.get("photo") and not text:
        # QR с фото на Этапе 1 не расшифровываем — честная подсказка
        from ..pool import ingest as _pool
        if _pool.pool_enabled(db):
            _send("📸 Фото пока не разбираю — пришлите QR строкой текстом: "
                  "в приложении ФНС откройте чек → «Поделиться» → скопируйте "
                  "строку и отправьте сюда.")
        return
    if text_low.startswith("/balance"):
        frm = msg.get("from") or {}
        bal = _pool_balance_text(db, str(frm.get("id") or "") or None, chat_id)
        if bal:
            _send(bal)
        return
    # --- v1.70.69: вход/регистрация на сайте — код «tglogin_…» ---
    # Deep-link https://t.me/БОТ?start=tglogin_… приходит текстом
    # «/start tglogin_…» — перехватываем раньше привязки, чтобы длинный
    # код входа не спутать с коротким 6-значным кодом привязки.
    m_tgl = re.search(r"tglogin_[A-Za-z0-9_-]{16,120}", text)
    if m_tgl:
        from . import telegram_login
        frm = msg.get("from") or {}
        display = " ".join(str(x) for x in (frm.get("first_name"),
                                            frm.get("last_name")) if x)
        ok = telegram_login.confirm(db, m_tgl.group(0), chat_id,
                                    username=str(frm.get("username") or ""),
                                    display=display)
        _send("✅ Вход на сайт подтверждён — вернитесь в браузер, вход "
              "выполнится автоматически." if ok else
              "⚠️ Код входа не найден или устарел — на сайте нажмите "
              "«Войти через Telegram» ещё раз.")
        return
    if text_low.startswith("/start"):
        code = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        uid = consume_bind_code(code)
        if not uid:
            from ..pool import ingest as _pool
            if not code and _pool.pool_enabled(db):
                _send(
                    "👋 Это бот «Ямастер Чек».\n\n"
                    "🎁 Пришлите строку QR чека (текстом) — проверим по "
                    "официальным источникам и начислим балл.\n"
                    f"📜 {ingest_offerta(db)}\n\n"
                    "Сотрудники компаний: код привязки — в Настройках "
                    "приложения (Настройки → Telegram → «Получить код»), "
                    "затем /start КОД.")
                return
            _send(
                "❌ Код не найден или устарел. Откройте «Ямастер Чек» → "
                "Настройки → Telegram и получите новый код.")
            return
        user = db.get(User, uid)
        if not user or not user.is_active:
            _send( "❌ Пользователь не найден или отключён.")
            return
        user.telegram_chat_id = chat_id
        db.commit()
        _send(
                     f"✅ Привязано: {user.full_name or user.username}. "
                     "Присылаю напоминания о чеках. Команды: /status, /help, /unbind")
        log.info("telegram bind: user=%s", user.username)
        return
    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    if text_low.startswith("/unbind"):
        if user:
            user.telegram_chat_id = None
            db.commit()
        _send( "🔌 Чат отвязан. Привяжите снова: "
                                     "Настройки → Telegram → код.")
        return
    if text_low.startswith("/status"):
        if user:
            _send( _fmt_today_stats(db, user))
        else:
            _send( "Сначала привяжите аккаунт: /start КОД "
                                         "(код — в Настройках приложения).")
        return
    # /help и всё остальное
    _send(
                 "🤖 Бот «Ямастер Чек».\nКоманды:\n"
                 "/status — мои чеки за сегодня\n"
                 "/balance — баллы «Чек-Пула»\n"
                 "Чек в пул: пришлите строку QR текстом.\n"
                 "/unbind — отвязать уведомления\n"
                 "Привязка: Настройки → Telegram → код → /start КОД")


# --------------------------------------------------------------------------
#  v1.30.0: «Чек-Пул» — приём чеков через бота
# --------------------------------------------------------------------------
def ingest_offerta(db: Session) -> str:
    """Краткая оферта пула для приветствия бота (v1.30.0).

    v1.70.47: сообщение читают в мессенджере, где относительный адрес
    страницы не откроется, поэтому в краткий текст подставляется полный
    адрес документа. Адрес берётся из настройки, из которой собираются
    ссылки в письмах (`pool.mailer.base_url`), — второй источник адреса
    сайта разъехался бы с первым при переезде домена.
    """
    from ..legal import offerta
    return offerta.short_terms_absolute(db)


def process_pool_message(db: Session, chat_id: str, from_id: str | None,
                         from_username: str, text: str) -> str | None:
    """Сообщение похоже на QR чека? Принять в пул и вернуть текст ответа.

    Синхронное ядро: handle_update вызывает его в фоновом потоке (бот
    отвечает мгновенно, ingest может идти сколько угодно долго)."""
    from ..pool import ingest as pool
    if not _QR_HINT_RE.search(text or ""):
        return None
    if not pool.pool_enabled(db):
        return "ℹ Приём чеков в «Чек-Пул» сейчас выключен. Попробуйте позже."
    user = pool.get_or_create_user(db, tg_user_id=from_id or chat_id,
                                   tg_username=from_username)
    # v1.70.37: канал регистрации участника — администратор видит, откуда он
    # пришёл (Telegram-бот, а не мини-приложение MAX).
    if not (user.reg_source or ""):
        user.reg_source = "tg"
        db.commit()
    if user.is_blocked:
        return "⛔ Приём чеков от этого аккаунта остановлен."
    res = pool.ingest_receipt(db, text.strip(), "telegram_bot", user)
    kind = res.get("result")
    icon = {"verified": "✅", "pending": "⏳", "rejected": "❌",
            "duplicate": "ℹ️", "flood": "🚦", "bad_qr": "🤔",
            "disabled": "ℹ"}.get(kind, "•")
    msg = f"{icon} {res.get('message', '')}"
    if kind == "verified":
        msg += (f"\n🎁 Баллов за чек: +{res.get('points', 0)} · "
                f"всего: {res.get('user_points', 0)}")
    if kind == "pending":
        msg += "\nЧек сохранён и ждёт проверки оператора — баллы начислим после подтверждения."
    if kind == "bad_qr":
        msg += ("\nПерешлите строку QR из приложения ФНС («Поделиться» → "
                "скопировать) — она вида t=…&s=…&fn=…&i=…&fp=…&n=…")
    return msg


def _pool_process_async(chat_id: str, from_id: str | None,
                        from_username: str, text: str) -> None:
    """ingest в фоне: ответ уходит в Telegram по готовности (не блокируя пуллинг)."""
    def _run() -> None:
        db = _db_session()
        try:
            _proxy = get_proxy(db)
            _base = get_api_base(db)                     # v1.70.12
            reply = process_pool_message(db, chat_id, from_id, from_username, text)
            if reply:
                send_message(get_token(db), chat_id, reply,
                             proxy=_proxy, base=_base)
        except Exception as e:                               # noqa: BLE001
            log.warning("pool ingest %s: %s", chat_id, e)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name="ymaster-pool-ingest").start()


def _pool_balance_text(db: Session, from_id: str | None, chat_id: str) -> str | None:
    """Баланс баллов пула (/balance)."""
    from ..pool import ingest as pool
    from ..pool.models import PoolUser
    user = None
    if from_id:
        user = db.query(PoolUser).filter(PoolUser.tg_user_id == str(from_id)).first()
    if user is None:
        return ("У вас пока нет чеков в «Чек-Пуле». Пришлите строку QR — "
                "за каждый проверенный чек начисляем балл.")
    return f"🎁 Ваш баланс: {user.points or 0} балл(ов). Пришлите следующий QR чека."


# --------------------------------------------------------------------------
#  Фоновый воркер: long-poll + ежедневные напоминания
# --------------------------------------------------------------------------
_worker: threading.Thread | None = None
_stop = threading.Event()
_bot_username: str = ""


def _db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _poll_once(offset: list[int]) -> None:
    """v1.67.1 (аудит/стресс): сессия БД больше НЕ держится во время
    25-секундного long-poll — короткие сессии на каждое действие."""
    db = _db_session()
    try:
        token = get_token(db)
        proxy = get_proxy(db)
        base = get_api_base(db)                          # v1.70.12
    finally:
        db.close()
    if not token:
        time.sleep(5)
        return
    try:
        result = api(token, "getUpdates", {
            "offset": offset[0], "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message"]}, timeout=POLL_TIMEOUT + 10,
            proxy=proxy,                                 # v1.29.0
            base=base)                                   # v1.70.12
    except TelegramError as e:
        msg = str(e)
        # v1.29.0: 409 = токен опрашивает другой процесс — ждём, апдейты
        # Telegram хранит и отдаст, когда конфликт исчезнет
        time.sleep(30 if ("409" in msg or "Conflict" in msg) else 15)
        log.warning("telegram poll: %s", msg[:120])
        return
    for upd in result or []:
        offset[0] = max(offset[0], (upd.get("update_id") or 0) + 1)
        db = _db_session()
        try:
            handle_update(db, upd)
        except Exception as e:                           # noqa: BLE001
            log.warning("telegram handle: %s", e)
        finally:
            db.close()


def _reminders_due(db: Session) -> bool:
    """Наступило ли время напоминаний сегодня (и ещё не отправляли)."""
    from . import appsettings
    tme = appsettings.get_setting(db, SETT_TIME, DEFAULT_REMIND_TIME)
    if not re.fullmatch(r"\d{2}:\d{2}", tme or ""):
        tme = DEFAULT_REMIND_TIME
    now = datetime.now()
    hh, mm = (int(x) for x in tme.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (target <= now < target + timedelta(minutes=2)):
        return False
    last = appsettings.get_setting(db, "telegram_last_reminder", "")
    return last != now.strftime("%Y-%m-%d")


def _users_to_remind(db: Session) -> list:
    """Привязанные активные сотрудники/бухгалтеры без чеков за сегодня."""
    from ..models import Receipt, User
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    users = (db.query(User)
             .filter(User.is_active.is_(True),
                     User.telegram_chat_id.isnot(None),
                     User.telegram_chat_id != "",
                     User.role.in_(("user", "accountant"))).all())
    out = []
    for u in users:
        has = (db.query(Receipt.id)
               .filter(Receipt.created_by == u.id,
                       Receipt.created_at >= day_start).first())
        if not has:
            out.append(u)
    return out


def _send_reminders(db: Session) -> None:
    from . import appsettings
    from ..models import User
    token = get_token(db)
    if not token:
        return
    text_tpl = appsettings.get_setting(db, SETT_TEXT, DEFAULT_REMIND_TEXT)
    proxy = get_proxy(db)                                # v1.29.0
    base = get_api_base(db)                              # v1.70.12
    sent = 0
    for u in _users_to_remind(db):
        try:
            name = (u.full_name or u.username or "").split()[0]
            send_message(token, u.telegram_chat_id,
                         text_tpl.format(name=name), proxy=proxy, base=base)
            sent += 1
            time.sleep(0.3)                              # анти-флуд
        except Exception as e:                           # noqa: BLE001
            log.warning("telegram remind %s: %s", u.username, e)
    if sent:
        appsettings.set_setting(db, "telegram_last_reminder",
                                datetime.now().strftime("%Y-%m-%d"))
        log.info("telegram reminders sent: %s", sent)


def _digest_due(db: Session) -> bool:
    """v1.66.0: утренний дайджест в 08:00 (один раз в сутки)."""
    from . import appsettings
    if appsettings.get_setting(db, SETT_DIGEST, "1") != "1":
        return False
    now = datetime.now()
    target = now.replace(hour=DIGEST_TIME[0], minute=DIGEST_TIME[1],
                         second=0, microsecond=0)
    if not (target <= now < target + timedelta(minutes=10)):
        return False
    return appsettings.get_setting(db, "tg_last_digest", "") != \
        now.strftime("%Y-%m-%d")


def _ping_due(db: Session, now: datetime | None = None) -> bool:
    """v1.70.14: суточный автозамер пинга — 15:00 МСК, окно 20 минут."""
    from . import appsettings
    now = now or datetime.now()
    target = now.replace(hour=PING_TIME[0], minute=PING_TIME[1],
                         second=0, microsecond=0)
    if not (target <= now < target + timedelta(minutes=20)):
        return False
    return appsettings.get_setting(db, "tg_last_ping", "") != \
        now.strftime("%Y-%m-%d")


def _ping_ms_or_none(db: Session) -> int | None:
    """Последний замер пинга (целые мс) или None."""
    from . import appsettings
    v = appsettings.get_setting(db, SETT_PING_MS, "")
    try:
        return int(v) if v else None
    except ValueError:
        return None


def ping_proxy(db: Session, samples: int = 3,
               scheduled: bool = False) -> dict:
    """v1.70.14: средний пинг настроенного пути до Telegram (3 запроса).

    Замеряем фактический путь бота: через прокси, если он задан в настройках,
    иначе напрямую. Любой HTTP-ответ считается успехом (важна латентность, не
    статус). Результат хранится в настройках и показывается в карточке
    Telegram. Экономно: 3 коротких запроса; scheduled=True — суточный
    автозамер 15:00 МСК.

    v1.70.24: при схеме AdGuard VPN «TUN + selective» поле прокси пусто, и
    «напрямую» — это НЕ обход VPN: трафик до api.telegram.org уводится в
    туннель прозрачно по доменному исключению, а DNS уходит в туннель. То
    есть замер при пустом прокси измеряет ровно тот путь, которым реально
    ходит бот. Прежняя схема SOCKS5 (прокси socks5://127.0.0.1:1080) тоже
    продолжает замеряться корректно — просто через прокси.
    """
    proxy = get_proxy(db)
    origin = get_api_base(db).rstrip("/")
    lat, fails = [], 0
    for i in range(samples):
        t0 = time.monotonic()
        try:
            httpx.get(origin + "/", timeout=8.0, proxy=proxy or None)
            lat.append((time.monotonic() - t0) * 1000.0)
        except Exception:                                # noqa: BLE001
            fails += 1
        if i < samples - 1:
            time.sleep(0.2)
    ok = bool(lat)
    ms = round(sum(lat) / len(lat)) if lat else None
    from . import appsettings
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    appsettings.set_setting(db, SETT_PING_MS, str(ms) if ms is not None else "")
    appsettings.set_setting(db, SETT_PING_AT, stamp)
    appsettings.set_setting(db, SETT_PING_OK, "1" if ok else "0")
    if scheduled:
        appsettings.set_setting(db, "tg_last_ping",
                                datetime.now().strftime("%Y-%m-%d"))
    if ok:
        log.info("telegram ping: %s мс (%d/%d ок, прокси: %s)",
                 ms, len(lat), samples, bool(proxy))
    else:
        log.warning("telegram ping: путь недоступен (%d/%d неудач, прокси: %s)",
                    fails, samples, bool(proxy))
    return {"ok": ok, "ms": ms, "ok_samples": len(lat), "samples": samples,
            "at": stamp, "proxy": bool(proxy)}


def _send_digest(db: Session) -> None:
    """v1.66.0: дайджест бухгалтерам/админам — в Telegram (кто привязан)."""
    from . import appsettings
    from . import max_bot as _mx
    from ..models import Receipt, User
    from sqlalchemy import func as _f
    if _mx.get_channel(db) == "max":      # канал «только MAX» — шлёт MAX-бот
        return
    token = get_token(db)
    if not token:
        return
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (db.query(Receipt.assignee, _f.count(Receipt.id),
                     _f.coalesce(_f.sum(Receipt.total_sum), 0.0))
            .filter(Receipt.created_at >= day_start, Receipt.assignee != "")
            .group_by(Receipt.assignee)
            .order_by(_f.sum(Receipt.total_sum).desc()).limit(12).all())
    lines = [f"📊 Дайджест «Ямастер Чек» на {now.strftime('%d.%m')}:"]  # noqa: E501
    if not rows:
        lines.append("Сегодня чеков пока нет.")
    for name, cnt, total in rows:
        lines.append(f"• {name}: {int(cnt)} чек(ов) на {float(total):.2f} ₽")
    from . import webhooks as _wh
    _stuck = _wh.stuck_count(db)                 # v1.68.0: контроль вебхуков
    if _stuck:
        lines.append(f"🪝 Вебхуки: {_stuck} недоставлено (исчерпаны "
                     "повторы) — проверьте приёмник в «Настройках»")
    text = "\n".join(lines)
    # v1.70.38: .all() и так возвращает список — копия в генераторе лишняя
    staff = db.query(User).filter(
        User.is_active.is_(True),
        User.telegram_chat_id.isnot(None),
        User.telegram_chat_id != "",
        User.role.in_(("admin", "accountant"))).all()
    proxy = get_proxy(db)
    base = get_api_base(db)                              # v1.70.12
    sent = 0
    for u in staff:
        try:
            send_message(token, u.telegram_chat_id, text, proxy=proxy, base=base)
            sent += 1
            time.sleep(0.3)                              # анти-флуд
        except Exception as e:                           # noqa: BLE001
            log.warning("digest %s: %s", u.username, e)
    if sent:
        appsettings.set_setting(db, "tg_last_digest",
                                now.strftime("%Y-%m-%d"))
        log.info("digest sent: %s", sent)


def _should_run(db: Session) -> bool:
    """v1.29.0: воркер нужен, если бот включён И токен задан.
    v1.66.0: канал «только MAX» — Telegram-воркер не нужен."""
    from . import appsettings
    from . import max_bot as _mx
    if _mx.get_channel(db) == "max":
        return False
    return (appsettings.get_setting(db, SETT, "0") == "1") and bool(get_token(db))


def _worker_loop() -> None:
    offset = [0]
    # при старте пропускаем накопившуюся очередь (старые сообщения не обрабатываем)
    db = _db_session()
    try:
        token = get_token(db)
        if token:
            try:
                result = api(token, "getUpdates", {"offset": -1, "timeout": 0})
                if result:
                    offset[0] = (result[-1].get("update_id") or 0) + 1
            except Exception:                            # noqa: BLE001
                pass
    finally:
        db.close()

    last_reminder_check = 0.0
    while not _stop.is_set():
        db = _db_session()
        try:
            # v1.29.0: выключили бота или удалили токен — поток завершается сам
            if not _should_run(db):
                log.info("telegram worker: бот выключен — воркер остановлен")
                return
            db.close()                                   # v1.67.1: БД не держим
            _poll_once(offset)                           # long-poll (до ~25 c)
            db = _db_session()
            if time.time() - last_reminder_check > 30:   # проверка не чаще 2х/мин
                last_reminder_check = time.time()
                if _reminders_due(db):
                    _send_reminders(db)
                if _digest_due(db):                      # v1.66.0: дайджест 08:00
                    _send_digest(db)
                if _ping_due(db):                    # v1.70.14: пинг 15:00 МСК
                    ping_proxy(db, scheduled=True)
        except Exception as e:                           # noqa: BLE001
            log.warning("telegram loop: %s", e)
            _stop.wait(10)
        finally:
            db.close()


def start_worker() -> bool:
    """Запустить воркер, если включён и есть токен. True — запущен."""
    global _worker
    db = _db_session()
    try:
        ok = _should_run(db)
    finally:
        db.close()
    if not ok:
        return False
    if _worker and _worker.is_alive():
        return True          # v1.29.0: уже работает — новый токен/прокси
                             # подхватятся в следующем цикле сами (без 409)
    _stop.clear()
    _worker = threading.Thread(target=_worker_loop, daemon=True,
                               name="ymaster-telegram")
    _worker.start()
    log.info("telegram worker started")
    return True


def stop_worker() -> None:
    global _worker
    _stop.set()
    if _worker and _worker.is_alive():
        _worker.join(timeout=2)
    _worker = None


def restart_worker() -> bool:
    """v1.29.0: привести воркер в соответствие настройкам (без перезапуска
    потока, если он уже работает, — нет гонки 409 Conflict)."""
    db = _db_session()
    try:
        should = _should_run(db)
    finally:
        db.close()
    if should:
        return start_worker()
    if _worker and _worker.is_alive():
        stop_worker()        # выключили — поток завершится в текущем цикле
    return False


def _probe(proxy: str, base: str = "") -> dict:
    """Одна проба пути до Telegram API (валидность токена не проверяем)."""
    origin = (base or "").rstrip("/") or TG_API_BASE_DEFAULT
    try:
        httpx.post(origin + "/bot0:x/getMe",
                   json={}, timeout=8.0, proxy=proxy or None)
        return {"ok": True, "error": ""}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": e.__class__.__name__}


def diagnose(db: Session) -> dict:
    """v1.29.0: почему бот молчит — путь до Telegram напрямую и через прокси."""
    direct = _probe("")
    proxy = get_proxy(db)
    via = _probe(proxy) if proxy else {"ok": False, "error": "прокси не задан"}
    api_base = get_api_base(db)                          # v1.70.12
    via_base = (_probe("", api_base)
                if api_base != TG_API_BASE_DEFAULT
                else {"ok": False, "error": "не задан"})
    if via_base["ok"]:
        v = ("ok", "Мост доступен — бот будет работать через адрес API "
             "из настроек. Нажмите «Сохранить и запустить».")
    elif via["ok"]:
        v = ("ok", "Через прокси Telegram доступен — бот будет работать. "
             "Нажмите «Сохранить и запустить».")
    elif direct["ok"]:
        v = ("ok", "Прямой доступ к Telegram есть — прокси не нужен.")
    else:
        v = ("bad", "Прямой доступ к api.telegram.org заблокирован (типично "
             "для РФ). Варианты: прокси (в т.ч. AdGuard VPN на этом же "
             "сервере — deploy/adguard-vpn), свой адрес API (мост — "
             "deploy/tg-bridge в комплекте) или канал MAX.")
    return {"direct": direct, "proxy": via, "proxy_configured": bool(proxy),
            "api_base": api_base,
            "via_base": via_base,                        # v1.70.12
            "verdict": v[0], "message": v[1]}


# --------------------------------------------------------------------------
#  Уведомления из бизнес-логики (fire-and-forget)
# --------------------------------------------------------------------------
_NOTIFY_SEM = threading.Semaphore(8)     # v1.68.1: потоков уведомлений ≤ 8


def notify_user(user_id: str, text: str) -> None:
    """Отправить сообщение пользователю, если привязан. Ошибки — в лог.

    v1.68.1 (аудит/стресс): поток+сессия на каждый скан выедали пул при
    всплесках. Теперь параллельных уведомлений ≤ 8; при занятой очереди
    уведомление пропускается (best-effort — напоминания и дайджесты
    останутся, этот канал не гарантия доставки)."""
    if not _NOTIFY_SEM.acquire(blocking=False):
        log.info("telegram notify %s: канал занят — уведомление пропущено",
                 user_id)
        return

    def _run() -> None:
        db = _db_session()
        try:
            from ..models import User
            u = db.get(User, user_id)
            token = get_token(db)
            if not u or not u.telegram_chat_id or not token:
                return
            send_message(token, u.telegram_chat_id, text,
                         proxy=get_proxy(db),
                         base=get_api_base(db))          # v1.29.0/v1.70.12
        except Exception as e:                           # noqa: BLE001
            log.info("telegram notify %s: %s", user_id, e)
        finally:
            db.close()
            _NOTIFY_SEM.release()
    threading.Thread(target=_run, daemon=True).start()


def refresh_username(db: Session) -> str:
    """Обновить кэш username бота (для настроек и ссылок t.me).

    v1.70.69 (исправление): запрос шёл на официальный адрес, игнорируя
    настройку «Адрес API» (v1.70.12). В условиях блокировки это значило,
    что имя бота не обновлялось и ссылка https://t.me/… в окне входа через
    Telegram не появлялась — вход оставался только «кодом текстом».
    """
    global _bot_username
    try:
        me = get_me(get_token(db), proxy=get_proxy(db),   # v1.29.0
                    base=get_api_base(db))                # v1.70.12/69
        _bot_username = me.get("username") or ""
    except Exception:                                    # noqa: BLE001
        _bot_username = ""
    return _bot_username
