# Ямастер Чек — тесты v1.35.0: «Чек-Пул» этап 6 — рефералы с порогами.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 6 (docs/dev_plan_checkpool.md):
#  - код YM-XXXXXX и ссылка /#/r/КОД; публичная информация о коде без ПДн;
#  - бонусы НЕ за регистрацию, а за качественные действия С ЗАДЕРЖКОЙ:
#    R_EMAIL +5 (24 ч), R_FIRST_CHECK +20 (7 д, чек ≥100 ₽), R_FIFTH +50 (14 д),
#    R_TWENTIETH +150 (30 д, trust≥1), R_FIFTIETH +500 (60 д, trust≥2);
#  - lifetime 5%: каждый 20-й чек → +1, потолок 200 баллов/мес;
#  - один уровень; лимит 50 приглашённых;
#  - «сам себя пригласил» НЕ приносит ни балла: общее устройство/подсеть/карантин
#    держат выплаты (fraud_hold), разбор ложного возвращает начисления;
#  - все выплаты — в журнале pool_points (reason R_*); ядро не затронуто.
import re
import time
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal, engine
from app.pool import antifraud, referral
from app.pool.models import (PoolConsent, PoolFingerprint, PoolItem, PoolIpLog,
                             PoolPoint, PoolReceipt, PoolReferral, PoolSignal,
                             PoolToken, PoolUser, ensure_pool_schema)
from app.services.external import ExternalItem, ExternalResult

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
            ids = [u.id for u in db.query(PoolUser).filter(
                (PoolUser.vid.isnot(None)) | (PoolUser.email != "")).all()]
            recs = [r[0] for r in db.query(PoolReceipt.id).filter(
                (PoolReceipt.source == "web")
                | (PoolReceipt.pool_user_id.in_(ids or [""]))).all()]
            if recs:
                db.query(PoolItem).filter(PoolItem.receipt_id.in_(recs)).delete(
                    synchronize_session=False)
                db.query(PoolReceipt).filter(PoolReceipt.id.in_(recs)).delete(
                    synchronize_session=False)
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
            db.query(PoolReferral).delete(synchronize_session=False)
            db.query(PoolSignal).delete(synchronize_session=False)
            db.query(PoolIpLog).delete(synchronize_session=False)
            db.query(PoolFingerprint).delete(synchronize_session=False)
            db.query(PoolToken).delete(synchronize_session=False)
            db.query(PoolConsent).delete(synchronize_session=False)
            db.query(PoolUser).filter(
                (PoolUser.vid.isnot(None)) | (PoolUser.email != "")).delete(
                synchronize_session=False)
            from app.services import appsettings
            appsettings.set_setting(db, "pool_enabled", "1")
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1350: не удалось очистить данные")


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


def _reg(c, email, password="parol-12345", headers=None):
    r = c.post("/api/v1/pool-auth/register",
               json={"email": email, "password": password,
                     "ref_code": (headers or {}).pop("ref_code", "")},
               headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


_TICKET = ExternalResult(
    ok=True, source="test", found=True, message="OK",
    date_time=datetime(2026, 10, 1, 12, 0),
    total_sum=300.0, operation=1,
    merchant_name="Пятёрочка", merchant_inn="7701",
    items=[ExternalItem(name="Хлеб", quantity=1.0, price=300.0, total=300.0)])


def _verified_receipt(db, referee, total=300.0, n_fn=None):
    """Создаёт верифицированный чек реферала и прогоняет бонусный движок."""
    _SEQ["k"] += 1
    r = PoolReceipt(pool_user_id=referee.id, source="web",
                    fn=n_fn or f"7700000000{_SEQ['k']:04d}", fd="1",
                    fp=str(_SEQ["k"]), total_sum=total, status="verified",
                    created_at=datetime.utcnow())
    db.add(r)
    db.flush()
    paid = referral.on_verified_receipt(db, referee, r)
    return r, paid


_SEQ = {"k": 1000}


class TestCodes:
    def test_generate_and_lookup(self):
        db = _db()
        u = PoolUser(email="code@x.ru", password_hash="x")
        db.add(u)
        db.flush()
        code = referral.get_or_create_code(db, u)
        assert re.fullmatch(r"YM-[A-Z2-9]{6}", code), code
        assert referral.get_or_create_code(db, u) == code   # стабильный
        assert referral.owner_of_code(db, code.lower()).id == u.id
        assert referral.owner_of_code(db, "YM-ZZZZZZ") is None
        db.rollback()
        db.close()

    def test_public_ref_info(self):
        c = _client()
        owner = _reg(c, "refowner@x.ru")
        db = _db()
        u = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, u)
        db.commit()
        db.close()
        info = c.get(f"/api/v1/public/pool/ref/{code}").json()
        assert info["valid"] is True and "***" in info["label"]
        assert c.get("/api/v1/public/pool/ref/YM-ZZZZZ9").json()["valid"] is False


