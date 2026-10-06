# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — антифрод «Чек-Пула» (v1.34.0, Этап 5): пять слоёв
# из концепции (docs/plan.md, разд. 3–5). Принцип: «не запрет, а
# ограничение скорости выгоды» — чеки участника в карантине сохраняются
# в пул, но баллы приостанавливаются до разбора модератором.
#
#   Слой 1 Device: X-Visitor-Id (отпечаток браузера, хэшируем сразу) —
#                  один visitor на 2+ аккаунтах → MULTI_ACCOUNT_DEVICE.
#   Слой 2 Network: журнал IP//24 — SUBNET_CLUSTER, DATACENTER_IP
#                  (офлайн-список CIDR облачных провайдеров).
#   Слой 3 Behavior: тайминг формы (BOT_PATTERN), ровные интервалы
#                  чеков (EVEN_INTERVALS), поток чеков (RECEIPT_FLOOD).
#   Слой 4 Graph: связи участника по visitor//24 (панель антифрода);
#                  REFERRAL_FRAUD — зарезервирован (рефералы — этап 6).
#   Слой 5 Rules: бизнес-правила (5+ одинаковых сумм подряд и т.п.).
#
# Ложные блокировки на честных недопустимы: сигналы только с большим
# отступом от нормального поведения; одноимённый сигнал не спамится
# (не чаще раза в сутки на участника); разбор — false_positive снимает
# вес из risk_score.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

QUARANTINE_RISK = 71          # карантин вместо бана (план, разд. 4)
SIGNAL_TTL_H = 24             # одноимённый сигнал — не чаще раза в сутки
FLOOD_PER_HOUR = 20           # чеков в час — поток (слой 5)
EVEN_MIN_S, EVEN_MAX_S, EVEN_SPREAD_S = 5, 60, 5   # ровные интервалы
REPEAT_SUMS_N = 5             # одинаковых сумм подряд
# Эвристика «облачный/датацентр»: известные диапазоны крупных провайдеров.
# Список уточняется по факту эксплуатации; частные сети игнорируем.
DATACENTER_CIDRS = (
    "3.0.0.0/8", "13.52.0.0/14", "15.177.0.0/16", "18.128.0.0/9",
    "34.64.0.0/10", "35.180.0.0/12", "52.0.0.0/8", "54.0.0.0/8",
    "104.16.0.0/12", "130.211.0.0/16", "138.197.0.0/16", "142.93.0.0/16",
    "159.65.0.0/16", "167.99.0.0/16", "206.189.0.0/16", "45.55.0.0/16",
    "95.213.0.0/16", "185.104.0.0/15", "194.87.0.0/16",
)
_DATACENTER_NETS = None

# code: (severity 1..5, вклад в risk_score, человекочитаемое описание)
SIGNALS = {
    "MULTI_ACCOUNT_DEVICE": (3, 60,
        "Одно устройство на несколько аккаунтов"),
    "SUBNET_CLUSTER": (3, 45,
        "Много регистраций из одной подсети за сутки"),
    "DATACENTER_IP": (2, 25,
        "Регистрация с адреса датацентра/облака"),
    "BOT_PATTERN": (3, 40,
        "Слишком быстрая отправка формы"),
    "EVEN_INTERVALS": (3, 40,
        "Чеки идут ровными интервалами — похоже на скрипт"),
    "RECEIPT_FLOOD": (3, 30,
        "Поток чеков — больше 20 за час"),
    "REPEAT_SUMS": (2, 20,
        "Много чеков с одинаковой суммой подряд"),
    "REFERRAL_FRAUD": (4, 50,
        "Связь кластера с реферальной сетью"),
    # v1.35.0: схемы вокруг рефералки
    "SAME_SUBNET_REFERRAL": (3, 45,
        "Пригласивший и реферал из одной подсети"),
    "FAST_REFERRAL": (3, 40,
        "Реферал сдал чек почти сразу после регистрации"),
    "REFERRAL_CYCLE": (3, 50,
        "Реферальная петля: пригласил тот, кого пригласили"),
}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


def visitor_hash(request) -> str:
    """Обезличенный отпечаток устройства из заголовка X-Visitor-Id."""
    raw = ((request.headers.get("x-visitor-id") or "").strip())[:200]
    return _sha(f"visitor:{raw}")[:32] if raw else ""


def ua_hash(request) -> str:
    return _sha(request.headers.get("user-agent") or "")[:16]


def _ip24(ip: str) -> str:
    parts = (ip or "").split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3])
    return ""


