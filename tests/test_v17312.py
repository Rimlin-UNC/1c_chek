# -*- coding: utf-8 -*-
"""Тесты v1.73.12: Этап 1 архитектуры 5 ролей, полная карточка чека у сотрудника и пула,
приглашения команды с мультикомпанийностью и архитектурное зерно платных подписок."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.clients.models import AccountantClient
from app.config import settings
from app.database import SessionLocal
from app.models import Receipt, ReceiptItem
from app.pool import accounts as pool_accounts
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.services import entitlements, rbac
from tests.conftest import login

ROOT = Path(__file__).resolve().parent.parent


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def test_version_17312_synced():
    """Версия 1.73.12 синхронна во всех точках кэша, манифеста, блоков и журналов."""
    ver = settings.APP_VERSION
    assert tuple(int(x) for x in ver.split(".")) >= (1, 73, 12)
    sw = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
    assert f"ymaster-check-v{ver}" in sw
    idx = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert f"app.css?v={ver}" in idx and f"home.css?v={ver}" in idx and f"app.js?v={ver}" in idx
    b2b = (ROOT / "app/static/b2b/index.html").read_text(encoding="utf-8")
    assert f"b2b.css?v={ver}" in b2b and f"b2b.js?v={ver}" in b2b
    mx = (ROOT / "app/static/maxapp/index.html").read_text(encoding="utf-8")
    assert f"maxapp.js?v={ver}" in mx
    app_js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    assert "'1.73.12':" in app_js
    ui_js = (ROOT / "app/static/js/ui.js").read_text(encoding="utf-8")
    assert "user: 'Сотрудник'" in ui_js
    assert "## [1.73.12]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.73.12 —" in (ROOT / "docs/ui_journal.md").read_text(encoding="utf-8")


def test_rbac_stage1_spaces():
    """Пространства ролей соответствуют утверждённой спецификации Этапа 1."""
    assert "scan" in rbac.space_views("manager")
    assert "export" not in rbac.space_views("manager")
    assert rbac.space_views("donor") == frozenset({"public", "my"})
    for role in ("admin", "accountant", "manager", "user"):
        assert "device" in rbac.space_settings(role)
    assert rbac.hierarchy_is_intact()


def test_multi_company_accountant_and_manager_invites_and_employee_card(client):
    """Бухгалтер с несколькими компаниями и руководитель приглашают только в свои компании,
    а сотрудник видит закреплённого бухгалтера, руководителя и полную карточку своего чека."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()

    c1 = client.post("/api/v1/companies", headers=h_adm,
                     json={"name": f"ООО Альфа {tag}", "inn": "7707083893"}).json()
    c2 = client.post("/api/v1/companies", headers=h_adm,
                     json={"name": f"ООО Бета Клиент {tag}", "inn": "7814148471"}).json()
    c3 = client.post("/api/v1/companies", headers=h_adm,
                     json={"name": f"ООО Чужая {tag}", "inn": "7721546864"}).json()

    # Создаём бухгалтера в C1 и назначаем ей на ведение клиентскую компанию C2
    inv_acc = client.post("/api/v1/invites", headers=h_adm, json={
        "role": "accountant", "company_id": c1["id"], "max_uses": 1, "note": "Главбух",
    }).json()
    reg_acc = client.post("/api/v1/auth/register", json={
        "token": inv_acc["token"], "username": f"acc_{tag}",
        "password": "AccPass2026!", "full_name": "Светлана Главбух",
    })
    assert reg_acc.status_code == 200
    acc_uid = reg_acc.json()["user"]["id"]

    db = SessionLocal()
    try:
        db.add(AccountantClient(user_id=acc_uid, company_id=c2["id"]))
        db.commit()
    finally:
        db.close()

    h_acc = login(client, f"acc_{tag}", "AccPass2026!")

    # Бухгалтер видит ОБЕ свои компании (C1 и C2), но не чужую C3
    team_acc = client.get("/api/v1/company-team", headers=h_acc)
    assert team_acc.status_code == 200
    t_data = team_acc.json()
    assert t_data["can_invite"] is True
    m_ids = [c["id"] for c in t_data["manageable_companies"]]
    assert c1["id"] in m_ids and c2["id"] in m_ids and c3["id"] not in m_ids
    assert set(t_data["invitable_roles"]) == {"user", "manager", "accountant"}

    # Бухгалтер приглашает руководителя и сотрудника во ВТОРУЮ (клиентскую) компанию C2!
    r_inv_mgr = client.post("/api/v1/company-invites", headers=h_acc, json={
        "role": "manager", "company_id": c2["id"], "max_uses": 1, "note": "Директор Бета",
    })
    assert r_inv_mgr.status_code == 200, r_inv_mgr.text
    inv_mgr = r_inv_mgr.json()
    assert inv_mgr["company_id"] == c2["id"]

    # QR приглашения доступен бухгалтеру
    r_qr = client.get(f"/api/v1/company-invites/{inv_mgr['id']}/qr", headers=h_acc)
    assert r_qr.status_code == 200 and r_qr.headers["content-type"] == "image/png"

    # В чужую компанию C3 бухгалтер пригласить НЕ может -> 403
    r_forbid_foreign = client.post("/api/v1/company-invites", headers=h_acc, json={
        "role": "user", "company_id": c3["id"], "max_uses": 1,
    })
    assert r_forbid_foreign.status_code == 403

    # Регистрируем руководителя в C2 по приглашению бухгалтера
    reg_mgr = client.post("/api/v1/auth/register", json={
        "token": inv_mgr["token"], "username": f"mgr_{tag}",
        "password": "MgrPass2026!", "full_name": "Игорь Директор",
    })
    assert reg_mgr.status_code == 200
    h_mgr = login(client, f"mgr_{tag}", "MgrPass2026!")

    # Руководитель сканирует чек по своей корпоративной карте -> assignee заполняется его ФИО!
    num = int(uuid.uuid4().int % 800000) + 100000
    qr_mgr = f"t=20261120T1415&s=2450.00&fn=9960440300{num}&i={num}&fp=88{num}&n=1"
    r_scan_mgr = client.post("/api/v1/receipts/scan", headers=h_mgr, json={"qr_data": qr_mgr})
    assert r_scan_mgr.status_code in (200, 201), r_scan_mgr.text
    scan_rc = r_scan_mgr.json()["receipt"]
    assert scan_rc["assignee"] == "Игорь Директор"
    assert scan_rc["company_id"] == c2["id"]

    # Руководитель приглашает сотрудника И второго бухгалтера в свою компанию C2,
    # но НЕ может пригласить второго руководителя или в чужую компанию C1!
    r_mgr_emp = client.post("/api/v1/company-invites", headers=h_mgr, json={
        "role": "user", "company_id": c2["id"], "max_uses": 1, "note": "Снабжение",
    })
    assert r_mgr_emp.status_code == 200
    inv_emp = r_mgr_emp.json()

    r_mgr_acc2 = client.post("/api/v1/company-invites", headers=h_mgr, json={
        "role": "accountant", "company_id": c2["id"], "max_uses": 1, "note": "Бухгалтер Бета",
    })
    assert r_mgr_acc2.status_code == 200
    assert client.post(
        f"/api/v1/company-invites/{r_mgr_acc2.json()['id']}/revoke", headers=h_mgr
    ).status_code == 200

    assert client.post("/api/v1/company-invites", headers=h_mgr, json={
        "role": "manager", "company_id": c2["id"], "max_uses": 1,
    }).status_code == 403
    assert client.post("/api/v1/company-invites", headers=h_mgr, json={
        "role": "user", "company_id": c1["id"], "max_uses": 1,
    }).status_code == 403

    # Регистрируем сотрудника в C2
    reg_emp = client.post("/api/v1/auth/register", json={
        "token": inv_emp["token"], "username": f"emp_{tag}",
        "password": "EmpPass2026!", "full_name": "Пётр Сотрудник",
    })
    assert reg_emp.status_code == 200
    emp_uid = reg_emp.json()["user"]["id"]
    h_emp = login(client, f"emp_{tag}", "EmpPass2026!")

    # Сотрудник видит в карточке компании и внешнего бухгалтера Светлану, и руководителя Игоря,
    # но сам создавать приглашения НЕ может (403)
    emp_team = client.get("/api/v1/company-team", headers=h_emp).json()
    assert emp_team["can_invite"] is False
    assert emp_team["company"]["id"] == c2["id"]
    assert any("Светлана Главбух" in a["name"] for a in emp_team["accountants"])
    assert any("Игорь Директор" in m["name"] for m in emp_team["managers"])
    assert client.post("/api/v1/company-invites", headers=h_emp, json={
        "role": "user", "company_id": c2["id"], "max_uses": 1,
    }).status_code == 403

    # Сотрудник открывает свой чек с позициями и видит полную карточку чека + QR как у бухгалтера!
    db = SessionLocal()
    try:
        rc = Receipt(
            qr_data=f"t=20261121T1015&s=890.00&fn=9960449900{num}&i=7{num}&fp=55{num}&n=1",
            fn=f"9960449900{num}", fd=f"7{num}", fp=f"55{num}",
            receipt_date=datetime(2026, 11, 21, 10, 15, tzinfo=timezone.utc).replace(tzinfo=None),
            total_sum=890.0, operation=1, status="verified", fns_status="valid",
            merchant_name="ООО Комус", merchant_inn="7721546864",
            company_id=c2["id"], created_by=emp_uid, assignee="Пётр Сотрудник",
        )
        db.add(rc)
        db.flush()
        db.add(ReceiptItem(
            receipt_id=rc.id, name="Бумага офисная А4 SvetoCopy",
            quantity=2.0, price=445.0, total=890.0, vat_rate="20", vat_sum=148.33,
        ))
        rc_id = rc.id
        db.commit()
    finally:
        db.close()

    emp_card = client.get(f"/api/v1/receipts/{rc_id}/card", headers=h_emp)
    assert emp_card.status_code == 200
    doc = emp_card.json()["document"]
    assert doc["seller"]["name"] == "ООО Комус"
    assert len(doc["items"]) == 1 and "SvetoCopy" in doc["items"][0]["name"]
    emp_qr = client.get(f"/api/v1/receipts/{rc_id}/qr.png", headers=h_emp)
    assert emp_qr.status_code == 200 and emp_qr.headers["content-type"] == "image/png"


