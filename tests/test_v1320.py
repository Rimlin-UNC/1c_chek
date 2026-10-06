# Ямастер Чек — тесты v1.32.0: «Чек-Пул» этап 3 — кабинет и регистрация.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 3 (docs/dev_plan_checkpool.md):
#  - гость, сдавший чеки БЕЗ регистрации, после создания кабинета видит
#    их и баллы (слияние по vid-cookie);
#  - кабинет — ОТДЕЛЬНЫЙ контур: ядро (users/roles) не затронуто;
#  - magic link и подтверждение e-mail — одноразовые токены (хэш в БД);
#  - «сдал чек — забрал чек»: CSV-экспорт и карточка чека с позициями;
#  - удаление аккаунта: ПДн стёрты, баллы сгорели, чеки остались;
#  - антифрод: rate-limit регистрации/входа; ошибки без раскрытия деталей.
import re
import time
from collections import deque
from datetime import datetime
from unittest.mock import patch

import pytest

from app.database import SessionLocal, engine
from app.pool import accounts, ingest as pool
from app.pool.models import (PoolConsent, PoolItem, PoolPoint, PoolReceipt,
                             PoolToken, PoolUser, ensure_pool_schema)
from app.services.external import ExternalItem, ExternalResult

ensure_pool_schema(engine)

_SEQ = {"n": 11000}


def _qr(sum_rub="900.00"):
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return (f"t=20260910T1000&s={sum_rub}&fn=9999078904{k:04d}"
            f"&i=66{k:03d}&fp=781{k:03d}&n=1")


_TICKET = ExternalResult(
    ok=True, source="test", found=True, message="OK",
    date_time=datetime(2026, 9, 10, 10, 0),
    total_sum=900.0, operation=1,
    merchant_name="ООО Ромашка", merchant_inn="7704001275",
    items=[ExternalItem(name="Кофе", quantity=1.0, price=900.0, total=900.0,
                        vat_rate="10", vat_sum=90.0)],
    raw={},
)


def _enable(db, on=True):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1" if on else "0")


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _wipe(db):
    """Чистим контур кабинета/веба: web+vid+email-аккаунты, токены, согласия."""
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
            db.query(PoolToken).delete(synchronize_session=False)
            db.query(PoolConsent).delete(synchronize_session=False)
            db.query(PoolUser).filter(
                (PoolUser.vid.isnot(None)) | (PoolUser.email != "")).delete(
                synchronize_session=False)
            _enable(db, False)
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1320: не удалось очистить данные кабинета")


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


