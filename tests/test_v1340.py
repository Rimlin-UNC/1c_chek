# Ямастер Чек — тесты v1.34.0: «Чек-Пул» этап 5 — антифрод из 5 слоёв.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 5 (docs/dev_plan_checkpool.md):
#  - «ферма аккаунтов»: один отпечаток устройства на 2+ аккаунтов → сигнал
#    MULTI_ACCOUNT_DEVICE, риск растёт;
#  - сеть: датацентр → DATACENTER_IP; 5+ регистраций из одной /24 → SUBNET_CLUSTER;
#  - поведение: быстрая форма → BOT_PATTERN; ровные интервалы → EVEN_INTERVALS;
#    поток 20+/час → RECEIPT_FLOOD; одинаковые суммы → REPEAT_SUMS;
#  - карантин вместо бана: risk ≥ 71 → чеки сохраняются, баллы приостанавливаются;
#  - разбор: false_positive снимает риск и карантин; confirmed — аннулирование;
#  - «ложных блокировок на честных нет»: нормальный участник — 0 сигналов;
#  - техданные ≤ 12 мес (purge); ядро не затронуто, версии синхронны.
import re
import time
from collections import deque
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal, engine
from app.pool import antifraud
from app.pool.models import (PoolReferral,
                             PoolConsent, PoolFingerprint, PoolItem, PoolIpLog,
                             PoolPoint, PoolReceipt, PoolSignal, PoolToken,
                             PoolUser, ensure_pool_schema)

ensure_pool_schema(engine)


