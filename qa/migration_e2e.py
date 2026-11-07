"""Сквозной тест миграции рабочей базы (#20): сценарий, фазы и проверки.

Путь, который пройдёт рабочая база при переходе на единый реестр чеков:

1. **Исходная база** создаётся кодом 1.70.80 (`b3db4fe`, версия рабочего
   сервера) — данные из `qa/upgrade_rehearsal.py`: три компании и компания по
   умолчанию, бухгалтеры, сотрудник, 22 чека реестра (один без ФП), 6 чеков
   Чек-Пула, среди них исторический двойник чека Альфы, закреплённый за Бетой.
2. **Обновление** до текущего кода: `init_db()` со сверкой при старте.
3. **Перенос архива** командой `python -m app.unified.backfill --dry-run`,
   затем настоящий и повторный (идемпотентность).
4. **Сверка инвариантов**: одна каноническая запись на тройку ФН + ФД + ФП;
   старые таблицы не изменились (побайтно); видимость по ролям; спорный чек в
   карантине до решения администратора; счётчики до и после совпадают. Затем
   администратор решает спор — как на экране «Разбор конфликтов».
5. **Откат на 1.70.80** (старая версия сканирует, переносит, удаляет и правит
   чеки, удаляет компанию) и **повторное обновление**: сверка пишет исправления
   в журнал, повторная сверка ничего не меняет, данные целы; повторный перенос
   архива подхватывает чек, отсканированный во время отката.
6. **Заявки компаний** (с 1.70.85, T-34): отказы /scan, записанные 1.70.80 в
   журнал аудита, становятся заявкой задним числом; проигравшая сторона спора
   получает заявку «отклонена»; сотрудникам список заявок недоступен; заявки
   переживают откат и повторное обновление.

Бэкенды: SQLite (файл во временном каталоге) и PostgreSQL (отдельная временная
база на сервере из `UNIFIED_POSTGRES_TEST_URL`, удаляется после прогона).

Каждая фаза — отдельный процесс с кодом своей версии (старый и новый пакет
`app` не уживаются в одном интерпретаторе); общая только база данных.

Запуск вне pytest (отчёт — JSON в stdout, проверки — в stderr):

    .venv/bin/python -m qa.migration_e2e                    # SQLite
    UNIFIED_POSTGRES_TEST_URL=... .venv/bin/python -m qa.migration_e2e --postgres

В pytest — `tests/test_migration_e2e.py` (CI: Python 3.11 и 3.12, PostgreSQL 17).
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
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from qa.upgrade_rehearsal import COMMON, MARK, SEED

ROOT = Path(__file__).resolve().parents[1]
# 1.70.80 — версия рабочего сервера (последний отчёт владельца, 01.11.2026)
OLD_COMMIT = "b3db4fef67942d59fad9567f579823b70c6bf16d"
OLD_LABEL = "1.70.80"
UNIFIED_TABLES = ["unified_company_links", "unified_documents", "unified_events", "unified_items",
                  "unified_origins", "unified_ownership"]
PRIMARY_TABLES = ["primary_document_lines", "primary_documents"]      # v1.70.94: первичка (УПД)
PRIMARY_MAIL_TABLES = ["primary_mail_files", "primary_mail_items", "primary_mailboxes"]   # v1.70.104: из почты
# v1.70.86 (фаза 3): компания, которую создаёт старая версия во время отката, —
# без колонки показа числа; флаг «нет» ей ставит значение по умолчанию в базе.
AFTER_ROLLBACK_COMPANY = "Компания после отката"


class OldCodeUnavailable(RuntimeError):
    """Коммита старой версии нет в репозитории и его не удалось загрузить."""


# ---------------------------------------------------------------- фазы -----
# Общие помощники фаз: COMMON из репетиции (fingerprint, emit, login, short,
# app_version) и постраничные списки — как их листает интерфейс.
PHASE_COMMON = COMMON + r'''
USERS = ("admin", "acc_a", "acc_b", "user_a", "acc_g")

def visible(c, headers):
    """Все чеки реестра, которые видит пользователь (постранично)."""
    ids, page = [], 1
    while True:
        r = c.get("/api/v1/receipts", headers=headers, params={"page": page, "page_size": 200})
        assert r.status_code == 200, ("список чеков", r.status_code, r.text[:200])
        body = r.json()
        ids += [x["id"] for x in body["items"]]
        if not body["items"] or len(ids) >= body["total"]:
            return sorted(ids)
        page += 1

def pool_mine(c, headers):
    r = c.get("/api/v1/pool-company/mine", headers=headers)
    return r.json().get("total") if r.status_code == 200 else r.status_code

def users_map():
    from app.database import SessionLocal
    from app.models import User
    with SessionLocal() as db:
        return {u.username: {"id": u.id, "role": u.role, "company_id": u.company_id}
                for u in db.query(User).filter(User.username.in_(USERS))}
'''

# 1.70.80 на PostgreSQL: первый же init_db() вставляет «компанию по умолчанию»
# с is_active = 1 — целое число в булев столбец; PostgreSQL это отвергает (SQLite
# принимает), поэтому чистая установка 1.70.80 на PostgreSQL невозможна. Делаем
# ровно то, что задумано этой миграцией, моделями самой 1.70.80 — дальше её
# init_db() видит компанию и шаг пропускает. На SQLite ту же компанию создаёт
# сама 1.70.80, так что исходные базы на обоих бэкендах одинаковы.
OLD_PG_PREAMBLE = r'''
import app.main  # noqa: F401 — регистрирует все модели в метаданных
from app.database import Base as _Base, SessionLocal as _Session, engine as _engine
from app.models import Company as _Company
_Base.metadata.create_all(_engine)
with _Session() as _db:
    if _db.query(_Company).first() is None:
        _db.add(_Company(name="ООО «Ямастер»", note="Создана автоматически при обновлении до 1.11.0"))
        _db.commit()
'''

BASELINE = PHASE_COMMON + r'''
from app.main import app
from fastapi.testclient import TestClient
pw = os.environ["REH_PASSWORD"]
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in USERS}
    emit({"version": app_version(), "visible": {u: visible(c, h) for u, h in H.items()},
          "pool": {u: pool_mine(c, H[u]) for u in ("acc_a", "acc_b")}, "users": users_map()})
'''

UPGRADE = PHASE_COMMON + r'''
from app.main import app
from app.database import SessionLocal, engine, init_db
init_db()                       # ровно то, что делает проверка целостности обновления
from sqlalchemy import inspect
from fastapi.testclient import TestClient
from app.unified.models import UnifiedDocument
pw = os.environ["REH_PASSWORD"]
res = {"version": app_version(), "fingerprint": fingerprint(),
       "unified_tables": sorted(t for t in inspect(engine).get_table_names() if t.startswith("unified_")),
       "primary_tables": sorted(t for t in inspect(engine).get_table_names()
                                if t.startswith("primary_") and not t.startswith("primary_mail")),
       "primary_mail_tables": sorted(t for t in inspect(engine).get_table_names() if t.startswith("primary_mail"))}
with SessionLocal() as db:
    res["documents"] = db.query(UnifiedDocument).count()
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in USERS}
    res["visible"] = {u: visible(c, h) for u, h in H.items()}
    res["pool"] = {u: pool_mine(c, H[u]) for u in ("acc_a", "acc_b")}
emit(res)
'''

# Факты для инвариантов — только для текущего кода (единый реестр).
FACTS = r'''
from collections import Counter
from app.models import Receipt, User
from app.pool.models import PoolReceipt
from app.unified import service as unified_service
from app.unified.models import (REVIEW_INCOMPLETE_IDENTITY, REVIEW_OWNERSHIP, UnifiedDocument,
                                UnifiedEvent, UnifiedOrigin)

def canon_facts(db):
    """Одна каноническая запись на тройку ФН + ФД + ФП."""
    sources = [("registry", r.id, r.fn, r.fd, r.fp) for r in db.query(Receipt)]
    sources += [("pool", p.id, p.fn, p.fd, p.fp) for p in db.query(PoolReceipt)]
    live = {(k, i) for k, i, *_ in sources}
    origins = {(o.source_kind, o.source_id): o.document_id for o in db.query(UnifiedOrigin)}
    docs = {d.id: d for d in db.query(UnifiedDocument)}
    copies = Counter(origins.values())
    # тройки считаются по самим ФН/ФД/ФП — независимо от хеша identity_key,
    # который проверяется отдельно (key_mismatch)
    by_key, incomplete = {}, []
    for kind, sid, fn, fd, fp in sources:
        triple, doc_id = (fn or "", fd or "", fp or ""), origins.get((kind, sid))
        if not all(v.strip() for v in triple):
            incomplete.append((kind, sid, doc_id))
        elif doc_id:
            by_key.setdefault(triple, set()).add(doc_id)
    live_keys = Counter((d.fn or "", d.fd or "", d.fp or "") for d in docs.values()
                        if d.identity_key and d.deleted_at is None)
    return {
        "sources": len(sources),
        "triples": len(by_key),
        "incomplete": len(incomplete),
        "documents_live": sum(1 for d in docs.values() if d.deleted_at is None),
        "unlinked": sorted(f"{k}:{i}" for k, i in live if (k, i) not in origins),
        "dangling": sorted(f"{k}:{i}" for k, i in origins if (k, i) not in live),
        "split_triples": sum(1 for v in by_key.values() if len(v) > 1),
        "duplicate_documents": sum(1 for n in live_keys.values() if n > 1),
        "key_mismatch": sorted(d.id for d in docs.values() if d.identity_key
                               and d.identity_key != unified_service.identity_key(d.fn, d.fd, d.fp)),
        "incomplete_bad": sorted(f"{k}:{i}" for k, i, doc_id in incomplete
                                 if not doc_id or docs[doc_id].identity_key is not None
                                 or not docs[doc_id].review_flags & REVIEW_INCOMPLETE_IDENTITY
                                 or copies[doc_id] != 1),
        "orphans": sorted(d.id for d in docs.values() if d.deleted_at is None and not copies[d.id]),
    }

def flagged_documents(db):
    """Документы на разборе спора о владельце и их копии."""
    out = []
    for d in db.query(UnifiedDocument).filter(UnifiedDocument.deleted_at.is_(None)).order_by(UnifiedDocument.id):
        if d.review_flags & REVIEW_OWNERSHIP:
            out.append({"id": d.id, "sources": sorted(f"{o.source_kind}:{o.source_id}" for o in d.origins)})
    return out

def _hidden(db):
    """Источники, скрытые до разбора: запись на разборе спора о владельце или закрыта."""
    out = set()
    for o in db.query(UnifiedOrigin):
        d = db.get(UnifiedDocument, o.document_id)
        if d is not None and (d.deleted_at is not None or d.review_flags & REVIEW_OWNERSHIP):
            out.add((o.source_kind, o.source_id))
    return out

def visibility_facts(db, seen):
    """Реестр: ожидаемое множество по ролям и «чужие» чеки среди видимых."""
    regs, hidden = {r.id: r for r in db.query(Receipt)}, _hidden(db)
    out = {}
    for u in db.query(User).filter(User.username.in_(USERS)):
        if u.role == "admin":
            expected, foreign = sorted(regs), []
        else:
            expected = sorted(r.id for r in regs.values() if r.company_id == u.company_id
                              and (u.role != "user" or r.created_by == u.id)
                              and ("registry", r.id) not in hidden)
            foreign = sorted(i for i in seen.get(u.username, []) if i not in regs
                             or regs[i].company_id != u.company_id
                             or (u.role == "user" and regs[i].created_by != u.id))
        out[u.username] = {"expected": expected, "foreign": foreign}
    return out

def unified_facts(db, seen):
    """Единый реестр: аккаунт видит документы своей компании без спора, сотрудник — свои."""
    regs = {r.id: r for r in db.query(Receipt)}
    pools = {p.id: p for p in db.query(PoolReceipt)}
    docs = {d.id: d for d in db.query(UnifiedDocument)}
    copies = {}
    for o in db.query(UnifiedOrigin):
        copies.setdefault(o.document_id, []).append(o)
    out = {}
    for u in db.query(User).filter(User.username.in_(USERS)):
        if u.role == "admin":
            continue
        expected = set()
        for d in docs.values():
            own = d.ownership
            if d.deleted_at is not None or d.review_flags & REVIEW_OWNERSHIP or own is None \
                    or own.company_id != u.company_id:
                continue
            reg = [regs[o.source_id] for o in copies.get(d.id, []) if o.source_kind == "registry" and o.source_id in regs]
            pool = [pools[o.source_id] for o in copies.get(d.id, []) if o.source_kind == "pool" and o.source_id in pools]
            if any(r.company_id != u.company_id for r in reg) \
                    or any(p.assigned_company_id not in (None, u.company_id) for p in pool):
                continue
            if u.role == "user":
                if own.created_by != u.id or not any(r.created_by == u.id for r in reg):
                    continue
            elif not reg and not any(p.assigned_company_id == u.company_id for p in pool):
                continue
            expected.add(d.id)
        got = set(seen.get(u.username, []))
        foreign = sorted(i for i in got if i not in docs or docs[i].ownership is None
                         or docs[i].ownership.company_id != u.company_id or docs[i].deleted_at is not None
                         or docs[i].review_flags & REVIEW_OWNERSHIP
                         or (u.role == "user" and docs[i].ownership.created_by != u.id))
        out[u.username] = {"seen": len(got), "expected": len(expected), "foreign": foreign,
                           "missing": len(expected - got), "extra": len(got - expected)}
    return out

def unified_visible(c, headers):
    ids, page = [], 1
    while True:
        r = c.get("/api/v1/unified/documents", headers=headers, params={"page": page, "per_page": 100})
        assert r.status_code == 200, ("единый реестр", r.status_code, r.text[:200])
        body = r.json()
        ids += [x["id"] for x in body["items"]]
        if not body["items"] or len(ids) >= body["total"]:
            return sorted(ids)
        page += 1

def doc_id_of(db, kind, source_id):
    doc = unified_service.document_for_source(db, kind, source_id)
    return doc.id if doc else None

def claims_facts(c, H, ids):
    """v1.70.85: заявки компаний — число и претенденты (администратор), доступ сотрудников."""
    def companies(rid):
        with SessionLocal() as db:
            doc_id = doc_id_of(db, "registry", rid)
        data = c.get(f"/api/v1/unified/documents/{doc_id}/companies", headers=H["admin"]).json()
        return {"count": data.get("count"), "claimants": [
            [x["company_id"] == ids["beta"], x["status"], x["source"], x["attempts"]]
            for x in data.get("companies", []) if x["role"] == "claimant"]}
    return {"open": c.get("/api/v1/unified/stats", headers=H["admin"]).json().get("claims_open"),
            "history": companies(ids["regs_a"][4]), "twin": companies(ids["regs_a"][0]),
            "staff": sorted({c.get("/api/v1/unified/claims", headers=H[u]).status_code
                             for u in ("acc_a", "acc_b", "user_a")})}

def phase3_facts(c, H, ids):
    """v1.70.86 (фаза 3): поле companies_count у ролей и фильтр администратора.

    Чек regs_a[4] — с заявкой Беты из журнала аудита (2 организации), regs_a[3] —
    обычный чек Альфы, regs_a[5] — чек сотрудника Альфы."""
    def field(user, rid):
        r = c.get(f"/api/v1/receipts/{rid}", headers=H[user])
        return r.json().get("companies_count", "нет") if r.status_code == 200 else r.status_code
    multi = c.get("/api/v1/receipts", headers=H["admin"],
                  params={"multi_company": "true", "page_size": 200}).json()
    found = {x["id"] for x in multi.get("items", [])}
    settings = c.get("/api/v1/unified/visibility", headers=H["admin"]).json()
    companies = settings.get("companies", [])
    return {"admin": field("admin", ids["regs_a"][4]), "acc_a": field("acc_a", ids["regs_a"][4]),
            "user_a": field("user_a", ids["regs_a"][5]), "acc_b": field("acc_b", ids["regs_b"][0]),
            "acc_g": field("acc_g", ids["reg_g"]),
            "multi": {"history": ids["regs_a"][4] in found, "single": ids["regs_a"][3] in found},
            "mode": settings.get("mode"), "roles": settings.get("roles"),
            "enabled": sorted(x["id"] for x in companies if x.get("enabled")),
            "late_company": [x.get("enabled") for x in companies if x.get("name") == "Компания после отката"]}

def inspect_all(c, H, ids):
    """Полный срез: старые таблицы, канон, карантин, видимость, счётчики."""
    res = {"fingerprint": fingerprint()}
    res["visible"] = {u: visible(c, h) for u, h in H.items()}
    unified_seen = {u: unified_visible(c, h) for u, h in H.items() if u != "admin"}
    res["pool"] = {u: pool_mine(c, H[u]) for u in ("acc_a", "acc_b")}
    res["stats"] = c.get("/api/v1/unified/stats", headers=H["admin"]).json()
    with SessionLocal() as db:
        res["canon"] = canon_facts(db)
        res["flagged"] = flagged_documents(db)
        res["visibility"] = visibility_facts(db, res["visible"])
        res["unified"] = unified_facts(db, unified_seen)
        res["reconcile_events"] = db.query(UnifiedEvent).filter_by(event_type="reconcile").count()
        res["docs"] = {name: doc_id_of(db, "registry", ids[key][idx] if idx is not None else ids[key])
                       for name, key, idx in (("twin", "regs_a", 0), ("moved", "regs_a", 1),
                                              ("deleted", "regs_a", 2), ("gamma", "reg_g", None),
                                              ("blank", "blank", None))}
    return res
'''

CHECK = PHASE_COMMON + FACTS + r'''
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in USERS}
    res = inspect_all(c, H, ids)
    res["claims"] = claims_facts(c, H, ids)
    # v1.70.86 (фаза 3): до разрешения число видит только администратор; затем
    # показ «выбранным компаниям» — Альфе, только бухгалтеру
    res["phase3_before"] = phase3_facts(c, H, ids)
    put = c.put("/api/v1/unified/visibility", headers=H["admin"],
                json={"mode": "selected", "roles": ["accountant"], "company_ids": [ids["alpha"]]})
    res["phase3_put"] = put.status_code
    res["phase3_after"] = phase3_facts(c, H, ids)
    twin = ids["regs_a"][0]
    res["twin"] = {"alpha": c.get(f"/api/v1/receipts/{twin}", headers=H["acc_a"]).status_code,
                   "admin": c.get(f"/api/v1/receipts/{twin}", headers=H["admin"]).status_code}
    listed = c.get("/api/v1/unified/documents", headers=H["admin"],
                   params={"conflicts": "true", "flag": 8, "per_page": 100}).json()
    res["conflicts_listed"] = [x["id"] for x in listed.get("items", [])]
    # решение администратора — как на экране «Разбор конфликтов» (#9)
    resolve = {}
    if len(res["conflicts_listed"]) == 1:
        doc_id = res["conflicts_listed"][0]
        card = c.get(f"/api/v1/unified/documents/{doc_id}/review", headers=H["admin"]).json()
        target = next((x["company_id"] for x in card.get("candidates", []) if "registry" in x["sources"]), None)
        r = c.post(f"/api/v1/unified/documents/{doc_id}/resolve-ownership", headers=H["admin"],
                   json={"company_id": target, "expected_version": card.get("version"),
                         "reason": "Сквозной тест миграции: чек реестра Альфы"})
        resolve = {"status": r.status_code, "target": target, "candidates": len(card.get("candidates", []))}
    res["resolve"] = resolve
    after = inspect_all(c, H, ids)
    after["twin_alpha"] = c.get(f"/api/v1/receipts/{twin}", headers=H["acc_a"]).status_code
    after["claims"] = claims_facts(c, H, ids)
    res["after"] = after
emit(res)
'''

ROLLBACK = PHASE_COMMON + r'''
from app.main import app
from app.database import init_db
init_db()                       # откат: старая версия стартует на обновлённой базе
from fastapi.testclient import TestClient
ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
res = {"version": app_version()}
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in USERS}
    res["visible_before"] = {u: visible(c, h) for u, h in H.items()}
    code = "t=20261021T1300&s=444.00&fn=9289000088888888&i=888&fp=3888888888&n=1"
    scan = c.post("/api/v1/receipts/scan", headers=H["acc_a"], json={"qr_data": code, "source": "web"})
    res["scan"] = {"status": scan.status_code, "id": (scan.json().get("receipt") or {}).get("id")}
    res["move"] = c.post("/api/v1/receipts/move", headers=H["admin"],
                         json={"receipt_ids": [ids["regs_a"][1]], "company_id": ids["beta"]}).status_code
    res["delete"] = c.delete(f"/api/v1/receipts/{ids['regs_a'][2]}", headers=H["admin"]).status_code
    res["patch"] = c.patch(f"/api/v1/receipts/{ids['blank']}", headers=H["acc_a"],
                           json={"total_sum": 333.0}).status_code
    gone = c.post(f"/api/v1/companies/{ids['gamma']}/delete", headers=H["admin"],
                  json={"mode": "move", "target_company_id": ids["beta"], "move_users": True})
    res["delete_company"] = {"status": gone.status_code, "body": short(gone)}
    # v1.70.86: старая версия не знает колонки показа числа — компания всё равно создаётся
    res["company_created"] = c.post("/api/v1/companies", headers=H["admin"],
                                    json={"name": "Компания после отката"}).status_code
    H = {u: login(c, u, pw) for u in USERS}   # сотрудник Гаммы теперь в Бете
    res["visible_after"] = {u: visible(c, h) for u, h in H.items()}
    res["pool_after"] = {u: pool_mine(c, H[u]) for u in ("acc_a", "acc_b")}
res["fingerprint"] = fingerprint()
emit(res)
'''

REUPGRADE = PHASE_COMMON + FACTS + r'''
from app.main import app
from app.database import SessionLocal, init_db
init_db()                       # повторное обновление: сверка с изменениями старой версии
from fastapi.testclient import TestClient
from app.unified.reconcile import reconcile
ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
docs = json.loads(os.environ["REH_DOCS"])
res = {"version": app_version(), "fingerprint": fingerprint()}
with SessionLocal() as db:
    journal = db.query(UnifiedEvent).filter_by(event_type="reconcile").order_by(UnifiedEvent.created_at).all()
    res["journal"] = [{"document": e.document_id, "reason": json.loads(e.details or "{}").get("reason"),
                       "to": e.to_company_id} for e in journal]
    owner = {}
    for name in ("moved", "gamma", "deleted"):
        doc = db.get(UnifiedDocument, docs[name]) if docs.get(name) else None
        owner[name] = {"company": doc.ownership.company_id if doc and doc.ownership else None,
                       "closed": bool(doc and doc.deleted_at)}
    res["owners"] = owner
    res["blank_total"] = float(db.get(Receipt, ids["blank"]).total_sum)
    # v1.70.88 (фаза 4а): правка старой версии после отката — в единой записи
    blank_doc = db.get(UnifiedDocument, docs["blank"]) if docs.get("blank") else None
    res["blank_canon"] = float(blank_doc.total_sum) if blank_doc else None
    second = reconcile(db)       # повторная сверка: исправлять больше нечего
    from app.unified.copies import candidates, reconcile_copies
    res["second_copies"] = reconcile_copies(db)
    db.commit()
    found = candidates(db)
    res["drift"] = {kind: len(v) for kind, v in found.items()}
    res["second_reconcile"] = second
    res["journal_after_second"] = db.query(UnifiedEvent).filter_by(event_type="reconcile").count()
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in USERS}
    res["visible"] = {u: visible(c, h) for u, h in H.items()}
    res["pool"] = {u: pool_mine(c, H[u]) for u in ("acc_a", "acc_b")}
emit(res)
'''

FINAL = PHASE_COMMON + FACTS + r'''
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in USERS}
    res = inspect_all(c, H, ids)
    res["claims"] = claims_facts(c, H, ids)
    res["phase3"] = phase3_facts(c, H, ids)
    emit(res)
'''

FRESH_INSTALL = r'''
import json
from sqlalchemy import text
from app.database import engine, init_db
init_db()                       # перезапуск сервиса после установки
with engine.connect() as conn:
    companies = [dict(r._mapping) for r in conn.execute(text("SELECT name, is_active FROM companies"))]
    admins = conn.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin'")).scalar()
print("@@RESULT@@" + json.dumps({"companies": companies, "admins": admins}, ensure_ascii=False, default=str))
'''


# ------------------------------------------------------------ окружение -----
def ensure_commit(sha: str = OLD_COMMIT) -> None:
    """Коммит старой версии в репозитории; в неглубоком клоне (CI) — догрузить."""
    def present() -> bool:
        return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT,
                              capture_output=True, check=False).returncode == 0
    if present():
        return
    fetch = subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", sha], cwd=ROOT,
                           capture_output=True, text=True, timeout=180, check=False)
    if not present():
        raise OldCodeUnavailable(
            f"нет коммита {sha[:7]} (версия {OLD_LABEL}) и загрузить его не удалось: "
            f"{(fetch.stderr or fetch.stdout).strip()[-300:]}")


def prepare_code(work: Path, sha: str = OLD_COMMIT) -> tuple[Path, Path]:
    """Код старой версии (git archive) и копия текущего — каждая со своим data/."""
    ensure_commit(sha)
    old_dir, new_dir = work / "old", work / "new"
    old_dir.mkdir(parents=True)
    archive = subprocess.run(["git", "archive", sha], cwd=ROOT, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(old_dir)], input=archive.stdout, check=True)
    common = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "*.db", "__pycache__",
                                    "test-results", ".pytest_cache", ".ruff_cache", "htmlcov", "coverage")

    def ignore(path: str, names: list[str]) -> set[str]:
        # v1.70.89: только корневой data/ (база и файлы программы); справочники
        # app/pool/geo/data — часть кода, без них сканирование падает
        skip = set(common(path, names))
        if Path(path) == ROOT and "data" in names:
            skip.add("data")
        return skip

    shutil.copytree(ROOT, new_dir, ignore=ignore)
    return old_dir, new_dir


def sqlite_url(work: Path) -> str:
    return f"sqlite:///{work / 'migration-e2e.db'}"


@contextmanager
def postgres_database(base_url: str):
    """Отдельная временная база на сервере PostgreSQL; после прогона удаляется."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    name = "qa_e2e_" + secrets.token_hex(6)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        yield make_url(base_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def phase_env(url: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SUPABASE_", "DATABASE_URL", "PYTEST_", "PYTHONPATH"))}
    env.update({"DATABASE_URL": url, "YM_NO_REEXEC": "1", "YM_FETCHQ_OFF": "1", "YM_OSMON_OFF": "1",
                "SECRET_KEY": secrets.token_hex(32), "REH_PASSWORD": "E2e-" + secrets.token_hex(6)})
    return env


