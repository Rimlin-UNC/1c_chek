# Ямастер Чек — тесты v1.39.0: «Чек-Пул» этап 9.1 — платное API для
# внешних клиентов.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 9.1 (docs/dev_plan_checkpool.md):
#  - ключи партнёров: в БД только sha256 (полный ключ виден один раз),
#    лимиты по тарифу (запросов/час и /мес), выдача/отзыв — в аудите;
#  - анонимные агрегаты и доступ к фильтрам — НЕ «продажа чеков»:
#    наружу только счётчики и суммы-агрегаты, без чеков/участников/ПДн;
#  - счётчики запросов (журнал ≤ 90 дней), API включается администратором,
#    по умолчанию выключен; ядро не затронуто (модели только добавляющие).
import time
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal, engine
from app.pool import router_api
from app.pool.models import (PoolAchievement, PoolApiCall, PoolApiKey,
                             PoolConsent, PoolFingerprint, PoolItem,
                             PoolIpLog, PoolPoint, PoolReceipt, PoolReferral,
                             PoolSignal, PoolToken, PoolUser, PoolWithdrawal,
                             ensure_pool_schema)

ensure_pool_schema(engine)

_FN = {"n": 0}


def _db():
    return SessionLocal()


def _wipe():
    db = _db()
    for _ in range(40):
        try:
            db.query(PoolApiCall).delete(synchronize_session=False)
            db.query(PoolApiKey).delete(synchronize_session=False)
            for t in (PoolItem, PoolReceipt, PoolPoint, PoolSignal,
                      PoolIpLog, PoolFingerprint, PoolAchievement,
                      PoolWithdrawal, PoolReferral, PoolToken, PoolConsent,
                      PoolUser):
                db.query(t).delete(synchronize_session=False)
            from app.services import appsettings
            appsettings.set_setting(db, "pool_enabled", "1")
            appsettings.set_setting(db, router_api.API_ENABLED_KEY, "1")
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1390: не удалось очистить данные")


@pytest.fixture(autouse=True)
def _clean(client):
    from app.pool import router_auth
    router_auth._HITS.clear()
    _wipe()
    yield
    router_auth._HITS.clear()
    _wipe()


def _seed(n=12, city="Санкт-Петербург", region="78", industry="food",
          total=300.0, merchant="Магнит", inn="7811000011", days_ago=2):
    db = _db()
    rows = []
    for i in range(n):
        _FN["n"] += 1
        k = _FN["n"]
        r = PoolReceipt(source="web", fn=str(k), fd=str(k), fp=str(k),
                        receipt_date=datetime.utcnow() - timedelta(days=days_ago),
                        total_sum=total, merchant_name=merchant,
                        merchant_inn=inn, region_code=region, city=city,
                        industry=industry, status="verified",
                        verified_at=datetime.utcnow() - timedelta(days=days_ago),
                        created_at=datetime.utcnow() - timedelta(days=days_ago))
        db.add(r)
        rows.append(r)
    db.commit()
    ids = [r.id for r in rows]
    db.close()
    return ids


def _mkkey(name="ООО Аналитика", rate=5, quota=100):
    db = _db()
    rec, raw = router_api.generate_key(db, name, created_by="admin",
                                       rate_per_hour=rate,
                                       monthly_quota=quota)
    db.commit()
    kid, prefix = rec.id, rec.prefix
    db.close()
    return kid, raw, prefix