def test_pool_participant_rich_receipt_card_and_search(client):
    """Участник Чек-Пула ищет свои чеки и открывает полную карточку чека с QR как у бухгалтера."""
    tag = _uid()
    num = int(uuid.uuid4().int % 800000) + 100000
    db = SessionLocal()
    try:
        u1 = PoolUser(email=f"donor1_{tag}@example.com", points=45, email_verified=True)
        u2 = PoolUser(email=f"donor2_{tag}@example.com", points=10, email_verified=True)
        db.add_all([u1, u2])
        db.flush()
        pr1 = PoolReceipt(
            pool_user_id=u1.id,
            qr_data=f"t=20261122T1830&s=1290.00&fn=99601100{num}&i=3{num}&fp=99{num}&n=1",
            fn=f"99601100{num}", fd=f"3{num}", fp=f"99{num}",
            receipt_date=datetime(2026, 11, 22, 18, 30, tzinfo=timezone.utc).replace(tzinfo=None),
            total_sum=1290.0, operation=1,
            merchant_name="ООО Лента", merchant_inn="7814148471",
            merchant_address="г. Санкт-Петербург, Пулковское ш., 33",
            status="verified", fns_confirmed=True, full_data=True, points_awarded=5,
        )
        pr2 = PoolReceipt(
            pool_user_id=u2.id,
            fn=f"99602200{num}", fd=f"4{num}", fp=f"77{num}",
            total_sum=500.0, status="verified",
        )
        db.add_all([pr1, pr2])
        db.flush()
        db.add(PoolItem(
            receipt_id=pr1.id, name="Кофе зерновой 500 г",
            quantity=1.0, price=1290.0, total=1290.0, vat_rate="20", vat_sum=215.0,
        ))
        tok1 = pool_accounts.issue_pool_token(u1.id)
        pr1_id, pr2_id = pr1.id, pr2.id
        db.commit()
    finally:
        db.close()

    hp = {"Authorization": f"Bearer {tok1}"}

    # Поиск по магазину в Моих чеках
    r_list = client.get("/api/v1/pool-my/receipts?q=Лента", headers=hp)
    assert r_list.status_code == 200 and r_list.json()["total"] == 1
    assert client.get("/api/v1/pool-my/receipts?q=НесуществующийМагазин", headers=hp).json()["total"] == 0

    # Полная карточка чека и QR-код (как у бухгалтера)
    r_card = client.get(f"/api/v1/pool-my/receipt/{pr1_id}/card", headers=hp)
    assert r_card.status_code == 200
    card = r_card.json()
    assert card["points_awarded"] == 5
    assert card["document"]["seller"]["name"] == "ООО Лента"
    assert len(card["document"]["items"]) == 1
    assert card["document"]["items"][0]["name"] == "Кофе зерновой 500 г"

    r_qr = client.get(f"/api/v1/pool-my/receipt/{pr1_id}/qr.png", headers=hp)
    assert r_qr.status_code == 200 and r_qr.headers["content-type"] == "image/png"

    # Чужой чек другого донора недоступен -> 404
    assert client.get(f"/api/v1/pool-my/receipt/{pr2_id}/card", headers=hp).status_code == 404


