# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — исходящие webhooks (v1.67.0, Фаза 1 концепции, п.7).
# Событие «чек verified» → POST JSON на URL администратора: во внешнюю
# таблицу, 1С-шлюз или посредника — «интеграции без границ» без
# тяжёлых коннекторов на нашем железе.
# Подпись: HMAC-SHA256 от "{unix-time}.{тело}" секретом — заголовки
#   X-Ymaster-Signature: hex-подпись
#   X-Ymaster-Task: unix-time отправки (проверка свежести у приёмника)
# Повторы при ошибке/не-2xx: 60 → 300 → 900 с (MAX_ATTEMPTS попыток),
# успех — любой ответ 2xx. Очередь — таблица webhook_logs (attempt,
# next_attempt_at), воркер в фоне. Всё локальное: httpx + таблица,
# без брокеров (1 ГБ RAM). Ошибки вебхуков не ломают приём чеков.
# ======================================================================
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

log = logging.getLogger("ymaster.webhooks")

SETT_URL = "webhook_url"
SETT_SECRET = "webhook_secret"       # секрет (appsettings.SECRET_KEYS)
SETT_EVENTS = "webhook_events"
SETT_ENABLED = "webhook_enabled"
DEFAULT_EVENTS = ["receipt_verified"]
ALL_EVENTS = ["receipt_verified"]    # v1.67.0: первое событие, далее расширяем
MAX_ATTEMPTS = 4                     # 1 попытка + 3 повтора
RETRY_DELAYS = [60, 300, 900]        # сек: 1 мин → 5 мин → 15 мин
TICK_SEC = 15                        # период воркера, сек
BATCH_LIMIT = 10                     # записей за тик (нагрузка — копейки)
MAX_RECORDS = 50                     # очередь хранит не более 50 записей
SEND_TIMEOUT = 10                    # HTTP-таймаут попытки, сек


class WebhookError(Exception):
    """Понятная ошибка для UI (без внутренностей)."""


# --------------------------------------------------------------------------
#  Настройки
# --------------------------------------------------------------------------
def get_url(db: Session) -> str:
    from . import appsettings
    return (appsettings.get_setting(db, SETT_URL, "") or "").strip()


def get_secret(db: Session) -> str:
    from . import appsettings
    return appsettings.get_setting(db, SETT_SECRET, "") or ""


def get_events(db: Session) -> list:
    from . import appsettings
    raw = appsettings.get_setting(db, SETT_EVENTS, "") or ""
    events = [e.strip() for e in raw.split(",") if e.strip()]
    known = [e for e in events if e in ALL_EVENTS]
    return known or list(DEFAULT_EVENTS)


def is_enabled(db: Session) -> bool:
    from . import appsettings
    return (appsettings.get_setting(db, SETT_ENABLED, "0") == "1") \
        and bool(get_url(db))


def settings_info(db: Session) -> dict:
    """Данные для GET /settings/webhooks (секрет не возвращаем)."""
    from ..models import WebhookLog
    from . import appsettings
    sec = get_secret(db)
    return {
        "enabled": is_enabled(db),
        "enabled_flag": appsettings.get_setting(db, SETT_ENABLED, "0") == "1",
        "url": get_url(db),
        "events": get_events(db),
        "all_events": list(ALL_EVENTS),
        "has_secret": bool(sec),
        "secret_masked": ("•••" + sec[-4:]) if len(sec) >= 4 else "",
        "retries": list(RETRY_DELAYS),
        "stats": {
            "pending": db.query(WebhookLog).filter(
                WebhookLog.delivered_at.is_(None)).count(),
            "delivered": db.query(WebhookLog).filter(
                WebhookLog.delivered_at.isnot(None)).count(),
        },
    }


def pending_count(db: Session) -> int:
    """Сколько записей ещё не доставлено (включая ждущие повтора)."""
    from ..models import WebhookLog
    return db.query(WebhookLog).filter(
        WebhookLog.delivered_at.is_(None)).count()


