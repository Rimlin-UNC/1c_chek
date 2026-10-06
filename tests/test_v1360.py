# Ямастер Чек — тесты v1.36.0: «Чек-Пул» этап 7 — вовлечение.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 7 (docs/dev_plan_checkpool.md):
#  - лидерборд: топ-10 по чекам за месяц, публично (только маски, без ПДн)
#    и в кабинете (со своим местом и местом города); стендинг регионов;
#  - ачивки: «50 чеков», «3 отрасли», «первый чек региона» — автоматически,
#    один раз, догоняют задним числом;
#  - цель вывода + заявка на вывод: минимум 100 баллов, списание сразу
#    с записью в журнал (WITHDRAW); телефон — ТОЛЬКО хэшем sha256;
#    первый вывод — SMS-подтверждение (код в pool_tokens, шлюз отдельно);
#  - ядро не затронуто: модели только добавляющие (pool_achievements,
#    pool_withdrawals).
import re
import time
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal, engine
from app.pool import accounts, engage, ingest
from app.pool.models import (PoolAchievement, PoolConsent, PoolFingerprint,
                             PoolItem, PoolIpLog, PoolPoint, PoolReceipt,
                             PoolReferral, PoolSignal, PoolToken, PoolUser,
                             PoolWithdrawal, ensure_pool_schema)

ensure_pool_schema(engine)


def _db():
    return SessionLocal()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _wipe(db):
    for _ in range(40):
        try:
            # лидерборд считает ВСЕ верифицированные чеки месяца — вайпаем
            # весь пул целиком (включая участников бот-эпохи без vid/e-mail)
            ids = [u.id for u in db.query(PoolUser).all()]
            recs = [r[0] for r in db.query(PoolReceipt.id).all()]
            if recs:
                rq = ",".join("?" * len(recs))
                db.query(PoolItem).filter(PoolItem.receipt_id.in_(recs)).delete(
                    synchronize_session=False)
                db.query(PoolReceipt).filter(PoolReceipt.id.in_(recs)).delete(
                    synchronize_session=False)
            if ids:
                q = ",".join("?" * len(ids))
                for tbl in (PoolPoint, PoolSignal, PoolIpLog, PoolFingerprint):
                    db.query(tbl).filter(tbl.user_id.in_(ids)).delete(
                        synchronize_session=False)
            db.query(PoolAchievement).delete(synchronize_session=False)
            db.query(PoolWithdrawal).delete(synchronize_session=False)
            db.query(PoolReferral).delete(synchronize_session=False)
            db.query(PoolSignal).delete(synchronize_session=False)
            db.query(PoolIpLog).delete(synchronize_session=False)
            db.query(PoolFingerprint).delete(synchronize_session=False)
            db.query(PoolToken).delete(synchronize_session=False)
            db.query(PoolConsent).delete(synchronize_session=False)
            db.query(PoolUser).delete(synchronize_session=False)
            from app.services import appsettings
            appsettings.set_setting(db, "pool_enabled", "1")
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1360: не удалось очистить данные")


@pytest.fixture(autouse=True)
def _clean():
    from app.pool import router_auth
    router_auth._HITS.clear()
    db = _db()
    _wipe(db)
    db.close()
    yield
    router_auth._HITS.clear()
    db = _db()
    _wipe(db)
    db.close()


def _reg(c, email, password="parol-12345"):
    r = c.post("/api/v1/pool-auth/register",
               json={"email": email, "password": password, "ref_code": ""})
    assert r.status_code == 200, r.text
    return r.json()


def _user(db, uid):
    return db.get(PoolUser, uid)


def _vreceipt(db, user, region="78", city="Санкт-Петербург",
              industry="food", at=None, total=300.0, n=[0]):
    """Верифицированный чек с гео/отраслью (юнит-посев)."""
    n[0] += 1
    r = PoolReceipt(pool_user_id=user.id, source="web",
                    fn=f"7700000099{n[0]:04d}", fd="1", fp=str(n[0]),
                    total_sum=total, status="verified",
                    region_code=region, city=city, industry=industry,
                    verified_at=at or datetime.utcnow(),
                    created_at=at or datetime.utcnow())
    db.add(r)
    db.flush()
    return r


_SEQ = {"k": 9000}


