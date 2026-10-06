# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — вовлечение «Чек-Пула» (v1.36.0, Этап 7).
# Лидерборд месяца (топ-10, публично и в кабинете; соревнование по
# регионам), ачивки («50 чеков», «3 отрасли», «первый чек региона»),
# цель вывода и заявка на вывод баллов.
#
# Приватность (152-ФЗ): публичные данные — только маскированные имена
# («а***@ya.ru» / «участник»), город и число чеков; телефон при выводе
# храним ТОЛЬКО хэшем sha256. SMS-подтверждение первого вывода готово
# кодом в pool_tokens (purpose="sms"); шлюз подключим отдельным решением.
# Все начисления/списания — через журнал pool_points (reason WITHDRAW).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (PoolAchievement, PoolReceipt, PoolUser,
                     PoolWithdrawal)

WITHDRAW_MIN = 100            # минимум баллов для заявки на вывод
WITHDRAW_GOAL_DEFAULT = 500   # цель вывода по умолчанию (настройка)
SMS_CODE_TTL = 15 * 60        # срок жизни SMS-кода, сек
TOP = 10                      # размер лидерборда

ACHIEVEMENTS: dict[str, dict] = {
    "RECEIPTS_50": {
        "name": "50 чеков",
        "desc": "50 верифицированных чеков в пуле",
        "goal": 50,
    },
    "INDUSTRIES_3": {
        "name": "3 отрасли",
        "desc": "чеки из трёх разных отраслей",
        "goal": 3,
    },
    "REGION_FIRST": {
        "name": "Первый чек региона",
        "desc": "первый чек из региона, откуда ещё никто не присылал",
        "goal": 1,
    },
}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _month_start(now: datetime | None = None) -> datetime:
    n = now or _utcnow()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _phone_hash(phone: str) -> str:
    return hashlib.sha256(phone.strip().encode()).hexdigest()


def withdraw_goal(db: Session) -> int:
    """Цель вывода (настройка pool_withdraw_goal, по умолчанию 500)."""
    from ..services import appsettings
    try:
        return max(WITHDRAW_MIN, int(appsettings.get_setting(
            db, "pool_withdraw_goal", str(WITHDRAW_GOAL_DEFAULT))))
    except (TypeError, ValueError):
        return WITHDRAW_GOAL_DEFAULT


def label_for(user: PoolUser) -> str:
    """Публичная маска участника (без ПДн)."""
    email = (user.email or "").strip()
    if email and "@" in email:
        name, _, domain = email.partition("@")
        return (name[:1] + "***@" + domain) if len(name) > 1 else email
    return f"участник {(user.vid or user.id)[:8]}"


# --- ачивки -----------------------------------------------------------------
def _award(db: Session, user: PoolUser, code: str, new: list[str],
           seen: set[str]) -> None:
    """Выдать ачивку один раз. seen — выданные в этом вызове: ядро с
    autoflush=False не видит только что добавленные строки запросом."""
    if code in seen:
        return
    exists = (db.query(PoolAchievement)
              .filter_by(user_id=user.id, code=code).first())
    if exists is None:
        db.add(PoolAchievement(user_id=user.id, code=code))
        seen.add(code)
        new.append(code)


def evaluate(db: Session, user: PoolUser) -> list[str]:
    """Пересчёт ачивок участника; возвращает коды, выданные сейчас.
    Идемпотентно: безопасно вызывать на каждом верифицированном чеке
    и при чтении кабинета (догоняет задним числом)."""
    new: list[str] = []
    seen: set[str] = set()
    vr = (db.query(PoolReceipt)
          .filter(PoolReceipt.pool_user_id == user.id,
                  PoolReceipt.status == "verified").all())
    n = len(vr)
    if n >= ACHIEVEMENTS["RECEIPTS_50"]["goal"]:
        _award(db, user, "RECEIPTS_50", new, seen)
    industries = {r.industry for r in vr if (r.industry or "").strip()}
    if len(industries) >= ACHIEVEMENTS["INDUSTRIES_3"]["goal"]:
        _award(db, user, "INDUSTRIES_3", new, seen)
    # «первый чек региона»: самый ранний верифицированный чек региона —
    # наш (по времени верификации, при равенстве — по времени создания).
    # Участник может быть первым сразу в нескольких регионах — дедуп в seen.
    regions = {r.region_code for r in vr if (r.region_code or "").strip()}
    for code in regions:
        first = (db.query(PoolReceipt)
                 .filter(PoolReceipt.region_code == code,
                         PoolReceipt.status == "verified",
                         PoolReceipt.pool_user_id.isnot(None))
                 .order_by(func.coalesce(PoolReceipt.verified_at,
                                         PoolReceipt.created_at),
                           PoolReceipt.created_at, PoolReceipt.id)
                 .first())
        if first is not None and first.pool_user_id == user.id:
            _award(db, user, "REGION_FIRST", new, seen)
    return new