def stuck_count(db: Session) -> int:
    """Недоставлено и попытки исчерпаны — нужен человек у приёмника."""
    from ..models import WebhookLog
    return db.query(WebhookLog).filter(
        WebhookLog.delivered_at.is_(None),
        WebhookLog.attempt >= MAX_ATTEMPTS).count()


_last_stuck_warn: list = [""]


def _warn_stuck_once_daily(db: Session) -> None:
    """v1.68.0: раз в сутки — предупреждение о застрявших вебхуках."""
    n = stuck_count(db)
    if not n:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_stuck_warn[0] == today:
        return
    _last_stuck_warn[0] = today
    log.warning("вебхуки: %d недоставлено (исчерпаны повторы) — проверьте "
                "приёмник %s или кнопку «Тестовый POST»", n, get_url(db))


def sign_payload(secret: str, body: str, ts: int) -> str:
    """HMAC-SHA256 от "{ts}.{body}" — приёмник считает так же."""
    mac = hmac.new(secret.encode("utf-8"),
                   f"{ts}.{body}".encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


# --------------------------------------------------------------------------
#  Очередь
# --------------------------------------------------------------------------
def enqueue_receipt_verified(db: Session, receipt_id: str,
                             max_records: int = MAX_RECORDS):
    """Поставить событие «чек verified» в очередь доставки.

    Вызывается сразу после коммита verified (проверка ФНС). Очередь
    не должна ломать приём чеков — ошибки глушим в лог."""
    try:
        return _enqueue(db, receipt_id, max_records)
    except Exception as e:                              # noqa: BLE001
        log.warning("webhook enqueue %s: %s", receipt_id, e)
        return None


def _enqueue(db: Session, receipt_id: str, max_records: int):
    from ..models import Receipt, WebhookLog
    if not get_url(db):
        return None
    # гигиена очереди: держим не более max_records (старые — долой)
    total = db.query(WebhookLog).count()
    if total >= max_records:
        old = (db.query(WebhookLog)
               .order_by(WebhookLog.created_at.asc(), WebhookLog.id.asc())
               .limit(total - max_records + 1).all())
        for o in old:
            db.delete(o)
        db.flush()
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        return None
    # дедуп: у чека уже есть живая (недоставленная) запись
    exists = (db.query(WebhookLog)
              .filter(WebhookLog.receipt_id == receipt_id,
                      WebhookLog.event_type == "receipt_verified",
                      WebhookLog.delivered_at.is_(None)).first())
    if exists:
        return exists
    payload = json.dumps({
        "event": "receipt_verified",
        "id": receipt.id,
        "fn": receipt.fn, "fd": receipt.fd, "fp": receipt.fp,
        "total_sum": receipt.total_sum,
        "operation": receipt.operation,
        "receipt_date": receipt.receipt_date.strftime("%Y-%m-%dT%H:%M:%S")
                        if receipt.receipt_date else None,
        "status": receipt.status,
        "company_id": receipt.company_id,
        "assignee": receipt.assignee or "",
        "category": receipt.category or "",
        "full_data": bool(receipt.full_data),
        "occurred_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, ensure_ascii=False)
    rec = WebhookLog(event_type="receipt_verified", receipt_id=receipt_id,
                     company_id=receipt.company_id or None,
                     url=get_url(db), payload=payload, attempt=0,
                     next_attempt_at=datetime.utcnow())
    db.add(rec)
    db.commit()
    _wake.set()                          # воркер: скорее к делу
    return rec


# --------------------------------------------------------------------------
#  Доставка и повторы
# --------------------------------------------------------------------------
def _http_post(url: str, body: str, secret: str, ts: int) -> int:
    headers = {
        "Content-Type": "application/json",
        "X-Ymaster-Task": str(ts),
        "X-Ymaster-Signature": sign_payload(secret, body, ts),
    }
    resp = httpx.post(url, content=body.encode("utf-8"),
                      headers=headers, timeout=SEND_TIMEOUT)
    return resp.status_code


def deliver_due(db: Session, now: datetime | None = None,
                limit: int = BATCH_LIMIT) -> tuple[int, int]:
    """Доставить готовые записи (попытки+повторы). → (успехов, последний код)"""
    from ..models import WebhookLog
    now = now or datetime.utcnow()
    url = get_url(db)
    if not url:
        return (0, 0)
    rows = (db.query(WebhookLog)
            .filter(WebhookLog.delivered_at.is_(None),
                    WebhookLog.attempt < MAX_ATTEMPTS,
                    WebhookLog.next_attempt_at.isnot(None),
                    WebhookLog.next_attempt_at <= now)
            .order_by(WebhookLog.created_at.asc(), WebhookLog.id.asc())
            .limit(limit).all())
    ok_cnt = 0
    last_code = 0
    for rec in rows:
        body = rec.payload or "{}"
        if rec.url != url:              # URL обновили — гонимся по свежему
            rec.url = url
        ts = int(time.time())
        err = ""
        try:
            code = _http_post(url, body, get_secret(db), ts)
        except Exception as e:                          # noqa: BLE001
            code = 0
            err = str(e)[:300]
        rec.attempt = (rec.attempt or 0) + 1
        rec.last_status = code if code else None
        if 200 <= code < 300:
            rec.delivered_at = datetime.utcnow()
            rec.next_attempt_at = None
            rec.last_error = ""
            ok_cnt += 1
            last_code = code
        else:
            idx = min(rec.attempt - 1, len(RETRY_DELAYS) - 1)
            rec.next_attempt_at = (now + timedelta(seconds=RETRY_DELAYS[idx])) \
                if rec.attempt < MAX_ATTEMPTS else None
            rec.last_error = err or f"HTTP {code}"
        db.commit()
    return (ok_cnt, last_code)


# --------------------------------------------------------------------------
#  Воркер (фон, без брокеров — как почтовый бот и ассистенты)
# --------------------------------------------------------------------------
_worker: threading.Thread | None = None
_wake = threading.Event()
_stop = threading.Event()


def worker_is_alive() -> bool:
    return _worker is not None and _worker.is_alive()


def _db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _worker_loop() -> None:
    while not _stop.is_set():
        _wake.wait(timeout=TICK_SEC)     # спим, пока не разбудят/не истечёт
        _wake.clear()
        if _stop.is_set():
            return
        db = _db_session()
        try:
            if not is_enabled(db):
                continue
            deliver_due(db)
            _warn_stuck_once_daily(db)           # v1.68.0
        except Exception:                               # noqa: BLE001
            log.exception("webhooks worker loop")
        finally:
            db.close()


def start_worker() -> bool:
    """Запустить воркер доставки (если включён и URL задан)."""
    global _worker
    db = _db_session()
    try:
        ok = is_enabled(db)
    finally:
        db.close()
    if not ok:
        return False
    if _worker is not None and _worker.is_alive():
        return True
    _stop.clear()
    _wake.clear()
    _worker = threading.Thread(target=_worker_loop, daemon=True,
                               name="ymaster-webhooks")
    _worker.start()
    log.info("webhooks worker started")
    return True


def stop_worker() -> None:
    _stop.set()
    _wake.set()


def restart_worker() -> bool:
    global _worker
    stop_worker()
    if _worker is not None:
        _worker.join(timeout=3)
    _worker = None
    return start_worker()


def send_test(db: Session) -> tuple[int, str]:
    """Тестовая отправка «event: test» (без записи в очередь). → (код, текст)"""
    url = get_url(db)
    if not url:
        raise WebhookError("Сначала задайте URL приёмника")
    body = json.dumps({
        "event": "test",
        "app": "Ямастер Чек",
        "occurred_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, ensure_ascii=False)
    ts = int(time.time())
    try:
        code = _http_post(url, body, get_secret(db), ts)
    except Exception as e:                              # noqa: BLE001
        raise WebhookError(f"Приёмник недоступен: {e}") from e
    if not (200 <= code < 300):
        raise WebhookError(f"Приёмник ответил кодом {code} (нужен 2xx)")
    return code, "Приёмник ответил 2xx — вебхук доставлен"
