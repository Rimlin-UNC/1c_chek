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
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("admin", "acc_a", "acc_b", "user_a")}
    res["totals"] = {u: total(c, h) for u, h in H.items()}
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
        check(checks, len(up["unified_tables"]) == 5, "созданы 5 таблиц unified_*")
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
        check(checks, again["move_back"] == {"status": 200, "alpha_card": 200}, "перенос обратно работает")
        check(checks, again["move_no_fp"] == 200, "чек без ФП переносится между компаниями, как в 1.70.80")
        # необязательный перенос архива — только на копии базы, для отчёта
        sqlite_copy(work / "rehearsal.db", work / "backfill-copy.db")
        bf = run_phase("перенос архива на копии", BACKFILL, new_dir,
                       {**env, "DATABASE_URL": f"sqlite:///{work / 'backfill-copy.db'}"})
        out["backfill_copy"] = bf
        pending = bf["dry_run"]["registry_pending"] + bf["dry_run"]["pool_pending"]
        check(checks, bf["run"]["registered"] == pending and bf["again"]["registered"] == 0,
              f"перенос архива на копии: {pending} источников, повторный запуск ничего не меняет")
        check(checks, bf["fingerprint_after"] == bf["fingerprint_before"], "перенос архива не меняет старые таблицы")
        check(checks, bf["stats"]["ownership_conflicts"] == 1 and bf["twin"] == {"alpha": 404, "admin": 200},
              "исторический двойник (реестр Альфы + пул Беты) уходит на разбор администратору")
        rv = bf["review"]
        check(checks, rv.get("listed") == 1 and rv.get("candidates") == 2 and rv.get("can_resolve") is True,
              "разбор конфликтов: спор виден в списке, в карточке две компании-кандидата")
        check(checks, rv.get("resolve") == 200 and rv.get("alpha_after") == 200
              and rv.get("stats_after", {}).get("ownership_conflicts") == 0,
              "решение спора в пользу Альфы: чек снова виден Альфе, споров о владельце нет")
        old = run_phase("откат после разбора", OLD_AFTER_REVIEW, old_dir,
                        {**env, "DATABASE_URL": f"sqlite:///{work / 'backfill-copy.db'}"})
        out["rollback_after_review"] = old
        check(checks, old["twin_alpha"] == 200 and all(isinstance(v, int) for v in old["totals"].values()),
              f"откат на v{old['version']} после разбора: вход и списки работают, чек у Альфы")
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
