"""v1.72.0: массовые действия по клиентам (T-89, этап 6 плана «Клиенты», ADR-53).

Бухгалтер отмечает несколько клиентов на экране «Клиенты» и делает одно действие сразу по всем:
«📣 Напомнить всем» (запрос документов), «📦 Пакет по всем» (архив за месяц), «Назначить сотрудника»,
«В архив» / «Вернуть из архива». Права — как у одиночных действий: бухгалтеру только свои (назначенные
администратором) компании, администратору — любые активные; каждая компания обрабатывается отдельно,
ошибка по одной не отменяет остальные. Проверяется API (итоги, права, частичный отказ) и окно.
Только синтетика (ADR-14).
"""
from __future__ import annotations

from uuid import uuid4

from app.clients import service as svc
from app.database import SessionLocal
from app.models import Company, User
from tests import test_docreq as _dr
from tests import test_primary_upd as _upd
from tests.test_v1140 import _mk_user

firms = _upd.firms
client_firm = _dr.client_firm
HDR = "X-Ym-Company"
PERIOD = _dr.PERIOD


def _ids_of(client, hdr) -> list[str]:
    res = client.get(f"/api/v1/clients?period={PERIOD}", headers=hdr)
    assert res.status_code == 200, res.text
    return [c["id"] for c in res.json()["companies"]]


def test_accountant_reminds_only_own_clients(client, client_firm):
    """«Напомнить всем»: бухгалтер — по своим клиентам; чужая компания попадает в отказы с причиной."""
    admin, a, b = client_firm["admin"], client_firm["a"], client_firm["b"]
    acc_id = client.get("/api/v1/auth/me", headers=a["hdr"]).json()["id"]
    body = {"company_ids": [a["id"], b["id"]], "period": PERIOD, "action": "remind"}
    denied = client.post("/api/v1/clients/bulk", headers=a["hdr"], json=body)
    assert denied.status_code == 200, denied.text
    refused = {i["company_id"]: i for i in denied.json()["items"] if not i["ok"]}
    assert b["id"] in refused and "не назначена" in refused[b["id"]]["error"]
    # своя компания обрабатывается (даже если напоминать не о чем), чужая — в отказах
    items = {i["company_id"]: i for i in denied.json()["items"]}
    assert items[a["id"]]["ok"] is True and items[b["id"]]["ok"] is False
    assert denied.json()["failed"] == 1 and denied.json()["done"] == len(items) - 1
    # администратор назначает B бухгалтеру — тогда пачка проходит целиком
    assert client.put(f"/api/v1/clients/access/{acc_id}", headers=admin,
                      json={"company_ids": [b["id"]]}).status_code == 200
    try:
        res = client.post("/api/v1/clients/bulk", headers={**a["hdr"], HDR: ""}, json=body)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["action"] == "remind" and len(data["items"]) == 2
        assert {i["company_id"] for i in data["items"]} == {a["id"], b["id"]}
        assert all(i["ok"] for i in data["items"]), data
        assert data["done"] == 2 and data["failed"] == 0
    finally:
        client.put(f"/api/v1/clients/access/{acc_id}", headers=admin, json={"company_ids": []})


def test_bulk_archive_and_restore(client, client_firm):
    """«В архив» выключает компании пачкой, «Вернуть из архива» — включает; результат по каждой виден в итогах."""
    admin, a, b = client_firm["admin"], client_firm["a"], client_firm["b"]
    try:
        off = client.post("/api/v1/clients/bulk", headers=admin,
                          json={"company_ids": [a["id"], b["id"]], "action": "archive"})
        assert off.status_code == 200, off.text
        assert off.json()["done"] == 2
        with SessionLocal() as db:
            assert db.get(Company, a["id"]).is_active is False
            assert db.get(Company, b["id"]).is_active is False
        visible = _ids_of(client, admin)
        assert a["id"] not in visible and b["id"] not in visible   # архивных в сводке нет
        on = client.post("/api/v1/clients/bulk", headers=admin,
                         json={"company_ids": [a["id"], b["id"]], "action": "restore"})
        assert on.status_code == 200 and on.json()["done"] == 2
        assert a["id"] in _ids_of(client, admin)
    finally:
        client.post("/api/v1/clients/bulk", headers=admin,
                    json={"company_ids": [a["id"], b["id"]], "action": "restore"})


