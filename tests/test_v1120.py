# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.12.0: компании из «памятки» приглашений.
# Однократная миграция: памятка → компания; перепривязка приглашений,
# сотрудников и их чеков. company_name в приглашении (найти/создать).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


def _qr(sum_: str = "500.00") -> str:
    import itertools
    if not hasattr(_qr, "n"):
        _qr.n = itertools.count(1000)
    n = next(_qr.n)
    return (f"t=20261004T1530&s={sum_}&fn=7381440700{n:07d}"
            f"&i={n}&fp={800000000 + n}&n=1")


class TestInviteByCompanyName:
    def test_company_name_creates_and_reuses(self, client):
        hdr = login(client, "admin", "admin123")
        # создаёт новую компанию
        r = client.post("/api/v1/invites", json={
            "role": "user", "company_name": 'ООО «Мигрант-1»'}, headers=hdr)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["company_name"] == 'ООО «Мигрант-1»'
        # второй вызов с тем же названием (в другом регистре) — та же компания
        r2 = client.post("/api/v1/invites", json={
            "role": "accountant", "company_name": 'ооо «мигрант-1»'}, headers=hdr)
        assert r2.status_code == 200
        assert r2.json()["company_id"] == inv["company_id"]
        # компания в общем списке
        comps = client.get("/api/v1/companies", headers=hdr).json()
        assert any(c["id"] == inv["company_id"] for c in comps)

    def test_register_via_named_company(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={
            "role": "accountant", "company_name": 'ООО «Мигрант-2»',
            "note": "бухгалтер"}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "migrant2_buh",
            "password": "parol123", "full_name": "Бух Мигрант-2"})
        assert reg.status_code == 200
        hdr_u = {"Authorization": "Bearer " + reg.json()["access_token"]}
        me = client.get("/api/v1/auth/me", headers=hdr_u).json()
        assert me["company_name"] == 'ООО «Мигрант-2»'


class TestNotesToCompaniesMigration:
    def test_migration_binds_invites_users_receipts(self, client):
        """Памятка без компании → регистрация без компании → чеки без компании;
        после migrate_companies_from_notes() всё связано в компанию."""
        from app.database import SessionLocal, migrate_companies_from_notes
        from app.models import AppSetting

        hdr = login(client, "admin", "admin123")
        # приглашение с памяткой, но БЕЗ компании (как до 1.11.x)
        inv = client.post("/api/v1/invites", json={
            "role": "user", "note": 'ООО «Старый-Клиент»'}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "old_client_u",
            "password": "parol123", "full_name": "Сотрудник Старого"})
        assert reg.status_code == 200
        hdr_u = {"Authorization": "Bearer " + reg.json()["access_token"]}
        me = client.get("/api/v1/auth/me", headers=hdr_u).json()
        assert me["organization"] == 'ООО «Старый-Клиент»'
        assert me["company_id"] is None          # ещё не привязан
        # чек сотрудника без компании
        r = client.post("/api/v1/receipts/scan",
                        json={"qr_data": _qr("333.00"), "source": "web"},
                        headers=hdr_u)
        assert r.status_code == 200
        rid = r.json()["receipt"]["id"]
        assert r.json()["receipt"]["company_id"] is None

        # сбрасываем флаг и запускаем миграцию (на бою она идёт при старте)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(
                AppSetting.key == "v1112_notes_companies_done").first()
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()
        out = migrate_companies_from_notes()
        assert out.get("skipped") is None, out

        # компания создана из памятки, пользователь и чек перепривязаны
        me2 = client.get("/api/v1/auth/me", headers=hdr_u).json()
        assert me2["company_name"] == 'ООО «Старый-Клиент»'
        d = client.get(f"/api/v1/receipts/{rid}", headers=hdr_u).json()
        assert d["company_id"] == me2["company_id"]
        # приглашение тоже привязано
        invs = client.get("/api/v1/invites", headers=hdr).json()
        target = next(x for x in invs if x["id"] == inv["id"])
        assert target["company_id"] == me2["company_id"]
        # идемпотентность: повтор — без изменений
        out2 = migrate_companies_from_notes()
        assert out2.get("users_moved", 0) >= 0        # не падает
        me3 = client.get("/api/v1/auth/me", headers=hdr_u).json()
        assert me3["company_id"] == me2["company_id"]


class TestNotesUi:
    def test_invite_dialog_company_first_and_filter(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "iv-company-name" in js and "iv-company-list" in js
        assert "company_name: companyNameVal" in js
        assert "p-company-filter" in js                  # фильтр группы
        assert "Компания (группа)" in js                 # колонка приглашений