class TestAttribution:
    def test_register_with_ref_code(self):
        c = _client()
        owner = _reg(c, "attr-owner@x.ru")
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        db.commit()
        db.close()
        newbie = _reg(c, "attr-new@x.ru", headers={"ref_code": code})
        assert newbie["referred"] is True
        db = _db()
        ref = (db.query(PoolReferral)
               .filter_by(referred_id=newbie["user"]["id"]).first())
        assert ref is not None and ref.referrer_id == owner["user"]["id"]
        assert ref.code_used == code
        db.close()

    def test_bad_code_ignored(self):
        c = _client()
        res = _reg(c, "attr-bad@x.ru", headers={"ref_code": "YM-NOPE1"})
        assert res["referred"] is False
        db = _db()
        assert db.query(PoolReferral).count() == 0
        db.close()

    def test_self_invite_impossible(self):
        db = _db()
        u = PoolUser(email="self@x.ru", password_hash="x")
        db.add(u)
        db.flush()
        code = referral.get_or_create_code(db, u)
        assert referral.attribute(db, u, code) is False   # сам себя
        db.rollback()
        db.close()

    def test_limit_50(self):
        c = _client()
        owner = _reg(c, "limit-owner@x.ru")
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        users = [PoolUser(email=f"l{i}@x.ru") for i in range(50)]
        db.add_all(users)
        db.flush()
        for u in users:
            db.add(PoolReferral(referrer_id=ou.id, referred_id=u.id,
                                code_used=code))
        db.commit()
        db.close()
        extra = _reg(c, "limit-51@x.ru", headers={"ref_code": code})
        assert extra["referred"] is False, "51-й не должен привязаться"


