# Ямастер Чек — тесты v1.37.0: «Чек-Пул» этап 8 — подбор из пула для компаний.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 8 (docs/dev_plan_checkpool.md):
#  - раздел бухгалтера «Подбор из пула»: фильтры период/регион/отрасль/сумма/
#    ИНН/поиск → привязка чеков к компании (assigned_company_id), выгрузка CSV;
#  - авто-подбор под авансовый отчёт (набор чеков ≈ сумма ±5%);
#  - импорт «своих» сотрудников (сценарий C): чек с подтверждённым e-mail,
#    совпадающим с логином сотрудника, уходит компании, не в общий пул;
#  - квоты/тариф: лимит подбора на компанию в месяц, привязанный чек
#    чужим не виден; ядро 1С/АО-1 не изменено.
import itertools
import time
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal, engine
from app.pool import ingest
from app.pool.models import (PoolAchievement, PoolConsent, PoolFingerprint,
                             PoolItem, PoolIpLog, PoolPoint, PoolReceipt,
                             PoolReferral, PoolSignal, PoolToken, PoolUser,
                             PoolWithdrawal, ensure_pool_schema)

ensure_pool_schema(engine)

_SEQ = itertools.count(7000)
_C = itertools.count(50)


def _db():
    return SessionLocal()


def _wipe_pool():
    db = _db()
    for _ in range(40):
        try:
            for t in (PoolItem, PoolReceipt, PoolPoint, PoolSignal, PoolIpLog,
                      PoolFingerprint, PoolAchievement, PoolWithdrawal,
                      PoolReferral, PoolToken, PoolConsent, PoolUser):
                db.query(t).delete(synchronize_session=False)
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1370: не удалось очистить пул")


@pytest.fixture(autouse=True)
def _clean(client):
    from app.pool import router_auth
    router_auth._HITS.clear()
    _wipe_pool()
    db = _db()
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1")
    appsettings.set_setting(db, "pool_pick_monthly_limit", "100")
    db.commit()
    db.close()
    yield
    router_auth._HITS.clear()
    _wipe_pool()


def _seed_receipt(region="78", city="Санкт-Петербург", industry="food",
                  total=300.0, days_ago=3, inn="7801000001",
                  merchant="Магнит", name="Кофе"):
    """Верифицированный незакреплённый чек (юнит-посев, уникальный fn/fp)."""
    n = next(_SEQ)
    db = _db()
    r = PoolReceipt(source="web", fn=str(n), fd=str(n), fp=str(n),
                    receipt_date=datetime.utcnow() - timedelta(days=days_ago),
                    total_sum=total, merchant_name=merchant, merchant_inn=inn,
                    region_code=region, city=city, industry=industry,
                    status="verified", full_data=True,
                    verified_at=datetime.utcnow() - timedelta(days=days_ago),
                    created_at=datetime.utcnow() - timedelta(days=days_ago))
    db.add(r)
    db.flush()
    db.add(PoolItem(receipt_id=r.id, name=name, quantity=1.0, price=total,
                    total=total))
    db.commit()
    rid = r.id
    db.close()
    return rid


def _setup_company(client, name="ООО Тестбор"):
    """Компания + бухгалтер. Возвращает (headers бухгалтера, company_id)."""
    from tests.conftest import login
    adm = login(client, "admin", "admin123")
    n = next(_C)
    r = client.post("/api/v1/companies", headers=adm,
                    json={"name": f"{name} {n:02d}"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = client.post("/api/v1/users", headers=adm,
                    json={"username": f"acc-{n}@tb.ru", "password": "par-123456",
                          "full_name": "Борис Бухгалтеров", "role": "accountant",
                          "company_id": cid})
    assert r.status_code in (200, 201), r.text
    acc = login(client, f"acc-{n}@tb.ru", "par-123456")
    return acc, cid


class TestAccess:
    def test_requires_core_auth(self):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)                    # без lifespan — админ не сеется
        assert c.get("/api/v1/pool-company/search").status_code == 401
        assert c.get("/api/v1/pool-company/quota").status_code == 401

    def test_core_user_forbidden(self, client):
        from tests.conftest import login
        acc, cid = _setup_company(client)
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/users", headers=hdr,
                        json={"username": f"plain-{next(_C)}@tb.ru",
                              "password": "par-123456", "role": "user",
                              "company_id": cid})
        assert r.status_code in (200, 201)
        plain = login(client, r.json()["username"], "par-123456")
        assert client.get("/api/v1/pool-company/search",
                          headers=plain).status_code == 403
        assert client.get("/api/v1/pool-company/search",
                          headers=acc).status_code == 200