class TestLeaderboard:
    def test_public_masked_sorted(self):
        c = _client()
        db = _db()
        a = _user(db, _reg(c, "lb-a@x.ru")["user"]["id"])
        b = _user(db, _reg(c, "lb-b@yandex.ru")["user"]["id"])
        _vreceipt(db, a, city="Санкт-Петербург")
        _vreceipt(db, a, city="Санкт-Петербург")
        _vreceipt(db, b, region="77", city="Москва")
        db.commit()
        db.close()
        d = c.get("/api/v1/public/pool/leaderboard").json()
        assert d["month"] == datetime.utcnow().strftime("%Y-%m")
        assert d["participants"] == 2
        assert d["entries"][0]["receipts"] == 2
        assert "l***@x.ru" in [e["label"] for e in d["entries"]]
        for e in d["entries"]:
            assert e["label"].count("***") == 1        # без полных e-mail
            assert "@x.ru" != e["label"][-5:] or "***" in e["label"]
        regions = {r["code"]: r["receipts"] for r in d["regions"]}
        assert regions.get("78") == 2 and regions.get("77") == 1

    def test_month_window(self):
        c = _client()
        db = _db()
        a = _user(db, _reg(c, "lb-m@x.ru")["user"]["id"])
        _vreceipt(db, a, at=datetime.utcnow() - timedelta(days=40))
        db.commit()
        db.close()
        d = c.get("/api/v1/public/pool/leaderboard").json()
        assert d["participants"] == 0 and d["entries"] == []

    def test_city_filter(self):
        c = _client()
        db = _db()
        a = _user(db, _reg(c, "lb-c1@x.ru")["user"]["id"])
        b = _user(db, _reg(c, "lb-c2@x.ru")["user"]["id"])
        _vreceipt(db, a, city="Санкт-Петербург")
        _vreceipt(db, b, city="Москва")
        db.commit()
        db.close()
        d = c.get("/api/v1/public/pool/leaderboard",
                  params={"city": "Санкт-Петербург"}).json()
        assert len(d["entries"]) == 1
        assert d["entries"][0]["city"] == "Санкт-Петербург"

    def test_cabinet_leaders_with_me(self):
        c = _client()
        me = _reg(c, "lb-me@x.ru")
        db = _db()
        u = _user(db, me["user"]["id"])
        _vreceipt(db, u, city="Санкт-Петербург")
        _vreceipt(db, u, city="Санкт-Петербург")
        db.commit()
        db.close()
        d = c.get("/api/v1/pool-my/leaders",
                  headers={"Authorization": f"Bearer {me['token']}"}).json()
        assert d["me"] and d["me"]["rank"] == 1
        assert d["me"]["receipts"] == 2
        assert d["me"]["city"] == "Санкт-Петербург"
        assert c.get("/api/v1/pool-my/leaders").status_code == 401


class TestAchievements:
    def _seed_region78(self, c):
        """Чужой ранний чек региона 78: REGION_FIRST не выигрываем по умолчанию."""
        decoy = _reg(c, "ach-decoy@x.ru")
        db = _db()
        _vreceipt(db, _user(db, decoy["user"]["id"]), region="78",
                  at=datetime.utcnow() - timedelta(days=1))
        db.commit()
        db.close()

    def test_receipts_50_once(self):
        c = _client()
        self._seed_region78(c)
        me = _reg(c, "ach-50@x.ru")
        db = _db()
        u = _user(db, me["user"]["id"])
        for i in range(49):
            _vreceipt(db, u, n=[500 + i])
        db.commit()
        assert engage.evaluate(db, u) == []
        db.close()
        db = _db()
        u = _user(db, me["user"]["id"])
        _vreceipt(db, u, n=[600])
        new = engage.evaluate(db, u)
        db.commit()
        db.close()
        assert new == ["RECEIPTS_50"], new
        db = _db()
        u = _user(db, me["user"]["id"])
        assert engage.evaluate(db, u) == []      # повторно не выдаём
        db.close()

    def test_industries_3(self):
        c = _client()
        self._seed_region78(c)
        me = _reg(c, "ach-ind@x.ru")
        db = _db()
        u = _user(db, me["user"]["id"])
        _vreceipt(db, u, industry="food")
        _vreceipt(db, u, industry="pharma")
        db.commit()
        assert engage.evaluate(db, u) == []
        db.close()
        db = _db()
        u = _user(db, me["user"]["id"])
        _vreceipt(db, u, industry="fuel")
        new = engage.evaluate(db, u)
        db.commit()
        db.close()
        assert new == ["INDUSTRIES_3"], new

    def test_region_first(self):
        c = _client()
        self._seed_region78(c)
        first = _reg(c, "ach-rf1@x.ru")
        second = _reg(c, "ach-rf2@x.ru")
        db = _db()
        u1 = _user(db, first["user"]["id"])
        u2 = _user(db, second["user"]["id"])
        _vreceipt(db, u1, region="66", city="Екатеринбург",
                  at=datetime.utcnow() - timedelta(hours=2))
        _vreceipt(db, u2, region="66", city="Екатеринбург",
                  at=datetime.utcnow())
        new1 = engage.evaluate(db, u1)
        new2 = engage.evaluate(db, u2)
        db.commit()
        db.close()
        assert "REGION_FIRST" in new1
        assert "REGION_FIRST" not in new2        # второму не достаётся

    def test_no_region_no_award(self):
        c = _client()
        self._seed_region78(c)
        me = _reg(c, "ach-noreg@x.ru")
        db = _db()
        u = _user(db, me["user"]["id"])
        _vreceipt(db, u, region="", city="")
        new = engage.evaluate(db, u)
        db.commit()
        db.close()
        assert new == []

    def test_achievements_endpoint_and_progress(self):
        c = _client()
        me = _reg(c, "ach-api@x.ru")
        db = _db()
        u = _user(db, me["user"]["id"])
        _vreceipt(db, u, industry="food")
        db.commit()
        db.close()
        d = c.get("/api/v1/pool-my/achievements",
                  headers={"Authorization": f"Bearer {me['token']}"}).json()
        codes = {i["code"]: i for i in d["items"]}
        assert set(codes) == {"RECEIPTS_50", "INDUSTRIES_3", "REGION_FIRST"}
        assert codes["RECEIPTS_50"]["progress"] == 1
        assert codes["INDUSTRIES_3"]["progress"] == 1
        assert codes["RECEIPTS_50"]["earned"] is False
        assert c.get("/api/v1/pool-my/achievements").status_code == 401