def on_verified_receipt(db: Session, user: PoolUser, receipt) -> list[str]:
    """Хук из ingest: ачивки после верификации чека."""
    return evaluate(db, user)


def achievements_payload(db: Session, user: PoolUser) -> list[dict]:
    earned = {a.code: a for a in (db.query(PoolAchievement)
                                .filter_by(user_id=user.id).all())}
    vr_n = (db.query(PoolReceipt)
            .filter(PoolReceipt.pool_user_id == user.id,
                    PoolReceipt.status == "verified").count())
    ind_n = len({r.industry for r in (db.query(PoolReceipt)
                .filter(PoolReceipt.pool_user_id == user.id,
                        PoolReceipt.status == "verified").all())
                if (r.industry or "").strip()})
    progress = {"RECEIPTS_50": min(vr_n, 50), "INDUSTRIES_3": min(ind_n, 3),
                "REGION_FIRST": 1 if earned.get("REGION_FIRST") else 0}
    items = []
    for code, meta in ACHIEVEMENTS.items():
        a = earned.get(code)
        items.append({
            "code": code, "name": meta["name"], "desc": meta["desc"],
            "earned": a is not None,
            "awarded_at": a.awarded_at.isoformat() if a else None,
            "progress": progress.get(code, 0), "goal": meta["goal"],
        })
    return items


# --- лидерборд --------------------------------------------------------------
def leaderboard(db: Session, user: PoolUser | None = None) -> dict:
    """Топ-10 участников месяца по верифицированным чекам + стендинг
    регионов («Ваш город на N-м месте»). Публично: маски, без ПДн."""
    from .geo import region_name
    start = _month_start()
    rows = (db.query(PoolReceipt.pool_user_id, PoolReceipt.region_code,
                     PoolReceipt.city)
            .filter(PoolReceipt.status == "verified",
                    PoolReceipt.pool_user_id.isnot(None),
                    func.coalesce(PoolReceipt.verified_at,
                                  PoolReceipt.created_at) >= start).all())
    per_user: dict[str, int] = {}
    city_of: dict[str, str] = {}
    by_region: dict[str, int] = {}
    for uid, rc, city in rows:
        per_user[uid] = per_user.get(uid, 0) + 1
        if (city or "").strip() and uid not in city_of:
            city_of[uid] = city.strip()
        if (rc or "").strip():
            by_region[rc] = by_region.get(rc, 0) + 1
    users = {u.id: u for u in (db.query(PoolUser)
                               .filter(PoolUser.id.in_(list(per_user))).all())}
    ordered = sorted(per_user.items(), key=lambda kv: (-kv[1], kv[0]))
    entries = []
    for uid, cnt in ordered[:TOP]:
        u = users.get(uid)
        if u is None or u.is_blocked:
            continue
        entries.append({"label": label_for(u), "city": city_of.get(uid, ""),
                        "receipts": cnt})
    regions = [{"code": rc, "name": region_name(rc), "receipts": cnt}
               for rc, cnt in sorted(by_region.items(),
                                     key=lambda kv: (-kv[1], kv[0]))[:TOP]]
    out: dict = {"month": start.strftime("%Y-%m"), "entries": entries,
                 "regions": regions, "participants": len(per_user),
                 "me": None}
    if user is not None and user.id in per_user:
        rank = 1 + sum(1 for uid, cnt in ordered
                       if cnt > per_user[user.id] and uid != user.id)
        me_city = city_of.get(user.id, "")
        city_ranks: dict[str, int] = {}
        for uid, cnt in ordered:
            c = city_of.get(uid, "")
            if c:
                city_ranks[c] = city_ranks.get(c, 0) + 1
        ahead = 0
        for uid, cnt in ordered:
            if uid != user.id and city_of.get(uid, "") == me_city \
                    and cnt > per_user[user.id]:
                ahead += 1
        city_rank = (ahead + 1) if me_city else None
        city_total = city_ranks.get(me_city)
        out["me"] = {"rank": rank, "receipts": per_user[user.id],
                     "city": me_city, "city_rank": city_rank,
                     "city_participants": city_total}
    return out


# --- вывод баллов -----------------------------------------------------------
def _normalize_phone(raw: str) -> str | None:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if 10 <= len(digits) <= 15:
        return "+" + digits
    return None