# v1.70.89 (фаза 4б, T-38): правило слияния. Бухгалтер Альфы сканирует QR — копия
# реестра знает только дату, сумму и тройку; затем тот же чек приходит в Чек-Пул с
# данными ФНС (адрес продавца, кассир). До 1.70.89 единая запись их не принимала, а
# правка бухгалтера и сверка на старте стирали их в Чек-Пуле. v1.70.90: то же при
# сохранении формы правки — она шлёт все поля, и пустые считались явной очисткой.
# Стадия последняя: свои чеки не меняют прежние проверки.
MERGE_RULE = PHASE_COMMON + r"""
from datetime import datetime
from app.main import app
from app.database import SessionLocal, init_db
init_db()
from fastapi.testclient import TestClient
from app.models import Receipt
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.unified import service
from app.unified.copies import candidates, reconcile_copies
ids, pw = json.loads(os.environ["REH_IDS"]), os.environ["REH_PASSWORD"]
ADDRESS, CASHIER = "г. Пул, ул. Полная, 1", "Кассир Пулов"
fn, fd, fp, when = "9489000000000077", "7707", "4400000077", datetime(2026, 10, 20, 12, 0)

def facts(db, pid, rid=None):
    db.expire_all()
    pool = db.get(PoolReceipt, pid)
    doc = service.document_for_source(db, service.POOL, pid)
    reg = db.get(Receipt, rid) if rid else None
    return {"canon": [doc.merchant_address, doc.cashier] if doc else None,
            "pool": [pool.merchant_address, pool.cashier],
            "registry": [reg.merchant_address or "", reg.cashier or ""] if reg else None,
            "linked": sorted(o.source_kind for o in doc.origins) if doc else [],
            "items": [len(doc.items) if doc else None,
                      db.query(PoolItem).filter(PoolItem.receipt_id == pid).count()]}

res = {}
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("acc_a",)}
    scan = c.post("/api/v1/receipts/scan", headers=H["acc_a"], json={
        "qr_data": f"t={when:%Y%m%dT%H%M}&s=77.00&fn={fn}&i={fd}&fp={fp}&n=1", "source": "web", "verify": False})
    res["scan"] = scan.status_code
    rid = (scan.json().get("receipt") or {}).get("id") if scan.status_code == 200 else None
with SessionLocal() as db:
    donor = db.query(PoolUser).first()
    pool = PoolReceipt(pool_user_id=donor.id if donor else None, fn=fn, fd=fd, fp=fp,
                       qr_data=f"t={when:%Y%m%dT%H%M}&s=77.00&fn={fn}&i={fd}&fp={fp}&n=1",
                       receipt_date=when, total_sum=77.0, operation=1, merchant_name="Магазин 4б",
                       merchant_inn="7801234567", merchant_address=ADDRESS, cashier=CASHIER,
                       status="verified", full_data=True, points_awarded=0)
    db.add(pool)
    db.flush()
    db.add(PoolItem(receipt_id=pool.id, name="Позиция 4б", quantity=1, price=77.0, total=77.0))
    db.flush()                        # как приём в пул: позиции записаны до регистрации
    service.register_source(db, service.POOL, pool, None, strict=True, event_type="ingest")
    db.commit()
    pid = pool.id
    res["after_pool"] = facts(db, pid, rid)
with TestClient(app) as c:
    H = {u: login(c, u, pw) for u in ("acc_a",)}
    res["patch"] = c.patch(f"/api/v1/receipts/{rid}", headers=H["acc_a"],
                           json={"comment": "правка бухгалтера"}).status_code if rid else None
    with SessionLocal() as db:
        res["after_patch"] = facts(db, pid, rid)
    # v1.70.90: сохранение формы правки — все поля, пустые пустой строкой (как openEditReceipt)
    with SessionLocal() as db:
        reg = db.get(Receipt, rid) if rid else None
        form = {"merchant_name": reg.merchant_name or "", "merchant_inn": reg.merchant_inn or "",
                "merchant_address": reg.merchant_address or "", "cashier": reg.cashier or "",
                "category": reg.category or "", "assignee": reg.assignee or "",
                "personal_sum": reg.personal_sum or 0, "comment": "сохранение формы",
                "notified": bool(reg.notified), "receipt_date": reg.receipt_date.isoformat(),
                "total_sum": reg.total_sum, "operation": reg.operation,
                "fn": reg.fn, "fd": reg.fd, "fp": reg.fp,
                "items": [{"name": it.name, "quantity": it.quantity, "price": it.price,
                           "total": it.total} for it in reg.items]} if reg else None
    res["form"] = (c.patch(f"/api/v1/receipts/{rid}", headers=H["acc_a"], json=form).status_code
                   if form else None)
    with SessionLocal() as db:
        res["after_form"] = facts(db, pid, rid)
    # v1.70.91 (шаг 4в, Q-28 — вариант «в»): карточка чека по QR показывает данные
    # единой записи отдельным блоком; поля копии компании не меняются
    card = c.get(f"/api/v1/receipts/{rid}", headers=H["acc_a"]).json() if rid else {}
    fill = card.get("unified_fill") or {}
    res["fill"] = {"fields": fill.get("fields"), "items": len(fill.get("items") or []),
                   "own": [card.get("merchant_address"), card.get("cashier"), len(card.get("items") or [])]}
with SessionLocal() as db:            # как сверка при следующем запуске
    res["reconcile"] = reconcile_copies(db)
    db.commit()
    res["after_restart"] = facts(db, pid, rid)
    res["drift"] = {kind: len(v) for kind, v in candidates(db).items()}
emit(res)
"""


