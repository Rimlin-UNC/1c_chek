"""Рабочее место аутсорсера (T-50, v1.70.100, ADR-30): клиенты бухгалтера, переключение клиента, сводка.

Критерии приёмки issue #35: роль и права — в обеих матрицах ролей; данные клиента не видны другим
клиентам; SQLite и PostgreSQL (весь набор идёт на обеих базах). Данные синтетические (ADR-14).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.clients import service as svc
from app.database import SessionLocal
from app.models import Company, Receipt, User
from app.services import audit_human
from tests import test_docreq as _dr
from tests.test_v1140 import _mk_user, _qr

firms = _dr.firms
client_firm = _dr.client_firm
PERIOD = _dr.PERIOD
HDR = "X-Ym-Company"


def _me(client, hdr, company: str | None = None) -> dict:
    headers = {**hdr, HDR: company} if company else hdr
    res = client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _grant(client, admin, user_id: str, *company_ids: str):
    return client.put(f"/api/v1/clients/access/{user_id}", headers=admin, json={"company_ids": list(company_ids)})


def test_accountant_switches_only_to_assigned_clients(client, firms):
    admin, a, b = firms
    acc_id = _me(client, a["hdr"])["id"]
    denied = client.get("/api/v1/auth/me", headers={**a["hdr"], HDR: b["id"]})
    assert denied.status_code == 403 and "компании клиента" in denied.json()["detail"]
    assert _grant(client, b["hdr"], acc_id, b["id"]).status_code == 403           # назначает только админ
    granted = _grant(client, admin, acc_id, b["id"], b["id"], a["id"])               # дубль и своя — отброшены
    assert granted.status_code == 200 and granted.json() == {"company_ids": [b["id"]], "added": 1, "removed": 0}
    me = _me(client, a["hdr"], b["id"])
    assert me["company_id"] == b["id"] and me["home_company_id"] == a["id"]
    assert [c["id"] for c in me["clients"]] == [a["id"], b["id"]] and me["clients"][0]["home"] is True
    # прежние экраны работают в компании клиента без правок: «что нужно от клиента» — компания B
    reqs = client.get(f"/api/v1/doc-requests?period={PERIOD}", headers={**a["hdr"], HDR: b["id"]})
    assert reqs.status_code == 200 and reqs.json()["company"]["id"] == b["id"]
    # запись — тоже в компании клиента; в своей компании бухгалтер этот чек не видит
    scan = client.post("/api/v1/receipts/scan", headers={**a["hdr"], HDR: b["id"]},
                       json={"qr_data": _qr("410.00"), "source": "web"})
    assert scan.status_code in (200, 201), scan.text
    rid = scan.json()["receipt"]["id"]
    with SessionLocal() as db:
        assert db.get(Receipt, rid).company_id == b["id"]
        assert db.get(User, acc_id).company_id == a["id"]                       # подмена не попала в базу
    def seen(hdr) -> bool:
        page = client.get(f"/api/v1/receipts?ids={rid}", headers=hdr).json()
        return rid in {r["id"] for r in page["items"]}
    assert seen({**a["hdr"], HDR: b["id"]}) and not seen(a["hdr"])
    assert seen(b["hdr"])                                   # бухгалтер B свою компанию видит как раньше
    assert client.get("/api/v1/auth/me", headers={**b["hdr"], HDR: a["id"]}).status_code == 403
    # доступ отозван — снова 403; компания отключена — тоже 403
    assert _grant(client, admin, acc_id).json() == {"company_ids": [], "added": 0, "removed": 1}
    assert client.get("/api/v1/auth/me", headers={**a["hdr"], HDR: b["id"]}).status_code == 403
    _grant(client, admin, acc_id, b["id"])
    assert client.patch(f"/api/v1/companies/{b['id']}", headers=admin, json={"is_active": False}).status_code == 200
    off = client.get("/api/v1/auth/me", headers={**a["hdr"], HDR: b["id"]})
    assert off.status_code == 403 and "отключена" in off.json()["detail"]
    assert [c["id"] for c in _me(client, a["hdr"])["clients"]] == [a["id"]]      # отключённую не предлагаем
    client.patch(f"/api/v1/companies/{b['id']}", headers=admin, json={"is_active": True})


def test_other_roles_ignore_header_admin_unaffected(client, firms):
    admin, a, b = firms
    tag = uuid4().hex[:8]
    emp = _mk_user(client, admin, a["id"], f"cl_emp_{tag}", role="user")
    boss = _mk_user(client, admin, a["id"], f"cl_boss_{tag}", role="manager")
    for hdr in (emp, boss):
        me = _me(client, hdr, b["id"])                                           # заголовок не действует
        assert me["company_id"] == a["id"] and "clients" not in me
    assert _me(client, admin, b["id"])["company_id"] is None
    assert client.get("/api/v1/clients", headers=emp).status_code == 403
    assert client.get("/api/v1/clients", headers=boss).status_code == 403
    emp_id = _me(client, emp)["id"]
    refused = _grant(client, admin, emp_id, b["id"])
    assert refused.status_code == 409 and "только бухгалтеру" in refused.json()["detail"]
    acc_id = _me(client, a["hdr"])["id"]
    assert _grant(client, admin, acc_id, str(uuid4())).status_code == 404
    assert client.get(f"/api/v1/clients/access/{acc_id}", headers=a["hdr"]).status_code == 403
    got = client.get(f"/api/v1/clients/access/{acc_id}", headers=admin).json()
    assert got["home_company_id"] == a["id"] and got["company_ids"] == []


def test_overview_counts_isolation_and_order(client, client_firm):
    admin, a, b = client_firm["admin"], client_firm["a"], client_firm["b"]
    data = client.get(f"/api/v1/clients?period={PERIOD}", headers=admin)
    assert data.status_code == 200, data.text
    body = data.json()
    assert body["period"] == PERIOD and body["period_title"] == "Октябрь 2026" and body["periods"]
    row = next(c for c in body["companies"] if c["id"] == a["id"])
    assert row["receipts"]["count"] == 1 and row["receipts"]["fns_bad"] == 1 and row["receipts"]["sum"] == 1250.0
    assert row["primary"]["errors"] == 1 and row["level"] == "bad" and row["score"] >= 6
    assert row["docreq"]["deadline"].startswith("2026-11-05T18:00")
    other = next(c for c in body["companies"] if c["id"] == b["id"])
    assert other["level"] == "ok" and other["score"] == 0
    names = [c["id"] for c in body["companies"]]
    assert names.index(a["id"]) < names.index(b["id"])                          # внимание — выше
    # бухгалтер видит только свою компанию, пока администратор не назначит клиентов
    own = client.get(f"/api/v1/clients?period={PERIOD}", headers=b["hdr"]).json()
    assert [c["id"] for c in own["companies"]] == [b["id"]]
    acc_id = _me(client, b["hdr"])["id"]
    _grant(client, admin, acc_id, a["id"])
    both = client.get(f"/api/v1/clients?period={PERIOD}", headers={**b["hdr"], HDR: a["id"]}).json()
    assert {c["id"] for c in both["companies"]} == {a["id"], b["id"]}
    assert both["totals"]["companies"] == 2 and both["totals"]["fns_bad"] == 1 and both["totals"]["bad"] == 1
    assert client.get("/api/v1/clients?period=2026-13", headers=admin).status_code == 422


def test_overview_overdue_escalated_and_bank(client_firm):
    a = client_firm["a"]
    with SessionLocal() as db:
        _dr.svc.detect(db, a["id"], PERIOD)
        acc = db.query(User).filter_by(role="accountant", company_id=a["id"]).first()
        before = svc.overview(db, acc, PERIOD, now=_dr.at(5, 17, 59))
        after = svc.overview(db, acc, PERIOD, now=_dr.at(5, 18, 1))
    row_before = next(c for c in before["companies"] if c["id"] == a["id"])
    row_after = next(c for c in after["companies"] if c["id"] == a["id"])
    assert row_before["docreq"]["open"] == 3 and row_before["docreq"]["overdue"] == 0
    assert row_before["docreq"]["days_left"] == 0
    assert row_after["docreq"]["overdue"] == 3 and row_after["docreq"]["days_left"] == -1
    assert row_after["score"] > row_before["score"]
    # v1.71.4 (T-87): у банка появился список подключений — здесь их нет
    assert row_after["bank"] == {"open": 0, "status": "none", "last_sync_at": "", "last_error": "", "banks": []}


def test_days_left_and_period_choices():
    due = datetime(2026, 11, 5, 18, 0, tzinfo=timezone(timedelta(hours=3)))
    assert svc._days_left(due, due - timedelta(days=2, hours=1)) == 2
    assert svc._days_left(due, due - timedelta(minutes=5)) == 0
    assert svc._days_left(due, due + timedelta(minutes=5)) == -1
    assert svc._days_left(due, due + timedelta(days=3, hours=2)) == -3
    choices = svc.period_choices(_dr.at(5, 12, 0))
    assert [c["period"] for c in choices[:3]] == ["2026-11", "2026-10", "2026-09"]
    assert choices[1]["title"] == "Октябрь 2026"


def test_deactivated_company_hidden_from_overview(client, firms):
    admin, a, b = firms
    client.patch(f"/api/v1/companies/{b['id']}", headers=admin, json={"is_active": False})
    try:
        ids = {c["id"] for c in client.get(f"/api/v1/clients?period={PERIOD}", headers=admin).json()["companies"]}
        assert a["id"] in ids and b["id"] not in ids
    finally:
        client.patch(f"/api/v1/companies/{b['id']}", headers=admin, json={"is_active": True})
    with SessionLocal() as db:
        assert db.get(Company, b["id"]).is_active is True


def test_audit_phrase_clients_access():
    action, note = audit_human.humanize_audit("clients_access_set",
                                              {"username": "buh", "total": 3, "added": 2, "removed": 1})
    assert action == "назначены клиенты бухгалтера"
    assert note == "бухгалтер: „buh“, клиентов: 3 (добавлено 2, снято 1)"