def request_ip(request) -> str:
    """IP клиента: в бою nginx ставит X-Forwarded-For — доверяем первому."""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff[:45]
    return (request.client.host if request.client else "")[:45]


def _is_datacenter(ip: str) -> bool:
    global _DATACENTER_NETS
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_private:
        return False
    if _DATACENTER_NETS is None:
        _DATACENTER_NETS = [ipaddress.ip_network(c, strict=False)
                            for c in DATACENTER_CIDRS]
    return any(addr in n for n in _DATACENTER_NETS)


# --- сигнал --------------------------------------------------------------
def record_signal(db: Session, user, code: str, receipt_id: str | None = None,
                  details: dict | None = None) -> bool:
    """Сигнал с дедупликацией (код+участник — раз в сутки).
    Пересчитывает risk_score и карантин. Возвращает True, если записан."""
    from .models import PoolSignal
    if code not in SIGNALS or user is None:
        return False
    fresh = (db.query(PoolSignal)
             .filter(PoolSignal.user_id == user.id, PoolSignal.code == code,
                     PoolSignal.created_at >= _now() - timedelta(hours=SIGNAL_TTL_H))
             .first())
    if fresh is not None:
        return False
    severity, points, _ = SIGNALS[code]
    db.add(PoolSignal(
        user_id=user.id, receipt_id=receipt_id, code=code,
        severity=severity, points=points,
        details=json.dumps(details or {}, ensure_ascii=False)[:2000]))
    db.flush()                       # autoflush=False: риск видит сигнал
    apply_user_risk(db, user)
    return True


def apply_user_risk(db: Session, user) -> None:
    """risk_score = сумма весов сигналов без false_positive (0..100).
    risk ≥ 71 → карантин; риск упал → карантин снимается сам."""
    from .models import PoolSignal
    rows = (db.query(PoolSignal)
            .filter(PoolSignal.user_id == user.id,
                    PoolSignal.status != "false_positive").all())
    risk = min(100, sum(s.points for s in rows))
    user.risk_score = risk
    if risk >= QUARANTINE_RISK:
        if not user.quarantined_at:
            user.quarantined_at = _now()
    else:
        user.quarantined_at = None


# --- журнал устройств и сетей (слои 1–2) ---------------------------------
def log_device_and_ip(db: Session, user, request, kind: str) -> None:
    from .models import PoolFingerprint, PoolIpLog
    # NB: autoflush=False в ядре — после добавлений нужен db.flush(),
    # иначе проверки кластеров не увидят собственные записи
    vh = visitor_hash(request)
    if vh:
        row = (db.query(PoolFingerprint)
               .filter(PoolFingerprint.user_id == user.id,
                       PoolFingerprint.visitor_hash == vh).first())
        if row is None:
            db.add(PoolFingerprint(user_id=user.id, visitor_hash=vh,
                                   ua_hash=ua_hash(request)))
        else:
            row.seen_count += 1
            row.last_at = _now()
    ip = request_ip(request)
    db.add(PoolIpLog(user_id=user.id, ip=ip, ip24=_ip24(ip),
                     ua_hash=ua_hash(request), kind=kind))
    db.flush()


# --- проверка регистрации/входа (слои 1–2) ---------------------------------
def after_auth(db: Session, user, request, kind: str) -> None:
    """Вызывается после register/login/magic: журнал + проверки."""
    from .models import PoolFingerprint, PoolUser, PoolIpLog

    log_device_and_ip(db, user, request, kind)
    vh = visitor_hash(request)
    ip = request_ip(request)

    # Слой 1: тот же отпечаток уже на другом аккаунте?
    if vh:
        others = (db.query(PoolFingerprint)
                  .filter(PoolFingerprint.visitor_hash == vh,
                          PoolFingerprint.user_id != user.id).count())
        if others:
            record_signal(db, user, "MULTI_ACCOUNT_DEVICE",
                          details={"devices_users": others + 1})

    # Слой 2: датацентр и кластер подсети (/24) за сутки
    if ip and _is_datacenter(ip):
        record_signal(db, user, "DATACENTER_IP", details={"ip": ip})
    subnet = _ip24(ip)
    if subnet:
        day_ago = _now() - timedelta(hours=24)
        subnet_users = set()
        for r in (db.query(PoolIpLog)
                  .filter(PoolIpLog.ip24 == subnet,
                          PoolIpLog.created_at >= day_ago).all()):
            subnet_users.add(r.user_id)
        if len(subnet_users) >= 5:
            record_signal(db, user, "SUBNET_CLUSTER",
                          details={"subnet": subnet, "users": len(subnet_users)})