def _wait_row(fn, timeout=6.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        db = _db()
        try:
            r = db.query(PoolReceipt).filter_by(source="web", fn=fn).first()
            if r is not None:
                n = db.query(PoolItem).filter_by(receipt_id=r.id).count()
                return r.id, r.status, n
        finally:
            db.close()
        time.sleep(0.05)
    return None


class TestRegistration:
    def test_register_login_flow(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        r = c.post("/api/v1/pool-auth/register",
                   json={"email": "anna@test.ru", "password": "parol-1234"})
        assert r.status_code == 200
        body = r.json()
        assert body["token"] and body["user"]["email"] == "anna@test.ru"
        assert body["user"]["email_verified"] is False
        # пароль хэширован (PBKDF2), не в открытом виде
        db = _db()
        u = accounts.find_by_email(db, "anna@test.ru")
        assert u is not None and u.password_hash.startswith("pbkdf2$")
        db.close()
        # вход
        r2 = c.post("/api/v1/pool-auth/login",
                    json={"email": "anna@test.ru", "password": "parol-1234"})
        assert r2.status_code == 200 and r2.json()["token"]
        # неверный пароль
        r3 = c.post("/api/v1/pool-auth/login",
                    json={"email": "anna@test.ru", "password": "WRONG"})
        assert r3.status_code == 401

    def test_register_validation(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        assert c.post("/api/v1/pool-auth/register",
                      json={"email": "плохой", "password": "parol-1234"}).status_code == 422
        assert c.post("/api/v1/pool-auth/register",
                      json={"email": "ok@test.ru", "password": "короче5"}).status_code == 422
        c.post("/api/v1/pool-auth/register",
               json={"email": "dup@test.ru", "password": "parol-1234"})
        assert c.post("/api/v1/pool-auth/register",
                      json={"email": "dup@test.ru", "password": "parol-1234"}).status_code == 422

    def test_register_disabled_pool(self):
        c = _client()
        r = c.post("/api/v1/pool-auth/register",
                   json={"email": "x@test.ru", "password": "parol-1234"})
        assert r.status_code == 403

    def test_rate_limit_login(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        code = None
        for i in range(12):
            r = c.post("/api/v1/pool-auth/login",
                       json={"email": "brute@test.ru", "password": f"p{i}"})
            code = r.status_code
        assert code == 429, "после 10 попыток — отказ по IP"


class TestGuestMerge:
    def test_guest_history_joins_account(self, monkeypatch):
        """Ключевой сценарий этапа: сдал 2 чека без регистрации →
        зарегистрировался → чеки и баллы в кабинете."""
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        for _ in range(2):
            b = c.post("/api/v1/public/pool/check",
                       json={"qr_text": _qr(), "offerta": True}).json()
            assert b["accepted"] and _wait_row(b["fn"])
        # гость зарегистрировался (vid-cookie та же сессия)
        r = c.post("/api/v1/pool-auth/register",
                   json={"email": "guest2acc@test.ru", "password": "parol-1234"})
        assert r.status_code == 200
        assert r.json()["merged_receipts"] == 2
        H = {"Authorization": f"Bearer {r.json()['token']}"}
        s = c.get("/api/v1/pool-my/summary", headers=H).json()
        assert s["receipts_total"] == 2 and s["points"] == 2
        # гость-пользователь исчез, чеки принадлежат аккаунту
        db = _db()
        assert db.query(PoolUser).filter(PoolUser.email == "guest2acc@test.ru").count() == 1
        assert db.query(PoolUser).filter(PoolUser.vid.isnot(None)).count() == 0
        assert db.query(PoolReceipt).filter(
            PoolReceipt.pool_user_id == r.json()["user"]["id"]).count() == 2
        db.close()

    def test_login_merges_too(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        c.post("/api/v1/pool-auth/register",
               json={"email": "later@test.ru", "password": "parol-1234"})
        c2 = _client()                        # новый гость без аккаунта
        b = c2.post("/api/v1/public/pool/check",
                    json={"qr_text": _qr(), "offerta": True}).json()
        assert _wait_row(b["fn"])
        # вошёл с той же cookie (TestClient хранит cookie сессии)
        r = c2.post("/api/v1/pool-auth/login",
                    json={"email": "later@test.ru", "password": "parol-1234"})
        assert r.json()["merged_receipts"] == 1
        H = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c2.get("/api/v1/pool-my/summary", headers=H).json()["points"] == 1


class TestMagicAndVerify:
    def test_magic_requires_smtp(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        assert c.post("/api/v1/pool-auth/magic",
                      json={"email": "any@test.ru"}).status_code == 400

    def test_magic_sends_one_time_token(self, monkeypatch):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        c.post("/api/v1/pool-auth/register",
               json={"email": "magic@test.ru", "password": "parol-1234"})
        sent = {}
        monkeypatch.setattr("app.pool.mailer.smtp_configured", lambda db: True)
        monkeypatch.setattr("app.pool.mailer.send_magic_link",
                            lambda db, email, link: sent.update(link=link) or (True, ""))
        r = c.post("/api/v1/pool-auth/magic", json={"email": "magic@test.ru"})
        assert r.status_code == 200 and "отправлено" in r.json()["message"]
        assert "#/pool-magic/" in sent["link"]
        raw = sent["link"].split("/#/pool-magic/")[1]
        db = _db()
        assert db.query(PoolToken).filter_by(purpose="login").count() == 1
        assert db.query(PoolToken).filter(
            PoolToken.token_hash == accounts._hash_token(raw)).count() == 1
        db.close()
        # вход по ссылке
        rc = c.post("/api/v1/pool-auth/magic/consume", json={"token": raw})
        assert rc.status_code == 200 and rc.json()["token"]
        # повторно — нельзя (одноразовый)
        assert c.post("/api/v1/pool-auth/magic/consume",
                      json={"token": raw}).status_code == 400

    def test_magic_unknown_email_silent(self, monkeypatch):
        """Анти-перечисление: для несуществующего e-mail ответ тот же."""
        monkeypatch.setattr("app.pool.mailer.smtp_configured", lambda db: True)
        sent = {}
        monkeypatch.setattr("app.pool.mailer.send_magic_link",
                            lambda db, email, link: sent.update(link=link) or (True, ""))
        c = _client()
        r = c.post("/api/v1/pool-auth/magic", json={"email": "nobody@test.ru"})
        assert r.status_code == 200 and "link" not in sent
        db = _db()
        assert db.query(PoolToken).count() == 0
        db.close()

    def test_verify_email(self, monkeypatch):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        r = c.post("/api/v1/pool-auth/register",
                   json={"email": "verify@test.ru", "password": "parol-1234"})
        uid = r.json()["user"]["id"]
        db = _db()
        raw = accounts._new_token(db, "verify", email="verify@test.ru",
                                  user_id=uid, ttl_seconds=3600)
        db.commit(); db.close()
        rv = c.post("/api/v1/pool-auth/verify", json={"token": raw})
        assert rv.status_code == 200
        db = _db()
        assert db.get(PoolUser, uid).email_verified is True
        db.close()
        # истёкший токен не принимается
        db = _db()
        raw2 = accounts._new_token(db, "verify", email="verify@test.ru",
                                   user_id=uid, ttl_seconds=-10)
        db.commit(); db.close()
        assert c.post("/api/v1/pool-auth/verify",
                      json={"token": raw2}).status_code == 400


class TestCabinet:
    def _account(self, c, email="cabinet@test.ru"):
        db = _db(); _enable(db, True); db.commit(); db.close()
        r = c.post("/api/v1/pool-auth/register",
                   json={"email": email, "password": "parol-1234"})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def test_summary_and_receipts(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        c = _client()
        H = self._account(c)
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True},
                   headers=H).json()          # фронт шлёт пул-токен кабинета
        assert _wait_row(b["fn"])
        s = c.get("/api/v1/pool-my/summary", headers=H).json()
        assert s["email"] == "cabinet@test.ru" and s["receipts_total"] == 1
        assert s["today"] == 1 and s["daily_limit"] == 50
        lst = c.get("/api/v1/pool-my/receipts", headers=H).json()
        assert lst["total"] == 1 and lst["items"][0]["items_count"] == 1
        # карточка чека с позициями
        det = c.get(f"/api/v1/pool-my/receipt/{lst['items'][0]['id']}",
                    headers=H).json()
        assert det["items"][0]["name"] == "Кофе"
        # чужой кабинет не видит этот чек
        c2 = _client(); H2 = self._account(c2, "other@test.ru")
        assert c2.get(f"/api/v1/pool-my/receipt/{lst['items'][0]['id']}",
                      headers=H2).status_code == 404
        # без токена — 401
        assert c.get("/api/v1/pool-my/summary").status_code == 401

    def test_export_csv(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        c = _client()
        H = self._account(c, "exporter@test.ru")
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True},
                   headers=H).json()
        assert _wait_row(b["fn"])
        r = c.get("/api/v1/pool-my/export.csv", headers=H)
        assert r.status_code == 200
        text = r.content.decode("utf-8-sig")   # BOM для Excel
        assert "Магазин" in text and "ООО Ромашка" in text
        assert "attachment" in r.headers.get("content-disposition", "")

    def test_password_and_email_change(self):
        c = _client()
        H = self._account(c, "chg@test.ru")
        # неверный старый пароль
        assert c.post("/api/v1/pool-my/password",
                      json={"old_password": "nope-nope", "new_password": "novyj-parol-1"},
                      headers=H).status_code == 403
        assert c.post("/api/v1/pool-my/password",
                      json={"old_password": "parol-1234", "new_password": "novyj-parol-1"},
                      headers=H).json()["ok"]
        # вход с новым паролем
        assert c.post("/api/v1/pool-auth/login",
                      json={"email": "chg@test.ru",
                            "password": "novyj-parol-1"}).status_code == 200
        # смена e-mail: занятый отклоняется
        c.post("/api/v1/pool-auth/register",
               json={"email": "busy@test.ru", "password": "parol-1234"})
        assert c.post("/api/v1/pool-my/email",
                      json={"password": "novyj-parol-1", "email": "busy@test.ru"},
                      headers=H).status_code == 409
        r = c.post("/api/v1/pool-my/email",
                   json={"password": "novyj-parol-1", "email": "fresh@test.ru"},
                   headers=H)
        assert r.status_code == 200
        db = _db()
        u = accounts.find_by_email(db, "fresh@test.ru")
        assert u is not None and u.email_verified is False
        db.close()

    def test_delete_account_wipes_pdn_burns_points(self, monkeypatch):
        """152-ФЗ: e-mail/пароль стёрты, баллы сгорели записью в журнале,
        чеки остались в пуле обезличенными."""
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        c = _client()
        H = self._account(c, "deleting@test.ru")
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True},
                   headers=H).json()
        assert _wait_row(b["fn"])
        assert c.post("/api/v1/pool-my/delete",
                      json={"password": "WRONG"}, headers=H).status_code == 403
        r = c.post("/api/v1/pool-my/delete",
                   json={"password": "parol-1234"}, headers=H)
        assert r.status_code == 200
        db = _db()
        assert db.query(PoolUser).filter_by(email="deleting@test.ru").count() == 0
        uid = r.json() and None
        # чек остался, без владельца
        rec = db.query(PoolReceipt).filter_by(fn=b["fn"]).first()
        assert rec is not None and rec.pool_user_id is None
        # запись о сгорании — в журнале баллов
        burn = db.query(PoolPoint).filter_by(reason="account_deleted").first()
        assert burn is not None and burn.delta == -1
        db.close()
        # вход больше невозможен
        assert c.post("/api/v1/pool-auth/login",
                      json={"email": "deleting@test.ru",
                            "password": "parol-1234"}).status_code == 401


class TestMailerAndAdmin:
    def test_smtp_settings_roundtrip_and_test(self, client):
        # session-клиент conftest: контекст запускает lifespan с сидом админа
        from tests.conftest import login
        c = client
        H = login(c, "admin", "admin123")
        r = c.put("/api/v1/pool-admin/smtp", headers=H, json={
            "host": "smtp.test.ru", "port": 465, "user": "chek@test.ru",
            "password": "secret-pass", "sender": "chek@test.ru",
            "tls": True, "base_url": "https://chek.ymaster.ru"})
        assert r.status_code == 200
        g = c.get("/api/v1/pool-admin/smtp", headers=H).json()
        assert g["host"] == "smtp.test.ru" and g["has_password"] is True
        assert g["configured"] is True
        # пароль в БД — только зашифрованным (secretbox)
        db = _db()
        from app.services import appsettings
        raw_val = db.get(appsettings.AppSetting, "smtp_pass")
        assert raw_val is not None and raw_val.value.startswith("enc:")
        db.close()
        with patch("app.pool.mailer.smtplib.SMTP") as m:
            m.return_value.__enter__ = lambda s: m.return_value
            rt = c.post("/api/v1/pool-admin/smtp-test", headers=H,
                        json={"email": "admin@test.ru"})
            assert rt.json()["ok"] is True

    def test_send_mail_without_config(self):
        from app.pool import mailer
        db = _db()
        ok, err = mailer.send_mail(db, "x@test.ru", "s", "body")
        db.close()
        assert ok is False and err


class TestVersion1320:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # Пин конкретной версии перенесён в tests/test_v1330.py (тест текущей версии)
        # v1.32.0: assert ver == "1.32.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.32.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.32.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "кабинет участника (v1.32.0)" in manual

    def test_cabinet_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "pool-account-root" in js and "ymaster-pool-token" in js
        assert "/api/v1/pool-auth/register" in js and "/api/v1/pool-auth/magic" in js
        assert "poolDownloadCsv" in js and "poolPrintList" in js
        assert "#/pool-magic/" in js and "#/pool-verify/" in js
        assert "my: viewPoolAccount" in js and "my: 'Мой Чек-Пул'" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="pool-account-screen"' in idx and 'id="pool-account-root"' in idx
        assert 'href="#/my" data-view="my"' in idx
        assert 'id="pool-smtp-save"' in js and "/api/v1/pool-admin/smtp" in js
        mn = open("app/main.py", encoding="utf-8").read()
        assert "pool_auth" in mn and "pool_my" in mn

    def test_core_untouched(self):
        """Ядро не знает о кабинете пула: users/roles без pool_, JWT пула — свой."""
        core = open("app/models.py", encoding="utf-8").read()
        assert "pool_" not in core
        acc = open("app/pool/accounts.py", encoding="utf-8").read()
        assert 'typ"] != "pool"' in acc.replace("'", '"') or '"typ"' in acc
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "pool_tokens" in md and "password_hash" in md