def run_phase(name: str, code: str, code_dir: Path, env: dict) -> dict:
    """Фаза — отдельный процесс с кодом своей версии; результат — строка-маркер."""
    proc = subprocess.run([sys.executable, "-c", code], cwd=code_dir,
                          env={**env, "PYTHONPATH": str(code_dir)}, capture_output=True,
                          text=True, timeout=600, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(MARK)]
    if proc.returncode != 0 or not lines:
        tail = (proc.stdout + "\n" + proc.stderr)[-3000:]
        raise RuntimeError(f"фаза «{name}» упала (код {proc.returncode}):\n{tail}")
    return json.loads(lines[-1][len(MARK):])


def run_backfill(new_dir: Path, env: dict, *args: str) -> dict:
    """Перенос архива — та же команда, что выполнит владелец на сервере."""
    proc = subprocess.run([sys.executable, "-m", "app.unified.backfill", *args], cwd=new_dir,
                          env={**env, "PYTHONPATH": str(new_dir)}, capture_output=True,
                          text=True, timeout=600, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"`python -m app.unified.backfill {' '.join(args)}` завершилась с кодом "
                           f"{proc.returncode}:\n{(proc.stdout + proc.stderr)[-3000:]}")
    # init_db() может печатать строки миграций (в том числе со словарями) — JSON
    # результата начинается с последней строки, состоящей из одной «{»
    lines = proc.stdout.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "{"]
    if not starts:
        raise RuntimeError(f"`python -m app.unified.backfill {' '.join(args)}` не вывела JSON:\n"
                           f"{proc.stdout[-2000:]}")
    return json.loads("\n".join(lines[starts[-1]:]))