class TestSearch:
    def test_excludes_assigned_and_filters(self, client):
        acc, cid = _setup_company(client)
        free = _seed_receipt(region="78", city="Санкт-Петербург",
                             industry="food", total=500.0)
        other = _seed_receipt(region="77", city="Москва", industry="fuel",
                              total=900.0, days_ago=10, inn="7701000002",
                              merchant="Лукойл", name="Дизель")
        old = _seed_receipt(region="78", city="Санкт-Петербург",
                            industry="food", total=200.0, days_ago=40)
        # фильтр регион
        d = client.get("/api/v1/pool-company/search", headers=acc,
                       params={"region": "78"}).json()
        ids = {i["id"] for i in d["items"]}
        assert free in ids and old in ids and other not in ids
        # период
        d = client.get("/api/v1/pool-company/search", headers=acc,
                       params={"date_from": (datetime.utcnow()
                                             - timedelta(days=15)
                                             ).strftime("%Y-%m-%d")}).json()
        ids = {i["id"] for i in d["items"]}
        assert free in ids and other in ids and old not in ids
        # сумма
        d = client.get("/api/v1/pool-company/search", headers=acc,
                       params={"sum_min": 400, "sum_max": 800}).json()
        assert {i["id"] for i in d["items"]} == {free}
        # поиск по товару (кириллица — Python-фильтр)
        d = client.get("/api/v1/pool-company/search", headers=acc,
                       params={"q": "дизель"}).json()
        assert {i["id"] for i in d["items"]} == {other}
        # город (кириллица, частичное совпадение)
        d = client.get("/api/v1/pool-company/search", headers=acc,
                       params={"city": "москв"}).json()
        assert {i["id"] for i in d["items"]} == {other}
        # привязка убирает из выдачи чужим и себе
        r = client.post("/api/v1/pool-company/assign", headers=acc,
                        json={"receipt_ids": [free]})
        assert r.status_code == 200
        d = client.get("/api/v1/pool-company/search", headers=acc).json()
        assert free not in {i["id"] for i in d["items"]}
        mine = client.get("/api/v1/pool-company/mine", headers=acc).json()
        assert free in {i["id"] for i in mine["items"]}


