# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — пайплайн приёма чека в пул (v1.30.0 Этап 1, v1.31.0 веб).
# QR → парсер ядра → дедуп (БД) → лимиты → движок источников → статус.
#
# Канал-агностично: ingest_receipt() — синхронный путь (бот, тесты);
# precheck_ingest() + ingest_parsed() — быстрый ответ + фоновая проверка
# (публичная форма сайта, v1.31.0: «чек принят — проверяем» ≤ 5 с).
#
# Статусы: verified (в пуле, +1 балл) · pending (ручная проверка:
# аномальная сумма или нет данных) · rejected (источник не нашёл чек).
# Дубликат не сохраняется (UNIQUE fn+fd+fp) и не приносит баллов —
# на Этапе 1 строже плана («половинные баллы» — этап 6), зато без
# серых схем «сдал чужой чек дважды».
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..services.qr import QRParseError, parse_qr

SETT_ENABLED = "pool_enabled"
DAILY_LIMIT = 50                # чеков в сутки на пользователя (антифрод, слой 5)
ANOMALY_SUM = 500_000.0         # сумма-аномалия → ручная проверка (план, разд. 4)
POINTS_PER_RECEIPT = 1          # 1 чек = 1 балл (план, разд. 2)
OFFERTA_VERSION = "2026-10"     # v1.31.0: редакция оферты для журнала согласий

OFFERTA_SHORT = (
    "Отправляя чек, вы подтверждаете, что он ваш, и передаёте его фискальные "
    "данные (ФН, ФД, ФП, ИНН продавца, сумма, состав) в открытую базу "
    "«Ямастер Чек-Пул». Персональные данные третьих лиц в базу не попадают. "
    "Для защиты от накрутки мы храним обезличенные технические признаки "
    "устройства и сети — они не позволяют установить личность и удаляются "
    "не позже чем через год."
)


def pool_enabled(db: Session) -> bool:
    from ..services import appsettings
    return appsettings.get_setting(db, SETT_ENABLED, "0") == "1"


def get_or_create_user(db: Session, tg_user_id: str | None = None,
                       tg_username: str = "") -> "object":
    """Анонимный пользователь пула по Telegram-ID (ленивое создание)."""
    from .models import PoolUser
    user = None
    if tg_user_id:
        user = db.query(PoolUser).filter(
            PoolUser.tg_user_id == str(tg_user_id)).first()
    if user is None:
        user = PoolUser(tg_user_id=str(tg_user_id) if tg_user_id else None,
                        tg_username=(tg_username or "")[:64])
        db.add(user)
        db.flush()
    elif tg_username and user.tg_username != tg_username[:64]:
        user.tg_username = tg_username[:64]
    return user


def add_points(db: Session, user, delta: int, reason: str, ref_id: str = "") -> None:
    """Начисление баллов с записью в журнал (points_ledger)."""
    from .models import PoolPoint
    if delta == 0:
        return
    user.points = (user.points or 0) + delta
    db.add(PoolPoint(user_id=user.id, delta=delta, reason=reason, ref_id=ref_id))


def _fetch_details(db: Session, qr_raw: str, fn: str, fd: str, fp: str,
                   total_rub: float, date_time):
    """Данные чека из движка источников (переиспользование v1.26–1.27).

    Отдельная функция — легко подменить в тестах."""
    from ..services.external import engine
    return engine.fetch(db, qr_raw, fn, fd, fp, total_rub, date_time)