class TestThresholdBonuses:
    def _pair(self, referrer_email="bonus-r@x.ru", referee_email="bonus-n@x.ru"):
        c = _client()
        owner = _reg(c, referrer_email)
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        db.commit()
        db.close()
        newbie = _reg(c, referee_email, headers={"ref_code": code})
        return owner, newbie

    def _referral_row(self, referee_id):
        db = _db()
        ref = db.query(PoolReferral).filter_by(referred_id=referee_id).first()
        db.close()
        return ref

    def _referrer_points(self, referrer_id, reason):
        db = _db()
        rows = (db.query(PoolPoint)
                .filter_by(user_id=referrer_id, reason=reason).all())
        db.close()
        return rows

    def test_email_bonus_after_24h(self):
        c = _client()
        owner, newbie = self._pair("em-r@x.ru", "em-n@x.ru")
        rid = self._referral_row(newbie["user"]["id"]).id
        # раньше срока (24 ч) — выплаты нет
        db = _db()
        nu = db.get(PoolUser, newbie["user"]["id"])
        nu.email_verified = True
        assert referral.on_email_verified(db, nu) is False
        db.close()
        assert not self._referrer_points(owner["user"]["id"], "R_EMAIL")
        # прошло 25 часов — выплата +5
        db = _db()
        ref = db.get(PoolReferral, rid)
        ref.created_at = datetime.utcnow() - timedelta(hours=25)
        nu = db.get(PoolUser, newbie["user"]["id"])
        assert referral.on_email_verified(db, nu) is True
        db.commit()
        db.close()
        rows = self._referrer_points(owner["user"]["id"], "R_EMAIL")
        assert rows and rows[0].delta == 5
        # повторно — не платим
        db = _db()
        nu = db.get(PoolUser, newbie["user"]["id"])
        assert referral.on_email_verified(db, nu) is False
        db.close()

    def test_first_check_min_sum_and_delay(self):
        c = _client()
        owner, newbie = self._pair("fc-r@x.ru", "fc-n@x.ru")
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ref.created_at = datetime.utcnow() - timedelta(days=8)   # задержка ок
        db.commit()
        # первый чек 80 ₽ — мало
        _verified_receipt(db, ru, total=80.0)
        db.commit()
        assert not self._referrer_points(owner["user"]["id"], "R_FIRST_CHECK")
        # но n уже 1 — «первый» сгорел; следующий чек (n=2) бонус не даёт
        _verified_receipt(db, ru, total=300.0)
        db.commit()
        assert not self._referrer_points(owner["user"]["id"], "R_FIRST_CHECK")
        db.close()

    def test_first_check_paid_when_valid(self):
        c = _client()
        owner, newbie = self._pair("fv-r@x.ru", "fv-n@x.ru")
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ref.created_at = datetime.utcnow() - timedelta(days=7)
        # регистрация «вчера» — иначе первый чек считается мгновенным (FAST)
        ru.created_at = datetime.utcnow() - timedelta(hours=20)
        db.commit()
        _verified_receipt(db, ru, total=250.0)
        db.commit()
        rows = self._referrer_points(owner["user"]["id"], "R_FIRST_CHECK")
        assert rows and rows[0].delta == 20
        db.close()

    def test_fifth_check(self):
        c = _client()
        owner, newbie = self._pair("f5-r@x.ru", "f5-n@x.ru")
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ref.created_at = datetime.utcnow() - timedelta(days=14)
        ru.created_at = datetime.utcnow() - timedelta(hours=20)
        db.commit()
        for i in range(4):
            _verified_receipt(db, ru, total=200.0 + i)
        db.commit()
        assert not self._referrer_points(owner["user"]["id"], "R_FIFTH_CHECK")
        _verified_receipt(db, ru, total=210.0)          # 5-й
        db.commit()
        rows = self._referrer_points(owner["user"]["id"], "R_FIFTH_CHECK")
        assert rows and rows[0].delta == 50
        db.close()

    def test_trust_gate_twentieth(self):
        c = _client()
        owner, newbie = self._pair("t20-r@x.ru", "t20-n@x.ru")
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ref.created_at = datetime.utcnow() - timedelta(days=31)
        ru.created_at = datetime.utcnow() - timedelta(hours=20)
        db.commit()
        for i in range(19):
            _verified_receipt(db, ru, total=100.0 + i)
        db.commit()
        # trust 0 — бонус ждёт
        _verified_receipt(db, ru, total=120.0)          # 20-й
        db.commit()
        assert not self._referrer_points(owner["user"]["id"], "R_TWENTIETH")
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ru.trust_level = 1
        db.commit()
        # 21-й не даёт (порог n==20 уже пройден) — верифицируем напрямую
        paid = referral.on_verified_receipt(db, ru, None)
        db.close()
        # бонус за 20-й не начисляется постфактум (n=21) — по плану честно
        assert not self._referrer_points(owner["user"]["id"], "R_TWENTIETH")

    def test_lifetime_and_month_cap(self):
        c = _client()
        owner, newbie = self._pair("lt-r@x.ru", "lt-n@x.ru")
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ru.created_at = datetime.utcnow() - timedelta(hours=20)
        db.commit()
        for i in range(19):
            _verified_receipt(db, ru, total=100.0 + i)
        db.commit()
        assert not self._referrer_points(owner["user"]["id"], "R_LIFETIME")
        _verified_receipt(db, ru, total=130.0)          # 20-й → +1 (5%)
        db.commit()
        rows = self._referrer_points(owner["user"]["id"], "R_LIFETIME")
        assert rows and rows[0].delta == 1
        # потолок 200/мес: реальная выплата одна (на 20-м); досыпаем 200
        # фейковых (delta=0) — месяц «закрыт», на 40-м чеке новых выплат нет
        db = _db()
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ou = db.get(PoolUser, owner["user"]["id"])
        for i in range(200):
            db.add(PoolPoint(user_id=ou.id, delta=0, reason="R_LIFETIME",
                             ref_id=ref.id))
        db.commit()
        db.close()
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        for i in range(20):
            _verified_receipt(db, ru, total=140.0 + i)  # дорастаем до 40-го
        db.commit()
        db.close()
        real = [p for p in self._referrer_points(owner["user"]["id"],
                                                 "R_LIFETIME") if p.delta == 1]
        assert len(real) == 1, "потолок 200/мес должен остановить выплаты"