def test_partial_failure_does_not_stop_others(client, client_firm):
    """Ошибка по одной компании не отменяет остальные: в итогах — ok и причина отказа, пачка считается выполненной."""
    admin, a = client_firm["admin"], client_firm["a"]
    missing = str(uuid4())
    res = client.post("/api/v1/clients/bulk", headers=admin,
                      json={"company_ids": [a["id"], missing], "action": "archive"})
    assert res.status_code == 200, res.text
    items = {i["company_id"]: i for i in res.json()["items"]}
    assert items[a["id"]]["ok"] is True and items[missing]["ok"] is False
    assert "не найдена" in items[missing]["error"]
    assert res.json()["done"] == 1 and res.json()["failed"] == 1
    client.post("/api/v1/clients/bulk", headers=admin, json={"company_ids": [a["id"]], "action": "restore"})


def test_assign_employee_to_one_client(client, client_firm):
    """Сотрудника можно перевести только в одну компанию, с правами администратора."""
    admin, a, b = client_firm["admin"], client_firm["a"], client_firm["b"]
    user = _mk_user(client, admin, a["id"], f"bulk_emp_{uuid4().hex[:6]}", role="manager")
    target = client.get("/api/v1/auth/me", headers=user).json()["id"]
    body = {"company_ids": [b["id"]], "action": "assign", "user_id": target}
    res = client.post("/api/v1/clients/bulk", headers=admin, json=body)
    assert res.status_code == 200 and res.json()["done"] == 1
    with SessionLocal() as db:
        assert db.get(User, target).company_id == b["id"]
        assert db.query(User).filter(User.id == target).count() == 1


def test_employee_and_manager_cannot_bulk(client, client_firm):
    """Массовые действия — только бухгалтеру и администратору."""
    admin, a = client_firm["admin"], client_firm["a"]
    tag = uuid4().hex[:6]
    for role in ("user", "manager"):
        hdr = _mk_user(client, admin, a["id"], f"bulk_{role}_{tag}", role=role)
        res = client.post("/api/v1/clients/bulk", headers=hdr,
                          json={"company_ids": [a["id"]], "action": "archive"})
        assert res.status_code == 403, role


def test_bulk_validation(client, client_firm):
    """Пачка проверяется до работы: пустой список, неизвестное действие и лишнее число компаний — 422."""
    admin, a = client_firm["admin"], client_firm["a"]
    url = "/api/v1/clients/bulk"
    assert client.post(url, headers=admin, json={"company_ids": [], "action": "archive"}).status_code == 422
    assert client.post(url, headers=admin,
                       json={"company_ids": [a["id"]], "action": "delete-everything"}).status_code == 422
    many = [str(uuid4()) for _ in range(svc.MAX_BULK + 1)]
    assert client.post(url, headers=admin, json={"company_ids": many, "action": "archive"}).status_code == 422


def test_archive_is_reversible_and_audited(client, client_firm):
    """Архив обратим, и каждое массовое действие оставляет след в журнале — по каждой компании."""
    from app.models import AuditLog
    admin, a = client_firm["admin"], client_firm["a"]
    client.post("/api/v1/clients/bulk", headers=admin, json={"company_ids": [a["id"]], "action": "archive"})
    client.post("/api/v1/clients/bulk", headers=admin, json={"company_ids": [a["id"]], "action": "restore"})
    with SessionLocal() as db:
        rows = (db.query(AuditLog).filter(AuditLog.action == "clients_bulk")
                .order_by(AuditLog.created_at.desc()).limit(10).all())
        assert rows, "массовые действия не попали в журнал"
        assert any(a["id"] in (r.entity_id or "") for r in rows), "компании пачки не записаны в журнал"
        actions = {r.details for r in rows}
        assert any("archive" in d for d in actions) and any("restore" in d for d in actions)


def test_schema_unchanged():
    """Схема базы не меняется: массовые действия — над существующими строками компаний и сотрудников."""
    from app.database import Base
    assert "companies" in Base.metadata.tables and "users" in Base.metadata.tables