class TestKeys:
    def test_key_hash_only_in_db(self):
        kid, raw, prefix = _mkkey()
        assert raw.startswith("apk_") and len(raw) > 40
        db = _db()
        k = db.get(PoolApiKey, kid)
        db.close()
        assert k.key_hash != raw and len(k.key_hash) == 64     # sha256
        assert raw not in k.key_hash
        assert prefix[:-1] == raw[:12] and prefix.endswith("…")
        assert k.active is True and k.rate_per_hour == 5

    def test_admin_only_endpoints(self, client):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)                    # без lifespan — 401
        assert c.get("/api/v1/pool-admin/api-keys").status_code == 401
        assert c.post("/api/v1/pool-admin/api-keys",
                      json={"name": "x"}).status_code == 401

    def test_issue_and_list_via_admin(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/pool-admin/api-keys", headers=hdr,
                        json={"name": "Партнёр Плюс", "rate_per_hour": 10,
                              "monthly_quota": 200})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key"].startswith("apk_")
        d = client.get("/api/v1/pool-admin/api-keys", headers=hdr).json()
        assert len(d["items"]) == 1
        it = d["items"][0]
        assert it["name"] == "Партнёр Плюс" and it["active"] is True
        assert it["monthly_quota"] == 200
        # полный ключ в списке наружу не идёт
        assert body["key"] not in str(d)
        # выдача пишется в аудит ядра
        from app.models import AuditLog
        from app.database import SessionLocal as _SL
        adb = _SL()
        actions = [a_.action for a_ in adb.query(AuditLog)
                   .filter(AuditLog.action.like("pool_api_key%")).all()]
        adb.close()
        assert "pool_api_key_created" in actions, actions

    def test_revoke(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        kid, raw, _ = _mkkey()
        H = {"X-API-Key": raw}
        assert client.get("/api/v1/pool-api/v1/stats/overview",
                          headers=H).status_code == 200
        r = client.post(f"/api/v1/pool-admin/api-keys/{kid}/revoke",
                        headers=hdr)
        assert r.status_code == 200
        assert client.get("/api/v1/pool-api/v1/stats/overview",
                          headers=H).status_code == 401


class TestAccess:
    def test_disabled_by_default_switch(self, client):
        db = _db()
        from app.services import appsettings
        appsettings.set_setting(db, router_api.API_ENABLED_KEY, "0")
        db.commit()
        db.close()
        _, raw, _ = _mkkey()
        r = client.get("/api/v1/pool-api/v1/stats/overview",
                       headers={"X-API-Key": raw})
        assert r.status_code == 403
        assert "выключен" in r.json()["detail"]

    def test_missing_or_bad_key(self, client):
        assert client.get(
            "/api/v1/pool-api/v1/stats/overview").status_code == 401
        assert client.get("/api/v1/pool-api/v1/stats/overview",
                          headers={"X-API-Key": "apk_bogus"}
                          ).status_code == 401

    def test_rate_limit_per_hour(self, client):
        _seed(3)
        _, raw, _ = _mkkey(rate=3, quota=100)
        H = {"X-API-Key": raw}
        codes = [client.get("/api/v1/pool-api/v1/stats/overview",
                            headers=H).status_code for _ in range(3)]
        assert codes == [200, 200, 200]
        r = client.get("/api/v1/pool-api/v1/stats/overview", headers=H)
        assert r.status_code == 429
        assert "запросов/час" in r.json()["detail"]

    def test_monthly_quota(self, client, monkeypatch):
        _seed(1)
        _, raw, _ = _mkkey(rate=100, quota=2)
        H = {"X-API-Key": raw}
        assert client.get("/api/v1/pool-api/v1/stats/regions",
                          headers=H).status_code == 200
        # сдвигаем вызовы «в прошлое» — месяц тот же, часовой лимит свободен
        db = _db()
        edge = datetime.utcnow() - timedelta(hours=2)
        for c_ in db.query(PoolApiCall).all():
            c_.created_at = edge
        db.commit()
        db.close()
        assert client.get("/api/v1/pool-api/v1/stats/regions",
                          headers=H).status_code == 200
        r = client.get("/api/v1/pool-api/v1/stats/regions", headers=H)
        assert r.status_code == 429
        assert "Квота" in r.json()["detail"]


class TestAggregates:
    def test_overview_numbers(self, client):
        _seed(7, total=400.0)
        _seed(3, city="Москва", region="77", industry="fuel", total=900.0,
              merchant="Лукойл", inn="7702000022")
        _, raw, _ = _mkkey()
        d = client.get("/api/v1/pool-api/v1/stats/overview",
                       headers={"X-API-Key": raw}).json()
        assert d["aggregated"] is True
        assert d["receipts_verified"] == 10
        assert d["geo_coverage_pct"] == 100.0
        assert d["avg_receipt_sum"] == round((7 * 400 + 3 * 900) / 10, 2)

    def test_regions_industries_merchants(self, client):
        _seed(5)
        _seed(2, city="Москва", region="77", industry="fuel", total=900.0,
              merchant="Лукойл", inn="7702000022")
        _, raw, _ = _mkkey()
        H = {"X-API-Key": raw}
        r = client.get("/api/v1/pool-api/v1/stats/regions",
                       headers=H).json()
        assert [i["region_code"] for i in r["items"]] == ["78", "77"]
        assert r["items"][0]["region"] == "Санкт-Петербург"
        ind = client.get("/api/v1/pool-api/v1/stats/industries",
                         headers=H).json()["items"]
        assert ind[0]["industry"] == "food" and ind[0]["receipts"] == 5
        assert ind[0]["avg_sum"] == 300.0
        m = client.get("/api/v1/pool-api/v1/stats/merchants",
                       headers=H).json()["items"]
        assert m[0]["merchant"] == "Магнит" and m[0]["inn"] == "7811000011"
        assert m[0]["receipts"] == 5

    def test_filters_count_and_aggregate(self, client):
        _seed(4, total=300.0)
        _seed(2, total=1000.0)
        _, raw, _ = _mkkey()
        H = {"X-API-Key": raw}
        d = client.post("/api/v1/pool-api/v1/filters/count", headers=H,
                        json={"sum_min": 500}).json()
        assert d == {"aggregated": True, "count": 2}
        d = client.post("/api/v1/pool-api/v1/filters/aggregate", headers=H,
                        json={"city": "петербург"}).json()
        assert d["count"] == 6
        assert d["min"] == 300.0 and d["max"] == 1000.0
        assert d["avg"] == round((4 * 300 + 2 * 1000) / 6, 2)
        assert d["by_industry"][0]["industry"] == "food"
        # пустой результат
        d = client.post("/api/v1/pool-api/v1/filters/aggregate", headers=H,
                        json={"region": "01"}).json()
        assert d["count"] == 0 and d["sum"] == 0.0

    def test_no_personal_data_in_responses(self, client):
        """Принцип этапа: наружу не уходят чеки/участники/ПДн."""
        _seed(3)
        u = PoolUser(email="pdn@x.ru", email_verified=True)
        db = _db()
        db.add(u)
        db.commit()
        uid = u.id
        db.close()
        db = _db()
        db.query(PoolReceipt).update(
            {PoolReceipt.pool_user_id: uid}, synchronize_session=False)
        db.commit()
        db.close()
        _, raw, _ = _mkkey()
        H = {"X-API-Key": raw}
        for path, method, body in (
                ("/api/v1/pool-api/v1/stats/overview", "get", None),
                ("/api/v1/pool-api/v1/stats/regions", "get", None),
                ("/api/v1/pool-api/v1/stats/industries", "get", None),
                ("/api/v1/pool-api/v1/stats/merchants", "get", None),
                ("/api/v1/pool-api/v1/filters/count", "post",
                 {"sum_min": 0}),
                ("/api/v1/pool-api/v1/filters/aggregate", "post", {})):
            r = (client.get(path, headers=H) if method == "get"
                 else client.post(path, headers=H, json=body))
            text = r.text
            assert uid not in text, path
            assert "pdn@x.ru" not in text, path
            assert "fn" not in text.replace("inn", "").replace("min", "") \
                .replace("агрегированные", ""), f"{path}: сырые реквизиты?"


class TestCallLog:
    def test_calls_logged_without_query(self, client):
        _seed(2)
        kid, raw, _ = _mkkey(rate=20)
        H = {"X-API-Key": raw}
        client.get("/api/v1/pool-api/v1/stats/overview?days=7", headers=H)
        client.post("/api/v1/pool-api/v1/filters/count", headers=H,
                    json={"region": "78"})
        db = _db()
        calls = db.query(PoolApiCall).filter_by(key_id=kid).all()
        k = db.get(PoolApiKey, kid)
        used = k.last_used_at
        db.close()
        assert [c.path for c in calls] == ["v1/stats/overview",
                                           "v1/filters/count"]
        assert all("?" not in c.path for c in calls)      # query не пишем
        assert used is not None

    def test_purge_old_calls(self, client):
        kid, raw, _ = _mkkey(rate=20)
        H = {"X-API-Key": raw}
        client.get("/api/v1/pool-api/v1/stats/overview", headers=H)
        db = _db()
        old = datetime.utcnow() - timedelta(days=95)
        db.query(PoolApiCall).update(
            {PoolApiCall.created_at: old}, synchronize_session=False)
        db.commit()
        db.close()
        db = _db()
        n = router_api.purge_calls(db)
        left = db.query(PoolApiCall).count()
        db.close()
        assert n == 1 and left == 0


class TestVersion1390:
    def test_versions_synced(self):
        # пин 1.39.0 перенесён в tests/test_v1400.py (версия ушла вперёд)
        cfg = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "' in cfg
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.39.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.39.0]") == 1
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "платное API (v1.39.0)" in manual

    def test_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "pool-api-enabled" in js and "poolApiKeysLoad" in js
        assert "/api/v1/pool-admin/api-keys" in js
        assert "Ключ партнёру" in js
        assert "(Этап 9.1 · v1.39.0)" in js

    def test_core_and_models(self):
        assert "pool_" not in open("app/models.py", encoding="utf-8").read()
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "pool_api_keys" in md and "pool_api_calls" in md
        ra = open("app/pool/router_api.py", encoding="utf-8").read()
        assert 'KEY_PREFIX = "apk_"' in ra
        assert "_hash_key" in ra and "CALLS_RETENTION_DAYS = 90" in ra
        rf = open("app/pool/router_fraud.py", encoding="utf-8").read()
        assert "purge_calls" in rf                    # чистка 90 дней
        mn = open("app/main.py", encoding="utf-8").read()
        assert "pool_api.router" in mn