def precheck_ingest(db: Session, qr_raw: str, user) -> tuple:
    """Быстрые проверки ДО обращения к источникам (v1.31.0): включённость,
    разбор QR, дедуп, суточный лимит. Возвращает (parsed, None) либо
    (None, result_dict) — машиночитаемый отказ. Не пишет в БД."""
    from .models import PoolReceipt
    from ..models import utcnow

    if not pool_enabled(db):
        return None, {"result": "disabled", "message": "Пул выключен",
                      "points": 0, "user_points": user.points if user else 0}
    # 1) разбор QR (парсер ядра: key=value и URL-форматы)
    try:
        parsed = parse_qr(qr_raw)
    except QRParseError as e:
        return None, {"result": "bad_qr", "message": f"Не похоже на чек: {e}",
                      "points": 0, "user_points": user.points if user else 0}
    # 2) дедупликация: UNIQUE (fn, fd, fp) — на уровне БД + быстрая проверка
    dup = (db.query(PoolReceipt)
           .filter(PoolReceipt.fn == parsed.fn, PoolReceipt.fd == parsed.fd,
                   PoolReceipt.fp == parsed.fp).first())
    if dup is not None:
        return None, {"result": "duplicate",
                      "message": "Этот чек уже в пуле — спасибо, что проверили!",
                      "points": 0, "user_points": user.points if user else 0}
    # 3) лимит на пользователя в сутки (антифрод, минимум; user может
    #    отсутствовать в претесте «а парсится ли строка вообще»)
    if user is not None:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today = (db.query(PoolReceipt)
                 .filter(PoolReceipt.pool_user_id == user.id,
                         PoolReceipt.created_at >= day_start).count())
        if today >= DAILY_LIMIT:
            return None, {"result": "flood",
                          "message": f"Лимит: не больше {DAILY_LIMIT} чеков в сутки",
                          "points": 0, "user_points": user.points}
    return parsed, None


def ingest_parsed(db: Session, qr_raw: str, parsed, source: str, user,
                  fast: bool = False) -> dict:
    """Движок источников → строки в БД → статус и баллы. Фоновая часть
    веб-приёма (v1.31.0); вызывается и синхронно из ingest_receipt."""
    from .models import PoolItem, PoolReceipt
    from ..models import utcnow

    # 4) данные чека из движка источников (ФНС GetTicket → ЧЗ → …)
    try:
        ext = _fetch_details(db, qr_raw, parsed.fn, parsed.fd, parsed.fp,
                             parsed.total_sum, parsed.date_time)
    except Exception as e:                               # noqa: BLE001
        ext = None
        ext_error = f"источники недоступны: {e.__class__.__name__}"
    else:
        ext_error = ""

    anomaly = parsed.total_sum > ANOMALY_SUM
    has_data = bool(ext and ext.found and ext.items)

    receipt = PoolReceipt(
        pool_user_id=user.id,
        source=source,
        qr_data=parsed.qr_data or qr_raw,
        fn=parsed.fn, fd=parsed.fd, fp=parsed.fp,
        receipt_date=(ext.date_time if ext and ext.date_time else parsed.date_time),
        total_sum=(ext.total_sum if ext and ext.total_sum is not None
                   else parsed.total_sum),
        operation=(ext.operation if ext and ext.operation in (1, 2)
                   else parsed.operation),
        merchant_name=(ext.merchant_name if ext else ""),
        merchant_inn=(ext.merchant_inn if ext else ""),
        merchant_address=(ext.merchant_address if ext else ""),
        cashier=(ext.cashier if ext else ""),
        status="pending",
        raw=(str(getattr(ext, "raw", "") or "")[:20000]),
    )

    db.add(receipt)
    db.flush()                       # id чека нужен для позиций (FK)

    # v1.33.0: гео (адрес → регион; fallback ИНН → Checko) и отрасль
    # (сеть/ключевые слова позиций) — офлайн-справочники, разметка
    # никогда не ломает приём
    try:
        from .geo import enrich_receipt
        enrich_receipt(db, receipt, items=list(ext.items) if has_data else None)
    except Exception:                                     # noqa: BLE001
        pass

    if has_data:
        receipt.full_data = True
        from .geo import normalize_item_name
        for it in ext.items:
            db.add(PoolItem(
                receipt_id=receipt.id, name=normalize_item_name(it.name)[:500],
                quantity=it.quantity or 1, price=it.price or 0,
                total=it.total or 0, vat_rate=str(it.vat_rate or "none"),
                vat_sum=it.vat_sum or 0))
        if anomaly:
            receipt.status = "pending"                   # сумма-аномалия → вручную
            receipt.status_message = "Сумма-аномалия — ручная проверка"
        else:
            receipt.status = "verified"
            receipt.status_message = "Данные чека подтверждены источником"
            receipt.verified_at = utcnow()
    else:
        if anomaly:
            receipt.status = "pending"
            receipt.status_message = "Сумма-аномалия — ручная проверка"
        else:
            receipt.status = "rejected"
            receipt.status_message = (ext.message if ext and ext.message
                                      else ext_error or
                                      "Источник не нашёл чек — проверьте QR")

    points = 0
    if receipt.status == "verified":
        # v1.34.0: карантин вместо бана — чек в пуле, баллы приостановлены
        if getattr(user, "quarantined_at", None):
            receipt.status_message = (receipt.status_message +
                " · баллы приостановлены до разбора (карантин антифрода)")[:500]
        else:
            points = POINTS_PER_RECEIPT
            add_points(db, user, points, "receipt", receipt.id)
        # v1.35.0: реферальные бонусы пригласившему (пороги + lifetime 5%)
        try:
            from . import referral
            referral.on_verified_receipt(db, user, receipt)
        except Exception:                                 # noqa: BLE001
            pass
    receipt.points_awarded = points
    if fast:  # v1.31.0: антифрод-минимум — слишком быстрая отправка формы
        receipt.status_message = (receipt.status_message +
                                  " · сигнал: быстрая отправка формы")[:500]
    db.commit()
    return {"result": receipt.status, "message": receipt.status_message,
            "points": points, "user_points": user.points,
            "receipt_id": receipt.id}