class TestFraudHold:
    def test_same_device_no_bonuses(self):
        """«Сам себя пригласил»: одно устройство → MULTI сигнал → выплаты стоят."""
        c = _client()
        H = {"X-Visitor-Id": "same-device-42", "X-Forwarded-For": "91.5.5.5"}
        owner = _reg(c, "self-r@x.ru", headers=H)
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        db.commit()
        db.close()
        # «второй аккаунт» с того же устройства
        newbie = _reg(c, "self-n@x.ru", headers={"ref_code": code, **H})
        assert newbie["referred"] is True        # связь создаётся...
        db = _db()
        ref = db.query(PoolReferral).filter_by(
            referred_id=newbie["user"]["id"]).first()
        ref.created_at = datetime.utcnow() - timedelta(days=2)
        nu = db.get(PoolUser, newbie["user"]["id"])
        nu.email_verified = True
        paid = referral.on_email_verified(db, nu)
        db.commit()
        hold, why = referral.fraud_hold(db, ref)
        db.close()
        assert hold, "пара с общим устройством должна держать выплаты"
        assert ("устройство" in why) or ("сигналы" in why)
        assert paid is False
        assert not self._points(owner["user"]["id"], "R_EMAIL")

    def _points(self, uid, reason):
        db = _db()
        rows = db.query(PoolPoint).filter_by(user_id=uid, reason=reason).all()
        db.close()
        return rows

    def test_same_subnet_signal(self):
        """Пригласивший и реферал из одной /24 → сигнал на пригласившего."""
        c = _client()
        owner = _reg(c, "net-r@x.ru", headers={"X-Forwarded-For": "91.7.7.7"})
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        db.commit()
        db.close()
        _reg(c, "net-n@x.ru", headers={"ref_code": code,
                                       "X-Forwarded-For": "91.7.7.99"})
        db = _db()
        sig = (db.query(PoolSignal)
               .filter_by(code="SAME_SUBNET_REFERRAL").first())
        db.close()
        assert sig is not None and sig.points == 45

    def test_fast_receipt_signal_holds_bonuses(self):
        """Чек через 2 минуты после регистрации → FAST_REFERRAL → бонус стоит."""
        c = _client()
        owner, newbie = None, None
        owner = _reg(c, "fast-r@x.ru")
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        db.commit()
        db.close()
        newbie = _reg(c, "fast-n@x.ru", headers={"ref_code": code})
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ref.created_at = datetime.utcnow() - timedelta(days=8)
        # чек через 2 минуты после регистрации (создан сейчас)
        _verified_receipt(db, ru, total=300.0)
        db.commit()
        sig = (db.query(PoolSignal)
               .filter_by(user_id=ru.id, code="FAST_REFERRAL").first())
        pts = self._points(owner["user"]["id"], "R_FIRST_CHECK")
        db.close()
        assert sig is not None and sig.points == 40
        assert not pts, "быстрый чек держит выплаты"

    def test_quarantined_referrer_no_payout_and_release(self):
        c = _client()
        owner, newbie = None, None
        owner = _reg(c, "quar-r@x.ru", headers={"X-Forwarded-For": "52.3.3.3"})
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        antifraud.record_signal(db, ou, "DATACENTER_IP")
        code = referral.get_or_create_code(db, ou)
        # добиваем риск до карантина
        antifraud.record_signal(db, ou, "BOT_PATTERN")
        antifraud.record_signal(db, ou, "EVEN_INTERVALS")
        db.commit()
        db.refresh(ou)
        assert ou.quarantined_at is not None
        db.close()
        newbie = _reg(c, "quar-n@x.ru", headers={"ref_code": code})
        db = _db()
        ru = db.get(PoolUser, newbie["user"]["id"])
        ref = db.query(PoolReferral).filter_by(referred_id=ru.id).first()
        ref.created_at = datetime.utcnow() - timedelta(days=2)
        nu = db.get(PoolUser, newbie["user"]["id"])
        nu.email_verified = True
        assert referral.on_email_verified(db, nu) is False
        # разбор: снимаем сигналы пригласившего → выплата проходит
        for s in (db.query(PoolSignal).filter_by(user_id=ou.id).all()):
            antifraud.resolve_signal(db, s, "false_positive", "admin")
        db.commit()
        assert referral.on_email_verified(db, nu) is True
        db.commit()
        rows = self._points(owner["user"]["id"], "R_EMAIL")
        db.close()
        assert rows and rows[0].delta == 5