def _db():
    return SessionLocal()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _wipe(db):
    """Полная очистка контура веба/кабинета + антифрода (ядро не трогаем)."""
    for _ in range(40):
        try:
            vids = [u.id for u in db.query(PoolUser.id)
                    .filter(PoolUser.vid.isnot(None)).all()]
            emails = [u.id for u in db.query(PoolUser.id)
                      .filter(PoolUser.email != "").all()]
            recs = [r[0] for r in db.query(PoolReceipt.id).filter(
                (PoolReceipt.source == "web")
                | (PoolReceipt.pool_user_id.in_([*vids, *emails] or [""]))).all()]
            if recs:
                db.query(PoolItem).filter(PoolItem.receipt_id.in_(recs)).delete(
                    synchronize_session=False)
                db.query(PoolReceipt).filter(PoolReceipt.id.in_(recs)).delete(
                    synchronize_session=False)
            ids = [*vids, *emails]
            if ids:
                db.query(PoolPoint).filter(PoolPoint.user_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(PoolSignal).filter(PoolSignal.user_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(PoolIpLog).filter(PoolIpLog.user_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(PoolFingerprint).filter(
                    PoolFingerprint.user_id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(PoolSignal).delete(synchronize_session=False)
            db.query(PoolIpLog).delete(synchronize_session=False)
            db.query(PoolFingerprint).delete(synchronize_session=False)
            db.query(PoolToken).delete(synchronize_session=False)
            db.query(PoolReferral).delete(synchronize_session=False)
            db.query(PoolConsent).delete(synchronize_session=False)
            db.query(PoolUser).filter(
                (PoolUser.vid.isnot(None)) | (PoolUser.email != "")).delete(
                synchronize_session=False)
            from app.services import appsettings
            # v1.34.0: в этом файле регистрация кабинета — часть сценариев,
            # оставляем приём включённым
            appsettings.set_setting(db, "pool_enabled", "1")
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1340: не удалось очистить данные")


@pytest.fixture(autouse=True)
def _clean():
    from app.pool import router_auth
    router_auth._HITS.clear()      # IP-лимиты кабинета — у каждого теста свои
    db = _db()
    _wipe(db)
    db.close()
    yield
    router_auth._HITS.clear()
    db = _db()
    _wipe(db)
    db.close()


def _wait_row(fn, timeout=6.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        db = _db()
        try:
            r = db.query(PoolReceipt).filter_by(source="web", fn=fn).first()
            if r is not None:
                return r.id, r.status
        finally:
            db.close()
        time.sleep(0.05)
    return None


def _wait_signal(vid, code, timeout=6.0):
    """Сигналы пишутся фоновым потоком после чека — ждём их появления."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        db = _db()
        try:
            u = db.query(PoolUser).filter_by(vid=vid).first()
            if u is not None:
                sig = db.query(PoolSignal).filter_by(user_id=u.id, code=code).first()
                if sig is not None:
                    return sig
        finally:
            db.close()
        time.sleep(0.05)
    return None


class TestDeviceLayer:
    def test_multi_account_device(self):
        """Ферма аккаунтов: один visitor — два аккаунта → сигнал +60."""
        c = _client()
        H = {"X-Visitor-Id": "device-same-123", "X-Forwarded-For": "91.1.1.1"}
        c.post("/api/v1/pool-auth/register", json={"email": "a1@x.ru", "password": "parol-1234"}, headers=H)
        r2 = c.post("/api/v1/pool-auth/register", json={"email": "a2@x.ru", "password": "parol-1234"}, headers=H)
        assert r2.status_code == 200
        uid = r2.json()["user"]["id"]
        db = _db()
        u = db.get(PoolUser, uid)
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="MULTI_ACCOUNT_DEVICE").first()
        db.close()
        assert sig is not None, "сигнал мультиаккаунта не записан"
        assert sig.points == 60
        assert u.risk_score == 60
        assert u.quarantined_at is None          # 60 < 71: пока только сигнал

    def test_fingerprint_hash_stored(self):
        """Отпечаток хранится хэшем (152-ФЗ), а не сырым значением."""
        c = _client()
        r = c.post("/api/v1/pool-auth/register", json={"email": "fp@x.ru", "password": "parol-1234"},
                   headers={"X-Visitor-Id": "device-hash-me"})
        uid = r.json()["user"]["id"]
        db = _db()
        rows = db.query(PoolFingerprint).filter_by(user_id=uid).all()
        db.close()
        assert len(rows) == 1
        raw_value = "visitor:device-hash-me"
        import hashlib
        assert rows[0].visitor_hash == hashlib.sha256(raw_value.encode()).hexdigest()[:32]
        assert "device-hash-me" not in rows[0].visitor_hash


class TestNetworkLayer:
    def test_datacenter_ip(self):
        c = _client()
        r = c.post("/api/v1/pool-auth/register", json={"email": "dc@x.ru", "password": "parol-1234"},
                   headers={"X-Forwarded-For": "52.14.9.9"})
        uid = r.json()["user"]["id"]
        db = _db()
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="DATACENTER_IP").first()
        db.close()
        assert sig is not None and sig.points == 25

    def test_private_ip_not_flagged(self):
        c = _client()
        r = c.post("/api/v1/pool-auth/register", json={"email": "lan@x.ru", "password": "parol-1234"},
                   headers={"X-Forwarded-For": "192.168.1.10"})
        uid = r.json()["user"]["id"]
        db = _db()
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="DATACENTER_IP").first()
        db.close()
        assert sig is None

    def test_subnet_cluster(self):
        """5+ регистраций из одной /24 за сутки → SUBNET_CLUSTER (+45)."""
        c = _client()
        for i in range(5):
            c.post("/api/v1/pool-auth/register",
                   json={"email": f"s{i}@x.ru", "password": "parol-1234"},
                   headers={"X-Forwarded-For": f"185.220.101.{i}"})
        db = _db()
        uid = db.query(PoolUser).filter_by(email="s4@x.ru").first().id
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="SUBNET_CLUSTER").first()
        db.close()
        assert sig is not None and sig.points == 45

    def test_ip_log_records(self):
        c = _client()
        r = c.post("/api/v1/pool-auth/register", json={"email": "log@x.ru", "password": "parol-1234"},
                   headers={"X-Forwarded-For": "91.2.3.4"})
        uid = r.json()["user"]["id"]
        db = _db()
        rows = db.query(PoolIpLog).filter_by(user_id=uid, kind="register").all()
        db.close()
        assert rows and rows[0].ip == "91.2.3.4" and rows[0].ip24 == "91.2.3"


class TestBehaviorLayer:
    def _enable_and_send(self, qr, form_ms, jar_client):
        from app.services import appsettings
        db = _db()
        appsettings.set_setting(db, "pool_enabled", "1")
        db.commit()
        db.close()
        return jar_client.post("/api/v1/public/pool/check",
                               json={"qr_text": qr, "offerta": True,
                                     "form_ms": form_ms})

    def test_fast_form_bot_pattern(self, monkeypatch):
        """Отправка формы быстрее 0,8 с → BOT_PATTERN (+40)."""
        from datetime import datetime as dt
        from app.pool import ingest as pool
        from app.services.external import ExternalItem, ExternalResult
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=dt(2026, 9, 20, 10, 0), total_sum=300.0, operation=1,
            merchant_name="Кафе", merchant_inn="7701",
            items=[ExternalItem(name="Кофе", quantity=1.0, price=300.0, total=300.0)]))
        c = _client()
        r = self._enable_and_send("t=20260920T1000&s=300&fn=999907890610001&i=70001&fp=790001&n=1",
                                  300, c)
        body = r.json()
        assert body["accepted"]
        assert _wait_row(body["fn"])
        vid = c.cookies.get("pool_vid").split(".")[0]
        sig = _wait_signal(vid, "BOT_PATTERN")
        assert sig is not None and sig.points == 40

    def test_receipt_flood(self, monkeypatch):
        """20+ чеков за час → RECEIPT_FLOOD (+30)."""
        db = _db()
        u = PoolUser(vid="flood-vid")
        db.add(u)
        db.flush()
        now = datetime.utcnow()
        for i in range(21):
            db.add(PoolReceipt(pool_user_id=u.id, source="web",
                               fn=f"8800000000{i:04d}", fd="1", fp=str(i),
                               total_sum=100.0, status="verified",
                               created_at=now - timedelta(minutes=30)))
        uid = u.id
        db.commit()
        antifraud.after_receipt(db, u, "none", None, form_ms=5000)
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="RECEIPT_FLOOD").first()
        db.close()
        assert sig is not None and sig.points == 30

    def test_even_intervals(self):
        """Чеки ровными интервалами (скрипт) → EVEN_INTERVALS (+40)."""
        db = _db()
        u = PoolUser(vid="even-vid")
        db.add(u)
        db.flush()
        now = datetime.utcnow()
        for i in range(6):
            db.add(PoolReceipt(pool_user_id=u.id, source="web",
                               fn=f"8900000000{i:04d}", fd="1", fp=str(i),
                               total_sum=100.0, status="verified",
                               created_at=now - timedelta(seconds=20 * (5 - i))))
        uid = u.id
        db.commit()
        antifraud.after_receipt(db, u, "none", None, form_ms=9000)
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="EVEN_INTERVALS").first()
        db.close()
        assert sig is not None and sig.points == 40

    def test_repeat_sums(self):
        """5 одинаковых сумм подряд → REPEAT_SUMS (+20)."""
        db = _db()
        u = PoolUser(vid="repeat-vid")
        db.add(u)
        db.flush()
        now = datetime.utcnow()
        for i in range(6):
            db.add(PoolReceipt(pool_user_id=u.id, source="web",
                               fn=f"8700000000{i:04d}", fd="1", fp=str(i),
                               total_sum=499.0, status="verified",
                               created_at=now - timedelta(minutes=3 * (5 - i))))
        uid = u.id
        db.commit()
        antifraud.after_receipt(db, u, "none", None, form_ms=9000)
        sig = db.query(PoolSignal).filter_by(user_id=uid, code="REPEAT_SUMS").first()
        db.close()
        assert sig is not None and sig.points == 20


class TestQuarantineAndReview:
    def _quarantined_user(self, email="quar@x.ru", risk_total=100):
        c = _client()
        H = {"X-Visitor-Id": "farm-device-9", "X-Forwarded-For": "52.1.2.3"}
        # «ферма»: сначала аккаунт-сосед, потом целевой — сигнал MULTI
        # получает ПОЗДНИЙ аккаунт на том же устройстве (60 + 25 = 85 ≥ 71)
        c.post("/api/v1/pool-auth/register",
               json={"email": email + "-seed", "password": "parol-1234"}, headers=H)
        r = c.post("/api/v1/pool-auth/register", json={"email": email, "password": "parol-1234"},
                   headers=H)
        uid = r.json()["user"]["id"]
        db = _db()
        u = db.get(PoolUser, uid)
        antifraud.record_signal(db, u, "BOT_PATTERN", details={"seed": 1})
        db.commit()
        db.refresh(u)
        return u.id

    def test_quarantine_withholds_points(self, monkeypatch):
        """risk ≥ 71: чек сохраняется в пул, баллы НЕ начисляются."""
        from datetime import datetime as dt
        from app.pool import ingest as pool
        from app.services import appsettings
        from app.services.external import ExternalItem, ExternalResult
        uid = self._quarantined_user("hold@x.ru")
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=dt(2026, 9, 20, 11, 0), total_sum=500.0, operation=1,
            merchant_name="Магнит", merchant_inn="7702",
            items=[ExternalItem(name="Хлеб", quantity=1.0, price=500.0, total=500.0)]))
        db = _db()
        appsettings.set_setting(db, "pool_enabled", "1")
        u = db.get(PoolUser, uid)
        assert u.risk_score >= 71 and u.quarantined_at is not None
        res = pool.ingest_receipt(db, "t=20260920T1100&s=500&fn=999907890610002&i=70002&fp=790002&n=1",
                                  "web", u)
        db.commit()
        db.refresh(u)
        assert res["result"] == "verified"
        assert u.points == 0, "в карантине баллы не начисляются"
        assert "приостановлены" in res["message"]
        db.close()

    def test_false_positive_releases(self):
        """Разбор «ложное срабатывание» снижает риск и снимает карантин."""
        from datetime import datetime as dt
        from app.pool import ingest as pool
        from app.services.external import ExternalItem, ExternalResult
        uid = self._quarantined_user("release@x.ru")
        db = _db()
        u = db.get(PoolUser, uid)
        assert u.quarantined_at is not None
        sigs = db.query(PoolSignal).filter_by(user_id=uid).all()
        assert sigs
        for s in sigs:
            antifraud.resolve_signal(db, s, "false_positive", "admin")
        db.commit()
        db.refresh(u)
        assert u.risk_score == 0 and u.quarantined_at is None
        # после снятия карантина баллы снова начисляются
        monkeyticket = ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=dt(2026, 9, 20, 12, 0), total_sum=200.0, operation=1,
            merchant_name="Магнит", merchant_inn="7703",
            items=[ExternalItem(name="Молоко", quantity=1.0, price=200.0, total=200.0)])
        pool._fetch_details = lambda *a, **k: monkeyticket  # noqa: SLF001
        res = pool.ingest_receipt(db, "t=20260920T1200&s=200&fn=999907890610003&i=70003&fp=790003&n=1",
                                  "web", u)
        db.commit()
        db.refresh(u)
        assert res["result"] == "verified" and u.points == 1
        db.close()

    def test_confirm_annuls_points(self):
        """Подтверждённый фрод: карантин держится; аннулирование — с записью."""
        uid = self._quarantined_user("fraudster@x.ru")
        db = _db()
        u = db.get(PoolUser, uid)
        from app.pool import ingest
        ingest.add_points(db, u, 5, "receipt", "seed")
        db.commit()
        db.refresh(u)
        assert u.points == 5
        sig = db.query(PoolSignal).filter_by(user_id=uid).first()
        antifraud.resolve_signal(db, sig, "confirmed", "admin")
        pts = antifraud.annul_points(db, u, type("A", (), {"username": "admin"})())
        db.commit()
        db.refresh(u)
        assert pts == 5 and u.points == 0
        assert u.quarantined_at is not None
        burn = db.query(PoolPoint).filter_by(user_id=uid, reason="risk_annulled").first()
        assert burn is not None and burn.delta == -5
        db.close()

    def test_honest_user_not_flagged(self, monkeypatch):
        """Критерий «ложных блокировок нет»: нормальный гость — 0 сигналов."""
        from datetime import datetime as dt
        from app.pool import ingest as pool
        from app.services import appsettings
        from app.services.external import ExternalItem, ExternalResult
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=dt(2026, 9, 20, 13, 0), total_sum=350.0, operation=1,
            merchant_name="Пятёрочка", merchant_inn="7704",
            items=[ExternalItem(name="Кофе", quantity=1.0, price=350.0, total=350.0)]))
        c = _client()
        db = _db()
        appsettings.set_setting(db, "pool_enabled", "1")
        db.commit()
        db.close()
        # 3 чека с нормальными таймингами (не идеально ровными)
        for i, ms in enumerate((6200, 9400, 7100)):
            b = c.post("/api/v1/public/pool/check",
                       json={"qr_text": f"t=20260920T1300&s=350&fn=99990789061001{i}&i=7000{i}&fp=79001{i}&n=1",
                             "offerta": True, "form_ms": ms}).json()
            assert b["accepted"] and _wait_row(b["fn"])
        vid = c.cookies.get("pool_vid").split(".")[0]
        db = _db()
        u = db.query(PoolUser).filter_by(vid=vid).first()
        n = db.query(PoolSignal).filter_by(user_id=u.id).count()
        points = u.points
        db.close()
        assert n == 0 and points == 3


class TestFraudPanel:
    def test_summary_signals_user_card_and_bulk(self, client):
        c = client
        H = {"X-Visitor-Id": "panel-device", "X-Forwarded-For": "52.9.9.1"}
        c.post("/api/v1/pool-auth/register", json={"email": "p1@x.ru", "password": "parol-1234"}, headers=H)
        r1 = c.post("/api/v1/pool-auth/register", json={"email": "p2@x.ru", "password": "parol-1234"}, headers=H)
        from tests.conftest import login
        A = login(c, "admin", "admin123")
        summ = c.get("/api/v1/pool-fraud/summary", headers=A).json()
        assert summ["new_signals"] >= 1 and summ["quarantine_threshold"] == 71
        lst = c.get("/api/v1/pool-fraud/signals", headers=A).json()
        assert lst["total"] >= 1
        sig = lst["items"][0]
        assert sig["code"] and "user" in sig
        # карточка участника
        uid = r1.json()["user"]["id"]
        card = c.get(f"/api/v1/pool-fraud/user/{uid}", headers=A).json()
        assert card["user"]["risk_score"] >= 60
        assert len(card["devices"]) == 1 and len(card["ips"]) == 1
        assert len(card["related_users"]) == 1     # второй аккаунт того же visitor
        # разбор bulk → ложные
        r = c.post("/api/v1/pool-fraud/signal/resolve-bulk", headers=A,
                   json={"ids": [x["id"] for x in lst["items"]], "status": "false_positive"})
        assert r.json()["resolved"] == lst["total"]
        db = _db()
        u = db.get(PoolUser, uid)
        db.refresh(u)
        assert u.risk_score == 0 and u.quarantined_at is None
        db.close()

    def test_purge_old_tech_data(self):
        """152-ФЗ: чистка техданных старше года."""
        db = _db()
        u = PoolUser(vid="purge-vid")
        db.add(u)
        db.flush()
        old = datetime.utcnow() - timedelta(days=400)
        db.add(PoolIpLog(user_id=u.id, ip="1.2.3.4", ip24="1.2.3",
                         created_at=old))
        db.add(PoolFingerprint(user_id=u.id, visitor_hash="oldfp",
                               last_at=old))
        uid = u.id
        db.commit()
        db.close()
        db = _db()
        res = antifraud.purge_tech_data(db)
        db.close()
        assert res["ip_log"] >= 1 and res["fingerprints"] >= 1


class TestVersion1340:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # Пин конкретной версии перенесён в tests/test_v1350.py (тест текущей версии)
        # v1.34.0: assert ver == "1.34.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.34.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.34.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "антифрод (v1.34.0)" in manual

    def test_fraud_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "fraud: viewFraud" in js and "fraud: 'Чек-Пул: антифрод'" in js
        assert "fraud: isAdmin()" in js
        assert "/api/v1/pool-fraud/summary" in js
        assert "resolve-bulk" in js and "annul_points" in js
        assert "poolFp" in js and "X-Visitor-Id" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'data-view="fraud"' in idx
        mn = open("app/main.py", encoding="utf-8").read()
        assert "pool_fraud" in mn

    def test_offerta_mentions_tech_data(self):
        assert "технические признаки" in open("app/pool/ingest.py", encoding="utf-8").read()

    def test_core_untouched(self):
        assert "pool_" not in open("app/models.py", encoding="utf-8").read()
        af = open("app/pool/antifraud.py", encoding="utf-8").read()
        for code in ("MULTI_ACCOUNT_DEVICE", "DATACENTER_IP", "BOT_PATTERN",
                     "EVEN_INTERVALS", "RECEIPT_FLOOD", "REPEAT_SUMS",
                     "REFERRAL_FRAUD"):
            assert code in af
