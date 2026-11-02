"""«Единая база», фазы 1–2 (ADR-20, #24, T-34): заявки компаний на чужие чеки.

Отказ «чек уже у другой организации» на скане QR, фото, PDF, ручном вводе и
переносе из Чек-Пула записывает заявку (unified_company_links). Сотрудник
видит только «Заявка передана администратору.» (Q-22) и никогда — число
организаций. Администратор видит число и список компаний и решает: передать
чек, отклонить заявку или не учитывать её.
"""
from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal
from app.models import AuditLog, Company, Receipt, User
from app.pool.models import PoolItem, PoolReceipt
from app.services import appsettings
from app.services.qr import parse_qr
from app.unified import links, service
from app.unified.models import (
    REVIEW_OWNERSHIP,
    UnifiedCompanyLink,
    UnifiedDocument,
    UnifiedEvent,
    UnifiedOwnership,
)
from tests.conftest import login
from tests.test_v1140 import _mk_user

NOTE = links.CLAIM_NOTE
DAY = datetime(2026, 9, 22, 15, 56)
SCAN_FOREIGN = "Этот чек уже учтён в другой организации."
SCAN_FOREIGN_OLD = ("Этот чек уже учтён в другой организации. Если он должен относиться к "
                    "вашей — попросите администратора переместить его.")


def _triple() -> tuple[str, str, str]:
    return "8" + str(int(uuid4().hex, 16) % 10**15).zfill(15), "3467", "2882574203"


def _qr(fn: str, fd: str, fp: str, total: str = "100.00") -> str:
    return f"t=20260922T1556&s={total}&fn={fn}&i={fd}&fp={fp}&n=1"


def _scan(client, head, qr):
    return client.post("/api/v1/receipts/scan", headers=head, json={"qr_data": qr})


def _links(document_id: str) -> dict:
    """{компания: (вид, статус, источник, попыток, первый, последний сотрудник)}."""
    with SessionLocal() as db:
        return {link.company_id: (link.kind, link.status, link.source, link.attempts,
                                  link.first_user_id, link.last_user_id)
                for link in db.query(UnifiedCompanyLink).filter_by(document_id=document_id)}


