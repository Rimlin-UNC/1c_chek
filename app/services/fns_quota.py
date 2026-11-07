# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — учёт суточного лимита запросов к Открытым API ФНС.
#
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru
#
# По условиям регистрации приложения в ФНС (Brand YmasterAPI, срок до
# 29.10.2027) приложению разрешено 1000 запросов за 24 часа. Считаются
# ВСЕ обращения: получение временного токена, отправка запроса на чек,
# каждый опрос результата. Поэтому лимит ведётся по факту сетевых
# запросов, а не по числу проверенных чеков.
#
# Правила (решение владельца):
#   * кэш результатов (30 дней) лимит НЕ тратит — повторная проверка
#     того же чека берётся из базы;
#   * при 80 % лимита — предупреждение администратору (баннер + журнал);
#   * при 100 % — чек остаётся «ожидает проверки» и проверяется
#     автоматически после сброса лимита.
# ======================================================================
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import settings

log = logging.getLogger("ymaster")

MSK = timezone(timedelta(hours=3))
DAILY_LIMIT = 1000
WARN_PERCENT = 80
_LOCAL_LOCK = None


def _lock():
    global _LOCAL_LOCK
    if _LOCAL_LOCK is None:
        import threading
        _LOCAL_LOCK = threading.Lock()
    return _LOCAL_LOCK


def msk_day(moment: datetime | None = None) -> str:
    """Текущие сутки по Москве в виде ГГГГ-ММ-ДД (сброс лимита у ФНС)."""
    now = moment or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(MSK).strftime("%Y-%m-%d")


def limit_value() -> int:
    """Лимит из настроек (по умолчанию 1000, как в регистрации ВП)."""
    return int(getattr(settings, "FNS_DAILY_LIMIT", DAILY_LIMIT)
               or DAILY_LIMIT)


def _now() -> datetime:
    """Текущее время без пояса (в базе проекта так и хранится)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _db():
    from ..database import SessionLocal
    return SessionLocal()


def used_today(db=None) -> int:
    """Сколько запросов к ФНС израсходовано за текущие сутки."""
    from ..models import FnsUsage
    own = db is None
    db = db or _db()
    try:
        row = db.execute(select(FnsUsage).where(
            FnsUsage.day == msk_day())).scalar_one_or_none()
        return int(row.count) if row else 0
    except Exception:                                    # noqa: BLE001
        log.debug("fns_quota: чтение счётчика не удалось", exc_info=True)
        return 0
    finally:
        if own:
            db.close()


def remaining(db=None) -> int:
    return max(0, limit_value() - used_today(db))


def spend(amount: int = 1, db=None) -> bool:
    """Учесть запрос к ФНС. → False, если лимит уже исчерпан.

    Вызывается ПЕРЕД каждым сетевым запросом: если возвращено False,
    обращение не выполняется, а чек остаётся в очереди до сброса лимита.
    """
    from ..models import FnsUsage
    own = db is None
    if not getattr(settings, "FNS_DAILY_LIMIT_ENABLED", True):
        return True
    with _lock():
        db = db or _db()
        try:
            day = msk_day()
            row = db.execute(select(FnsUsage).where(
                FnsUsage.day == day)).scalar_one_or_none()
            if row is None:
                row = FnsUsage(day=day, count=0,
                               updated_at=_now())
                db.add(row)
                db.flush()
            if int(row.count) + amount > limit_value():
                db.rollback()
                return False
            before = int(row.count)
            row.count = before + amount
            row.updated_at = _now()
            db.commit()
            _maybe_warn(before, int(row.count), db)
            return True
        except Exception:                                # noqa: BLE001
            log.warning("fns_quota: не удалось учесть запрос", exc_info=True)
            db.rollback()
            return True            # сбой учёта не должен блокировать проверку
        finally:
            if own:
                db.close()


def _maybe_warn(before: int, after: int, db) -> None:
    """Предупреждение администратору при достижении порога 80 %."""
    limit = limit_value()
    warn_at = limit * WARN_PERCENT // 100
    if before >= warn_at or after < warn_at:
        return
    text = (f"Израсходовано {after} из {limit} запросов к ФНС за сутки "
            f"({after * 100 // limit} %). После лимита чеки будут проверены "
            f"автоматически после его сброса.")
    # 1) баннер в интерфейсе администратора
    try:
        from .appsettings import set_setting
        set_setting(db, "fns_limit_warning", json_safe({
            "day": msk_day(), "used": after, "limit": limit,
            "text": text, "at": _now().isoformat()}))
        db.commit()
    except Exception:                                    # noqa: BLE001
        log.debug("fns_quota: баннер не сохранён", exc_info=True)
    # 2) запись в журнал действий
    try:
        from ..models import AuditLog
        db.add(AuditLog(username="system", action="fns_limit_warning",
                        entity_type="fns", details=json_safe({
                            "used": after, "limit": limit,
                            "day": msk_day()})))
        db.commit()
    except Exception:                                    # noqa: BLE001
        log.debug("fns_quota: запись в журнал не удалась", exc_info=True)
        db.rollback()
    # 3) сообщение администратору в Telegram (если привязан)
    try:
        from ..models import User
        from .telegram_bot import notify_user
        admins = db.execute(select(User).where(
            User.role == "admin",
            User.is_active.is_(True))).scalars().all()
        for admin in admins:
            if admin.telegram_chat_id:
                notify_user(admin.id, f"⚠️ Лимит запросов к ФНС: {text}")
    except Exception:                                    # noqa: BLE001
        log.debug("fns_quota: уведомление не отправлено", exc_info=True)
    log.warning("⚠️ ЛИМИТ ФНС: %s", text)


def json_safe(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def warning(db=None) -> dict:
    """Активное предупреждение о лимите (для баннера в интерфейсе)."""
    from .appsettings import get_setting
    own = db is None
    db = db or _db()
    try:
        raw = get_setting(db, "fns_limit_warning", "")
        if not raw:
            return {}
        data = json.loads(raw)
        if data.get("day") != msk_day():        # предупреждение прошлых суток
            return {}
        return data
    except Exception:                                    # noqa: BLE001
        return {}
    finally:
        if own:
            db.close()


def stats(db=None) -> dict:
    """Состояние лимита: использовано, остаток, проценты, порог."""
    used = used_today(db)
    limit = limit_value()
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "percent": min(100, used * 100 // limit) if limit else 0,
        "warn_percent": WARN_PERCENT,
        "exhausted": used >= limit,
        "day": msk_day(),
    }


def exhausted_message() -> str:
    """Текст для чека, ожидающего проверки из-за лимита."""
    st = stats()
    return (f"Достигнут суточный лимит запросов к ФНС "
            f"({st['used']} из {st['limit']}). Чек будет проверен "
            f"автоматически после сброса лимита.")


def reset_today(db=None) -> None:
    """Сбросить счётчик текущих суток (кнопка в админке)."""
    from ..models import FnsUsage
    own = db is None
    db = db or _db()
    try:
        row = db.execute(select(FnsUsage).where(
            FnsUsage.day == msk_day())).scalar_one_or_none()
        if row:
            row.count = 0
            row.updated_at = _now()
            db.commit()
    finally:
        if own:
            db.close()