def test_subscription_entitlements_seed(client):
    """Модульный реестр подписок отдаётся в /api/v1/auth/me и умеет ограничивать модули компании."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    comp = client.post("/api/v1/companies", headers=h_adm,
                       json={"name": f"Тариф Тест {tag}"}).json()
    inv = client.post("/api/v1/invites", headers=h_adm, json={
        "role": "accountant", "company_id": comp["id"], "max_uses": 1,
    }).json()
    client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": f"sub_{tag}",
        "password": "SubPass2026!", "full_name": "Бухгалтер Подписка",
    })
    h_sub = login(client, f"sub_{tag}", "SubPass2026!")

    me_default = client.get("/api/v1/auth/me", headers=h_sub).json()
    codes = {m["code"] for m in me_default["entitlements"]["catalog"]}
    assert codes == {"receipts", "upd", "manager_cabinet", "counterparties", "contracts"}
    assert "primary" in me_default["space"]["active_views"]
    assert "receipts" in me_default["space"]["active_views"]

    # Оставляем компании только подписку «Чеки» (без УПД и Контрагентов)
    db = SessionLocal()
    try:
        entitlements.set_company_modules(db, comp["id"], ["receipts"])
        db.commit()
    finally:
        db.close()

    me_limited = client.get("/api/v1/auth/me", headers=h_sub).json()
    active_views = set(me_limited["space"]["active_views"])
    assert "receipts" in active_views and "scan" in active_views and "export" in active_views
    assert "primary" not in active_views and "bank" not in active_views and "counterparties" not in active_views

    # У администратора системы всегда активны все экраны
    me_adm = client.get("/api/v1/auth/me", headers=h_adm).json()
    assert "primary" in me_adm["space"]["active_views"]