def fresh_install(url: str, new_dir: Path) -> dict:
    """Чистая установка текущей версии, как deploy.sh: python -m app.seed, затем перезапуск."""
    env = phase_env(url)
    seed = subprocess.run([sys.executable, "-m", "app.seed"], cwd=new_dir,
                          env={**env, "PYTHONPATH": str(new_dir)}, capture_output=True,
                          text=True, timeout=600, check=False)
    if seed.returncode != 0:
        return {"seed_code": seed.returncode, "error": (seed.stdout + seed.stderr)[-2000:]}
    return {"seed_code": 0, **run_phase("перезапуск после установки", FRESH_INSTALL, new_dir, env)}


# ------------------------------------------------------------- проверки -----
def _diff(got: list, want: list, limit: int = 3) -> str:
    extra, missing = sorted(set(got) - set(want)), sorted(set(want) - set(got))
    return f"лишние {extra[:limit]}{'…' if len(extra) > limit else ''} ({len(extra)}), " \
           f"нет {missing[:limit]}{'…' if len(missing) > limit else ''} ({len(missing)})"


def _sets_differ(got: dict, want: dict) -> dict:
    return {u: _diff(got.get(u, []), want.get(u, [])) for u in want if sorted(got.get(u, [])) != sorted(want[u])}