# --- проверка после приёма чека (слои 3 и 5) --------------------------------
def after_receipt(db: Session, user, receipt_id: str, request,
                  form_ms: int = 0) -> None:
    from .models import PoolReceipt
    if user is None:
        return
    db.flush()                       # autoflush=False: считаем то, что есть

    # Слой 3: мгновенная отправка формы
    if form_ms and form_ms < 800:
        record_signal(db, user, "BOT_PATTERN",
                      receipt_id=receipt_id, details={"form_ms": form_ms})

    # поток чеков (слой 5)
    hour_ago = _now() - timedelta(hours=1)
    last_hour = (db.query(PoolReceipt)
                 .filter(PoolReceipt.pool_user_id == user.id,
                         PoolReceipt.created_at >= hour_ago).count())
    if last_hour >= FLOOD_PER_HOUR:
        record_signal(db, user, "RECEIPT_FLOOD",
                      receipt_id=receipt_id, details={"per_hour": last_hour})

    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.pool_user_id == user.id)
            .order_by(PoolReceipt.created_at.desc()).limit(6).all())
    if len(rows) >= REPEAT_SUMS_N + 1:
        sums = [round(r.total_sum, 2) for r in rows[:REPEAT_SUMS_N + 1]]
        if max(sums) - min(sums) <= 0.5:
            record_signal(db, user, "REPEAT_SUMS",
                          receipt_id=receipt_id, details={"sums": sums[:5]})

    # ровные интервалы (слой 3): 5 интервалов подряд почти одинаковые
    if len(rows) >= 6:
        stamps = [r.created_at for r in rows]
        deltas = [(stamps[i] - stamps[i + 1]).total_seconds()
                  for i in range(5)]
        if (all(EVEN_MIN_S <= d <= EVEN_MAX_S for d in deltas)
                and (max(deltas) - min(deltas)) <= EVEN_SPREAD_S):
            record_signal(db, user, "EVEN_INTERVALS",
                          receipt_id=receipt_id,
                          details={"deltas": [int(d) for d in deltas]})


# --- разбор сигналов (панель антифрода) --------------------------------------
def resolve_signal(db: Session, signal, status: str, admin_username: str) -> None:
    """reviewing | false_positive | confirmed. Пересчитывает риск/карантин."""
    from .models import PoolSignal, PoolUser
    if status not in ("reviewing", "false_positive", "confirmed"):
        raise ValueError("неизвестный статус")
    signal.status = status
    signal.resolved_at = _now()
    signal.resolved_by = admin_username[:64]
    db.flush()                       # autoflush=False: риск видит разбор
    user = db.get(PoolUser, signal.user_id)
    if user is not None:
        apply_user_risk(db, user)
        if status == "confirmed" and user.quarantined_at is None:
            user.quarantined_at = _now()      # подтверждённый сигнал держит карантин


def release_user(db: Session, user) -> None:
    """Снять карантин вручную (все сигналы разобраны модератором)."""
    from .models import PoolSignal
    (db.query(PoolSignal)
     .filter(PoolSignal.user_id == user.id,
             PoolSignal.status.in_(("new", "reviewing")))
     .update({PoolSignal.status: "false_positive",
              PoolSignal.resolved_at: _now()}, synchronize_session=False))
    apply_user_risk(db, user)


def annul_points(db: Session, user, admin) -> int:
    """Аннулирование баллов нарушителя с записью в журнале."""
    from . import ingest
    pts = user.points or 0
    if pts > 0:
        ingest.add_points(db, user, -pts, "risk_annulled")
    return pts


def purge_tech_data(db: Session, days: int = 365) -> dict:
    """152-ФЗ: технические данные (IP, устройства) храним ≤ 12 месяцев."""
    from .models import PoolFingerprint, PoolIpLog, PoolSignal
    border = _now() - timedelta(days=days)
    ip_n = (db.query(PoolIpLog)
            .filter(PoolIpLog.created_at < border).delete(synchronize_session=False))
    fp_n = (db.query(PoolFingerprint)
            .filter(PoolFingerprint.last_at < border).delete(synchronize_session=False))
    sig_n = (db.query(PoolSignal)
             .filter(PoolSignal.created_at < border,
                     PoolSignal.status.in_(("false_positive", "confirmed")))
             .delete(synchronize_session=False))
    db.commit()
    return {"ip_log": ip_n, "fingerprints": fp_n, "signals": sig_n}
