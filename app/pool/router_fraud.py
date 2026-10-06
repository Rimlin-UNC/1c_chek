# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — панель антифрода «Чек-Пула» (v1.34.0, Этап 5).
# Лента сигналов по severity, карточка участника (устройства, IP,
# связанные аккаунты), разбор сигналов (reviewing / false_positive /
# confirmed), карантин и аннулирование баллов — с записью в аудит.
# Технические данные (IP/устройства) храним ≤ 12 месяцев (152-ФЗ),
# чистка — кнопкой (purge).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User
from . import antifraud
from .models import (PoolFingerprint, PoolIpLog, PoolReferral, PoolSignal,
                     PoolUser)

router = APIRouter(prefix="/api/v1/pool-fraud", tags=["pool-fraud"])


def _user_label(db: Session, user_id: str) -> dict:
    u = db.get(PoolUser, user_id)
    if u is None:
        return {"id": user_id, "label": "—"}
    label = u.email or (f"гость {u.vid[:8] if u.vid else u.id[:8]}")
    return {"id": user_id, "label": label, "risk": u.risk_score,
            "quarantined": bool(u.quarantined_at)}


@router.get("/summary", summary="Антифрод: сводка для панели (админ)")
def summary(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from sqlalchemy import func
    new_signals = (db.query(PoolSignal)
                   .filter(PoolSignal.status == "new").count())
    quarantined = (db.query(PoolUser)
                   .filter(PoolUser.quarantined_at.isnot(None)).count())
    by_code = dict(db.query(PoolSignal.code, func.count(PoolSignal.id))
                   .filter(PoolSignal.status == "new")
                   .group_by(PoolSignal.code).all())
    return {"new_signals": new_signals, "quarantined": quarantined,
            "by_code": by_code,
            "quarantine_threshold": antifraud.QUARANTINE_RISK}


@router.get("/signals", summary="Антифрод: лента сигналов (админ)")
def signals(status: str = Query("", max_length=16),
            min_severity: int = Query(0, ge=0, le=5),
            page: int = Query(1, ge=1),
            page_size: int = Query(30, ge=1, le=100),
            db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    q = db.query(PoolSignal)
    if status:
        q = q.filter(PoolSignal.status == status)
    if min_severity:
        q = q.filter(PoolSignal.severity >= min_severity)
    total = q.count()
    rows = (q.order_by(PoolSignal.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "id": s.id, "code": s.code, "severity": s.severity,
            "points": s.points, "status": s.status,
            "details": s.details, "receipt_id": s.receipt_id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "resolved_by": s.resolved_by,
            "user": _user_label(db, s.user_id),
        } for s in rows],
    }


@router.get("/user/{uid}", summary="Антифрод: карточка участника (админ)")
def user_card(uid: str, db: Session = Depends(get_db),
              admin: User = Depends(require_admin)):
    u = db.get(PoolUser, uid)
    if u is None:
        raise HTTPException(404, "Участник не найден")
    sig = (db.query(PoolSignal).filter(PoolSignal.user_id == uid)
           .order_by(PoolSignal.created_at.desc()).limit(50).all())
    devices = (db.query(PoolFingerprint).filter(PoolFingerprint.user_id == uid)
               .order_by(PoolFingerprint.last_at.desc()).limit(20).all())
    ips = (db.query(PoolIpLog).filter(PoolIpLog.user_id == uid)
           .order_by(PoolIpLog.created_at.desc()).limit(50).all())
    # слой 4 (граф): кто ещё на тех же устройствах/подсетях
    vh_list = [d.visitor_hash for d in devices]
    subnets = list({i.ip24 for i in ips if i.ip24})
    related = set()
    if vh_list:
        for r in (db.query(PoolFingerprint)
                  .filter(PoolFingerprint.visitor_hash.in_(vh_list),
                          PoolFingerprint.user_id != uid).all()):
            related.add(r.user_id)
    if subnets:
        day_ago = antifraud._now() - __import__("datetime").timedelta(days=1)
        for r in (db.query(PoolIpLog)
                  .filter(PoolIpLog.ip24.in_(subnets),
                          PoolIpLog.user_id != uid,
                          PoolIpLog.created_at >= day_ago).all()):
            related.add(r.user_id)
    related.discard(uid)
    uniq_ips = []
    seen = set()
    for i in ips:
        if i.ip not in seen:
            seen.add(i.ip)
            uniq_ips.append({"ip": i.ip, "ip24": i.ip24,
                             "at": i.created_at.isoformat() if i.created_at else None})
    return {
        "user": {"id": u.id, "email": u.email, "vid": u.vid,
                 "points": u.points, "risk_score": u.risk_score,
                 "quarantined": bool(u.quarantined_at),
                 "quarantined_at": (u.quarantined_at.isoformat()
                                    if u.quarantined_at else None),
                 "trust_level": u.trust_level,
                 "is_blocked": bool(u.is_blocked)},
        "signals": [{
            "id": s.id, "code": s.code, "severity": s.severity,
            "points": s.points, "status": s.status, "details": s.details,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in sig],
        "devices": [{"visitor": d.visitor_hash[:12], "seen": d.seen_count,
                     "last_at": d.last_at.isoformat() if d.last_at else None}
                    for d in devices],
        "ips": uniq_ips[:10],
        "related_users": [_user_label(db, r) for r in list(related)[:20]],
    }


@router.post("/signal/{sid}/resolve",
             summary="Антифрод: разбор сигнала (админ)")
def resolve_signal(sid: str, body: dict, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    s = db.get(PoolSignal, sid)
    if s is None:
        raise HTTPException(404, "Сигнал не найден")
    status = (body or {}).get("status", "")
    try:
        antifraud.resolve_signal(db, s, status, admin.username)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    log_action(admin, "pool_fraud_signal_resolved",
               details={"signal_id": sid, "status": status})
    return {"ok": True, "id": sid, "status": s.status}


@router.post("/signal/resolve-bulk",
             summary="Антифрод: массовый разбор сигналов (админ)")
def resolve_bulk(body: dict, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    ids = list((body or {}).get("ids") or [])[:200]
    status = (body or {}).get("status", "")
    if not ids:
        raise HTTPException(422, "Не выбраны сигналы")
    n = 0
    for sid in ids:
        s = db.get(PoolSignal, sid)
        if s is not None:
            try:
                antifraud.resolve_signal(db, s, status, admin.username)
                n += 1
            except ValueError:
                raise HTTPException(422, "неизвестный статус")
    db.commit()
    log_action(admin, "pool_fraud_signals_bulk",
               details={"count": n, "status": status})
    return {"ok": True, "resolved": n}


@router.post("/user/{uid}/action",
             summary="Антифрод: действие над участником (админ)")
def user_action(uid: str, body: dict, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    u = db.get(PoolUser, uid)
    if u is None:
        raise HTTPException(404, "Участник не найден")
    action = (body or {}).get("action", "")
    if action == "release":
        antifraud.release_user(db, u)
        db.commit()
        log_action(admin, "pool_fraud_user_released", details={"user_id": uid})
        return {"ok": True, "message": "Карантин снят, сигналы помечены ложными",
                "risk_score": u.risk_score}
    if action == "annul_points":
        pts = antifraud.annul_points(db, u, admin)
        db.commit()
        log_action(admin, "pool_fraud_points_annulled",
                   details={"user_id": uid, "points": pts})
        return {"ok": True, "message": f"Аннулировано баллов: {pts}"}
    if action == "set_trust":
        val = int((body or {}).get("value", 0))
        if val not in (0, 1, 2, 3):
            raise HTTPException(422, "Доверие: 0–3")
        u.trust_level = val
        db.commit()
        log_action(admin, "pool_fraud_trust_set",
                   details={"user_id": uid, "value": val})
        return {"ok": True, "message": f"Уровень доверия: {val}"}
    raise HTTPException(422, "action: release | annul_points | set_trust")


@router.post("/purge", summary="Антифрод: чистка техданных старше года (152-ФЗ)")
def purge(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .router_api import purge_calls
    res = antifraud.purge_tech_data(db)
    res["api_calls"] = purge_calls(db)       # v1.39.0: журнал API ≤ 90 дней
    from ..services.mail_center import purge_log
    res["mail_log"] = purge_log(db)          # v1.44.0: журнал писем ≤ 90 дней
    db.commit()                              # аудит отдельной сессией —
    log_action(admin, "pool_fraud_purge", details=res)   # коммитим заранее
    return {"ok": True, **res,
            "message": f"Удалено: сетей {res['ip_log']}, устройств "
                       f"{res['fingerprints']}, закрытых сигналов {res['signals']}"
                       f", вызовов API {res['api_calls']}"}


# --------------------------------------------------------------------------
# v1.38.0: граф-аналитика связей — кластеры участников, объединённые
# устройством (visitor_hash), подсетью /24 и реферальными парами.
# Служит для визуальной оценки «ферм»: несколько узлов с общими рёбрами.
# --------------------------------------------------------------------------
def _clusters(nodes: list[str], edges: list[tuple[str, str]]) -> list[set[str]]:
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    out: dict[str, set[str]] = {}
    for n in nodes:
        out.setdefault(find(n), set()).add(n)
    return sorted(out.values(), key=lambda s: (-len(s), sorted(s)[0]))


@router.get("/graph", summary="Антифрод: граф связей участников (админ)")
def graph(days: int = Query(30, ge=1, le=365),
          max_nodes: int = Query(60, ge=4, le=200),
          db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(days=days)

    users = {u.id: u for u in db.query(PoolUser).all()}
    if not users:
        return {"nodes": [], "edges": [], "clusters": 0, "days": days}

    edges: list[tuple[str, str, str]] = []
    # устройство: общий visitor_hash
    rows = (db.query(PoolFingerprint.user_id, PoolFingerprint.visitor_hash)
            .filter(PoolFingerprint.visitor_hash != "").all())
    by_vh: dict[str, list[str]] = {}
    for uid, vh in rows:
        if uid in users:
            by_vh.setdefault(vh, []).append(uid)
    for vh, uids in by_vh.items():
        uids = sorted(set(uids))
        for i in range(len(uids) - 1):
            edges.append((uids[i], uids[i + 1], "device"))
    # подсеть /24: общая сеть за окно (ip24 — a.b.c, индексированное)
    rows = (db.query(PoolIpLog.user_id, PoolIpLog.ip24)
            .filter(PoolIpLog.created_at >= since,
                    PoolIpLog.ip24 != "").all())
    by_sub: dict[str, list[str]] = {}
    for uid, ip24 in rows:
        if uid in users:
            by_sub.setdefault(ip24, []).append(uid)
    for sub, uids in by_sub.items():
        uids = sorted(set(uids))
        if len(uids) >= 2:
            for i in range(len(uids) - 1):
                edges.append((uids[i], uids[i + 1], "subnet"))
    # реферальные пары
    for ref in db.query(PoolReferral).all():
        if ref.referrer_id in users and ref.referred_id in users:
            edges.append((ref.referrer_id, ref.referred_id, "referral"))

    e_simple = [(a, b) for a, b, _ in edges]
    groups = [g for g in _clusters(list(users), e_simple) if len(g) >= 2]
    picked: set[str] = set()
    for g in groups:
        if len(picked) + len(g) > max_nodes and picked:
            break
        picked |= g
    kept = [(a, b, k) for a, b, k in edges
            if a in picked and b in picked]
    nodes = []
    for uid in picked:
        u = users[uid]
        nodes.append({"id": uid,
                      "label": (u.email or
                                f"гость {(u.vid or uid)[:8]}")[:40],
                      "risk": u.risk_score or 0,
                      "quarantined": bool(u.quarantined_at)})
    return {"nodes": nodes,
            "edges": [{"a": a, "b": b, "kind": k} for a, b, k in kept],
            "clusters": len(groups), "days": days}