def _canon_problems(canon: dict) -> list[str]:
    problems = []
    for key, label in (("unlinked", "источники без единой записи"), ("dangling", "ссылки на удалённые строки"),
                       ("key_mismatch", "ключ тройки не совпадает с ФН/ФД/ФП"),
                       ("incomplete_bad", "неполная тройка склеена или без пометки"),
                       ("orphans", "живые записи без копий")):
        if canon[key]:
            problems.append(f"{label}: {canon[key][:3]}")
    if canon["split_triples"]:
        problems.append(f"тройка разошлась по нескольким записям: {canon['split_triples']}")
    if canon["duplicate_documents"]:
        problems.append(f"несколько живых записей на одну тройку: {canon['duplicate_documents']}")
    if canon["documents_live"] != canon["triples"] + canon["incomplete"]:
        problems.append(f"живых записей {canon['documents_live']}, а троек {canon['triples']} "
                        f"+ неполных {canon['incomplete']}")
    return problems


def _visibility_problems(facts: dict) -> dict:
    out = {}
    for user, row in facts["visibility"].items():
        got = facts["visible"].get(user, [])
        if row["foreign"]:
            out[user] = f"чужие чеки: {row['foreign'][:3]}"
        elif sorted(got) != row["expected"]:
            out[user] = _diff(got, row["expected"])
    for user, row in facts["unified"].items():
        if row["foreign"] or row["missing"] or row["extra"]:
            out[f"{user} (единый реестр)"] = (f"чужие {row['foreign'][:3]}, нет {row['missing']}, "
                                              f"лишние {row['extra']} из {row['seen']}")
    return out


