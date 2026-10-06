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
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
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
BIND_TTL = 15 * 60                       # код привязки живёт 15 минут
POLL_TIMEOUT = 25                        # long-poll, сек
SETT = "telegram_enabled"
SETT_TOKEN = "telegram_bot_token"
SETT_PROXY = "telegram_proxy"            # v1.29.0: socks5://… или http://…
PROXY_SCHEMES = ("socks5://", "socks5h://", "http://", "https://")
# v1.30.0: «Чек-Пул» — бот принимает строки QR от любого человека
_QR_HINT_RE = re.compile(r"fn=\d{8,}", re.IGNORECASE)
SETT_TIME = "telegram_reminder_time"     # "HH:MM"
SETT_TEXT = "telegram_reminder_text"
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
    }


# --------------------------------------------------------------------------
#  Вызовы API (обезличенные ошибки)
# --------------------------------------------------------------------------
def api(token: str, method: str, payload: dict | None = None,
        timeout: float = 35.0, proxy: str = "") -> dict:
    if not token:
        raise TelegramError("Токен бота не задан — укажите его в настройках")
    try:
        resp = httpx.post(API_BASE.format(token=token, method=method),
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


def get_me(token: str, proxy: str = "") -> dict:
    me = api(token, "getMe", proxy=proxy)
    return {"username": (me.get("username") or ""), "name": (me.get("first_name") or "")}


def send_message(token: str, chat_id: str | int, text: str, proxy: str = "") -> bool:
    api(token, "sendMessage", {"chat_id": chat_id, "text": text[:3500],
                               "parse_mode": "", "disable_web_page_preview": True},
        proxy=proxy)
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

    def _send(txt: str) -> None:
        send_message(token, chat_id, txt, proxy=proxy)

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
                    f"📜 {ingest_offerta()}\n\n"
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
def ingest_offerta() -> str:
    """Краткая оферта пула для приветствия бота (v1.30.0)."""
    from ..pool import ingest as pool
    return pool.OFFERTA_SHORT


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
            reply = process_pool_message(db, chat_id, from_id, from_username, text)
            if reply:
                send_message(get_token(db), chat_id, reply, proxy=_proxy)
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
_wake = threading.Event()
_bot_username: str = ""


def _db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _poll_once(db: Session, offset: list[int]) -> None:
    token = get_token(db)
    if not token:
        time.sleep(5)
        return
    try:
        result = api(token, "getUpdates", {
            "offset": offset[0], "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message"]}, timeout=POLL_TIMEOUT + 10,
            proxy=get_proxy(db))                         # v1.29.0
    except TelegramError as e:
        msg = str(e)
        # v1.29.0: 409 = токен опрашивает другой процесс — ждём, апдейты
        # Telegram хранит и отдаст, когда конфликт исчезнет
        time.sleep(30 if ("409" in msg or "Conflict" in msg) else 15)
        log.warning("telegram poll: %s", msg[:120])
        return
    for upd in result or []:
        offset[0] = max(offset[0], (upd.get("update_id") or 0) + 1)
        try:
            handle_update(db, upd)
        except Exception as e:                           # noqa: BLE001
            log.warning("telegram handle: %s", e)


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
    sent = 0
    for u in _users_to_remind(db):
        try:
            name = (u.full_name or u.username or "").split()[0]
            send_message(token, u.telegram_chat_id,
                         text_tpl.format(name=name), proxy=proxy)
            sent += 1
            time.sleep(0.3)                              # анти-флуд
        except Exception as e:                           # noqa: BLE001
            log.warning("telegram remind %s: %s", u.username, e)
    if sent:
        appsettings.set_setting(db, "telegram_last_reminder",
                                datetime.now().strftime("%Y-%m-%d"))
        log.info("telegram reminders sent: %s", sent)


def _should_run(db: Session) -> bool:
    """v1.29.0: воркер нужен, если бот включён И токен задан."""
    from . import appsettings
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
            _poll_once(db, offset)                       # long-poll (до ~25 c)
            if time.time() - last_reminder_check > 30:   # проверка не чаще 2х/мин
                last_reminder_check = time.time()
                if _reminders_due(db):
                    _send_reminders(db)
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


def _probe(proxy: str) -> dict:
    """Одна проба пути до Telegram API (валидность токена не проверяем)."""
    try:
        httpx.post(API_BASE.format(token="0:x", method="getMe"),
                   json={}, timeout=8.0, proxy=proxy or None)
        return {"ok": True, "error": ""}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": e.__class__.__name__}


def diagnose(db: Session) -> dict:
    """v1.29.0: почему бот молчит — путь до Telegram напрямую и через прокси."""
    direct = _probe("")
    proxy = get_proxy(db)
    via = _probe(proxy) if proxy else {"ok": False, "error": "прокси не задан"}
    if via["ok"]:
        v = ("ok", "Через прокси Telegram доступен — бот будет работать. "
             "Нажмите «Сохранить и запустить».")
    elif direct["ok"]:
        v = ("ok", "Прямой доступ к Telegram есть — прокси не нужен.")
    elif proxy:
        v = ("bad", "И прямой доступ, и прокси не работают — проверьте "
             "адрес/логин/пароль прокси.")
    else:
        v = ("bad", "С сервера нет доступа к api.telegram.org (частая ситуация "
             "в РФ). Укажите прокси в поле выше и повторите.")
    return {"direct": direct, "proxy": via, "proxy_configured": bool(proxy),
            "verdict": v[0], "message": v[1]}


def wake() -> None:
    """Разбудить цикл после изменений настроек."""
    _wake.set()


# --------------------------------------------------------------------------
#  Уведомления из бизнес-логики (fire-and-forget)
# --------------------------------------------------------------------------
def notify_user(user_id: str, text: str) -> None:
    """Отправить сообщение пользователю, если привязан. Ошибки — в лог."""
    def _run() -> None:
        db = _db_session()
        try:
            from ..models import User
            u = db.get(User, user_id)
            token = get_token(db)
            if not u or not u.telegram_chat_id or not token:
                return
            send_message(token, u.telegram_chat_id, text,
                         proxy=get_proxy(db))            # v1.29.0
        except Exception as e:                           # noqa: BLE001
            log.info("telegram notify %s: %s", user_id, e)
        finally:
            db.close()
    threading.Thread(target=_run, daemon=True).start()


def refresh_username(db: Session) -> str:
    """Обновить кэш username бота (для настроек)."""
    global _bot_username
    try:
        me = get_me(get_token(db), proxy=get_proxy(db))  # v1.29.0
        _bot_username = me.get("username") or ""
    except Exception:                                    # noqa: BLE001
        _bot_username = ""
    return _bot_username