def active_withdrawal(db: Session, user: PoolUser) -> PoolWithdrawal | None:
    return (db.query(PoolWithdrawal)
            .filter(PoolWithdrawal.user_id == user.id,
                    PoolWithdrawal.status.in_(["pending", "pending_sms"]))
            .order_by(PoolWithdrawal.created_at.desc()).first())


def request_withdrawal(db: Session, user: PoolUser, points: int,
                       phone: str) -> tuple[bool, dict]:
    """Заявка на вывод: баллы списываются сразу (журнал WITHDRAW),
    телефон — только хэшем. Первый вывод ждёт SMS-подтверждения."""
    if active_withdrawal(db, user) is not None:
        return False, {"error": "Активная заявка уже есть — дождитесь выплаты"}
    points = int(points or 0)
    if points < WITHDRAW_MIN:
        return False, {"error": f"Минимум для вывода — {WITHDRAW_MIN} баллов"}
    if points > (user.points or 0):
        return False, {"error": "Недостаточно баллов"}
    norm = _normalize_phone(phone)
    if norm is None:
        return False, {"error": "Укажите телефон в формате +7…"}

    first = (db.query(PoolWithdrawal)
             .filter(PoolWithdrawal.user_id == user.id).count() == 0)
    w = PoolWithdrawal(user_id=user.id, points=points,
                       phone_hash=_phone_hash(norm),
                       sms_required=first,
                       status="pending_sms" if first else "pending")
    db.add(w)
    db.flush()                                    # id для журнала
    from .ingest import add_points
    add_points(db, user, -points, "withdraw", w.id)
    if first:
        # 6-значный SMS-код: в БД только sha256 (pool_tokens, purpose="sms")
        from . import accounts
        from .models import PoolToken
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        db.add(PoolToken(token_hash=accounts._hash_token(code), purpose="sms",
                         email=(user.email or "").lower(), user_id=user.id,
                         expires_at=_utcnow() + timedelta(seconds=SMS_CODE_TTL)))
    db.flush()
    return True, {"id": w.id, "points": points, "status": w.status,
                  "sms_required": first,
                  "message": ("Заявка принята. Первый вывод подтверждается "
                              "по SMS — код придёт после подключения шлюза"
                              if first else
                              "Заявка принята — ожидайте выплаты")}


def confirm_withdrawal(db: Session, user: PoolUser, raw_code: str) \
        -> tuple[bool, str]:
    """SMS-подтверждение первого вывода (код из pool_tokens)."""
    from . import accounts
    tok_user, err = accounts.consume_token(db, (raw_code or "").strip(), "sms")
    if err:
        return False, err
    if tok_user is None or tok_user.id != user.id:
        return False, "Код не подходит к этому аккаунту"
    w = (db.query(PoolWithdrawal)
         .filter(PoolWithdrawal.user_id == user.id,
                 PoolWithdrawal.status == "pending_sms")
         .order_by(PoolWithdrawal.created_at.desc()).first())
    if w is None:
        return False, "Нет заявки, ожидающей подтверждения"
    w.status = "pending"
    return True, "Телефон подтверждён — заявка принята"


def status_for(db: Session, user: PoolUser) -> dict:
    """Карточка «Вовлечение» кабинета: цель, ачивки, заявки."""
    goal = withdraw_goal(db)
    active = active_withdrawal(db, user)
    history = (db.query(PoolWithdrawal)
               .filter(PoolWithdrawal.user_id == user.id)
               .order_by(PoolWithdrawal.created_at.desc()).limit(5).all())
    return {
        "points": user.points or 0,
        "goal": goal,
        "min": WITHDRAW_MIN,
        "can_withdraw": (user.points or 0) >= WITHDRAW_MIN
                        and active is None,
        "active": ({"points": active.points, "status": active.status,
                    "sms_required": active.sms_required,
                    "at": active.created_at.isoformat()} if active else None),
        "achievements": achievements_payload(db, user),
        "history": [{"points": w.points, "status": w.status,
                     "sms_required": w.sms_required,
                     "at": w.created_at.isoformat()} for w in history],
    }


def admin_counts(db: Session) -> dict:
    """Счётчики заявок для админ-обзора (обработка — со шлюзом)."""
    q = db.query(PoolWithdrawal)
    return {
        "pending": q.filter(PoolWithdrawal.status == "pending").count(),
        "pending_sms": q.filter(
            PoolWithdrawal.status == "pending_sms").count(),
        "total_requests": q.count(),
        "points_reserved": int(db.query(
            func.coalesce(func.sum(PoolWithdrawal.points), 0))
            .filter(PoolWithdrawal.status.in_(["pending", "pending_sms"]))
            .scalar() or 0),
    }
