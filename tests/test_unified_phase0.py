"""«Единая база», фаза 0 (ADR-20, #24): пакетный перенос и журнал карточки.

T-33 — POST /api/v1/receipts/move переносит чеки пачкой. Итог в базе тот же,
что у прежнего поштучного цикла (эталон _reference_move — его копия из
59fac17), а число SQL-операций не зависит от числа чеков.

T-32 — штатные роли (бухгалтер, руководитель) видят в карточке единой записи
только свою организацию: чужие компании, их сотрудники и администратор
платформы скрыты, свободный текст причины решения спора — тоже.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import (
    Company,
    MerchantRule,
    Receipt,
    ReceiptIdentityLock,
    ReceiptItem,
    User,
)
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.routers.receipts import move_receipts
from app.schemas import ReceiptMoveBody
from app.services import merchant_rules, policy
from app.services.receipt_identity import lock_identities
from app.unified import service
from app.unified.models import (
    REVIEW_AMOUNT,
    REVIEW_INCOMPLETE_IDENTITY,
    REVIEW_OWNERSHIP,
    UnifiedDocument,
    UnifiedEvent,
    UnifiedItem,
    UnifiedOrigin,
    UnifiedOwnership,
)
from tests.conftest import login
from tests.test_v1140 import _mk_user

DAY = datetime(2026, 9, 22, 15, 56)
CHECKED = datetime(2026, 9, 23, 10, 0)
# ИНН поставщика с правилом — свой на каждый запуск: общая база тестов не пересекается
# с другими файлами по ИНН (статистика пула, правила поставщиков).
RULE_INN = "39" + str(uuid4().int)[:8]
OTHER_INN = "4703148343"


def _fn() -> str:
    return "9" + str(int(uuid4().hex, 16) % 10**15).zfill(15)


def _registry(db, company, *, fp="2882574203", total=100.0, author=None, items=1,
              inn=OTHER_INN, **extra) -> Receipt:
    fn = _fn()
    row = Receipt(fn=fn, fd="3467", fp=fp,
                  qr_data=f"t=20260922T1556&s={total}&fn={fn}&i=3467&fp={fp}&n=1",
                  receipt_date=DAY, total_sum=total, operation=1, company_id=company.id,
                  created_by=author.id if author else None,
                  merchant_name="Магазин", merchant_inn=inn, **extra)
    db.add(row)
    db.flush()
    for k in range(items):
        db.add(ReceiptItem(receipt_id=row.id, name=f"Товар {k}", quantity=1,
                           price=total / items, total=total / items, position=k))
    return row


def _pool(db, receipt: Receipt, company) -> PoolReceipt:
    """Пуловая копия того же чека (те же реквизиты и позиция)."""
    row = PoolReceipt(pool_user_id=None, fn=receipt.fn, fd=receipt.fd, fp=receipt.fp,
                      qr_data=receipt.qr_data, receipt_date=receipt.receipt_date,
                      total_sum=receipt.total_sum, operation=1,
                      merchant_name=receipt.merchant_name, merchant_inn=receipt.merchant_inn,
                      status="verified", full_data=True, assigned_company_id=company.id)
    db.add(row)
    db.flush()
    db.add(PoolItem(receipt_id=row.id, name="Товар 0", quantity=1,
                    price=receipt.total_sum, total=receipt.total_sum))
    db.flush()
    return row


def _admin(db) -> User:
    return db.query(User).filter(User.username == "admin").one()


def _reference_move(db, receipt_ids, company_id, admin) -> tuple[int, int]:
    """Эталон: прежний обработчик переноса из 59fac17 (без подотчётного лица)."""
    comp = db.get(Company, company_id)
    rows = db.query(Receipt).filter(Receipt.id.in_(receipt_ids)).all()
    moved = same = 0
    for r in sorted(rows, key=lambda x: (x.fn, x.fd, x.fp, x.id)):
        doc = service.document_for_source(db, service.REGISTRY, r.id)
        if doc is None:
            doc = service.register_source(db, service.REGISTRY, r, admin, strict=True)
        if doc.ownership and doc.ownership.company_id == comp.id:
            same += 1
            continue
        service.move_document(db, doc.id, comp.id, admin)
        moved += 1
    for r in rows:
        policy.refresh_receipt(db, r)
    for r in rows:
        merchant_rules.apply_rules(db, r)
    db.commit()
    return moved, same


def _scenario(db, admin, tag: str) -> dict:
    """Смешанная пачка: всё, что прежний цикл обрабатывал по-разному."""
    a = Company(name=f"Фаза 0 А {tag} {uuid4().hex[:8]}")
    b = Company(name=f"Фаза 0 Б {tag} {uuid4().hex[:8]}",
                limit_per_month=300.0, limit_per_check=110.0)
    db.add_all([a, b])
    db.flush()
    emp = User(username=f"phase0_emp_{uuid4().hex[:10]}", password_hash="x", role="user",
               company_id=a.id, is_active=True)
    rule = MerchantRule(company_id=b.id, merchant_inn=RULE_INN, field="category",
                        value="Канцтовары")
    db.add_all([emp, rule])
    db.flush()
    petrov = {"assignee": "Петров", "assignee_lc": "петров"}
    r = {
        # новая полная тройка: пакетная регистрация, позиции, статус ФНС, выгрузка
        "fresh": _registry(db, a, total=120.0, author=emp, items=2, inn=RULE_INN,
                           fns_status="valid", fns_checked_at=CHECKED, exported=True,
                           exported_at=CHECKED, **petrov),
        "fresh_bare": _registry(db, a, total=90.0, items=0, **petrov),
        "registered": _registry(db, a, total=80.0, **petrov),
        "twin": _registry(db, a, total=70.0),          # с пуловой копией
        "pool_first": _registry(db, a, total=60.0),    # запись создана из пула
        "incomplete": _registry(db, a, fp="", total=50.0),
        "dangling": _registry(db, a, total=40.0, inn=RULE_INN, category="Своя категория"),
        "same": _registry(db, b, total=30.0),
        "same_new": _registry(db, b, total=20.0),
        "same_blocked": _registry(db, b, total=10.0),
        "b_petrov": _registry(db, b, total=100.0, **petrov),   # не переносится
    }
    for label in ("registered", "twin", "dangling", "same", "same_blocked", "b_petrov"):
        service.register_source(db, service.REGISTRY, r[label], admin, strict=True)
    pools = {"twin": _pool(db, r["twin"], a), "pool_first": _pool(db, r["pool_first"], a)}
    for row in pools.values():
        service.register_source(db, service.POOL, row, admin, strict=True)
    dangling = service.document_for_source(db, service.REGISTRY, r["dangling"].id)
    db.add(UnifiedOrigin(document_id=dangling.id, source_kind=service.POOL,
                         source_id=str(uuid4())))
    blocked = service.document_for_source(db, service.REGISTRY, r["same_blocked"].id)
    blocked.review_flags |= REVIEW_AMOUNT
    db.commit()
    moving = [r[k].id for k in ("fresh", "fresh_bare", "registered", "twin", "pool_first",
                                "incomplete", "dangling", "same", "same_new", "same_blocked")]
    return {"companies": {a.id: "А", b.id: "Б"}, "users": {admin.id: "админ", emp.id: "сотрудник"},
            "receipts": {row.id: label for label, row in r.items()},
            "pools": {row.id: f"пул:{label}" for label, row in pools.items()},
            "rule": rule.id, "source": a.id, "target": b.id, "moving": moving}


def _snapshot(db, scen: dict) -> dict:
    """Состояние базы после переноса без id — для сравнения двух наборов."""
    def comp(value):
        return scen["companies"].get(value, "чужая") if value else None

    def user(value):
        return scen["users"].get(value, "чужой") if value else None

    names = {**scen["receipts"], **scen["pools"]}
    out: dict = {}
    for rid, label in sorted(scen["receipts"].items(), key=lambda kv: kv[1]):
        r = db.get(Receipt, rid)
        entry = {"компания": comp(r.company_id), "выгружен": r.exported,
                 "дата выгрузки": r.exported_at, "сотрудник": (r.assignee, r.assignee_lc),
                 "политика": r.policy_flags, "категория": r.category}
        doc = service.document_for_source(db, service.REGISTRY, rid)
        if doc is not None:
            own = doc.ownership
            events = sorted(db.query(UnifiedEvent).filter_by(document_id=doc.id),
                            key=lambda e: (e.created_at, e.event_type))
            entry["запись"] = {
                "версия": doc.version, "пометки": doc.review_flags,
                "ФНС": (doc.fns_status, doc.fns_checked_at),
                "удалена": doc.deleted_at is not None, "ключ": doc.identity_key is not None,
                "реквизиты": (doc.fd, doc.fp, doc.receipt_date, doc.total_sum, doc.operation,
                              doc.merchant_name, doc.merchant_inn, doc.merchant_address,
                              doc.cashier, doc.region_code, doc.region_city, doc.industry),
                "владелец": (comp(own.company_id), user(own.created_by), user(own.assigned_by),
                             own.assigned_at is not None) if own else None,
                "позиции": sorted((i.position, i.name, i.quantity, i.price, i.total,
                                   i.vat_rate, i.vat_sum) for i in doc.items),
                "источники": sorted((o.source_kind, names.get(o.source_id, "висячий"))
                                    for o in doc.origins),
                "журнал": [(e.event_type, user(e.actor_id), comp(e.from_company_id),
                            comp(e.to_company_id), json.loads(e.details or "{}"))
                           for e in events],
            }
        out[label] = entry
    for pid, label in sorted(scen["pools"].items(), key=lambda kv: kv[1]):
        row = db.get(PoolReceipt, pid)
        out[label] = {"компания": comp(row.assigned_company_id), "назначил": user(row.assigned_by),
                      "дата назначения": row.assigned_at is not None}
    out["правило применено"] = db.get(MerchantRule, scen["rule"]).applied_count
    return out


def test_bulk_move_matches_reference_loop(client):
    """Пакетный перенос и прежний цикл дают одинаковое состояние базы."""
    hdr = login(client, "admin", "admin123")
    with SessionLocal() as db:
        admin = _admin(db)
        new = _scenario(db, admin, "пакет")
        ref = _scenario(db, admin, "эталон")
    resp = client.post("/api/v1/receipts/move", headers=hdr,
                       json={"receipt_ids": new["moving"], "company_id": new["target"]})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        moved, same = _reference_move(db, ref["moving"], ref["target"], _admin(db))
    assert (resp.json()["moved"], resp.json()["already_there"]) == (moved, same) == (7, 3)
    with SessionLocal() as db:
        got, want = _snapshot(db, new), _snapshot(db, ref)
        assert got == want
        # и сам результат такой, как задуман, а не просто одинаковый
        assert got["fresh"]["политика"] == f"{policy.LIMIT_CHECK},{policy.LIMIT_MONTH}"
        assert got["fresh"]["категория"] == "Канцтовары"
        assert got["fresh"]["выгружен"] is False and got["fresh"]["дата выгрузки"] is None
        assert got["fresh"]["запись"]["ФНС"] == ("valid", CHECKED)
        assert [e[0] for e in got["fresh"]["запись"]["журнал"]] == ["register", "move"]
        assert got["dangling"]["категория"] == "Своя категория"
        assert got["dangling"]["запись"]["источники"] == [("registry", "dangling")]
        assert got["incomplete"]["запись"]["пометки"] & REVIEW_INCOMPLETE_IDENTITY
        assert got["пул:twin"]["компания"] == "Б" and got["пул:pool_first"]["компания"] == "Б"
        assert got["same_blocked"]["запись"]["пометки"] & REVIEW_AMOUNT
        assert got["same_blocked"]["запись"]["журнал"][-1][0] != "move"
        for label in ("fresh", "fresh_bare", "registered", "twin", "pool_first",
                      "incomplete", "dangling"):
            assert got[label]["компания"] == "Б", label
            assert got[label]["запись"]["владелец"][0] == "Б", label
        for rid in new["moving"]:
            r = db.get(Receipt, rid)
            assert (r.policy_flags or "") == ",".join(policy.flags_for(db, r))


def test_bulk_move_assignee_counts_every_moved_receipt(client):
    """Новое подотчётное лицо учитывается в месячной сумме у всех чеков пачки.

    Прежний цикл не сохранял сотрудника последнего чека до пересчёта политики
    (сессия без autoflush), и месячный лимит видели не все чеки.
    """
    hdr = login(client, "admin", "admin123")
    with SessionLocal() as db:
        a = Company(name="Фаза 0 сотрудник А " + uuid4().hex[:8])
        b = Company(name="Фаза 0 сотрудник Б " + uuid4().hex[:8], limit_per_month=250.0)
        db.add_all([a, b])
        db.flush()
        rows = [_registry(db, a, total=100.0, assignee=f"Старый {k}", assignee_lc=f"старый {k}")
                for k in range(3)]
        db.commit()
        ids, target = [r.id for r in rows], b.id
    resp = client.post("/api/v1/receipts/move", headers=hdr,
                       json={"receipt_ids": ids, "company_id": target, "assignee": "  Сидоров  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] == 3
    with SessionLocal() as db:
        for rid in ids:
            r = db.get(Receipt, rid)
            assert (r.company_id, r.assignee, r.assignee_lc) == (target, "Сидоров", "сидоров")
            assert r.policy_flags == policy.LIMIT_MONTH       # 300 > 250 у каждого чека
            assert r.policy_flags == ",".join(policy.flags_for(db, r))


def test_bulk_move_blocking_flag_rejects_whole_batch(client):
    """Чек, требующий разбора, отменяет всю пачку — как прежний цикл с откатом."""
    hdr = login(client, "admin", "admin123")
    with SessionLocal() as db:
        a = Company(name="Фаза 0 разбор А " + uuid4().hex[:8])
        b = Company(name="Фаза 0 разбор Б " + uuid4().hex[:8])
        db.add_all([a, b])
        db.flush()
        fresh, bad = _registry(db, a), _registry(db, a)
        doc = service.register_source(db, service.REGISTRY, bad, _admin(db), strict=True)
        doc.review_flags |= REVIEW_AMOUNT
        db.commit()
        ids, source, target, doc_id = [fresh.id, bad.id], a.id, b.id, doc.id
    resp = client.post("/api/v1/receipts/move", headers=hdr,
                       json={"receipt_ids": ids, "company_id": target})
    assert resp.status_code == 409
    assert "разбора" in resp.json()["detail"]
    with SessionLocal() as db:
        assert {db.get(Receipt, i).company_id for i in ids} == {source}
        assert service.document_for_source(db, service.REGISTRY, fresh.id) is None
        assert db.get(UnifiedOwnership, doc_id).company_id == source
        assert db.query(UnifiedEvent).filter_by(document_id=doc_id, event_type="move").count() == 0


def _seed_batch(n: int, *, with_rule: bool = True) -> tuple[list[str], str, str]:
    with SessionLocal() as db:
        a = Company(name="Фаза 0 пачка А " + uuid4().hex[:8])
        b = Company(name="Фаза 0 пачка Б " + uuid4().hex[:8], limit_per_month=10**6)
        db.add_all([a, b])
        db.flush()
        if with_rule:
            db.add(MerchantRule(company_id=b.id, merchant_inn=RULE_INN, field="category",
                                value="Канцтовары"))
        rows = [_registry(db, a, inn=RULE_INN, assignee=f"Сотрудник {k % 7}",
                          assignee_lc=f"сотрудник {k % 7}") for k in range(n)]
        db.commit()
        return [r.id for r in rows], a.id, b.id


def _move_direct(ids: list[str], target: str) -> dict:
    """Вызов обработчика без HTTP — в потоке теста (для счёта и замера)."""
    with SessionLocal() as db:
        return move_receipts(ReceiptMoveBody(receipt_ids=ids, company_id=target),
                             user=_admin(db), db=db)


def _statements(action) -> int:
    """Число SQL-операций, выполненных в текущем потоке во время action()."""
    me, count = threading.get_ident(), 0

    def listener(*_args):
        nonlocal count
        if threading.get_ident() == me:
            count += 1

    event.listen(engine, "before_cursor_execute", listener)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return count


@pytest.mark.usefixtures("client")
def test_bulk_move_statement_count_does_not_grow_with_batch():
    """Число SQL-операций не зависит от числа чеков (прежний цикл: ~13 на чек)."""
    counts = {}
    for n in (20, 200):
        ids, source, target = _seed_batch(n)
        first = _statements(lambda ids=ids, target=target: _move_direct(ids, target))
        again = _statements(lambda ids=ids, source=source: _move_direct(ids, source))
        counts[n] = (first, again)
    (small_first, small_again), (large_first, large_again) = counts[20], counts[200]
    assert large_first <= small_first + 2, counts
    assert large_again <= small_again + 2, counts
    print("SQL-операций (первый, повторный):", counts)
    assert large_first < 80 and large_again < 80, counts


@pytest.mark.usefixtures("client")
def test_bulk_move_thousand_receipts_is_fast():
    """1000 чеков: замер на SQLite в потоке теста (без HTTP).

    Локально (ноябрь 2026): первый перенос с регистрацией ≈ 0,5 с, повторный
    ≈ 0,35 с; прежний цикл — 9,6 с и 4,9 с. Потолки с запасом на CI (-j4).
    """
    ids, source, target = _seed_batch(1000)
    started = time.perf_counter()
    result = _move_direct(ids, target)
    first = time.perf_counter() - started
    started = time.perf_counter()
    again_result = _move_direct(ids, source)
    again = time.perf_counter() - started
    print(f"перенос 1000 чеков: первый {first:.2f} с, повторный {again:.2f} с")
    assert result["moved"] == 1000 and again_result["moved"] == 1000
    assert first < 3.0, first
    assert again < 2.0, again


# --------------------------------------------------------------------------
#  T-32: журнал карточки без сведений о других организациях
# --------------------------------------------------------------------------
SECRET_REASON = "Альфа подтвердила передачу письмом от 01.11"


@pytest.fixture
def privacy_case(client):
    """Чек сотрудника Альфы перенесён в Бету, спор решён администратором."""
    hdr = login(client, "admin", "admin123")
    ca = client.post("/api/v1/companies", headers=hdr,
                     json={"name": "Фаза 0 Альфа " + uuid4().hex[:8]}).json()["id"]
    cb = client.post("/api/v1/companies", headers=hdr,
                     json={"name": "Фаза 0 Бета " + uuid4().hex[:8]}).json()["id"]
    names = {key: f"phase0_{key}_{uuid4().hex[:8]}"
             for key in ("a_user", "a_acc", "b_acc", "b_man")}
    heads = {
        "a_user": _mk_user(client, hdr, ca, names["a_user"], role="user"),
        "a_acc": _mk_user(client, hdr, ca, names["a_acc"], role="accountant"),
        "b_acc": _mk_user(client, hdr, cb, names["b_acc"], role="accountant"),
        "b_man": _mk_user(client, hdr, cb, names["b_man"], role="manager"),
        "admin": hdr,
    }
    client.cookies.clear()
    with SessionLocal() as db:
        ids = {key: db.query(User.id).filter(User.username == value).scalar()
               for key, value in names.items()}
        author = db.get(User, ids["a_user"])
        receipt = _registry(db, db.get(Company, ca), author=author)
        doc = service.register_source(db, service.REGISTRY, receipt, author, strict=True)
        db.commit()
        doc_id, receipt_id, admin_id = doc.id, receipt.id, _admin(db).id
    resp = client.post("/api/v1/receipts/move", headers=hdr,
                       json={"receipt_ids": [receipt_id], "company_id": cb})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        doc = db.get(UnifiedDocument, doc_id)
        doc.review_flags |= REVIEW_OWNERSHIP
        version = doc.version
        db.commit()
    resp = client.post(f"/api/v1/unified/documents/{doc_id}/resolve-ownership", headers=hdr,
                       json={"company_id": cb, "expected_version": version,
                             "reason": SECRET_REASON})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        # сверка пишет код причины и признаки исправлений (reconcile.py)
        db.add(UnifiedEvent(document_id=doc_id, event_type="reconcile", actor_id=None,
                            from_company_id=ca, to_company_id=cb,
                            details=json.dumps({"reason": "projection_changed", "company": True})))
        db.commit()
    yield {"doc": doc_id, "ca": ca, "cb": cb, "ids": ids, "admin": admin_id, "heads": heads}
    client.cookies.clear()


def _card(client, case, who):
    client.cookies.clear()
    return client.get(f"/api/v1/unified/documents/{case['doc']}", headers=case["heads"][who])


@pytest.mark.parametrize("who", ["b_acc", "b_man"])
def test_card_journal_hides_other_organisations(client, privacy_case, who):
    case = privacy_case
    resp = _card(client, case, who)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for secret in (case["ca"], case["ids"]["a_user"], case["admin"], SECRET_REASON):
        assert secret not in resp.text
    assert body["company_id"] == case["cb"]
    assert body["created_by"] is None and body["created_by_hidden"] is True
    events = {e["type"]: e for e in body["events"]}
    assert list(events) == ["register", "move", "resolve_ownership", "reconcile"]
    register, move = events["register"], events["move"]
    assert (register["actor_id"], register["actor_hidden"]) == (None, True)
    assert (register["to_company_id"], register["to_company_hidden"]) == (None, True)
    assert register["details"] == {"source_kind": "registry", "created": True}
    assert (move["from_company_id"], move["from_company_hidden"]) == (None, True)
    assert (move["to_company_id"], move["to_company_hidden"]) == (case["cb"], False)
    assert move["actor_hidden"] is True
    assert events["resolve_ownership"]["details"] == {}
    assert events["reconcile"]["details"] == {"reason": "projection_changed", "company": True}
    assert events["reconcile"]["actor_hidden"] is False


def test_card_journal_unchanged_for_admin(client, privacy_case):
    case = privacy_case
    resp = _card(client, case, "admin")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_by"] == case["ids"]["a_user"]
    assert "created_by_hidden" not in body
    events = {e["type"]: e for e in body["events"]}
    assert events["register"]["actor_id"] == case["ids"]["a_user"]
    assert events["move"]["from_company_id"] == case["ca"]
    assert events["move"]["actor_id"] == case["admin"]
    assert events["resolve_ownership"]["details"] == {"reason": SECRET_REASON}
    assert all("actor_hidden" not in e for e in body["events"])


def test_former_company_and_list_do_not_leak(client, privacy_case):
    case = privacy_case
    # бывшая компания карточку не видит вовсе
    assert _card(client, case, "a_acc").status_code == 404
    assert _card(client, case, "a_user").status_code == 404
    # в списке новой компании автор из другой организации скрыт
    client.cookies.clear()
    resp = client.get("/api/v1/unified/documents", headers=case["heads"]["b_acc"],
                      params={"per_page": 100})
    assert resp.status_code == 200, resp.text
    item = next(i for i in resp.json()["items"] if i["id"] == case["doc"])
    assert item["created_by"] is None and item["created_by_hidden"] is True
    assert case["ids"]["a_user"] not in resp.text and case["ca"] not in resp.text


# --------------------------------------------------------------------------
#  PostgreSQL: пакетные блокировки и встречные переносы без взаимоблокировки
# --------------------------------------------------------------------------
@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="адрес тестового PostgreSQL не задан")
def test_postgres_bulk_move_concurrent_batches():
    url = os.environ["UNIFIED_POSTGRES_TEST_URL"]
    pg = create_engine(url, pool_size=4, max_overflow=0)
    if pg.dialect.name != "postgresql":
        pg.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    schema = "qa_phase0_" + uuid4().hex
    tables = [Company.__table__, User.__table__, Receipt.__table__, ReceiptItem.__table__,
              PoolUser.__table__, PoolReceipt.__table__, PoolItem.__table__,
              ReceiptIdentityLock.__table__, UnifiedDocument.__table__, UnifiedItem.__table__,
              UnifiedOwnership.__table__, UnifiedOrigin.__table__, UnifiedEvent.__table__]

    def session() -> Session:
        db = Session(pg)
        db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        return db

    try:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            Base.metadata.create_all(bind=conn, tables=tables)
        with session() as db:
            a, b, c = Company(name="PG А"), Company(name="PG Б"), Company(name="PG В")
            admin = User(username="pg_phase0_admin", password_hash="x", role="admin")
            db.add_all([a, b, c, admin])
            db.flush()
            rows = [_registry(db, a, total=10.0 + k) for k in range(6)]
            # повтор тройки в одной пачке блокировок — без ошибки ON CONFLICT
            triple = (rows[0].fn, rows[0].fd, rows[0].fp)
            assert lock_identities(db, [triple, triple]) == 1
            service.register_source(db, service.REGISTRY, rows[0], admin, strict=True)
            db.commit()
            ids, targets = [r.id for r in rows], (b.id, c.id)
        barrier = Barrier(2)

        def move(order_and_target):
            order, target = order_and_target
            with session() as db:
                actor = db.query(User).filter_by(username="pg_phase0_admin").one()
                receipts = db.query(Receipt).filter(Receipt.id.in_(order)).all()
                barrier.wait(timeout=10)
                try:
                    moved, _same = service.move_registry_receipts(db, receipts, target, actor)
                    db.commit()
                    return len(moved)
                except Exception as exc:        # noqa: BLE001 — результат сравнивается ниже
                    db.rollback()
                    return type(exc).__name__

        jobs = [(ids, targets[0]), (list(reversed(ids)), targets[1])]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(move, jobs))
        assert sorted(results) == [6, 6], results
        with session() as db:
            owners = set()
            for rid in ids:
                receipt = db.get(Receipt, rid)
                doc = service.document_for_source(db, service.REGISTRY, rid)
                assert doc is not None and doc.ownership.company_id == receipt.company_id
                assert db.query(UnifiedEvent).filter_by(document_id=doc.id,
                                                        event_type="move").count() == 2
                owners.add(receipt.company_id)
            assert len(owners) == 1 and owners <= set(targets)
            assert db.query(UnifiedDocument).count() == 6
    finally:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.dispose()