class TestAssignQuota:
    def test_assign_fields_and_unassign(self, client):
        acc, cid = _setup_company(client)
        rid = _seed_receipt()
        r = client.post("/api/v1/pool-company/assign", headers=acc,
                        json={"receipt_ids": [rid]})
        assert r.status_code == 200 and r.json()["assigned"] == 1
        db = _db()
        row = db.get(PoolReceipt, rid)
        db.close()
        assert row.assigned_company_id == cid
        assert row.assigned_at is not None and row.assigned_by
        # повторно занятый чек нельзя
        r2 = client.post("/api/v1/pool-company/assign", headers=acc,
                         json={"receipt_ids": [rid]})
        assert r2.status_code == 409
        # возврат в пул
        r3 = client.post("/api/v1/pool-company/unassign", headers=acc,
                         json={"receipt_ids": [rid]})
        assert r3.status_code == 200
        db = _db()
        row = db.get(PoolReceipt, rid)
        db.close()
        assert row.assigned_company_id is None
        q = client.get("/api/v1/pool-company/quota", headers=acc).json()
        assert q["used"] == 0, "возврат в пул освобождает квоту месяца"

    def test_monthly_limit_and_admin_setting(self, client):
        from tests.conftest import login
        acc, cid = _setup_company(client)
        adm = login(client, "admin", "admin123")
        r = client.put("/api/v1/pool-admin/settings", headers=adm,
                       json={"enabled": True, "pick_limit": 2})
        assert r.status_code == 200
        ids = [_seed_receipt() for _ in range(3)]
        r = client.post("/api/v1/pool-company/assign", headers=acc,
                        json={"receipt_ids": ids[:2]})
        assert r.status_code == 200
        r = client.post("/api/v1/pool-company/assign", headers=acc,
                        json={"receipt_ids": [ids[2]]})
        assert r.status_code == 409, "лимит 2/мес исчерпан"
        q = client.get("/api/v1/pool-company/quota", headers=acc).json()
        assert q["limit"] == 2 and q["used"] == 2 and q["left"] == 0
        # 0 — подбор выключен
        client.put("/api/v1/pool-admin/settings", headers=adm,
                   json={"enabled": True, "pick_limit": 0})
        _2 = _seed_receipt()
        r = client.post("/api/v1/pool-company/assign", headers=acc,
                        json={"receipt_ids": [_2]})
        assert r.status_code == 403

    def test_other_company_cannot_see_or_unassign(self, client):
        acc1, cid1 = _setup_company(client, name="ООО Альфа")
        acc2, cid2 = _setup_company(client, name="ООО Бета")
        rid = _seed_receipt()
        assert client.post("/api/v1/pool-company/assign", headers=acc1,
                           json={"receipt_ids": [rid]}).status_code == 200
        # чужая компания не видит привязанный чек и не может его вернуть
        d = client.get("/api/v1/pool-company/mine", headers=acc2).json()
        assert rid not in {i["id"] for i in d["items"]}
        r = client.post("/api/v1/pool-company/unassign", headers=acc2,
                        json={"receipt_ids": [rid]})
        assert r.status_code == 200 and r.json()["unassigned"] == 0
        db = _db()
        assert db.get(PoolReceipt, rid).assigned_company_id == cid1
        db.close()


class TestAutosuggest:
    def test_sum_within_5pct(self, client):
        acc, cid = _setup_company(client)
        a = _seed_receipt(total=600.0, merchant="А")
        b = _seed_receipt(total=400.0, merchant="Б")
        c = _seed_receipt(total=50.0, merchant="В")
        d = client.post("/api/v1/pool-company/autosuggest", headers=acc,
                        json={"target_sum": 1000})
        assert d.status_code == 200
        j = d.json()
        assert j["ok"] is True and j["sum"] == 1000.0
        assert set(j["ids"]) == {a, b}
        # вернуть не подобранное — не мешает
        r = client.post("/api/v1/pool-company/assign", headers=acc,
                        json={"receipt_ids": j["ids"]})
        assert r.status_code == 200

    def test_no_match(self, client):
        acc, _ = _setup_company(client)
        _seed_receipt(total=10.0)
        j = client.post("/api/v1/pool-company/autosuggest", headers=acc,
                        json={"target_sum": 5000}).json()
        assert j["ok"] is False

    def test_too_small_target(self, client):
        acc, _ = _setup_company(client)
        assert client.post("/api/v1/pool-company/autosuggest", headers=acc,
                           json={"target_sum": 10}).status_code == 422


