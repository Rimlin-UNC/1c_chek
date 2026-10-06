# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — реферальная программа «Чек-Пула» (v1.35.0, Этап 6).
# Строго по концепции (docs/plan.md, разд. 2):
#   • бонусы НЕ за регистрацию, а за качественные действия реферала
#     с ЗАДЕРЖКОЙ (сопоставимой с честным циклом жизни);
#   • lifetime-хвост 5% (каждый 20-й верифицированный чек реферала
#     приносит пригласившему 1 балл) с потолком 200 баллов/мес;
#   • ОДИН уровень (без многоуровневой рефералки);
#   • лимит 50 приглашённых на одного участника;
#   • «сам себя пригласил» и фермы не приносят ни балла: пары с
#     антифрод-сигналами (та же подсеть, то же устройство, петля,
#     мгновенный чек, карантин) не получают выплаты, пока не разобраны.
# Все выплаты — через журнал pool_points (reason = код бонуса),
# разбор сигналов в панели антифрода возвращает начисления.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

REFERRAL_LIMIT = 50            # максимум приглашённых на участника (план)
LIFETIME_DIVISOR = 20          # каждый 20-й чек реферала → +1 балл (= 5%)
LIFETIME_MONTH_CAP = 200       # потолок lifetime-баллов в месяц на реферала
FIRST_CHECK_MIN_SUM = 100.0    # первый чек — от 100 ₽ (план)
FAST_RECEIPT_MIN = 10 * 60     # чек быстрее 10 минут после регистрации

# (код, баллы, требуется верифицированных чеков, trust реферала, задержка дней)
THRESHOLDS = [
    ("R_FIRST_CHECK", 20, 1, 0, 7),
    ("R_FIFTH_CHECK", 50, 5, 0, 14),
    ("R_TWENTIETH", 150, 20, 1, 30),
    ("R_FIFTIETH", 500, 50, 2, 60),
]
HOLD_SIGNALS = ("SAME_SUBNET_REFERRAL", "FAST_REFERRAL", "REFERRAL_CYCLE",
                "REFERRAL_FRAUD", "MULTI_ACCOUNT_DEVICE")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- код приглашения ---------------------------------------------------------
def get_or_create_code(db: Session, user) -> str:
    """YM-XXXXXX (без похожих O/0/I/1). Ленивая выдача."""
    if user.referral_code:
        return user.referral_code
    from .models import PoolUser
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(10):
        tail = "".join(secrets.choice(alphabet) for _ in range(6))
        code = f"YM-{tail}"
        if not db.query(PoolUser).filter_by(referral_code=code).first():
            user.referral_code = code
            db.flush()
            return code
    return ""                      # крайне маловероятно: 10 коллизий подряд


def owner_of_code(db: Session, code: str):
    from .models import PoolUser
    return (db.query(PoolUser)
            .filter(PoolUser.referral_code == (code or "").strip().upper(),
                    PoolUser.is_blocked.is_(False)).first())


def referral_of(db: Session, referee):
    from .models import PoolReferral
    if referee is None:
        return None
    return db.query(PoolReferral).filter_by(referred_id=referee.id).first()


# --- привязка при регистрации ---------------------------------------------------
def attribute(db: Session, new_user, code: str, request=None) -> bool:
    """Привязывает реферала к пригласившему. Молча возвращает False, если
    код недействителен, это самоприглашение или лимит исчерпан."""
    from .models import PoolReferral
    if not (code or "").strip():
        return False
    owner = owner_of_code(db, code)
    if owner is None or owner.id == new_user.id or owner.is_blocked:
        return False
    invited = (db.query(PoolReferral)
               .filter(PoolReferral.referrer_id == owner.id).count())
    if invited >= REFERRAL_LIMIT:
        return False
    db.add(PoolReferral(referrer_id=owner.id, referred_id=new_user.id,
                        code_used=(code or "").strip().upper()[:16]))
    db.flush()

    # антифрод (слой 4): та же подсеть / петля — сигналы пригласившему
    from . import antifraud
    from .models import PoolIpLog, PoolReferral
    subnet = antifraud._ip24(antifraud.request_ip(request)) if request else ""
    if subnet:
        last = (db.query(PoolIpLog)
                .filter(PoolIpLog.user_id == owner.id)
                .order_by(PoolIpLog.created_at.desc()).first())
        if last is not None and last.ip24 == subnet:
            antifraud.record_signal(db, owner, "SAME_SUBNET_REFERRAL",
                                    details={"subnet": subnet})
    up = (db.query(PoolReferral)
          .filter(PoolReferral.referred_id == owner.id).first())
    if up is not None and up.referrer_id == new_user.id:
        antifraud.record_signal(db, owner, "REFERRAL_CYCLE",
                                details={"with": new_user.id})
    return True


