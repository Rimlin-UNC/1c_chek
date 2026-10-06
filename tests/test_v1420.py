# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.42.0: иерархия ролей.
# 1) Меню переключения ролей — только у администратора (не-админ его
#    не видит вовсе). 2) В режиме просмотра права строго равны правам
#    роли: сотрудник правит только уведомление/комментарий своего чека,
#    бухгалтер — любые поля чека компании; админ-эндпоинты недоступны.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
from datetime import datetime

from tests.conftest import login


def _mk_company(db, name):
    """Компания напрямую моделью (фикстура client — session-scope: API мог
    бы ответить 409 «дубли» на имена из других тестов)."""
    from app.models import Company
    c = Company(name=name)
    db.add(c)
    db.commit()
    db.refresh(c)
    cid = c.id
    db.close()
    return cid


def _mk_role_user(client, role, username, company_id):
    """Приглашение с компанией → (заголовки НОВОГО пользователя, id)."""
    adm = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites",
                      json={"role": role, "company_id": company_id},
                      headers=adm).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    uid = r.json()["user"]["id"] if "user" in r.json() else r.json()["id"]
    return {"Authorization": "Bearer " + r.json()["access_token"]}, uid


def _mk_receipt(db, company_id, user_id, fn):
    from app.models import Receipt
    r = Receipt(qr_data=f"t=20260101T1200&s=500.00&fn={fn}&i=1&fp=1&n=1",
                fn=fn, fd="1", fp="1",
                receipt_date=datetime.utcnow(), total_sum=500.0,
                status="verified", fns_status="valid", source="manual",
                company_id=company_id, created_by=user_id, raw_data="{}")
    db.add(r)
    db.commit()
    db.refresh(r)
    rid = r.id
    db.close()
    return rid


class TestVersion1420:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.42.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "v=1.41.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.42.0':" in js
        assert js.index("'1.42.0':") < js.index("'1.41.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.42.0]") == 1
        assert ch.index("## [1.42.0]") < ch.index("## [1.41.0]")
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "только у\nадминистратора" in manual


class TestMenuAdminOnly:
    """Меню переключения ролей видит только администратор."""

    def test_persona_menu_guarded(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # не-админ: только свой профиль + пояснение
        assert "Переключение между профилями доступно только администратору." in js
        # клик по карточке активирует меню только у админа/в режиме просмотра
        assert "if (isAdmin() || viewing) {" in js
        assert "chip.onclick = null;" in js
        # раздел «Посмотреть глазами» в шаблоне один и только у админа
        assert js.count('Посмотреть глазами <span class="pm-hint">пароль не нужен</span>') == 1

    def test_hierarchy_callout_in_users(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Иерархия ролей: <b>Администратор</b> (всё) →" in js
        assert "отдельный контур" in js.lower() or "Отдельный контур" in js


class TestRoleScopedEdit:
    """В режиме просмотра права = права роли (не выше и не ниже)."""

    def test_employee_can_only_notify_comment(self, client):
        from app.database import SessionLocal
        adm = login(client, "admin", "admin123")
        db = SessionLocal()
        cid = _mk_company(db, "ООО «Иерархия-Ус»")
        uhdr, uid = _mk_role_user(client, "user", "hier_usr", cid)
        rid = _mk_receipt(db, cid, uid, "1420000000001")
        d = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm).json()
        vh = {"Authorization": "Bearer " + d["access_token"]}
        # своё: уведомление и комментарий — можно
        r1 = client.patch(f"/api/v1/receipts/{rid}",
                          json={"comment": "комментарий сотрудника"}, headers=vh)
        assert r1.status_code == 200, r1.text
        r2 = client.patch(f"/api/v1/receipts/{rid}", json={"notified": True},
                          headers=vh)
        assert r2.status_code == 200
        # суммы и реквизиты — нельзя (права роли не выше положенного)
        r3 = client.patch(f"/api/v1/receipts/{rid}", json={"personal_sum": 100},
                          headers=vh)
        assert r3.status_code == 400
        assert "уведомление и комментарий" in r3.json()["detail"]
        # админ-эндпоинт в режиме просмотра недоступен
        assert client.get("/api/v1/users", headers=vh).status_code == 403

    def test_accountant_edits_company_receipt(self, client):
        from app.database import SessionLocal
        adm = login(client, "admin", "admin123")
        db = SessionLocal()
        cid = _mk_company(db, "ООО «Иерархия-Эр»")
        ahdr, aid = _mk_role_user(client, "accountant", "hier_acc", cid)
        uhdr, uid = _mk_role_user(client, "user", "hier_usr2", cid)
        rid = _mk_receipt(db, cid, uid, "1420000000002")
        d = client.post(f"/api/v1/admin/impersonate/{aid}", headers=adm).json()
        vh = {"Authorization": "Bearer " + d["access_token"]}
        # бухгалтер: суммы чека компании — можно
        r = client.patch(f"/api/v1/receipts/{rid}", json={"personal_sum": 80.5},
                         headers=vh)
        assert r.status_code == 200, r.text
        assert abs(float(r.json().get("personal_sum")) - 80.5) < 0.01
        # а раздел пользователей — нельзя (это уже админ)
        assert client.get("/api/v1/users", headers=vh).status_code == 403