def _changed_tables(before: dict, after: dict) -> list[str]:
    return sorted(t for t in before if before[t] != after.get(t))


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, ok: bool, what: str, details: object = "") -> None:
        if isinstance(details, list):
            details = "; ".join(map(str, details))
        elif isinstance(details, dict):
            details = "; ".join(f"{k}: {v}" for k, v in details.items())
        self.checks.append({"ok": bool(ok), "check": what, "details": "" if ok else str(details)[:1500]})


def run_scenario(url: str, old_dir: Path, new_dir: Path, *, postgres: bool,
                 log=lambda msg: print(msg, file=sys.stderr, flush=True)) -> tuple[list[dict], dict]:
    """Путь миграции целиком на одной базе. Возвращает (проверки, сырые результаты фаз)."""
    env, rep, raw = phase_env(url), Report(), {}
    started = time.monotonic()

    def phase(name: str, code: str, code_dir: Path, extra: dict | None = None) -> dict:
        t0 = time.monotonic()
        out = run_phase(name, code, code_dir, {**env, **(extra or {})})
        log(f"  фаза «{name}» — {time.monotonic() - t0:.1f} с")
        raw[name] = out
        return out

    # 1. исходная база — кодом 1.70.80
    seed = phase("1.70.80 создаёт базу", (OLD_PG_PREAMBLE if postgres else "") + SEED, old_dir)
    env["REH_IDS"] = json.dumps(seed["ids"])
    counts = {t: v["count"] for t, v in seed["fingerprint"].items()}
    rep.check(seed["version"] == OLD_LABEL and counts == {
        "companies": 4, "users": 5, "receipts": 22, "receipt_items": 22, "pool_receipts": 6,
        "pool_items": 6, "pool_users": 1},
        f"исходная база создана кодом {OLD_LABEL}: 4 компании, 5 пользователей, 22 чека реестра "
        "с позициями, 6 чеков Чек-Пула", f"версия {seed['version']}, строк {counts}")
    base = phase("1.70.80: списки по ролям", BASELINE, old_dir)
    users = base["users"]

    # 2. обновление
    up = phase("обновление: init_db и сверка при старте", UPGRADE, new_dir)
    rep.check(up["unified_tables"] == UNIFIED_TABLES and up.get("primary_tables") == PRIMARY_TABLES
              and up.get("primary_mail_tables") == PRIMARY_MAIL_TABLES and up["documents"] == 0,
              "обновление: init_db() создал 6 таблиц unified_* (с 1.70.85 — заявки компаний), 2 таблицы "
              "первички (с 1.70.94) и 3 таблицы первички из почты (с 1.70.104), перенос архива при старте "
              "не запускается",
              f"таблицы {up['unified_tables']} {up.get('primary_tables')} {up.get('primary_mail_tables')}, "
              f"документов {up['documents']}")
    rep.check(up["fingerprint"] == seed["fingerprint"], "обновление не изменило старые таблицы (побайтно)",
              f"изменились: {_changed_tables(seed['fingerprint'], up['fingerprint'])}")
    rep.check(up["visible"] == base["visible"] and up["pool"] == base["pool"],
              f"после обновления каждая роль видит ровно те же чеки, что в {OLD_LABEL}",
              f"реестр {_sets_differ(up['visible'], base['visible'])}; пул {up['pool']} ≠ {base['pool']}")

    # 3. перенос архива — командой, как на сервере
    t0 = time.monotonic()
    dry = run_backfill(new_dir, env, "--dry-run")
    run = run_backfill(new_dir, env)
    again = run_backfill(new_dir, env)
    log(f"  перенос архива (пробный, настоящий, повторный) — {time.monotonic() - t0:.1f} с")
    raw["backfill"] = {"dry_run": dry, "run": run, "again": again}
    rep.check(dry.get("registry_pending") == 22 and dry.get("pool_pending") == 6 and dry.get("registered") == 0,
              "перенос архива --dry-run: к переносу 22 чека реестра и 6 чеков пула, ничего не записано", dry)
    rep.check(run.get("registered") == 28 and again.get("registered") == 0,
              "перенос архива: зарегистрировано 28 источников, повторный запуск ничего не меняет",
              f"первый {run}, повторный {again}")
    dry_c, run_c, again_c = (x.get("claims", {}) for x in (dry, run, again))
    rep.check((dry_c.get("audit_rows_pending"), dry_c.get("audit_claims"), dry_c.get("audit_skipped")) == (3, 1, 1)
              and (run_c.get("audit_claims"), run_c.get("audit_skipped"), run_c.get("twin_claims")) == (1, 1, 0)
              and again_c.get("audit_rows_pending") == 0,
              f"заявки задним числом: три отказа /scan из журнала аудита {OLD_LABEL} — одна заявка Беты "
              "(отказ своей компании пропущен), спорный двойник ждёт решения; повтор ничего не добавляет",
              f"пробный {dry_c}, настоящий {run_c}, повторный {again_c}")

    # 4. инварианты, карантин и решение администратора
    chk = phase("сверка инвариантов и разбор спора", CHECK, new_dir)
    canon = chk["canon"]
    rep.check(not _canon_problems(canon),
              f"одна каноническая запись на тройку ФН+ФД+ФП: {canon['documents_live']} записей на "
              f"{canon['sources']} источников ({canon['triples']} троек и {canon['incomplete']} неполная)",
              _canon_problems(canon))
    rep.check(chk["fingerprint"] == seed["fingerprint"], "перенос архива не изменил старые таблицы (побайтно)",
              f"изменились: {_changed_tables(seed['fingerprint'], chk['fingerprint'])}")
    rep.check(not _visibility_problems(chk),
              "видимость по ролям: аккаунт видит только чеки своей компании, сотрудник — только свои; "
              "в едином реестре — так же", _visibility_problems(chk))
    twin_sources = sorted([f"registry:{seed['ids']['regs_a'][0]}", f"pool:{seed['ids']['overlap']}"])
    flagged = chk["flagged"]
    rep.check(len(flagged) == 1 and flagged[0]["sources"] == twin_sources
              and chk["twin"] == {"alpha": 404, "admin": 200}
              and chk["conflicts_listed"] == [flagged[0]["id"]] and chk["stats"].get("ownership_conflicts") == 1
              and chk["pool"] == {"acc_a": base["pool"]["acc_a"], "acc_b": base["pool"]["acc_b"] - 1},
              "спорный чек (реестр Альфы и пул Беты) — в карантине: скрыт от обеих компаний, виден "
              "администратору в «Разборе конфликтов»",
              f"на разборе {flagged}, карточка {chk['twin']}, в списке {chk['conflicts_listed']}, "
              f"статистика {chk['stats']}, пул {chk['pool']}")
    hidden = {u: len(set(base["visible"][u]) - set(chk["visible"].get(u, []))) for u in base["visible"]}
    rep.check(counts == {t: v["count"] for t, v in chk["fingerprint"].items()}
              and chk["stats"].get("documents") == canon["documents_live"]
              and chk["stats"].get("origins") == canon["sources"]
              and hidden == {"admin": 0, "acc_a": 1, "acc_b": 0, "user_a": 0, "acc_g": 0},
              "счётчики: строк в старых таблицах столько же; видимых чеков у каждой роли — как в "
              f"{OLD_LABEL}, кроме одного в карантине",
              f"скрыто {hidden}, статистика {chk['stats']}")
    rep.check(chk["reconcile_events"] == 0, "сверка без отката ничего не исправляет: событий reconcile нет",
              f"событий {chk['reconcile_events']}")
    claims = chk["claims"]
    rep.check(claims["open"] == 1 and claims["history"] == {"count": 2, "claimants": [[True, "open", "scan_qr", 2]]}
              and claims["staff"] == [403],
              "заявка Беты из истории: у чека Альфы 2 организации, 2 попытки; сотрудникам список заявок "
              "недоступен (403)", claims)
    before3, after3 = chk["phase3_before"], chk["phase3_after"]
    rep.check(before3["mode"] == "off" and before3["admin"] == 2
              and all(before3[u] == "нет" for u in ("acc_a", "user_a", "acc_b", "acc_g"))
              and before3["multi"] == {"history": True, "single": False},
              "фаза 3: до разрешения число организаций видит только администратор (2 у чека с заявкой Беты); "
              "фильтр «две и больше» находит этот чек и не находит обычный", before3)
    rep.check(chk["phase3_put"] == 200 and after3["acc_a"] == 2
              and all(after3[u] == "нет" for u in ("user_a", "acc_b", "acc_g"))
              and (after3["mode"], after3["roles"], after3["enabled"]) == ("selected", ["accountant"],
                                                                           [seed["ids"]["alpha"]]),
              "фаза 3: показ «выбранным компаниям» (Альфа, бухгалтер) — бухгалтер Альфы видит 2, сотрудник "
              "Альфы, Бета и Гамма числа не получают",
              {"put": chk["phase3_put"], **after3})
    after, resolve = chk["after"], chk["resolve"]
    rep.check(resolve.get("status") == 200 and resolve.get("candidates") == 2 and not after["flagged"]
              and after["twin_alpha"] == 200 and after["visible"] == base["visible"]
              and after["pool"] == {"acc_a": base["pool"]["acc_a"] + 1, "acc_b": base["pool"]["acc_b"] - 1}
              and _changed_tables(seed["fingerprint"], after["fingerprint"]) == ["pool_receipts"]
              and not _visibility_problems(after) and not _canon_problems(after["canon"]),
              "решение администратора в пользу Альфы: спор снят, видимость реестра по ролям — ровно как в "
              f"{OLD_LABEL}, копия пула перешла к Альфе (меняется только pool_receipts)",
              f"решение {resolve}, на разборе {after['flagged']}, карточка {after['twin_alpha']}, "
              f"реестр {_sets_differ(after['visible'], base['visible'])}, пул {after['pool']}, "
              f"изменились {_changed_tables(seed['fingerprint'], after['fingerprint'])}, "
              f"нарушения {_visibility_problems(after)} {_canon_problems(after['canon'])}")
    rep.check(after["claims"]["twin"] == {"count": 2, "claimants": [[True, "declined", "archive_twin", 1]]}
              and after["claims"]["open"] == 1,
              "проигравшая сторона спора (Бета) — заявка «отклонена»: у чека 2 организации, "
              "ждущих решения заявок по-прежнему 1", after["claims"])

    # 5. откат на 1.70.80 и повторное обновление
    back = phase(f"откат на {OLD_LABEL}", ROLLBACK, old_dir)
    rep.check(back["version"] == OLD_LABEL and back["visible_before"] == after["visible"],
              f"откат: {OLD_LABEL} стартует на обновлённой базе, каждая роль видит то же, что до отката",
              f"версия {back['version']}, реестр {_sets_differ(back['visible_before'], after['visible'])}")
    rep.check(back["scan"]["status"] == 200 and back["scan"]["id"] and back["move"] == 200
              and back["delete"] == 200 and back["patch"] == 200 and back["delete_company"]["status"] == 200
              and back["company_created"] in (200, 201),
              f"откат: {OLD_LABEL} сканирует, переносит, удаляет и правит чеки, удаляет и создаёт компанию",
              {k: back[k] for k in ("scan", "move", "delete", "patch", "delete_company", "company_created")})
    docs = after["docs"]
    again_up = phase("повторное обновление: сверка", REUPGRADE, new_dir, {"REH_DOCS": json.dumps(docs)})
    reasons = Counter(e["reason"] for e in again_up["journal"])
    beta = seed["ids"]["beta"]
    owners = again_up["owners"]
    rep.check(reasons == Counter({"projection_changed": 2, "source_deleted": 1, "legacy_edit": 1})
              and owners["moved"]["company"] == beta and owners["gamma"]["company"] == beta
              and owners["deleted"]["closed"]
              and {e["document"] for e in again_up["journal"]}
              == {docs["moved"], docs["gamma"], docs["deleted"], docs["blank"]},
              "повторное обновление: сверка записала в журнал 4 исправления — перенос чека, удаление компании "
              "с переносом чеков, удалённый чек и правка суммы старой версией",
              f"журнал {again_up['journal']}, владельцы {owners}")
    copies2 = again_up["second_copies"]
    rep.check(again_up["blank_canon"] == 333.0 and again_up["drift"] == {"registry": 0, "pool": 0}
              and not any(v for v in copies2.values()),
              f"фаза 4а: правка суммы на {OLD_LABEL} после отката принята в единую запись (333 ₽), копии "
              "реестра и пула не расходятся с единой записью, повторная сверка копий ничего не меняет",
              f"единая запись {again_up['blank_canon']}, расходятся {again_up['drift']}, повторно {copies2}")
    second = again_up["second_reconcile"]
    rep.check(not any(second.values()) and again_up["journal_after_second"] == len(again_up["journal"]),
              "повторная сверка ничего не меняет", f"результат {second}, событий {again_up['journal_after_second']}")
    rep.check(again_up["fingerprint"] == back["fingerprint"],
              "повторное обновление не изменило старые таблицы (побайтно)",
              f"изменились: {_changed_tables(back['fingerprint'], again_up['fingerprint'])}")
    rep.check(again_up["visible"] == back["visible_after"] and again_up["pool"] == back["pool_after"]
              and again_up["blank_total"] == 333.0,
              f"данные целы: каждая роль видит ровно то же, что в {OLD_LABEL} после отката; правка старой "
              "версии сохранилась",
              f"реестр {_sets_differ(again_up['visible'], back['visible_after'])}, пул {again_up['pool']} ≠ "
              f"{back['pool_after']}, сумма чека без ФП {again_up['blank_total']}")
    t0 = time.monotonic()
    dry2, run2 = run_backfill(new_dir, env, "--dry-run"), run_backfill(new_dir, env)
    log(f"  повторный перенос архива — {time.monotonic() - t0:.1f} с")
    raw["backfill_again"] = {"dry_run": dry2, "run": run2}
    rep.check(dry2.get("registry_pending") == 1 and dry2.get("pool_pending") == 0 and run2.get("registered") == 1,
              f"чек, отсканированный на {OLD_LABEL} во время отката, подхватывает повторный перенос архива",
              f"пробный {dry2}, настоящий {run2}")
    final = phase("итоговая сверка", FINAL, new_dir)
    rep.check(not _canon_problems(final["canon"]) and not _visibility_problems(final) and not final["flagged"]
              and final["fingerprint"] == back["fingerprint"] and final["visible"] == back["visible_after"],
              "итог: одна каноническая запись на тройку, видимость по ролям и единый реестр без нарушений, "
              "старые таблицы не изменились",
              f"канон {_canon_problems(final['canon'])}, видимость {_visibility_problems(final)}, "
              f"на разборе {final['flagged']}, изменились "
              f"{_changed_tables(back['fingerprint'], final['fingerprint'])}")
    rep.check(final["claims"] == after["claims"],
              f"заявки пережили откат на {OLD_LABEL} и повторное обновление: число организаций и статусы те же",
              f"после разбора {after['claims']}, итог {final['claims']}")
    final3 = final["phase3"]
    rep.check((final3["mode"], final3["roles"], final3["enabled"]) == (after3["mode"], after3["roles"],
                                                                      after3["enabled"])
              and final3["acc_a"] == 2 and final3["acc_b"] == "нет" and final3["late_company"] == [False],
              f"фаза 3: показ числа пережил откат на {OLD_LABEL} и повторное обновление; компания, созданная "
              f"старой версией («{AFTER_ROLLBACK_COMPANY}»), без отметки", final3)
    merge = phase("фаза 4б: правило слияния", MERGE_RULE, new_dir)
    kept = ["г. Пул, ул. Полная, 1", "Кассир Пулов"]
    rep.check(merge["scan"] == 200 and merge["patch"] == 200
              and all(merge[k]["canon"] == kept and merge[k]["pool"] == kept
                      for k in ("after_pool", "after_patch", "after_restart"))
              and merge["after_restart"]["registry"] == ["", ""]
              and merge["after_pool"]["linked"] == ["pool", "registry"]
              and merge["drift"] == {"registry": 0, "pool": 0} and merge["reconcile"]["candidates"] == 0,
              "фаза 4б: копия реестра, отсканированная по QR раньше, чем чек пришёл в Чек-Пул, не стирает "
              "адрес продавца и кассира из ФНС — ни при правке бухгалтера, ни при сверке на старте; "
              "единая запись их принимает",
              merge)
    rep.check(merge["form"] == 200 and merge["after_form"]["canon"] == kept
              and merge["after_form"]["pool"] == kept and merge["after_form"]["registry"] == ["", ""]
              and merge["after_form"]["items"] == [1, 1] and merge["after_restart"]["items"] == [1, 1],
              "v1.70.90: сохранение формы правки у чека по QR (все поля, пустые — пустой строкой) не "
              "стирает адрес продавца, кассира и позиции ни в единой записи, ни в Чек-Пуле",
              merge)
    rep.check(merge["fill"]["fields"] == {"merchant_name": "Магазин 4б", "merchant_inn": "7801234567",
                                          "merchant_address": kept[0], "cashier": kept[1]}
              and merge["fill"]["items"] == 1 and merge["fill"]["own"] == ["", "", 0],
              "v1.70.91, фаза 4в: карточка чека по QR показывает адрес, кассира и позицию из единой "
              "записи отдельным блоком с пометкой, а копия компании и выгрузка в 1С остаются своими",
              merge)
    raw["users"], raw["seconds"] = users, round(time.monotonic() - started, 1)
    return rep.checks, raw