class TestScenarioC:
    def _mk_employee(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        n = next(_C)
        r = client.post("/api/v1/companies", headers=adm,
                        json={"name": f"ООО Сотрудники {n:02d}"})
        cid = r.json()["id"]
        uname = f"emp-{n}@corp.ru"
        r = client.post("/api/v1/users", headers=adm,
                        json={"username": uname, "password": "par-123456",
                              "full_name": "Эдуард Сотрудников",
                              "role": "user", "company_id": cid})
        assert r.status_code in (200, 201), r.text
        return cid, uname

    def test_verified_email_assigns_to_company(self, client, monkeypatch):
        cid, uname = self._mk_employee(client)
        from app.services.external import ExternalItem, ExternalResult
        monkeypatch.setattr(ingest, "_fetch_details", lambda *a, **k: ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=datetime(2026, 10, 6, 12, 0), total_sum=1250.0, operation=1,
            merchant_name="Магнит", merchant_inn="7801000001",
            merchant_address="г Санкт-Петербург, Невский проспект 20",
            items=[ExternalItem(name="Кофе", quantity=1.0, price=1250.0,
                                total=1250.0)]))
        db = _db()
        u = PoolUser(email=uname, email_verified=True, vid="emp-vid-1")
        db.add(u)
        db.commit()
        uid = u.id
        db.close()
        res = ingest.ingest_receipt(
            db, f"t=20261006T1200&s=1250.00&fn=9999077777{next(_SEQ) & 9999:04d}"
                f"&i=77701&fp=77701&n=1", "web", u)
        assert res["result"] == "verified"
        db.commit()
        db.close()
        db = _db()
        row = db.get(PoolReceipt, res["receipt_id"])
        ach = db.query(PoolAchievement).all()
        db.close()
        assert row.assigned_company_id == cid, "сценарий C: чек ушёл компании"
        assert "сотрудника компании" in row.status_message
        assert row.assigned_by == "employee-link"

    def test_unverified_email_not_assigned(self, client):
        cid, uname = self._mk_employee(client)
        db = _db()
        u = PoolUser(email=uname, email_verified=False, vid="emp-vid-2")
        db.add(u)
        db.commit()
        from app.pool.router_company import assign_employee_receipt
        r = PoolReceipt(source="web", fn="1", fd="1", fp="1",
                        total_sum=100.0, status="verified")
        db.add(r)
        db.flush()
        assert assign_employee_receipt(db, u, r) is False
        assert r.assigned_company_id is None
        db.rollback()
        db.close()

    def test_employees_listing(self, client):
        cid, uname = self._mk_employee(client)
        acc, _ = _setup_company(client)      # чужой бухгалтер не видит
        db = _db()
        db.add(PoolUser(email=uname, email_verified=True))
        db.commit()
        db.close()
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/pool-company/employees", headers=adm,
                       params={"company_id": cid})
        assert r.status_code == 200
        j = r.json()
        assert len(j["items"]) == 1
        assert j["items"][0]["pool_linked"] is True
        assert j["items"][0]["username"] == uname


class TestExportCsv:
    def test_csv_of_company_receipts(self, client):
        acc, cid = _setup_company(client)
        rid = _seed_receipt(total=1234.5, name="Кофе зерновой")
        assert client.post("/api/v1/pool-company/assign", headers=acc,
                           json={"receipt_ids": [rid]}).status_code == 200
        r = client.get("/api/v1/pool-company/export.csv", headers=acc)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        body = r.content.decode("utf-8-sig")
        assert "Дата чека" in body and "Позиции" in body
        assert "Магнит" in body and "Кофе зерновой" in body
        assert "1234,50" in body


class TestVersion1370:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.37.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.37.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.37.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "подбор для компаний (v1.37.0)" in manual

    def test_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "viewPoolPick" in js and "poolpk" in js
        assert "/api/v1/pool-company/search" in js
        assert "/api/v1/pool-company/autosuggest" in js
        assert "/api/v1/pool-company/export.csv" in js
        assert "pool-company/employees" in js
        assert "pub-emp" in js and "employee_email" in js
        assert "(Этап 8 · v1.37.0)" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'data-view="poolpick"' in idx

    def test_core_and_models(self):
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "assigned_at" in md and "assigned_by" in md
        rc = open("app/pool/router_company.py", encoding="utf-8").read()
        assert "PICK_LIMIT_DEFAULT = 100" in rc
        assert "assign_employee_receipt" in rc
        ing = open("app/pool/ingest.py", encoding="utf-8").read()
        assert "assign_employee_receipt" in ing
