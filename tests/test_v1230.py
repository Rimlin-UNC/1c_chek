# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.23.0: фильтры базы чеков (кто добавил / полные
# данные) и официальная форма АО-1 (Постановление №55, ОКУД 0302001).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login

QR = "t=20260915T1200&s=800.00&fn=9999078902001299&i=61001&fp=888999001&n=1"


def _mk_user(client, role, username, company_id=None):
    hdr = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites", json={
        "role": role, "company_id": company_id}, headers=hdr).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Созда {username}"})
    assert r.status_code in (200, 201), r.text
    j = r.json()
    uid = j["user"]["id"] if "user" in j else j.get("id")
    return {"Authorization": "Bearer " + j["access_token"]}, uid


class TestFullDataFlag:
    def test_external_sets_flag(self, client, monkeypatch):
        import app.routers.receipts as rr
        import app.services.external as ex
        u, _uid = _mk_user(client, "user", "fd_usr")
        s = client.post("/api/v1/receipts/scan", headers=u, json={
            "qr_data": QR, "source": "manual", "verify": False})
        assert s.status_code == 200, s.text
        rid = s.json()["receipt"]["id"]
        # имитируем успешную загрузку полных данных
        class R:
            ok = True; found = True; source = "fns_api"; message = "Данные получены"
            date_time = None; total_sum = None; operation = None
            merchant_name = "ООО Магнит-Тест"; merchant_inn = "7704001236"
            merchant_address = "г. Москва"; cashier = ""; cash_sum = None
            ecash_sum = None; items = []; raw = {}
        monkeypatch.setattr(ex.engine, "fetch",
                            lambda db, *a, **k: R())
        adm = login(client, "admin", "admin123")
        r = client.post(f"/api/v1/receipts/{rid}/fetch-details", headers=adm)
        assert r.status_code == 200, r.text
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            rec = db.get(Receipt, rid)
            assert rec.full_data is True
        finally:
            db.close()
        # фильтр «получены» находит
        q = client.get("/api/v1/receipts?full_data=true", headers=u)
        assert any(x["id"] == rid for x in q.json()["items"])
        # фильтр «ожидают» — не находит
        q2 = client.get("/api/v1/receipts?full_data=false", headers=u)
        assert not any(x["id"] == rid for x in q2.json()["items"])

    def test_creator_filter(self, client):
        u1, uid1 = _mk_user(client, "user", "cr_one")
        u2, uid2 = _mk_user(client, "user", "cr_two")
        client.post("/api/v1/receipts/scan", headers=u1, json={
            "qr_data": "t=20260916T1000&s=210.00&fn=9999078902001307&i=61021&fp=888999021&n=1",
            "source": "manual", "verify": False})
        client.post("/api/v1/receipts/scan", headers=u2, json={
            "qr_data": "t=20260916T1005&s=220.00&fn=9999078902001314&i=61022&fp=888999022&n=1",
            "source": "manual", "verify": False})
        adm = login(client, "admin", "admin123")
        # список создателей
        cr = client.get("/api/v1/receipts/creators", headers=adm).json()
        ids = {x["id"] for x in cr}
        assert uid1 in ids and uid2 in ids
        row = next(x for x in cr if x["id"] == uid1)
        assert row["count"] >= 1 and row["name"]
        # фильтр по создателю
        q = client.get(f"/api/v1/receipts?creator={uid1}", headers=adm)
        items = q.json()["items"]
        assert items and all(x["created_by_id"] == uid1 for x in items)

    def test_ui_filter_and_row(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="f-creator"' in js and 'id="f-full"' in js
        assert "/api/v1/receipts/creators" in js
        # v1.25.2: у чека с полными данными кнопки запроса нет вовсе —
        # только «изменить»; 📥 остаётся для чеков без полных данных
        assert "r.full_data ? '' :" in js
        assert "📥✓" not in js


class TestAO1Official:
    def test_advance_report_has_requisites(self, client, monkeypatch):
        from app.routers import companies as cr
        adm = login(client, "admin", "admin123")
        cc = client.post("/api/v1/companies", json={
            "name": "ООО АО-Карточка", "inn": "9909000011"}, headers=adm)
        assert cc.status_code == 201, cc.text
        comp = cc.json()
        monkeypatch.setattr(cr.checko, "fetch_card", lambda k, inn: {"card": {
            "kind": "legal", "inn": inn, "name_full": "ООО «АО-Карточка Плюс»",
            "name_short": "АО-Карточка", "kpp": "770401001", "okpo": "55667788",
            "ogrn": "1157704000001", "address": "г. Москва, Ленина, 1",
            "director": "Смирнов А.В."}})
        client.post(f"/api/v1/companies/{comp['id']}/refresh-card", headers=adm)
        u, _ = _mk_user(client, "user", "ao_emp", comp["id"])
        client.post("/api/v1/receipts/scan", headers=u, json={
            "qr_data": "t=20260915T1200&s=950.00&fn=9999078902001299&i=61010&fp=888999010&n=1",
            "source": "manual", "verify": False})
        rep = client.post("/api/v1/receipts/advance-report", headers=adm,
                          json={"date_from": "2026-09-01", "date_to": "2026-09-30",
                                "company_id": comp["id"],
                                "assignee": "Созда ao_emp"}).json()
        assert rep["rows"], "чек должен попасть в отчёт"
        org = rep["requisites"]["Организация"]
        assert org["НаименованиеПолное"] == "ООО «АО-Карточка Плюс»"
        assert org["КПП"] == "770401001" and org["ОКПО"] == "55667788"
        assert rep["person"]["ФИО"] == "Созда ao_emp"

    def test_ui_official_form(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("0302001", "Форма по ОКУД", "УТВЕРЖДАЮ",
                       "Оборотная сторона формы № АО-1", "ao1Json",
                       "JSON для 1С", "ao1-calc", "ao1-receipt",
                       "Получено из кассы", "ao-dept", "ao-head"):
            assert needle in js, needle

    def test_ui_no_old_simple_form(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "АВАНСОВЫЙ ОТЧЁТ №" not in js   # старая упрощённая шапка ушла

    def test_css_ao1(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        for needle in (".ao1-page", ".ao1-calc", ".ao1-back-table",
                       ".ao1-receipt", "page-break-after"):
            assert needle in css, needle


class TestVersion1230:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # актуальный пин — в тесте текущей версии (v1.24.0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        assert "ADD COLUMN full_data" in open("app/database.py",
                                              encoding="utf-8").read()
