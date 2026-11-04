# -*- coding: utf-8 -*-
"""Репетиция обновления на копии: старая версия → текущий код → откат → снова.

Запуск из корня репозитория (нужна история git со старым коммитом):

    .venv/bin/python -m qa.upgrade_rehearsal --from-ref b3db4fe

Сценарий повторяет то, что происходит на сервере при обновлении из программы
и при откате через «Последние рабочие версии»:

1. Старая версия выгружается (`git archive`) во временный каталог и сама
   создаёт базу: две компании и одна удаляемая, бухгалтеры, сотрудник, чеки
   реестра (в том числе без ФП), чеки Чек-Пула (закреплённые, свободные и
   исторический двойник чека реестра в другой компании с другой суммой).
2. Текущий код выполняет то же, что проверка целостности обновления
   (`from app.main import app; init_db()`), и рабочие сценарии через HTTP:
   списки по ролям, карточка, правки, проверка ФНС (ответ подменён — сеть ФНС
   не нужна), Чек-Пул, новый скан и чужой дубликат, счётчики единого реестра.
3. Старая версия открывает обновлённую базу (откат): вход, списки, перенос и
   удаление чеков, правка, удаление компании с переносом её чеков.
4. Текущий код снова (повторное обновление): сверка при старте, чеки видны
   своим компаниям, перенос обратно работает.
5. Пакетный перенос (с 1.70.84, T-32/T-33): чеки Альфы — с единой записью и
   без неё, чек без ФП, чеки, уже лежащие у Беты, — уходят в Бету и обратно
   одной операцией; журнал карточки и список у бухгалтера Беты не раскрывают
   Альфу; 1000 старых чеков туда и обратно — с замером времени.
6. Перенос архива и разбор спора — на копии базы; старая версия открывает
   копию после разбора и основную базу после пакетного переноса (откат).
7. Заявки компаний (с 1.70.85, T-34): чужой дубликат — «Заявка передана
   администратору», администратор видит число организаций и список, решает
   «отклонить» и «передать»; сотрудникам — ни списка, ни числа. Отказы /scan
   старой версии из журнала аудита попадают в заявки задним числом (на копии).
   Старая версия открывает базу с заявками, повторное обновление их сохраняет.

Ни одна фаза не трогает рабочую базу: всё живёт во временном каталоге.
Код возврата 0 — только если все проверки прошли; отчёт — JSON в stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARK = "@@RESULT@@"

COMMON = r'''
import hashlib, json, os, sys
from sqlalchemy import text

def fingerprint():
    from app.database import engine
    out = {}
    queries = (
        ("companies", "SELECT id, name FROM companies ORDER BY id"),
        ("users", "SELECT id, username, role, company_id FROM users ORDER BY id"),
        ("receipts", "SELECT id, fn, fd, fp, total_sum, company_id, created_by FROM receipts ORDER BY id"),
        ("receipt_items", "SELECT receipt_id, name, quantity, total FROM receipt_items ORDER BY receipt_id, position, id"),
        ("pool_receipts", "SELECT id, fn, fd, fp, total_sum, assigned_company_id, status, points_awarded "
                          "FROM pool_receipts ORDER BY id"),
        ("pool_items", "SELECT receipt_id, name, total FROM pool_items ORDER BY receipt_id, id"),
        ("pool_users", "SELECT id, points FROM pool_users ORDER BY id"),
    )
    with engine.connect() as conn:
        for name, sql in queries:
            rows = [[str(v) for v in r] for r in conn.execute(text(sql))]
            out[name] = {"count": len(rows),
                         "sha": hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]}
    return out

def emit(obj):
    print("@@RESULT@@" + json.dumps(obj, ensure_ascii=False, default=str))

def login(c, username, password):
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, (username, r.status_code, r.text[:200])
    return {"Authorization": "Bearer " + r.json()["access_token"]}

def total(c, headers):
    r = c.get("/api/v1/receipts", headers=headers, params={"per_page": 1})
    return r.json().get("total") if r.status_code == 200 else r.status_code

def short(resp):
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return {k: body.get(k) for k in ("duplicate", "message", "detail") if k in body}
    return str(body)[:200]

def app_version():
    from app.config import settings
    return settings.APP_VERSION
'''

SEED = COMMON + r'''
from datetime import datetime, timedelta
from app.seed import seed_if_needed
seed_if_needed()
from app.auth import hash_password
from app.database import SessionLocal
from app.models import Company, Receipt, ReceiptItem, User
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.services import appsettings

pw = os.environ["REH_PASSWORD"]
db = SessionLocal()
admin = db.query(User).filter_by(username="admin").one()
admin.password_hash = hash_password(pw)
admin.must_change_password = False
alpha, beta, gamma = Company(name="ООО «Альфа»"), Company(name="ООО «Бета»"), Company(name="ООО «Гамма»")
db.add_all([alpha, beta, gamma])
db.flush()

def person(login_name, role, comp):
    u = User(username=login_name, password_hash=hash_password(pw), role=role, company_id=comp.id,
             full_name=login_name, is_active=True, must_change_password=False)
    db.add(u)
    db.flush()
    return u

acc_a, acc_b = person("acc_a", "accountant", alpha), person("acc_b", "accountant", beta)
user_a, acc_g = person("user_a", "user", alpha), person("acc_g", "accountant", gamma)
for key, value in (("auto_verify", "0"), ("pool_enabled", "1"), ("pool_pick_monthly_limit", "100")):
    appsettings.set_setting(db, key, value)
base = datetime(2026, 10, 1, 10, 0)

def qr(fn, fd, fp, amount, when):
    return f"t={when:%Y%m%dT%H%M}&s={amount:.2f}&fn={fn}&i={fd}&fp={fp}&n=1"

def registry(i, comp, author, amount, fp=None):
    fn, fd = f"92890000{i:08d}", str(1000 + i)
    fp = str(3000000000 + i) if fp is None else fp
    when = base + timedelta(days=i % 28, hours=i)
    r = Receipt(fn=fn, fd=fd, fp=fp, qr_data=qr(fn, fd, fp, amount, when), receipt_date=when,
                total_sum=amount, operation=1, company_id=comp.id, created_by=author.id,
                merchant_name=f"Магазин {i}", merchant_inn="7701234567", status="new")
    db.add(r)
    db.flush()
    db.add(ReceiptItem(receipt_id=r.id, name=f"Товар {i}", quantity=1, price=amount, total=amount, position=0))
    return r

regs_a = [registry(i, alpha, user_a if i % 3 == 0 else acc_a, 100.0 + i) for i in range(1, 13)]
regs_b = [registry(i, beta, acc_b, 200.0 + i) for i in range(13, 21)]
blank = registry(21, alpha, acc_a, 321.0, fp="")
reg_g = registry(22, gamma, acc_g, 422.0)
donor = PoolUser(vid="rehearsal-donor", points=60)
db.add(donor)
db.flush()

def pool(i, comp, amount, triple=None):
    fn, fd, fp = triple or (f"93890000{i:08d}", str(5000 + i), str(4000000000 + i))
    when = base + timedelta(days=i)
    p = PoolReceipt(pool_user_id=donor.id, fn=fn, fd=fd, fp=fp, qr_data=qr(fn, fd, fp, amount, when),
                    receipt_date=when, total_sum=amount, operation=1, merchant_name=f"Пул {i}",
                    merchant_inn="7801234567", status="verified", full_data=True,
                    assigned_company_id=comp.id if comp else None, points_awarded=10)
    db.add(p)
    db.flush()
    db.add(PoolItem(receipt_id=p.id, name=f"Позиция {i}", quantity=1, price=amount, total=amount))
    return p

pools_a = [pool(i, alpha, 50.0 + i) for i in range(1, 4)]
pools_free = [pool(i, None, 70.0 + i) for i in range(4, 6)]
r0 = regs_a[0]
overlap = pool(6, beta, r0.total_sum + 5, triple=(r0.fn, r0.fd, r0.fp))
db.commit()
# Отказ по чужому дубликату так, как его пишет /scan старой версии: два раза
# от бухгалтера Беты и раз от сотрудника Альфы (своя компания — не заявка).
from app.services.audit import log_action
r4 = regs_a[4]
for who in (acc_b, acc_b, user_a):
    log_action(who, "receipt_duplicate", "receipt", r4.id, {"fn": r4.fn, "fd": r4.fd})
emit({"version": app_version(), "fingerprint": fingerprint(),
      "ids": {"regs_a": [r.id for r in regs_a], "regs_b": [r.id for r in regs_b], "blank": blank.id,
              "reg_g": reg_g.id, "pools_a": [p.id for p in pools_a],
              "pools_free": [p.id for p in pools_free], "overlap": overlap.id,
              "alpha": alpha.id, "beta": beta.id, "gamma": gamma.id}})
'''

UPGRADE = COMMON + r'''
from app.main import app
from app.database import SessionLocal, engine, init_db
init_db()                       # ровно то, что делает проверка целостности обновления
from sqlalchemy import inspect
from fastapi.testclient import TestClient
from app.models import FnsLog, Receipt
from app.unified.models import UnifiedDocument

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
res = {"version": app_version(),
       "unified_tables": sorted(t for t in inspect(engine).get_table_names() if t.startswith("unified_")),
       "primary_tables": sorted(t for t in inspect(engine).get_table_names() if t.startswith("primary_")),
       "fingerprint_after_init": fingerprint()}
with SessionLocal() as db:
    res["docs_after_init"] = db.query(UnifiedDocument).count()
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b", "user_a", "acc_g")}
    res["totals"] = {u: total(c, h) for u, h in H.items()}
    rid = ids["regs_a"][1]
    res["card"] = {"own": c.get(f"/api/v1/receipts/{rid}", headers=H["acc_a"]).status_code,
                   "foreign": c.get(f"/api/v1/receipts/{rid}", headers=H["acc_b"]).status_code}
    res["patch"] = {
        "plain": c.patch(f"/api/v1/receipts/{rid}", headers=H["acc_a"], json={"comment": "после обновления"}).status_code,
        "pool_twin": c.patch(f"/api/v1/receipts/{ids['regs_a'][0]}", headers=H["acc_a"],
                             json={"category": "Хозрасходы"}).status_code,
        "no_fp": c.patch(f"/api/v1/receipts/{ids['blank']}", headers=H["acc_a"], json={"comment": "без ФП"}).status_code,
        "gamma": c.patch(f"/api/v1/receipts/{ids['reg_g']}", headers=H["acc_g"], json={"comment": "Гамма"}).status_code,
    }
    from app.routers import receipts as rr
    from app.services.fns import FnsResult
    rr.check_receipt = lambda *a, **k: FnsResult(status="valid", message="Чек найден (репетиция)", ok=True,
                                                 raw={"provider": "fns", "items": []})
    vid = ids["regs_a"][2]
    rr._verify_one_sync(vid)
    with SessionLocal() as db:
        row = db.get(Receipt, vid)
        res["fns"] = {"status": row.fns_status, "log_rows": db.query(FnsLog).filter_by(receipt_id=vid).count()}
    mine = c.get("/api/v1/pool-company/mine", headers=H["acc_a"])
    res["pool_mine_a"] = mine.json().get("total") if mine.status_code == 200 else mine.status_code
    code = "t=20261020T1200&s=555.00&fn=9289000099999999&i=777&fp=3999999999&n=1"
    first = c.post("/api/v1/receipts/scan", headers=H["acc_a"], json={"qr_data": code, "source": "web"})
    second = c.post("/api/v1/receipts/scan", headers=H["acc_b"], json={"qr_data": code, "source": "web"})
    res["scan"] = {"new": first.status_code, "new_id": (first.json().get("receipt") or {}).get("id"),
                   "foreign": second.status_code, "foreign_body": short(second)}
    claims = c.get("/api/v1/unified/claims", headers=H["admin"]).json()
    res["claims"] = {"total": claims.get("total"),
                     "companies": sorted(i["company_id"] for i in claims.get("items", [])),
                     "sources": sorted({i["source"] for i in claims.get("items", [])}),
                     "staff": c.get("/api/v1/unified/claims", headers=H["acc_b"]).status_code}
    res["unified_stats"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
    res["about"] = c.get("/api/v1/about").json().get("version")
from app.unified import backfill
with SessionLocal() as db:
    res["backfill_dry_run"] = backfill.backfill(db, dry_run=True)
emit(res)
'''

ROLLBACK = COMMON + r'''
from app.main import app
from app.database import init_db
init_db()                       # старая версия стартует на обновлённой базе
from fastapi.testclient import TestClient

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
res = {"version": app_version()}
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b", "user_a")}
    res["totals"] = {u: total(c, h) for u, h in H.items()}
    res["card"] = c.get(f"/api/v1/receipts/{ids['regs_a'][1]}", headers=H["acc_a"]).status_code
    res["move"] = c.post("/api/v1/receipts/move", headers=H["admin"],
                         json={"receipt_ids": [ids["regs_a"][1]], "company_id": ids["beta"]}).status_code
    res["delete"] = c.delete(f"/api/v1/receipts/{ids['regs_a'][2]}", headers=H["admin"]).status_code
    res["patch"] = c.patch(f"/api/v1/receipts/{ids['blank']}", headers=H["acc_a"],
                           json={"total_sum": 333.0}).status_code
    gone = c.post(f"/api/v1/companies/{ids['gamma']}/delete", headers=H["admin"],
                  json={"mode": "move", "target_company_id": ids["beta"], "move_users": True})
    res["delete_company"] = {"status": gone.status_code, "body": short(gone)}
    res["totals_after"] = {u: total(c, h) for u, h in H.items()}
emit(res)
'''

REUPGRADE = COMMON + r'''
import logging
logging.basicConfig(level=logging.INFO)
from app.main import app
from app.database import SessionLocal, init_db
init_db()                       # сверка канона с изменениями старой версии
from fastapi.testclient import TestClient
from app.models import Receipt
from app.unified import service
from app.unified.models import UnifiedDocument, UnifiedEvent

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
res = {"version": app_version()}
with SessionLocal() as db:
    res["reconcile_events"] = db.query(UnifiedEvent).filter_by(event_type="reconcile").count()
    moved_doc = service.document_for_source(db, service.REGISTRY, ids["regs_a"][1])
    gamma_doc = service.document_for_source(db, service.REGISTRY, ids["reg_g"])
    deleted = db.query(UnifiedDocument).filter(UnifiedDocument.deleted_at.isnot(None)).count()
    res["canonical"] = {"moved_owner_is_beta": moved_doc.ownership.company_id == ids["beta"],
                        "gamma_owner_is_beta": gamma_doc.ownership.company_id == ids["beta"],
                        "closed_documents": deleted}
    moved_doc_id = moved_doc.id
    # v1.70.88 (фаза 4а): правка старой версии — в единой записи уже при старте
    from app.unified.copies import candidates
    blank_doc = service.document_for_source(db, service.REGISTRY, ids["blank"])
    reasons = [json.loads(e.details or "{}").get("reason") for e in db.query(UnifiedEvent)
               .filter_by(document_id=blank_doc.id, event_type="reconcile")] if blank_doc else []
    res["phase4a"] = {"blank_canon_at_start": float(blank_doc.total_sum) if blank_doc else None,
                      "legacy_edit": "legacy_edit" in reasons,
                      "drift": {k: len(v) for k, v in candidates(db).items()}}
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b", "user_a")}
    res["totals"] = {u: total(c, h) for u, h in H.items()}
    res["phase4a"]["readiness_admin"] = c.get("/api/v1/unified/readiness", headers=H["admin"]).status_code
    res["phase4a"]["readiness_acc"] = c.get("/api/v1/unified/readiness", headers=H["acc_a"]).status_code
    moved = ids["regs_a"][1]
    res["moved"] = {"beta_card": c.get(f"/api/v1/receipts/{moved}", headers=H["acc_b"]).status_code,
                    "alpha_card": c.get(f"/api/v1/receipts/{moved}", headers=H["acc_a"]).status_code,
                    "alpha_unified": c.get(f"/api/v1/unified/documents/{moved_doc_id}", headers=H["acc_a"]).status_code,
                    "beta_unified": c.get(f"/api/v1/unified/documents/{moved_doc_id}", headers=H["acc_b"]).status_code}
    res["gamma_receipt_beta_card"] = c.get(f"/api/v1/receipts/{ids['reg_g']}", headers=H["acc_b"]).status_code
    res["patch_after_rollback"] = c.patch(f"/api/v1/receipts/{ids['blank']}", headers=H["acc_a"],
                                          json={"comment": "снова на новой версии"}).status_code
    res["move_no_fp"] = c.post("/api/v1/receipts/move", headers=H["admin"],
                               json={"receipt_ids": [ids["blank"]], "company_id": ids["beta"]}).status_code
    back = c.post("/api/v1/receipts/move", headers=H["admin"],
                  json={"receipt_ids": [moved], "company_id": ids["alpha"]})
    res["move_back"] = {"status": back.status_code,
                        "alpha_card": c.get(f"/api/v1/receipts/{moved}", headers=H["acc_a"]).status_code}
    res["unified_stats"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
with SessionLocal() as db:
    res["blank_total_synced"] = db.get(Receipt, ids["blank"]).total_sum == service.document_for_source(
        db, service.REGISTRY, ids["blank"]).total_sum
emit(res)
'''

BACKFILL = COMMON + r'''
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
from app.unified import backfill

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
res = {"fingerprint_before": fingerprint()}
with SessionLocal() as db:
    res["dry_run"] = backfill.backfill(db, dry_run=True)
    res["run"] = backfill.backfill(db)
    res["again"] = backfill.backfill(db)
res["fingerprint_after"] = fingerprint()
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b", "user_a")}
    res["totals"] = {u: total(c, h) for u, h in H.items()}
    res["stats"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
    twin = ids["regs_a"][0]
    res["twin"] = {"alpha": c.get(f"/api/v1/receipts/{twin}", headers=H["acc_a"]).status_code,
                   "admin": c.get(f"/api/v1/receipts/{twin}", headers=H["admin"]).status_code}
    # v1.70.85: отказы /scan старой версии из журнала аудита — заявка Беты
    from app.unified import service
    with SessionLocal() as db:
        r4_doc = service.document_for_source(db, service.REGISTRY, ids["regs_a"][4]).id
    r4 = c.get(f"/api/v1/unified/documents/{r4_doc}/companies", headers=H["admin"]).json()
    res["history_claim"] = {"count": r4.get("count"), "claimants": [
        (x["company_id"] == ids["beta"], x["status"], x["source"], x["attempts"])
        for x in r4.get("companies", []) if x["role"] == "claimant"]}
    # v1.70.83: спор о владельце разбирается на экране «Разбор конфликтов» (#9):
    # список с фильтром, карточка с кандидатами, решение в пользу компании реестра.
    listed = c.get("/api/v1/unified/documents", headers=H["admin"],
                   params={"conflicts": "true", "flag": 8, "per_page": 100}).json()
    review = {"listed": listed.get("total")}
    if listed.get("items"):
        doc_id = listed["items"][0]["id"]
        card = c.get(f"/api/v1/unified/documents/{doc_id}/review", headers=H["admin"]).json()
        target = next((x["company_id"] for x in card.get("candidates", []) if "registry" in x["sources"]), None)
        review.update({"candidates": len(card.get("candidates", [])), "can_resolve": card.get("can_resolve")})
        resolved = c.post(f"/api/v1/unified/documents/{doc_id}/resolve-ownership", headers=H["admin"],
                          json={"company_id": target, "expected_version": card.get("version"),
                                "reason": "Репетиция: чек реестра Альфы"})
        review["resolve"] = resolved.status_code
        review["alpha_after"] = c.get(f"/api/v1/receipts/{twin}", headers=H["acc_a"]).status_code
        review["stats_after"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
        loser = c.get(f"/api/v1/unified/documents/{doc_id}/companies", headers=H["admin"]).json()
        review["companies_after"] = {"count": loser.get("count"), "claimants": [
            (x["company_id"] == ids["beta"], x["status"], x["source"])
            for x in loser.get("companies", []) if x["role"] == "claimant"]}
    res["review"] = review
emit(res)
'''

OLD_AFTER_REVIEW = COMMON + r'''
from app.main import app
from app.database import init_db
init_db()
from fastapi.testclient import TestClient

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b")}
    twin = ids["regs_a"][0]
    emit({"version": app_version(), "totals": {u: total(c, h) for u, h in H.items()},
          "twin_alpha": c.get(f"/api/v1/receipts/{twin}", headers=H["acc_a"]).status_code})
'''

# v1.70.84: пакетный перенос (T-33) и журнал карточки без чужих организаций
# (T-32) на обновлённой базе — после отката и повторного обновления.
BULK = COMMON + r'''
import time
from datetime import datetime, timedelta
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
from app.models import Company, Receipt, ReceiptItem, User
from app.unified import service

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
res = {"version": app_version()}

def doc_of(db, rid):
    return service.document_for_source(db, service.REGISTRY, rid)

def move(c, headers, receipt_ids, company_id, **extra):
    started = time.perf_counter()
    resp = c.post("/api/v1/receipts/move", headers=headers,
                  json={"receipt_ids": receipt_ids, "company_id": company_id, **extra})
    body = resp.json() if resp.status_code == 200 else {"detail": short(resp)}
    return {"status": resp.status_code, "seconds": round(time.perf_counter() - started, 3),
            "moved": body.get("moved"), "already_there": body.get("already_there"),
            "detail": body.get("detail")}

def brief(event):
    return {k: event.get(k) for k in ("type", "from_company_id", "from_company_hidden", "to_company_id",
                                      "to_company_hidden", "actor_id", "actor_hidden")}

with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b")}
    res["totals_before"] = {u: total(c, h) for u, h in H.items()}
    res["conflicts_before"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json().get("conflicts")
    alpha = [r["id"] for r in c.get("/api/v1/receipts", headers=H["acc_a"],
                                    params={"page_size": 200}).json()["items"]]
    with SessionLocal() as db:
        fresh = [rid for rid in alpha if doc_of(db, rid) is None]
    res.update({"alpha": alpha, "unregistered": len(fresh)})
    # Смешанная пачка: чеки Альфы с единой записью и без неё плюс два чека,
    # которые уже у Беты (чек без ФП и чек Беты), — они пропускаются.
    res["there"] = move(c, H["admin"], alpha + [ids["blank"], ids["regs_b"][0]], ids["beta"],
                        assignee="Репетиция")
    res["totals_there"] = {u: total(c, h) for u, h in H.items()}
    with SessionLocal() as db:
        rows = db.query(Receipt).filter(Receipt.id.in_(alpha)).all()
        res["assignee_all"] = len(rows) == len(alpha) and all(
            r.company_id == ids["beta"] and r.assignee == "Репетиция" for r in rows)
        alpha_docs = {doc_of(db, rid).id for rid in alpha if doc_of(db, rid) is not None}
        res["registered_after"] = len(alpha_docs)
        card_id = doc_of(db, fresh[0]).id if fresh else None
    if card_id:
        staff = c.get(f"/api/v1/unified/documents/{card_id}", headers=H["acc_b"]).json()
        admin = c.get(f"/api/v1/unified/documents/{card_id}", headers=H["admin"]).json()
        res["card_staff"] = {"created_by": staff.get("created_by"),
                             "created_by_hidden": staff.get("created_by_hidden"),
                             "events": [brief(e) for e in staff.get("events", [])]}
        res["card_admin"] = [brief(e) for e in admin.get("events", [])]
    listed = [d for d in c.get("/api/v1/unified/documents", headers=H["acc_b"],
                               params={"per_page": 100}).json().get("items", []) if d["id"] in alpha_docs]
    res["staff_list"] = {"listed": len(listed), "authors_hidden": bool(listed) and all(
        d.get("created_by") is None and d.get("created_by_hidden") is True for d in listed)}
    # Обратно одной операцией — вместе с чеком без ФП (он изначально у Альфы).
    res["back"] = move(c, H["admin"], alpha + [ids["blank"]], ids["alpha"])
    res["totals_back"] = {u: total(c, h) for u, h in H.items()}
    # Объём: 1000 чеков (наибольшая пачка API), записанных как старой версией —
    # без единой записи. Первый перенос регистрирует их, обратный — повторный.
    with SessionLocal() as db:
        author = db.query(User).filter_by(username="admin").one()
        delta = Company(name="ООО «Дельта»")
        db.add(delta)
        db.flush()
        base, rows = datetime(2026, 9, 1, 9, 0), []
        for i in range(1000):
            fn, fd, fp = f"94890000{i:08d}", str(20000 + i), str(5000000000 + i)
            when, amount = base + timedelta(minutes=17 * i), 150.0 + i % 50
            rows.append(Receipt(fn=fn, fd=fd, fp=fp, receipt_date=when, total_sum=amount, operation=1,
                                qr_data=f"t={when:%Y%m%dT%H%M}&s={amount:.2f}&fn={fn}&i={fd}&fp={fp}&n=1",
                                company_id=delta.id, created_by=author.id, merchant_name=f"Склад {i % 7}",
                                merchant_inn="7701234567", status="new"))
        db.add_all(rows)
        db.flush()
        db.add_all([ReceiptItem(receipt_id=r.id, name="Позиция", quantity=1, price=r.total_sum,
                                total=r.total_sum, position=0) for r in rows])
        db.commit()
        volume, delta_id = [r.id for r in rows], delta.id
    res["volume_first"] = move(c, H["admin"], volume, ids["beta"])
    res["volume_again"] = move(c, H["admin"], volume, delta_id)
    with SessionLocal() as db:
        res["volume_home"] = db.query(Receipt).filter(Receipt.company_id == delta_id).count()
    res["conflicts_after"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json().get("conflicts")
    res["totals_final"] = {u: total(c, h) for u, h in H.items()}
emit(res)
'''

# v1.70.85: заявки компаний на чужие чеки (T-34) на основной базе — после
# пакетных переносов; затем откат на старую версию и повторное обновление.
CLAIMS = COMMON + r'''
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
from app.models import Receipt
from app.unified import service

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
alpha_ids, upgrade_scan = json.loads(os.environ["REH_BULK"]), os.environ["REH_SCAN"]
res = {"version": app_version()}

def doc_id(rid):
    with SessionLocal() as db:
        return service.document_for_source(db, service.REGISTRY, rid).id

def code_of(rid):
    with SessionLocal() as db:
        r = db.get(Receipt, rid)
        return f"t={r.receipt_date:%Y%m%dT%H%M}&s={r.total_sum:.2f}&fn={r.fn}&i={r.fd}&fp={r.fp}&n=1"

with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b")}
    companies = lambda rid: c.get(f"/api/v1/unified/documents/{doc_id(rid)}/companies",
                                  headers=H["admin"]).json()
    res["totals_before"] = {u: total(c, h) for u, h in H.items()}
    res["open_before"] = c.get("/api/v1/unified/claims", headers=H["admin"]).json().get("total")
    first = companies(upgrade_scan)
    res["upgrade_scan"] = {"count": first.get("count"),
                           "roles": sorted(x["role"] for x in first.get("companies", []))}
    # Не из первых трёх чеков Альфы — их потом переносит старая версия.
    give, keep = alpha_ids[-1], alpha_ids[-2]
    res["replies"] = [short(c.post("/api/v1/receipts/scan", headers=H["acc_b"],
                                   json={"qr_data": code_of(rid), "source": "web"})) for rid in (give, keep)]
    listed = c.get("/api/v1/unified/claims", headers=H["admin"], params={"per_page": 100}).json()
    mine = {i["document_id"]: i for i in listed.get("items", [])}
    res["listed"] = listed.get("total")
    res["staff"] = sorted({c.get("/api/v1/unified/claims", headers=H[u]).status_code for u in ("acc_a", "acc_b")}
                          | {c.get(f"/api/v1/unified/documents/{doc_id(give)}/companies",
                                   headers=H["acc_a"]).status_code})
    res["count_open"] = companies(give).get("count")

    def decide(rid, action, reason):
        item = mine.get(doc_id(rid)) or {}
        return c.post(f"/api/v1/unified/links/{item.get('link_id')}/{action}", headers=H["admin"],
                      json={"expected_version": item.get("version"), "reason": reason}).status_code

    res["decline"] = decide(keep, "decline", "Репетиция: чек оплачен Альфой")
    res["transfer"] = decide(give, "transfer", "Репетиция: чек оплачен Бетой")
    res["count_declined"] = companies(keep).get("count")
    after = companies(give)
    res["after_transfer"] = {"count": after.get("count"), "roles": sorted(
        (x["role"], x["company_id"] == ids["alpha"]) for x in after.get("companies", []))}
    with SessionLocal() as db:
        res["give_in_beta"] = db.get(Receipt, give).company_id == ids["beta"]
    staff_cards = [c.get(f"/api/v1/unified/documents/{doc_id(give)}", headers=H["acc_b"]).json(),
                   c.get(f"/api/v1/unified/documents/{doc_id(keep)}", headers=H["acc_a"]).json()]
    res["staff_events"] = sorted({e["type"] for card in staff_cards for e in card.get("events", [])})
    feed = c.get("/api/v1/dashboard/recent", headers=H["acc_a"], params={"limit": 100}).json()
    res["feed_claims"] = sum(1 for i in feed if str(i.get("action", "")).startswith("unified_claim_"))
    res["stats"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
    res["totals_final"] = {u: total(c, h) for u, h in H.items()}

    # v1.70.86 (фаза 3): число организаций компаниям — до разрешения поля нет;
    # «всем компаниям» (бухгалтер и руководитель) — только число и по своим чекам
    def field(user, rid):
        r = c.get(f"/api/v1/receipts/{rid}", headers=H[user])
        return r.json().get("companies_count", "нет") if r.status_code == 200 else r.status_code

    res["vis_before"] = {"admin_keep": field("admin", keep), "acc_a_keep": field("acc_a", keep),
                         "acc_b_give": field("acc_b", give)}
    res["vis_put"] = c.put("/api/v1/unified/visibility", headers=H["admin"],
                           json={"mode": "all", "roles": ["accountant", "manager"]}).status_code
    res["vis_field"] = {"acc_a_keep": field("acc_a", keep), "acc_b_give": field("acc_b", give),
                        "acc_b_keep": field("acc_b", keep)}
    multi = c.get("/api/v1/receipts", headers=H["admin"],
                  params={"multi_company": "true", "page_size": 200}).json()
    found = {x["id"] for x in multi.get("items", [])}
    res["vis_multi"] = {"keep": keep in found, "give": give in found}
res["give"], res["keep"] = give, keep
emit(res)
'''

# Повторное обновление после отката: заявки и число организаций на месте,
# новая заявка пишется как обычно.
CLAIMS_AGAIN = COMMON + r'''
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
from app.models import Receipt
from app.unified import service

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
cl = json.loads(os.environ["REH_CLAIMS"])
res = {"version": app_version()}

def doc_id(rid):
    with SessionLocal() as db:
        return service.document_for_source(db, service.REGISTRY, rid).id

with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b")}
    res["totals"] = {u: total(c, h) for u, h in H.items()}
    res["declined"] = c.get("/api/v1/unified/claims", headers=H["admin"],
                            params={"status": "declined"}).json().get("total")
    res["counts"] = [c.get(f"/api/v1/unified/documents/{doc_id(rid)}/companies",
                           headers=H["admin"]).json().get("count") for rid in (cl["give"], cl["keep"])]
    with SessionLocal() as db:
        r = db.get(Receipt, cl["give"])
        code = f"t={r.receipt_date:%Y%m%dT%H%M}&s={r.total_sum:.2f}&fn={r.fn}&i={r.fd}&fp={r.fp}&n=1"
    res["again_reply"] = short(c.post("/api/v1/receipts/scan", headers=H["acc_a"],
                                      json={"qr_data": code, "source": "web"}))
    res["open_after"] = c.get("/api/v1/unified/claims", headers=H["admin"]).json().get("total")
    res["stats"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
    # v1.70.86 (фаза 3): показ числа пережил откат; компания старой версии — без флага
    settings = c.get("/api/v1/unified/visibility", headers=H["admin"]).json()
    keep = c.get(f"/api/v1/receipts/{cl['keep']}", headers=H["acc_a"])
    res["vis"] = {"mode": settings.get("mode"), "roles": settings.get("roles"),
                  "keep_acc_a": keep.json().get("companies_count", "нет") if keep.status_code == 200
                  else keep.status_code,
                  "late_flag": [x.get("enabled") for x in settings.get("companies", [])
                                if x.get("name") == "Компания после отката"]}
emit(res)
'''

# Откат после пакетного переноса: старая версия открывает основную базу,
# видит перенесённые чеки и сама переносит их своим прежним циклом.
OLD_AFTER_BULK = COMMON + r'''
from app.main import app
from app.database import init_db
init_db()
from fastapi.testclient import TestClient

ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
moved = json.loads(os.environ["REH_BULK"])
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b")}
    res = {"version": app_version(), "totals": {u: total(c, h) for u, h in H.items()},
           "cards": sorted({c.get(f"/api/v1/receipts/{rid}", headers=H["acc_a"]).status_code for rid in moved})}
    res["move"] = c.post("/api/v1/receipts/move", headers=H["admin"],
                         json={"receipt_ids": moved[:3], "company_id": ids["beta"]}).status_code
    given = os.environ.get("REH_GIVEN")         # v1.70.85: чек, переданный Бете по заявке
    if given:
        res["given_beta"] = c.get(f"/api/v1/receipts/{given}", headers=H["acc_b"]).status_code
    # v1.70.86: старая версия не знает колонки показа числа — компания всё равно создаётся
    res["company_created"] = c.post("/api/v1/companies", headers=H["admin"],
                                    json={"name": "Компания после отката"}).status_code
    res["totals_after"] = {u: total(c, h) for u, h in H.items()}
emit(res)
'''


def sqlite_copy(src: Path, dst: Path) -> None:
    """Согласованная копия SQLite (WAL учитывается) — как резервная копия программы."""
    import sqlite3
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)


def run_phase(name: str, code: str, code_dir: Path, env: dict) -> dict:
    proc = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-c", code], cwd=code_dir,
                          env={**env, "PYTHONPATH": str(code_dir)}, capture_output=True,
                          text=True, timeout=600, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(MARK)]
    if proc.returncode != 0 or not lines:
        tail = (proc.stdout + "\n" + proc.stderr)[-3000:]
        raise RuntimeError(f"фаза «{name}» упала (код {proc.returncode}):\n{tail}")
    return json.loads(lines[-1][len(MARK):])


def check(report: list, ok: bool, what: str) -> None:
    report.append({"ok": bool(ok), "check": what})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--from-ref", default="b3db4fe", help="коммит установленной версии")
    ap.add_argument("--keep", action="store_true", help="не удалять временный каталог")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="ym-rehearsal-"))
    old_dir, new_dir = work / "old", work / "new"
    old_dir.mkdir()
    archive = subprocess.run(["git", "archive", args.from_ref], cwd=ROOT, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(old_dir)], input=archive.stdout, check=True)
    # новая версия — копия рабочего дерева: её data/ не смешивается с data/ разработчика
    shutil.copytree(ROOT, new_dir, ignore=shutil.ignore_patterns(
        ".git", ".venv", "venv", "node_modules", "data", "*.db", "__pycache__", "test-results"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SUPABASE_", "DATABASE_URL"))}
    env.update({"DATABASE_URL": f"sqlite:///{work / 'rehearsal.db'}", "YM_NO_REEXEC": "1",
                "YM_FETCHQ_OFF": "1", "YM_OSMON_OFF": "1", "SECRET_KEY": secrets.token_hex(32),
                "REH_PASSWORD": "Rehearsal-" + secrets.token_hex(6)})
    checks: list = []
    out: dict = {"from_ref": args.from_ref, "workdir": str(work)}
    try:
        seed = run_phase("старая версия: база", SEED, old_dir, env)
        env["REH_IDS"] = json.dumps(seed["ids"])
        out["seed"] = seed
        up = run_phase("обновление", UPGRADE, new_dir, env)
        out["upgrade"] = up
        check(checks, len(up["unified_tables"]) == 6
              and up.get("primary_tables") == ["primary_document_lines", "primary_documents"],
              "созданы 6 таблиц unified_* (с v1.70.85 — заявки компаний unified_company_links) "
              "и 2 таблицы первички (с v1.70.94)")
        check(checks, up["docs_after_init"] == 0, "перенос архива при старте не запускается")
        check(checks, up["fingerprint_after_init"] == seed["fingerprint"], "старые таблицы после init_db не изменились")
        check(checks, up["totals"] == {"admin": 22, "acc_a": 13, "acc_b": 8, "user_a": 4, "acc_g": 1},
              "списки по ролям как до обновления")
        check(checks, up["card"] == {"own": 200, "foreign": 404}, "карточка: своя 200, чужая 404")
        check(checks, all(v == 200 for v in up["patch"].values()), "правки старых чеков проходят")
        check(checks, up["fns"] == {"status": "valid", "log_rows": 1}, "результат ФНС сохранён, журнал ФНС ведётся")
        check(checks, up["pool_mine_a"] == 3, "Чек-Пул: закреплённые чеки компании на месте")
        check(checks, up["scan"]["new"] == 200 and up["scan"]["new_id"], "новый скан принят")
        check(checks, (up["scan"]["foreign_body"] or {}).get("duplicate") is True
              and "другой организации" in str(up["scan"]["foreign_body"]), "чужой дубликат отклонён без раскрытия данных")
        check(checks, up["unified_stats"].get("conflicts") == 0, "единый реестр: ложных конфликтов нет")
        check(checks, "Заявка передана администратору" in str(up["scan"]["foreign_body"])
              and up["claims"] == {"total": 1, "companies": [seed["ids"]["beta"]], "sources": ["scan_qr"],
                                   "staff": 403}
              and up["unified_stats"].get("claims_open") == 1,
              "чужой дубликат: «Заявка передана администратору», заявка Беты у администратора, "
              "бухгалтеру Беты список заявок недоступен")
        check(checks, up["about"] == up["version"], "версия в /api/v1/about")
        back = run_phase("откат на старую версию", ROLLBACK, old_dir, env)
        out["rollback"] = back
        check(checks, back["version"] == seed["version"], "откат: запущена старая версия")
        check(checks, back["totals"]["admin"] == 23, "откат: все чеки на месте (включая новый скан)")
        check(checks, back["card"] == 200 and back["move"] == 200 and back["delete"] == 200
              and back["patch"] == 200, "откат: старая версия правит, переносит и удаляет чеки")
        check(checks, back["delete_company"]["status"] == 200, "откат: старая версия удаляет компанию")
        again = run_phase("повторное обновление", REUPGRADE, new_dir, env)
        out["reupgrade"] = again
        check(checks, again["canonical"]["moved_owner_is_beta"] and again["canonical"]["gamma_owner_is_beta"],
              "сверка: владелец в едином реестре догнал перенос старой версией")
        check(checks, again["canonical"]["closed_documents"] >= 1, "сверка: удалённый старой версией чек закрыт")
        check(checks, again["moved"] == {"beta_card": 200, "alpha_card": 404, "alpha_unified": 404, "beta_unified": 200},
              "перенесённый чек виден новой компании и не виден прежней")
        check(checks, again["gamma_receipt_beta_card"] == 200, "чеки удалённой компании видны компании-получателю")
        check(checks, again["patch_after_rollback"] == 200 and again["blank_total_synced"],
              "правка после повторного обновления синхронизирует единую запись")
        legacy = not (Path(old_dir) / "app" / "unified").is_dir()
        p4 = again["phase4a"]
        check(checks, p4["blank_canon_at_start"] == 333.0 and p4["legacy_edit"] == legacy
              and p4["drift"] == {"registry": 0, "pool": 0}
              and p4["readiness_admin"] == 200 and p4["readiness_acc"] == 403,
              "фаза 4а: правка суммы старой версией (333 ₽) — в единой записи уже при старте "
              + ("(сверка копий, событие legacy_edit)" if legacy else "(синхронизировала сама старая версия)")
              + "; копии не расходятся; отчёт готовности — только администратору")
        check(checks, again["move_back"] == {"status": 200, "alpha_card": 200}, "перенос обратно работает")
        check(checks, again["move_no_fp"] == 200, "чек без ФП переносится между компаниями, как в 1.70.80")
        # v1.70.84: пакетный перенос (T-33) и журнал карточки без чужих организаций (T-32)
        bulk = run_phase("пакетный перенос", BULK, new_dir, env)
        out["bulk"] = bulk
        ids, n = seed["ids"], len(bulk["alpha"])
        before, there, ret = bulk["totals_before"], bulk["there"], bulk["back"]
        check(checks, there["status"] == 200 and there["moved"] == n and there["already_there"] == 2
              and bulk["unregistered"] >= 1 and bulk["registered_after"] == n,
              f"пакетный перенос: {n} чеков Альфы в Бету одной операцией ({bulk['unregistered']} ещё без "
              "единой записи), 2 чека, уже лежащие у Беты, пропущены")
        check(checks, bulk["assignee_all"] and bulk["totals_there"]["acc_a"] == 0
              and bulk["totals_there"]["acc_b"] == before["acc_b"] + n,
              f"после переноса все {n} чеков видит Бета, Альфа — ни одного; подотчётное лицо назначено у всех")
        staff = bulk.get("card_staff") or {}
        ev_s = {e["type"]: e for e in staff.get("events", [])}
        ev_a = {e["type"]: e for e in bulk.get("card_admin", [])}
        mv_s, rg_s, mv_a = ev_s.get("move", {}), ev_s.get("register", {}), ev_a.get("move", {})
        check(checks, staff.get("created_by") is None and staff.get("created_by_hidden") is True
              and mv_s.get("from_company_id") is None and mv_s.get("from_company_hidden") is True
              and mv_s.get("to_company_id") == ids["beta"] and mv_s.get("actor_hidden") is True
              and rg_s.get("to_company_id") is None and rg_s.get("to_company_hidden") is True
              and mv_a.get("from_company_id") == ids["alpha"] and mv_a.get("to_company_id") == ids["beta"]
              and bool(mv_a.get("actor_id")),
              "журнал карточки у бухгалтера Беты: Альфа, автор из Альфы и администратор скрыты; "
              "администратор видит перенос полностью")
        check(checks, bulk["staff_list"]["listed"] == n and bulk["staff_list"]["authors_hidden"],
              f"список единого реестра у бухгалтера Беты: {n} перенесённых чеков, авторы из Альфы скрыты")
        check(checks, ret["status"] == 200 and ret["moved"] == n + 1
              and bulk["totals_back"]["acc_a"] == n + 1 and bulk["totals_back"]["acc_b"] == before["acc_b"] - 1,
              f"обратно одной операцией: {n + 1} чеков снова у Альфы, включая чек без ФП")
        vf, va = bulk["volume_first"], bulk["volume_again"]
        check(checks, vf["status"] == 200 and vf["moved"] == 1000 and va["status"] == 200
              and va["moved"] == 1000 and bulk["volume_home"] == 1000
              and vf["seconds"] < 3.0 and va["seconds"] < 2.0,
              f"1000 старых чеков одной операцией: первый перенос {vf['seconds']} с, обратный "
              f"{va['seconds']} с (потолки 3 с и 2 с; прежний цикл — 9,6 с и 4,9 с)")
        check(checks, bulk["conflicts_before"] == 0 and bulk["conflicts_after"] == 0,
              "единый реестр после пакетных переносов: ложных конфликтов нет")
        # необязательный перенос архива — только на копии базы, для отчёта
        sqlite_copy(work / "rehearsal.db", work / "backfill-copy.db")
        bf = run_phase("перенос архива на копии", BACKFILL, new_dir,
                       {**env, "DATABASE_URL": f"sqlite:///{work / 'backfill-copy.db'}"})
        out["backfill_copy"] = bf
        pending = bf["dry_run"]["registry_pending"] + bf["dry_run"]["pool_pending"]
        check(checks, bf["run"]["registered"] == pending and bf["again"]["registered"] == 0,
              f"перенос архива на копии: {pending} источников, повторный запуск ничего не меняет")
        dry_c, run_c, again_c = (bf[k].get("claims", {}) for k in ("dry_run", "run", "again"))
        audit = (dry_c.get("audit_rows_pending"), dry_c.get("audit_claims"), dry_c.get("audit_skipped"))
        if tuple(int(x) for x in seed["version"].split(".")[:3]) >= (1, 70, 85):
            # С 1.70.85 заявки пишет сам приём: старая версия при первом старте ставит
            # границу журнала, а отказы сида записаны после неё — наполнение их не трогает.
            check(checks, audit == (0, 0, 0) and run_c.get("audit_claims") == 0
                  and again_c.get("audit_rows_pending") == 0
                  and bf["history_claim"] == {"count": 1, "claimants": []},
                  f"заявки задним числом: база v{seed['version']} уже пишет заявки сама — записи журнала "
                  "после её границы наполнение не трогает, повторный запуск ничего не добавляет")
        else:
            check(checks, audit == (3, 1, 1) and run_c.get("audit_claims") == 1
                  and again_c.get("audit_rows_pending") == 0
                  and bf["history_claim"] == {"count": 2, "claimants": [[True, "open", "scan_qr", 2]]},
                  "заявки задним числом: два отказа /scan старой версии — одна заявка Беты (2 попытки), "
                  "отказ своей компании пропущен, повторный запуск ничего не добавляет")
        check(checks, bf["fingerprint_after"] == bf["fingerprint_before"], "перенос архива не меняет старые таблицы")
        check(checks, bf["stats"]["ownership_conflicts"] == 1 and bf["twin"] == {"alpha": 404, "admin": 200},
              "исторический двойник (реестр Альфы + пул Беты) уходит на разбор администратору")
        rv = bf["review"]
        check(checks, rv.get("listed") == 1 and rv.get("candidates") == 2 and rv.get("can_resolve") is True,
              "разбор конфликтов: спор виден в списке, в карточке две компании-кандидата")
        check(checks, rv.get("resolve") == 200 and rv.get("alpha_after") == 200
              and rv.get("stats_after", {}).get("ownership_conflicts") == 0,
              "решение спора в пользу Альфы: чек снова виден Альфе, споров о владельце нет")
        check(checks, rv.get("companies_after") == {"count": 2, "claimants": [[True, "declined", "archive_twin"]]},
              "проигравшая сторона спора (Бета) — заявка «отклонена», в числе организаций 2")
        old = run_phase("откат после разбора", OLD_AFTER_REVIEW, old_dir,
                        {**env, "DATABASE_URL": f"sqlite:///{work / 'backfill-copy.db'}"})
        out["rollback_after_review"] = old
        check(checks, old["twin_alpha"] == 200 and all(isinstance(v, int) for v in old["totals"].values()),
              f"откат на v{old['version']} после разбора: вход и списки работают, чек у Альфы")
        # v1.70.85: заявки компаний на основной базе (T-34)
        cl = run_phase("заявки компаний", CLAIMS, new_dir,
                       {**env, "REH_BULK": json.dumps(bulk["alpha"]), "REH_SCAN": up["scan"]["new_id"]})
        out["claims"] = cl
        check(checks, cl["open_before"] == 0 and cl["upgrade_scan"] == {"count": 1, "roles": ["former_owner", "owner"]},
              "заявка из обновления исполнена пакетным переносом в Бету; после возврата Бета — "
              "прежний владелец, в числе организаций 1")
        check(checks, all(r.get("duplicate") is True and "Заявка передана администратору" in str(r)
                          for r in cl["replies"]) and cl["listed"] == 2 and cl["staff"] == [403]
              and cl["count_open"] == 2,
              "две заявки Беты: администратор видит список и число 2, бухгалтерам Альфы и Беты — 403")
        check(checks, cl["decline"] == 200 and cl["count_declined"] == 2 and cl["transfer"] == 200
              and cl["give_in_beta"] and cl["after_transfer"] == {
                  "count": 1, "roles": [["former_owner", True], ["owner", False]]},
              "«отклонить» оставляет число 2; «передать» переносит чек в Бету, Альфа — прежний владелец")
        check(checks, not any(t.startswith("claim_") for t in cl["staff_events"]) and cl["feed_claims"] == 0
              and cl["stats"].get("claims_open") == 0 and cl["stats"].get("conflicts") == 0
              and cl["totals_final"]["acc_a"] == cl["totals_before"]["acc_a"] - 1
              and cl["totals_final"]["acc_b"] == cl["totals_before"]["acc_b"] + 1,
              "решения по заявкам не видны сотрудникам (журнал карточки, лента дашборда); "
              "заявок и конфликтов не осталось")
        # v1.70.86 (фаза 3): число организаций компаниям
        check(checks, cl["vis_before"] == {"admin_keep": 2, "acc_a_keep": "нет", "acc_b_give": "нет"}
              and cl["vis_put"] == 200
              and cl["vis_field"] == {"acc_a_keep": 2, "acc_b_give": 1, "acc_b_keep": 404}
              and cl["vis_multi"] == {"keep": True, "give": False},
              "показ числа компаниям: до разрешения поля нет; «всем компаниям» — бухгалтер Альфы видит 2 "
              "(Бета отклонена), Беты — 1 у переданного чека (прежний владелец не в числе), чужой чек — 404; "
              "фильтр администратора находит чек двух организаций")
        # v1.70.84: откат после пакетного переноса — основная база, старая версия
        # (с v1.70.85 — и после заявок: таблица связей старой версии не мешает)
        oldb = run_phase("откат после пакетного переноса и заявок", OLD_AFTER_BULK, old_dir,
                         {**env, "REH_BULK": json.dumps([r for r in bulk["alpha"] if r != cl["give"]]),
                          "REH_GIVEN": cl["give"]})
        out["rollback_after_bulk"] = oldb
        check(checks, oldb["totals"] == cl["totals_final"] and oldb["cards"] == [200]
              and oldb.get("given_beta") == 200 and oldb.get("company_created") in (200, 201)
              and oldb["move"] == 200 and oldb["totals_after"]["acc_b"] == oldb["totals"]["acc_b"] + 3,
              f"откат на v{oldb['version']} после пакетного переноса и заявок: списки и карточки те же, "
              "переданный по заявке чек виден Бете, старая версия сама переносит чеки и создаёт компанию")
        ca = run_phase("повторное обновление после заявок", CLAIMS_AGAIN, new_dir,
                       {**env, "REH_CLAIMS": json.dumps({"give": cl["give"], "keep": cl["keep"]})})
        out["claims_again"] = ca
        check(checks, ca["totals"] == oldb["totals_after"] and ca["declined"] == 1 and ca["counts"] == [1, 2]
              and "Заявка передана администратору" in str(ca["again_reply"]) and ca["open_after"] == 1
              and ca["stats"].get("conflicts") == 0,
              "повторное обновление после отката: заявки и число организаций на месте, "
              "новая заявка (Альфа хочет вернуть переданный чек) пишется как обычно")
        check(checks, ca["vis"] == {"mode": "all", "roles": ["accountant", "manager"], "keep_acc_a": 2,
                                    "late_flag": [False]},
              "показ числа компаниям пережил откат и повторное обновление; компания, созданная старой "
              "версией, — без отметки")
    except Exception as exc:  # noqa: BLE001 — отчёт важнее трассировки
        check(checks, False, f"сбой: {exc}")
    finally:
        out["checks"] = checks
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        failed = [c for c in checks if not c["ok"]]
        for c in checks:
            print(("✅ " if c["ok"] else "❌ ") + c["check"], file=sys.stderr)
        print(f"Итого: {len(checks) - len(failed)} из {len(checks)} проверок пройдено", file=sys.stderr)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    return 0 if checks and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