def _company(client, hdr, prefix: str) -> str:
    resp = client.post("/api/v1/companies", headers=hdr,
                       json={"name": f"{prefix} {uuid4().hex[:8]}"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.fixture
def case(client, monkeypatch):
    """Альфа владеет чеками; Бета и Гамма пытаются их учесть."""
    monkeypatch.setattr("app.routers.receipts._maybe_auto_verify", lambda *a: False)
    monkeypatch.setattr("app.routers.receipts._maybe_auto_fetch", lambda *a: False)
    hdr = login(client, "admin", "admin123")
    ca, cb, cc = (_company(client, hdr, name) for name in
                  ("Заявки Альфа", "Заявки Бета", "Заявки Гамма"))
    roles = {"a_user": ("user", ca), "a_user2": ("user", ca), "a_acc": ("accountant", ca),
             "b_user": ("user", cb), "b_acc": ("accountant", cb), "b_man": ("manager", cb),
             "c_acc": ("accountant", cc)}
    names = {key: f"claims_{key}_{uuid4().hex[:8]}" for key in roles}
    heads = {key: _mk_user(client, hdr, comp, names[key], role=role)
             for key, (role, comp) in roles.items()}
    heads["admin"] = hdr
    client.cookies.clear()
    with SessionLocal() as db:
        ids = {key: db.query(User.id).filter(User.username == value).scalar()
               for key, value in names.items()}
    yield SimpleNamespace(h=heads, ids=ids, ca=ca, cb=cb, cc=cc)
    client.cookies.clear()


def _alpha_receipt(client, case, total: str = "100.00") -> SimpleNamespace:
    """Чек сотрудника Альфы (скан QR — запись в едином реестре создаётся сразу)."""
    fn, fd, fp = _triple()
    qr = _qr(fn, fd, fp, total)
    resp = _scan(client, case.h["a_user"], qr)
    assert resp.status_code == 200 and resp.json()["receipt"], resp.text
    rid = resp.json()["receipt"]["id"]
    with SessionLocal() as db:
        doc = service.document_for_source(db, service.REGISTRY, rid)
        assert doc is not None and doc.ownership.company_id == case.ca
        return SimpleNamespace(id=rid, doc=doc.id, qr=qr, fn=fn, fd=fd, fp=fp)


def _companies(client, case, doc_id: str) -> dict:
    resp = client.get(f"/api/v1/unified/documents/{doc_id}/companies", headers=case.h["admin"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _link_id(doc_id: str, company_id: str) -> str:
    with SessionLocal() as db:
        return (db.query(UnifiedCompanyLink.id)
                .filter_by(document_id=doc_id, company_id=company_id).scalar())


def _decide(client, case, doc_id, company_id, action, reason="Решение администратора"):
    version = _companies(client, case, doc_id)["version"]
    return client.post(f"/api/v1/unified/links/{_link_id(doc_id, company_id)}/{action}",
                       headers=case.h["admin"],
                       json={"expected_version": version, "reason": reason})


def _pool_row(db, fn, fd, fp, company_id, **extra) -> PoolReceipt:
    row = PoolReceipt(fn=fn, fd=fd, fp=fp, qr_data=_qr(fn, fd, fp), receipt_date=DAY,
                      total_sum=100.0, operation=1, merchant_name="Магазин", status="verified",
                      full_data=True, assigned_company_id=company_id, **extra)
    db.add(row)
    db.flush()
    db.add(PoolItem(receipt_id=row.id, name="Товар", quantity=1, price=100.0, total=100.0))
    db.flush()
    return row


# --------------------------------------------------------------------------
#  Где появляется заявка
# --------------------------------------------------------------------------
def test_scan_foreign_duplicate_records_claim_without_count(client, case):
    """Скан QR: заявка с первым и последним сотрудником; ответ — без числа и данных."""
    r = _alpha_receipt(client, case)
    resp = _scan(client, case.h["b_user"], r.qr)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"receipt": None, "duplicate": True, "auto_verify": False,
                           "message": f"{SCAN_FOREIGN} {NOTE}"}
    again = _scan(client, case.h["b_acc"], r.qr)
    assert again.json()["message"] == f"{SCAN_FOREIGN} {NOTE}"
    assert _links(r.doc) == {case.cb: ("claim", "open", "scan_qr", 2,
                                       case.ids["b_user"], case.ids["b_acc"])}


def test_colleague_duplicate_is_not_a_claim(client, case):
    """Своя компания: бухгалтер видит чек, коллега получает прежний ответ — заявки нет."""
    r = _alpha_receipt(client, case)
    own = _scan(client, case.h["a_acc"], r.qr).json()
    assert own["duplicate"] and own["receipt"]["id"] == r.id
    colleague = _scan(client, case.h["a_user2"], r.qr).json()
    assert colleague["receipt"] is None and colleague["message"] == SCAN_FOREIGN_OLD
    assert _links(r.doc) == {}


def test_image_pdf_and_manual_record_claims(client, case, monkeypatch):
    """Фото, PDF и ручной ввод: заявка со своим источником и пометкой в ответе."""
    image_r, pdf_r, manual_r = (_alpha_receipt(client, case) for _ in range(3))
    monkeypatch.setattr("app.routers.receipts.imaging.decode_qr_image",
                        lambda *a: SimpleNamespace(parsed=parse_qr(image_r.qr), total_codes=1))
    image = client.post("/api/v1/receipts/scan/image", headers=case.h["b_acc"],
                        files={"file": ("receipt.png", b"image", "image/png")}).json()
    assert image["receipt"] is None
    assert image["message"] == f"Чек уже зарегистрирован в другой организации. {NOTE}"
    monkeypatch.setattr("app.services.pdf_receipts.extract_qr_texts", lambda *a: [pdf_r.qr])
    pdf = client.post("/api/v1/receipts/scan/pdf", headers=case.h["b_acc"],
                      files={"file": ("receipt.pdf", b"%PDF-test", "application/pdf")}).json()
    assert pdf["receipt"] is None
    assert pdf["message"] == f"Чек уже зарегистрирован за другой организацией. {NOTE}"
    manual = client.post("/api/v1/receipts/manual", headers=case.h["b_acc"], json={
        "date_time": "22.09.2026 15:56", "total_sum": 100, "fn": manual_r.fn,
        "fd": manual_r.fd, "fp": manual_r.fp, "operation": 1}).json()
    assert manual["receipt"] is None
    assert manual["message"] == f"Чек уже зарегистрирован в другой организации. {NOTE}"
    for r, source in ((image_r, "scan_image"), (pdf_r, "scan_pdf"), (manual_r, "manual")):
        assert _links(r.doc)[case.cb][:4] == ("claim", "open", source, 1), source


def test_pool_copy_of_other_company_rejects_scan_with_claim(client, case):
    """Пуловая копия закреплена за Альфой: скан Беты — 409 с пометкой, приём откатан."""
    fn, fd, fp = _triple()
    with SessionLocal() as db:
        pool = _pool_row(db, fn, fd, fp, case.ca)
        doc = service.register_source(db, service.POOL, pool, None, strict=True)
        db.commit()
        doc_id = doc.id
    resp = _scan(client, case.h["b_acc"], _qr(fn, fd, fp))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"Чек уже зарегистрирован за другой организацией. {NOTE}"
    with SessionLocal() as db:
        assert db.query(Receipt).filter_by(fn=fn).count() == 0
    assert _links(doc_id)[case.cb][:4] == ("claim", "open", "scan_qr", 1)


def test_pool_transfer_into_registry_of_other_company_records_claim(client, case):
    """Перенос закреплённого чека пула в реестр: чек уже учтён Альфой — заявка Беты."""
    r = _alpha_receipt(client, case)
    with SessionLocal() as db:
        pid = _pool_row(db, r.fn, r.fd, r.fp, case.cb).id
        db.commit()
    resp = client.post("/api/v1/pool-company/transfer", headers=case.h["b_acc"],
                       json={"receipt_ids": [pid]})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"Чек уже зарегистрирован в другой организации. {NOTE}"
    assert _links(r.doc)[case.cb][:4] == ("claim", "open", "pool_transfer", 1)
    with SessionLocal() as db:
        assert db.get(PoolReceipt, pid).assigned_company_id == case.cb
        assert db.query(Receipt).filter_by(fn=r.fn).one().company_id == case.ca


def test_pool_pick_of_receipt_accounted_elsewhere_records_claim(client, case):
    """Подбор из пула чека, уже учтённого Альфой: отказ всей пачки и заявка Беты."""
    r = _alpha_receipt(client, case)
    with SessionLocal() as db:
        appsettings.set_setting(db, "pool_enabled", "1")
        appsettings.set_setting(db, "pool_pick_monthly_limit", "100")
        pid = _pool_row(db, r.fn, r.fd, r.fp, None).id
        db.commit()
    resp = client.post("/api/v1/pool-company/pick-and-transfer", headers=case.h["b_acc"],
                       json={"receipt_ids": [pid]})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"Один из чеков уже есть в реестре — обновите список. {NOTE}"
    assert _links(r.doc)[case.cb][:3] == ("claim", "open", "pool_transfer")
    with SessionLocal() as db:
        assert db.get(PoolReceipt, pid).assigned_company_id is None


def test_archive_receipt_is_registered_softly_without_foreign_actor(client, case):
    """Чек архива регистрируется при заявке от имени системы — без сотрудника Беты."""
    fn, fd, fp = _triple()
    with SessionLocal() as db:
        receipt = Receipt(fn=fn, fd=fd, fp=fp, qr_data=_qr(fn, fd, fp), receipt_date=DAY,
                          total_sum=100.0, operation=1, company_id=case.ca,
                          created_by=case.ids["a_user"], merchant_name="Магазин", status="new")
        db.add(receipt)
        db.commit()
        rid = receipt.id
        assert service.document_for_source(db, service.REGISTRY, rid) is None
    assert _scan(client, case.h["b_acc"], _qr(fn, fd, fp)).json()["message"] == f"{SCAN_FOREIGN} {NOTE}"
    with SessionLocal() as db:
        doc = service.document_for_source(db, service.REGISTRY, rid)
        assert doc is not None and doc.ownership.company_id == case.ca
        events = db.query(UnifiedEvent).filter_by(document_id=doc.id).all()
        assert [(e.event_type, e.actor_id) for e in events] == [("sync", None)]
        doc_id = doc.id
    assert _links(doc_id)[case.cb][:2] == ("claim", "open")


def test_claim_failure_keeps_old_answer(client, case, monkeypatch):
    """Сбой записи заявки не ломает приём: прежний ответ, без слов о заявке."""
    r = _alpha_receipt(client, case)

    def broken(*_args, **_kwargs):
        raise RuntimeError("проверка: сбой записи заявки")

    monkeypatch.setattr(links, "_upsert", broken)
    resp = _scan(client, case.h["b_acc"], r.qr)
    assert resp.status_code == 200 and resp.json()["message"] == SCAN_FOREIGN_OLD
    assert _links(r.doc) == {}


# --------------------------------------------------------------------------
#  Администратор: число, список, решения
# --------------------------------------------------------------------------
def test_admin_sees_count_list_and_decides(client, case):
    """Число = владелец + «ждёт решения» и «отклонена»; передача — обычное перемещение."""
    r = _alpha_receipt(client, case)
    _scan(client, case.h["b_acc"], r.qr)
    _scan(client, case.h["c_acc"], r.qr)
    hdr = case.h["admin"]
    data = _companies(client, case, r.doc)
    assert data["count"] == 3
    assert {c["company_id"]: (c["role"], c["status"], c["counted"]) for c in data["companies"]} == {
        case.ca: ("owner", None, True), case.cb: ("claimant", "open", True),
        case.cc: ("claimant", "open", True)}
    inbox = client.get("/api/v1/unified/claims", headers=hdr, params={"per_page": 100})
    assert inbox.status_code == 200, inbox.text
    mine = {i["company_id"]: i for i in inbox.json()["items"] if i["document_id"] == r.doc}
    assert set(mine) == {case.cb, case.cc}
    assert mine[case.cb]["owner_company_id"] == case.ca and mine[case.cb]["attempts"] == 1
    assert mine[case.cb]["last_user_name"] and mine[case.cb]["blocking"] is False
    assert client.get("/api/v1/unified/stats", headers=hdr).json()["claims_open"] >= 2

    assert _decide(client, case, r.doc, case.cc, "decline").json()["status"] == "declined"
    assert _companies(client, case, r.doc)["count"] == 3          # отклонённая — в числе (Q-19)
    assert _decide(client, case, r.doc, case.cc, "void").json()["status"] == "void"
    assert _companies(client, case, r.doc)["count"] == 2          # «не учитывать» — нет
    declined = client.get("/api/v1/unified/claims", headers=hdr,
                          params={"status": "void", "per_page": 100}).json()
    assert any(i["document_id"] == r.doc and i["company_id"] == case.cc for i in declined["items"])

    resp = _decide(client, case, r.doc, case.cb, "transfer", "Чек оплачен картой Беты")
    assert resp.status_code == 200 and resp.json()["status"] == "transferred", resp.text
    with SessionLocal() as db:
        moved = db.get(Receipt, r.id)
        assert moved.company_id == case.cb and moved.exported is False
    data = _companies(client, case, r.doc)
    assert data["count"] == 1
    assert {c["company_id"]: (c["role"], c["counted"]) for c in data["companies"]} == {
        case.cb: ("owner", True), case.ca: ("former_owner", False), case.cc: ("claimant", False)}
    review = client.get(f"/api/v1/unified/documents/{r.doc}/review", headers=hdr).json()
    assert {"move", "claim_decline", "claim_void", "claim_transfer"} <= {e["type"] for e in review["events"]}


def test_decision_validation(client, case):
    """Причина обязательна, версия проверяется, повтор решения — 409."""
    r = _alpha_receipt(client, case)
    _scan(client, case.h["b_acc"], r.qr)
    hdr, link = case.h["admin"], _link_id(r.doc, case.cb)
    version = _companies(client, case, r.doc)["version"]
    url = f"/api/v1/unified/links/{link}"
    assert client.post(url + "/decline", headers=hdr,
                       json={"expected_version": version, "reason": "    "}).status_code == 422
    assert client.post(url + "/decline", headers=hdr,
                       json={"expected_version": version + 7, "reason": "Причина"}).status_code == 409
    assert client.post(url + "/explode", headers=hdr,
                       json={"expected_version": version, "reason": "Причина"}).status_code == 404
    assert client.post("/api/v1/unified/links/no-such-link/decline", headers=hdr,
                       json={"expected_version": version, "reason": "Причина"}).status_code == 404
    assert _decide(client, case, r.doc, case.cb, "decline").status_code == 200
    assert _decide(client, case, r.doc, case.cb, "decline").status_code == 409
    assert _decide(client, case, r.doc, case.cb, "transfer").status_code == 200
    assert _decide(client, case, r.doc, case.cb, "void").status_code == 409
    assert client.get("/api/v1/unified/claims", headers=hdr,
                      params={"status": "nonsense"}).status_code == 422


@pytest.mark.parametrize("who", ["a_user", "a_acc", "b_acc", "b_man"])
def test_staff_never_see_claims_or_count(client, case, who):
    """Штатные роли: ни списка, ни числа, ни решений в журнале карточки."""
    r = _alpha_receipt(client, case)
    _scan(client, case.h["b_acc"], r.qr)
    assert _decide(client, case, r.doc, case.cb, "decline").status_code == 200
    head = case.h[who]
    link = _link_id(r.doc, case.cb)
    assert client.get("/api/v1/unified/claims", headers=head).status_code == 403
    assert client.get(f"/api/v1/unified/documents/{r.doc}/companies", headers=head).status_code == 403
    assert client.post(f"/api/v1/unified/links/{link}/void", headers=head,
                       json={"expected_version": 1, "reason": "Причина"}).status_code == 403
    card = client.get(f"/api/v1/unified/documents/{r.doc}", headers=head)
    if who.startswith("a_"):
        assert card.status_code == 200, card.text
        assert {e["type"] for e in card.json()["events"]}.isdisjoint(links.ADMIN_ONLY_EVENTS)
        assert case.cb not in card.text and "Заявки Бета" not in card.text
        assert "count" not in card.json() and "companies" not in card.json()
    else:
        assert card.status_code == 404


def test_dashboard_feed_hides_claim_decisions(client, case):
    """Решения по заявкам видит только администратор (лента дашборда бухгалтера)."""
    r = _alpha_receipt(client, case)
    _scan(client, case.h["b_acc"], r.qr)
    assert _decide(client, case, r.doc, case.cb, "decline").status_code == 200
    feed = client.get("/api/v1/dashboard/recent", headers=case.h["a_acc"],
                      params={"limit": 100}).json()
    assert not any(item["action"] in links.ADMIN_ONLY_AUDIT for item in feed)
    admin_feed = client.get("/api/v1/dashboard/recent", headers=case.h["admin"],
                            params={"limit": 100}).json()
    assert any(item["action"] == "unified_claim_decline" for item in admin_feed)


# --------------------------------------------------------------------------
#  Перемещения, спор о владельце, удаление компании, сверка
# --------------------------------------------------------------------------
def test_bulk_move_closes_claim_and_remembers_former_owner(client, case):
    """Перемещение: заявка получателя исполнена, прежний владелец — в списке."""
    r = _alpha_receipt(client, case)
    _scan(client, case.h["b_acc"], r.qr)
    resp = client.post("/api/v1/receipts/move", headers=case.h["admin"],
                       json={"receipt_ids": [r.id], "company_id": case.cb})
    assert resp.status_code == 200, resp.text
    got = _links(r.doc)
    assert got[case.cb][:2] == ("claim", "transferred")
    assert got[case.ca][:4] == ("former_owner", "open", "move", 0)
    assert _companies(client, case, r.doc)["count"] == 1
    # прежний владелец снова сканирует чек — снова претендент
    assert _scan(client, case.h["a_acc"], r.qr).json()["message"] == f"{SCAN_FOREIGN} {NOTE}"
    assert _links(r.doc)[case.ca][:4] == ("claim", "open", "scan_qr", 1)
    assert _companies(client, case, r.doc)["count"] == 2


def test_resolve_ownership_declines_loser(client, case):
    """Решение спора о владельце: у проигравшей стороны — заявка «отклонена»."""
    fn, fd, fp = _triple()
    with SessionLocal() as db:
        receipt = Receipt(fn=fn, fd=fd, fp=fp, qr_data=_qr(fn, fd, fp), receipt_date=DAY,
                          total_sum=100.0, operation=1, company_id=case.ca,
                          merchant_name="Магазин", status="new")
        db.add(receipt)
        db.flush()
        pool = _pool_row(db, fn, fd, fp, case.cb)
        service.register_source(db, service.REGISTRY, receipt, None, strict=False,
                                event_type="backfill")
        doc = service.register_source(db, service.POOL, pool, None, strict=False,
                                      event_type="backfill")
        assert doc.review_flags & REVIEW_OWNERSHIP
        db.commit()
        doc_id, version = doc.id, doc.version
    resp = client.post(f"/api/v1/unified/documents/{doc_id}/resolve-ownership",
                       headers=case.h["admin"],
                       json={"company_id": case.ca, "expected_version": version,
                             "reason": "Чек оплачен картой Альфы"})
    assert resp.status_code == 200, resp.text
    assert _links(doc_id) == {case.cb: ("claim", "declined", "archive_twin", 1, None, None)}
    assert _companies(client, case, doc_id)["count"] == 2


def test_resolve_ownership_former_owner_becomes_declined_claimant(client, case):
    """Проигравший спора был прежним владельцем: заявка «отклонена» с источником спора."""
    r = _alpha_receipt(client, case)
    hdr = case.h["admin"]
    for target in (case.cb, case.ca):
        resp = client.post("/api/v1/receipts/move", headers=hdr,
                           json={"receipt_ids": [r.id], "company_id": target})
        assert resp.status_code == 200, resp.text
    assert _links(r.doc)[case.cb][:4] == ("former_owner", "open", "move", 0)
    with SessionLocal() as db:
        pool = _pool_row(db, r.fn, r.fd, r.fp, case.cb)
        doc = service.register_source(db, service.POOL, pool, None, strict=False,
                                      event_type="backfill")
        assert doc.review_flags & REVIEW_OWNERSHIP
        db.commit()
        version = doc.version
    resp = client.post(f"/api/v1/unified/documents/{r.doc}/resolve-ownership", headers=hdr,
                       json={"company_id": case.ca, "expected_version": version,
                             "reason": "Чек оплачен картой Альфы"})
    assert resp.status_code == 200, resp.text
    assert _links(r.doc)[case.cb][:4] == ("claim", "declined", "archive_twin", 1)
    assert _companies(client, case, r.doc)["count"] == 2


def test_company_delete_voids_its_claims(client, case):
    """Удалённая компания выпадает из числа; её заявка — «не учитывать»."""
    r = _alpha_receipt(client, case)
    hdr = case.h["admin"]
    cd = _company(client, hdr, "Заявки Дельта")
    d_acc = _mk_user(client, hdr, cd, f"claims_d_{uuid4().hex[:8]}", role="accountant")
    client.cookies.clear()
    _scan(client, d_acc, r.qr)
    assert _companies(client, case, r.doc)["count"] == 2
    resp = client.post(f"/api/v1/companies/{cd}/delete", headers=hdr,
                       json={"mode": "move", "target_company_id": case.cc, "move_users": True})
    assert resp.status_code == 200, resp.text
    assert _links(r.doc)[cd][:2] == ("claim", "void")
    data = _companies(client, case, r.doc)
    assert data["count"] == 1
    deleted = next(c for c in data["companies"] if c["company_id"] == cd)
    assert deleted["company_deleted"] is True and deleted["counted"] is False


def test_reconcile_closes_deleted_company_and_owned_claims(client, case):
    """Сверка после отката: заявка удалённой компании закрыта, заявка нового владельца исполнена."""
    r = _alpha_receipt(client, case)
    _scan(client, case.h["b_acc"], r.qr)
    with SessionLocal() as db:
        ghost = Company(name="Заявки Призрак " + uuid4().hex[:8])
        db.add(ghost)
        db.commit()
        ghost_id = ghost.id
        assert links.record_attempt(db, r.fn, r.fd, r.fp, company_id=ghost_id, user=None,
                                    source="manual")
        db.commit()
        # старая версия удалила компанию и перенесла чек к Бете; основная сверка
        # уже свела владельца к реестру
        db.query(Company).filter(Company.id == ghost_id).delete(synchronize_session=False)
        db.get(UnifiedDocument, r.doc).ownership.company_id = case.cb
        db.commit()
        result = links.reconcile_links(db)
        db.commit()
    assert result["links_closed"] >= 1 and result["claims_satisfied"] >= 1
    got = _links(r.doc)
    assert got[ghost_id][1] == "void" and got[case.cb][1] == "transferred"
    with SessionLocal() as db:
        again = links.reconcile_links(db)
        db.commit()
    assert _links(r.doc) == got and again["links_orphaned"] == 0


# --------------------------------------------------------------------------
#  Наполнение задним числом
# --------------------------------------------------------------------------
def test_backfill_claims_from_audit_and_pool_twins(client, case):
    """Отказы /scan из журнала аудита и пары «реестр ↔ пул другой компании»; повтор безопасен."""
    r = _alpha_receipt(client, case)
    twin = _alpha_receipt(client, case)
    with SessionLocal() as db:
        saved = {key: appsettings.get_setting(db, key, "")
                 for key in (links.LIVE_FROM_KEY, links.AUDIT_DONE_KEY)}
        start = db.query(func.coalesce(func.max(AuditLog.id), 0)).scalar() or 0
        attempts = [(case.cb, case.ids["b_acc"], datetime(2026, 10, 1, 9)),
                    (case.cb, case.ids["b_user"], datetime(2026, 10, 2, 9)),
                    (case.cc, case.ids["c_acc"], datetime(2026, 10, 3, 9)),
                    (case.ca, case.ids["a_acc"], datetime(2026, 10, 4, 9))]   # своя компания
        for n, (company, user, when) in enumerate(attempts, 1):
            db.add(AuditLog(id=start + n, user_id=user, username="история", company_id=company,
                            action="receipt_duplicate", entity_type="receipt", entity_id=r.id,
                            details="{}", created_at=when))
        _pool_row(db, twin.fn, twin.fd, twin.fp, case.cb, assigned_at=datetime(2026, 10, 5))
        appsettings.set_setting(db, links.AUDIT_DONE_KEY, str(start))
        appsettings.set_setting(db, links.LIVE_FROM_KEY, str(start + len(attempts)))
        db.commit()
        try:
            dry = links.backfill_claims(db, dry_run=True)
            assert (dry["audit_rows_pending"], dry["audit_claims"], dry["audit_skipped"]) == (4, 2, 1)
            assert _links(r.doc) == {} and _links(twin.doc) == {}
            first = links.backfill_claims(db)
            again = links.backfill_claims(db)
        finally:
            for key, value in saved.items():
                appsettings.set_setting(db, key, value)
            db.commit()
    assert first["audit_claims"] == 2 and first["twin_claims"] >= 1
    assert again["audit_rows_pending"] == 0
    got = _links(r.doc)
    assert got[case.cb] == ("claim", "open", "scan_qr", 2, case.ids["b_acc"], case.ids["b_user"])
    assert got[case.cc][:4] == ("claim", "open", "scan_qr", 1) and case.ca not in got
    assert _links(twin.doc) == {case.cb: ("claim", "open", "archive_twin", 1, None, None)}
    with SessionLocal() as db:
        link = (db.query(UnifiedCompanyLink)
                .filter_by(document_id=r.doc, company_id=case.cb).one())
        assert (link.first_at, link.last_at) == (datetime(2026, 10, 1, 9), datetime(2026, 10, 2, 9))


# --------------------------------------------------------------------------
#  PostgreSQL: те же команды ON CONFLICT (стенд qa/pg_local.py)
# --------------------------------------------------------------------------
@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="не задан UNIFIED_POSTGRES_TEST_URL")
def test_postgres_link_upserts():
    """Повтор попытки, прежний владелец, проигравший спора — одинаково на PostgreSQL."""
    pg = create_engine(os.environ["UNIFIED_POSTGRES_TEST_URL"], pool_size=2, max_overflow=0)
    if pg.dialect.name != "postgresql":
        pg.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    schema = "qa_links_" + uuid4().hex
    tables = [Company.__table__, UnifiedDocument.__table__, UnifiedOwnership.__table__,
              UnifiedCompanyLink.__table__]
    try:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            Base.metadata.create_all(bind=conn, tables=tables)
        with Session(pg) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            doc = UnifiedDocument(identity_key=uuid4().hex, fn="1", fd="2", fp="3")
            db.add(doc)
            db.flush()
            t1, t2 = datetime(2026, 10, 1), datetime(2026, 10, 2)
            row = links._row(doc.id, "b", "claim", "open", "scan_qr", attempts=1,
                             first_at=t1, last_at=t1, user_id="u1")
            again = links._row(doc.id, "b", "claim", "open", "manual", attempts=1,
                               first_at=t2, last_at=t2, user_id="u2")
            links._upsert(db, [row], links._attempt_set)
            links._upsert(db, [again, dict(again, id="dup")], links._attempt_set)   # дубль пары
            links.after_move(db, [(doc.id, "a")], "b", "admin", t2)
            links.after_resolve(db, doc.id, {"c", "b"}, "b", "admin", t2)
            db.flush()
            got = {link.company_id: (link.kind, link.status, link.source, link.attempts,
                                     link.first_user_id, link.last_user_id)
                   for link in db.query(UnifiedCompanyLink)}
            assert got == {"b": ("claim", "transferred", "scan_qr", 2, "u1", "u2"),
                           "a": ("former_owner", "open", "move", 0, None, None),
                           "c": ("claim", "declined", "archive_twin", 1, None, None)}
            # сверка при старте: UPDATE и DELETE с коррелированными подзапросами
            alive = Company(name="PG Живая")
            db.add(alive)
            db.flush()                     # id компании назначается при записи
            db.add(UnifiedOwnership(document_id=doc.id, company_id=alive.id))
            db.flush()
            links._upsert(db, [links._row(doc.id, alive.id, "claim", "open", "manual", attempts=1,
                                          first_at=t1, last_at=t1)], links._attempt_set)
            result = links.reconcile_links(db)
            assert result == {"links_closed": 2, "links_orphaned": 0, "claims_satisfied": 1}
            statuses = {link.company_id: link.status for link in
                        db.query(UnifiedCompanyLink).populate_existing()}
            assert statuses == {"a": "void", "b": "transferred", "c": "void", alive.id: "transferred"}
            db.rollback()
    finally:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.dispose()