def format_checks(checks: list[dict]) -> str:
    lines = []
    for c in checks:
        lines.append(("✅ " if c["ok"] else "❌ ") + c["check"])
        if not c["ok"] and c["details"]:
            lines.append(f"     ↳ {c['details']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Сквозной тест миграции рабочей базы (#20)")
    ap.add_argument("--postgres", action="store_true",
                    help="на PostgreSQL из UNIFIED_POSTGRES_TEST_URL (иначе — SQLite)")
    ap.add_argument("--keep", action="store_true", help="не удалять временный каталог")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="ym-migration-e2e-"))
    try:
        old_dir, new_dir = prepare_code(work)
        if args.postgres:
            base_url = os.environ.get("UNIFIED_POSTGRES_TEST_URL")
            if not base_url:
                print("нужен UNIFIED_POSTGRES_TEST_URL (локально — python3 qa/pg_local.py pytest "
                      "tests/test_migration_e2e.py)", file=sys.stderr)
                return 2
            with postgres_database(base_url) as url:
                checks, raw = run_scenario(url, old_dir, new_dir, postgres=True)
        else:
            checks, raw = run_scenario(sqlite_url(work), old_dir, new_dir, postgres=False)
    finally:
        if args.keep:
            print(f"каталог прогона сохранён: {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"checks": checks, "raw": raw}, ensure_ascii=False, indent=1, default=str))
    print(format_checks(checks), file=sys.stderr)
    failed = sum(not c["ok"] for c in checks)
    print(f"Итого: {len(checks) - failed} из {len(checks)} проверок пройдено", file=sys.stderr)
    return 0 if checks and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