class TestWithdraw:
    def _user_with_points(self, c, email, points=150):
        me = _reg(c, email)
        db = _db()
        u = _user(db, me["user"]["id"])
        ingest.add_points(db, u, points, "receipt")
        db.commit()
        db.close()
        return me

    def test_validation(self):
        c = _client()
        me = self._user_with_points(c, "wd-v@x.ru", points=150)
        H = {"Authorization": f"Bearer {me['token']}"}
        for body, why in (({"points": 50, "phone": "+79001234567"}, "минимум"),
                          ({"points": 100000, "phone": "+79001234567"},
                           "больше баланса"),
                          ({"points": 100, "phone": "абракадабра"}, "телефон")):
            r = c.post("/api/v1/pool-my/withdraw", json=body, headers=H)
            assert r.status_code == 422, (body, r.text)

    def test_first_withdraw_sms_and_ledger(self):
        c = _client()
        me = self._user_with_points(c, "wd-1@x.ru", points=150)
        H = {"Authorization": f"Bearer {me['token']}"}
        r = c.post("/api/v1/pool-my/withdraw",
                   json={"points": 100, "phone": "8 900 123-45-67"},
                   headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending_sms"
        assert r.json()["sms_required"] is True
        db = _db()
        u = _user(db, me["user"]["id"])
        assert u.points == 50                        # списано сразу
        rows = db.query(PoolPoint).filter_by(user_id=u.id,
                                             reason="withdraw").all()
        wd = db.query(PoolWithdrawal).filter_by(user_id=u.id).first()
        db.close()
        assert len(rows) == 1 and rows[0].delta == -100
        assert wd is not None and wd.phone_hash
        assert len(wd.phone_hash) == 64              # sha256, не телефон
        # повторная заявка при активной — отказ
        r2 = c.post("/api/v1/pool-my/withdraw",
                    json={"points": 100, "phone": "+79001234567"}, headers=H)
        assert r2.status_code == 409
        # статус и история
        s = c.get("/api/v1/pool-my/withdraw", headers=H).json()
        assert s["active"]["status"] == "pending_sms"
        assert s["can_withdraw"] is False
        assert s["goal"] == engage.WITHDRAW_GOAL_DEFAULT
        assert len(s["history"]) == 1

    def test_confirm_sms_flow(self):
        c = _client()
        me = self._user_with_points(c, "wd-c@x.ru", points=200)
        H = {"Authorization": f"Bearer {me['token']}"}
        assert c.post("/api/v1/pool-my/withdraw",
                      json={"points": 100, "phone": "+79001234567"},
                      headers=H).status_code == 200
        # чужой/неверный код
        assert c.post("/api/v1/pool-my/withdraw/confirm",
                      json={"code": "000000"}, headers=H).status_code == 400
        # сеем известный нам код (в бою его пришлёт SMS-шлюз)
        db = _db()
        u = _user(db, me["user"]["id"])
        db.add(PoolToken(token_hash=accounts._hash_token("135790"),
                         purpose="sms", email=u.email, user_id=u.id,
                         expires_at=datetime.utcnow() + timedelta(minutes=10)))
        db.commit()
        db.close()
        r = c.post("/api/v1/pool-my/withdraw/confirm",
                   json={"code": "135790"}, headers=H)
        assert r.status_code == 200, r.text
        db = _db()
        u = _user(db, me["user"]["id"])
        wd = db.query(PoolWithdrawal).filter_by(user_id=u.id).first()
        db.close()
        assert wd.status == "pending"
        # тот же код повторно — уже использован
        assert c.post("/api/v1/pool-my/withdraw/confirm",
                      json={"code": "135790"}, headers=H).status_code == 400

    def test_second_withdraw_no_sms(self):
        c = _client()
        me = self._user_with_points(c, "wd-2@x.ru", points=300)
        H = {"Authorization": f"Bearer {me['token']}"}
        # первая заявка с известным кодом → подтверждаем → статус pending
        db = _db()
        u = _user(db, me["user"]["id"])
        db.add(PoolToken(token_hash=accounts._hash_token("246800"),
                         purpose="sms", email=u.email, user_id=u.id,
                         expires_at=datetime.utcnow() + timedelta(minutes=10)))
        db.commit()
        db.close()
        assert c.post("/api/v1/pool-my/withdraw",
                      json={"points": 100, "phone": "+79001234568"},
                      headers=H).status_code == 200
        assert c.post("/api/v1/pool-my/withdraw/confirm",
                      json={"code": "246800"}, headers=H).status_code == 200
        # первую заявку «выплатили» (обработка — со шлюзом, отдельным решением)
        db = _db()
        u = _user(db, me["user"]["id"])
        w1 = (db.query(PoolWithdrawal).filter_by(user_id=u.id).first())
        w1.status = "paid"
        db.commit()
        db.close()
        # вторая заявка — уже без SMS (телефон подтверждён ранее)
        r = c.post("/api/v1/pool-my/withdraw",
                   json={"points": 100, "phone": "+79001234568"}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        assert r.json()["sms_required"] is False


class TestIngestHook:
    def test_submit_awards_region_first(self, monkeypatch):
        """Хук v1.36.0 в ingest: верифицированный из формы чек выдаёт ачивку."""
        from app.services.external import ExternalItem, ExternalResult
        monkeypatch.setattr(ingest, "_fetch_details", lambda *a, **k: ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=datetime(2026, 10, 6, 12, 0), total_sum=1250.0, operation=1,
            merchant_name="ООО Ромашка", merchant_inn="7801000000",
            merchant_address="г Санкт-Петербург, Невский проспект 20",
            items=[ExternalItem(name="Кофе", quantity=1.0, price=1250.0,
                                total=1250.0)]))
        db = _db()
        from app.services import appsettings
        appsettings.set_setting(db, "pool_enabled", "1")
        db.commit()
        db.close()
        c = _client()
        # согласие оферты фиксируем как на форме (v1.31.0: /check)
        r = c.post("/api/v1/public/pool/check",
                   json={"qr_text": "t=20261006T1200&s=1250.00&fn=9999078999"
                                    "0001&i=65001&fp=78001&n=1",
                         "offerta": True, "hp": "", "form_ms": 5000})
        assert r.status_code == 200 and r.json()["ok"], r.text
        fn = r.json()["fn"]
        ach = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 8:
            db = _db()
            rec = db.query(PoolReceipt).filter_by(source="web", fn=fn).first()
            if rec is not None and rec.status == "verified":
                ach = (db.query(PoolAchievement)
                       .filter_by(code="REGION_FIRST").first())
            db.close()
            if ach is not None:
                break
            time.sleep(0.1)
        assert ach is not None, "хук не выдал «первый чек региона»"
        assert (rec.city or "") == "Санкт-Петербург"    # гео-фикс города


class TestVersion1360:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.36.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.36.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.36.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "вывод баллов (v1.36.0)" in manual

    def test_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "pool-engage" in js and "poolLoadEngage" in js
        assert "poolLeadersHTML" in js and "pub-leaders" in js
        assert "/api/v1/pool-my/withdraw" in js
        assert "/api/v1/pool-my/achievements" in js
        assert "/api/v1/pool-my/leaders" in js
        assert "(Этап 7 · v1.36.0)" in js

    def test_core_and_models(self):
        assert "pool_" not in open("app/models.py", encoding="utf-8").read()
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "pool_achievements" in md and "pool_withdrawals" in md
        en = open("app/pool/engage.py", encoding="utf-8").read()
        assert "RECEIPTS_50" in en and "INDUSTRIES_3" in en
        assert "REGION_FIRST" in en and "WITHDRAW_MIN = 100" in en
        ing = open("app/pool/ingest.py", encoding="utf-8").read()
        assert "engage.on_verified_receipt" in ing