def ingest_receipt(db: Session, qr_raw: str, source: str, user) -> dict:
    """Полный синхронный приём одного чека в пул (бот, тесты).
    Возвращает машиночитаемый результат: result, message, points, user_points."""
    parsed, err = precheck_ingest(db, qr_raw, user)
    if err:
        return err
    return ingest_parsed(db, qr_raw, parsed, source, user)


def overview(db: Session) -> dict:
    """Счётчики для админ-панели (Этап 1: минимальный дашборд)."""
    from sqlalchemy import func
    from .models import PoolPoint, PoolReceipt, PoolUser
    q = db.query(PoolReceipt)
    total = q.count()
    # v1.33.0: покрытие — доля среди чеков, у которых есть данные продавца
    # (адрес или ИНН): чек без данных геокодировать невозможно в принципе
    base = q.filter((PoolReceipt.merchant_address != "")
                    | (PoolReceipt.merchant_inn != "")).count()
    with_region = q.filter(
        (PoolReceipt.region_code != "")
        & ((PoolReceipt.merchant_address != "")
           | (PoolReceipt.merchant_inn != ""))).count()
    with_industry = q.filter(
        (PoolReceipt.industry != "")
        & ((PoolReceipt.merchant_address != "")
           | (PoolReceipt.merchant_inn != ""))).count()
    pct = lambda n: round(100 * n / base) if base else 0
    return {
        "receipts_total": total,
        "verified": q.filter(PoolReceipt.status == "verified").count(),
        "pending": q.filter(PoolReceipt.status == "pending").count(),
        "rejected": q.filter(PoolReceipt.status == "rejected").count(),
        "users_total": db.query(PoolUser).count(),
        "points_total": db.query(
            func.coalesce(func.sum(PoolPoint.delta), 0)).scalar(),
        # покрытие (цели этапа 4 — ≥70% регион, ≥60% отрасль)
        "with_region": with_region, "with_industry": with_industry,
        "geo_coverage": pct(with_region),
        "industry_coverage": pct(with_industry),
    }