class TestReferralsApi:
    def test_referrals_endpoint(self):
        c = _client()
        owner = _reg(c, "api-r@x.ru")
        H = {"Authorization": f"Bearer {owner['token']}"}
        db = _db()
        ou = db.get(PoolUser, owner["user"]["id"])
        code = referral.get_or_create_code(db, ou)
        db.commit()
        db.close()
        _reg(c, "api-n@x.ru", headers={"ref_code": code})
        d = c.get("/api/v1/pool-my/referrals", headers=H).json()
        assert re.fullmatch(r"YM-[A-Z2-9]{6}", d["code"])
        assert "#/r/" in d["link"]
        assert d["invited"] == 1 and d["limit"] == 50
        assert d["items"][0]["label"].startswith("а***") or "***" in d["items"][0]["label"]
        assert "next_bonus" in d

    def test_referrals_require_auth(self):
        c = _client()
        assert c.get("/api/v1/pool-my/referrals").status_code == 401


class TestVersion1350:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.35.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.35.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.35.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "приглашения (v1.35.0)" in manual

    def test_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "#/r/" in js and "poolRefGet" in js and "poolRefSave" in js
        assert "pool-invite" in js and "/api/v1/pool-my/referrals" in js
        assert "ref_code: poolRefGet()" in js
        assert "pub-ref" in js and "p-ref-hint" in js
        assert "(Этап 6 · v1.35.0)" in js

    def test_core_and_model(self):
        assert "pool_" not in open("app/models.py", encoding="utf-8").read()
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "pool_referrals" in md
        rf = open("app/pool/referral.py", encoding="utf-8").read()
        assert "R_FIRST_CHECK" in rf and "R_FIFTIETH" in rf
        assert "LIFETIME_MONTH_CAP = 200" in rf
        af = open("app/pool/antifraud.py", encoding="utf-8").read()
        for code in ("SAME_SUBNET_REFERRAL", "FAST_REFERRAL", "REFERRAL_CYCLE"):
            assert code in af