# --- удержание выплат (антифрод, слой 4) ---------------------------------------
def fraud_hold(db: Session, referral) -> tuple[bool, str]:
    """True, если паре пока нельзя платить: карантин одного из участников,
    активный сигнал реферальной схемы, общее устройство."""
    from .models import (PoolFingerprint, PoolSignal, PoolUser)
    if referral is None:
        return True, "нет связи"
    a = db.get(PoolUser, referral.referrer_id)
    b = db.get(PoolUser, referral.referred_id)
    if a is None or b is None:
        return True, "участник не найден"
    if a.quarantined_at or b.quarantined_at:
        return True, "карантин антифрода"
    codes = set()
    for u in (a, b):
        for s in (db.query(PoolSignal)
                  .filter(PoolSignal.user_id == u.id,
                          PoolSignal.code.in_(HOLD_SIGNALS),
                          PoolSignal.status.in_(("new", "reviewing", "confirmed")))
                  .all()):
            codes.add(s.code)
    if codes:
        return True, "сигналы: " + ", ".join(sorted(codes))
    vh_a = [f.visitor_hash for f in (db.query(PoolFingerprint)
                                     .filter_by(user_id=a.id).all())]
    if vh_a and (db.query(PoolFingerprint)
                 .filter(PoolFingerprint.user_id == b.id,
                         PoolFingerprint.visitor_hash.in_(vh_a)).count()):
        return True, "общее устройство"
    return False, ""


# --- выплаты -----------------------------------------------------------------
def _pay(db: Session, referral, code: str, points: int) -> bool:
    from . import ingest
    from .models import PoolPoint, PoolUser
    already = (db.query(PoolPoint)
               .filter(PoolPoint.user_id == referral.referrer_id,
                       PoolPoint.reason == code,
                       PoolPoint.ref_id == referral.id).first())
    if already is not None:
        return False
    hold, _why = fraud_hold(db, referral)
    if hold:
        return False
    referrer = db.get(PoolUser, referral.referrer_id)
    if referrer is None or referrer.is_blocked:
        return False
    ingest.add_points(db, referrer, points, code, referral.id)
    return True


def on_email_verified(db: Session, referee) -> bool:
    """+5 пригласившему: e-mail реферала подтверждён, с регистрации ≥ 24 ч."""
    referral = referral_of(db, referee)
    if referral is None:
        return False
    if _now() < referral.created_at + timedelta(days=1):
        return False
    return _pay(db, referral, "R_EMAIL", 5)


def on_verified_receipt(db: Session, referee, receipt) -> int:
    """Пороговые бонусы (1-й/5-й/20-й/50-й чек) + lifetime-хвост 5%.
    Возвращает число начисленных бонусов за этот чек."""
    from .models import PoolPoint, PoolReceipt
    referral = referral_of(db, referee)
    if referral is None:
        return 0
    paid = 0
    n = (db.query(PoolReceipt)
         .filter(PoolReceipt.pool_user_id == referee.id,
                 PoolReceipt.status == "verified").count())

    # FAST_REFERRAL: чек почти сразу после регистрации — сигнал качеству
    if n == 1 and receipt is not None and receipt.created_at is not None:
        from . import antifraud
        if (receipt.created_at - referee.created_at) < timedelta(
                seconds=FAST_RECEIPT_MIN):
            antifraud.record_signal(db, referee, "FAST_REFERRAL",
                                    receipt_id=receipt.id)

    if receipt is not None and n == 1 and receipt.total_sum < FIRST_CHECK_MIN_SUM:
        return 0                          # первый чек — только от 100 ₽

    for code, points, min_count, min_trust, delay_days in THRESHOLDS:
        if n != min_count:
            continue
        if (referee.trust_level or 0) < min_trust:
            continue
        if _now() < referral.created_at + timedelta(days=delay_days):
            continue
        if _pay(db, referral, code, points):
            paid += 1

    # lifetime 5%: каждый 20-й верифицированный чек → +1, потолок 200/мес
    if n > 0 and n % LIFETIME_DIVISOR == 0:
        month_start = _now().replace(day=1, hour=0, minute=0, second=0,
                                     microsecond=0)
        this_month = (db.query(PoolPoint)
                      .filter(PoolPoint.user_id == referral.referrer_id,
                              PoolPoint.reason == "R_LIFETIME",
                              PoolPoint.ref_id == referral.id,
                              PoolPoint.created_at >= month_start).count())
        if this_month < LIFETIME_MONTH_CAP:
            if _pay(db, referral, "R_LIFETIME", 1):
                paid += 1
    return paid


def hint_for(db: Session, referee) -> dict:
    """Какой бонус ближайший и чего ждём (для кабинета)."""
    from .models import PoolReceipt
    referral = referral_of(db, referee)
    if referral is None:
        return {}
    n = (db.query(PoolReceipt)
         .filter(PoolReceipt.pool_user_id == referee.id,
                 PoolReceipt.status == "verified").count())
    for code, points, min_count, min_trust, delay_days in THRESHOLDS:
        if n >= min_count:
            continue
        return {"code": code, "points": points, "need_receipts": min_count,
                "have_receipts": n,
                "ready_at": (referral.created_at
                             + timedelta(days=delay_days)).isoformat()}
    return {}
