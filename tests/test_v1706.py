# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.6: диплинки по username бота из токена
# (в т.ч. служебному id…_bot) и фото-сканер мини-приложения (/scan-image).
# ======================================================================
import hashlib
import hmac as hmac_mod
import io
import json
import re
import time
import urllib.parse

import pytest

BOT_TOKEN = "700001.ABCDEF"


def init_data(uid: int, username: str = "qa") -> str:
    ad = int(time.time())
    raw = [("auth_date", str(ad)),
           ("query_id", f"q{uid}"),
           ("user", json.dumps({"id": uid, "username": username},
                               ensure_ascii=False))]
    launch = "\n".join(f"{k}={v}" for k, v in sorted(raw))
    sec = hmac_mod.new(b"WebAppData", BOT_TOKEN.encode(),
                       hashlib.sha256).digest()
    sig = hmac_mod.new(sec, launch.encode(), hashlib.sha256).hexdigest()
    pairs = [(k, urllib.parse.quote(v, safe="")) for k, v in raw]
    pairs.append(("hash", sig))
    return "&".join(f"{k}={v}" for k, v in pairs)


def _setup(client, nick=""):
    from app.database import SessionLocal
    from app.services import appsettings
    s = SessionLocal()
    try:
        appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
        appsettings.set_setting(s, "max_bot_username", nick)
    finally:
        s.rollback(); s.close()


def _admin(client):
    r = client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "admin123"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


class TestLinksFollowToken:
    def test_tech_username_valid_for_links(self, client):
        _setup(client, nick="id4712030034_bot")
        from app.database import SessionLocal
        from app.services import max_bot
        s = SessionLocal()
        try:
            assert max_bot.bot_username(s) == "id4712030034_bot"
        finally:
            s.rollback(); s.close()

    def test_login_link_uses_token_bot(self, client):
        _setup(client, nick="id4712030034_bot")
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.json()["max_link"].startswith(
            "https://max.ru/id4712030034_bot?startapp=login_")

    def test_about_returns_token_bot(self, client):
        _setup(client, nick="id4712030034_bot")
        d = client.get("/api/v1/about").json()
        assert d["max_bot_username"] == "id4712030034_bot"

    def test_refresh_saves_real_username(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "id999999_bot"})
        _setup(client, nick="")
        s = SessionLocal()
        try:
            max_bot.refresh_username(s)
            assert appsettings.get_setting(
                s, "max_bot_username", "") == "id999999_bot"
        finally:
            s.rollback(); s.close()
            appsettings.set_setting(s, "max_bot_username", "")

    def test_check_token_mismatch_semantics(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        h = _admin(client)
        # ник из токена id…_bot; в настройках вручную оставлен chek.ymaster.bot
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "id4712030034_bot"})
        _setup(client, nick="chek.ymaster.bot")
        d = client.get("/api/v1/settings/max/check-token", headers=h).json()
        assert d["mismatch"] is True and d["bot"] == "id4712030034_bot"
        # ник очищен и совпадает с токен-ботом → нет несовпадения
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "chek.ymaster.bot"})
        max_bot._username_cache = ""             # изоляция глобального кэша
        _setup(client, nick="")
        d2 = client.get("/api/v1/settings/max/check-token", headers=h).json()
        assert d2["mismatch"] is False


class TestScanImage:
    def _session(self, client, uid):
        _setup(client)
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data": init_data(uid)})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["access_token"]}

    def _png(self, text: str) -> bytes:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(text).save(buf, format="PNG")
        return buf.getvalue()

    def test_photo_scan_accepts_receipt(self, client, monkeypatch):
        from datetime import datetime as _dt
        from app.pool import ingest
        from app.services.external import ExternalResult
        monkeypatch.setattr(
            ingest, "_fetch_details",
            lambda *a, **k: ExternalResult(
                ok=True, source="test", found=True, message="OK",
                date_time=_dt(2026, 10, 13, 11, 0), total_sum=700.0,
                operation=1, merchant_name="ООО Фото", merchant_inn="1",
                items=[], raw={}))
        H = self._session(client, 86001)
        qr = ("t=20261013T110000&s=700&fn=9900000000088888"
              "&i=88888&fp=888888&n=1")
        r = client.post("/api/v1/maxapp/scan-image", headers=H,
                        files={"file": ("qr.png", self._png(qr),
                                        "image/png")})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["accepted"] is True and d["fn"] == "9900000000088888", d
        time.sleep(0.8)
        me = client.get("/api/v1/maxapp/me", headers=H).json()
        assert any(x.get("fn") == "9900000000088888"
                   for x in me.get("receipts", []))

    def test_photo_without_qr_422ish_message(self, client):
        H = self._session(client, 86002)
        r = client.post("/api/v1/maxapp/scan-image", headers=H,
                        files={"file": ("x.png", self._png("привет"),
                                        "image/png")})
        assert r.status_code == 200
        d = r.json()
        assert d["accepted"] is False and d["result"] == "bad_qr"

    def test_photo_requires_auth(self, client):
        r = client.post("/api/v1/maxapp/scan-image",
                        files={"file": ("x.png", b"1", "image/png")})
        assert r.status_code in (401, 403)

    def test_scan_endpoint_still_works(self, client, monkeypatch):
        from datetime import datetime as _dt
        from app.pool import ingest
        from app.services.external import ExternalResult
        monkeypatch.setattr(
            ingest, "_fetch_details",
            lambda *a, **k: ExternalResult(
                ok=True, source="test", found=True, message="OK",
                date_time=_dt(2026, 10, 13, 11, 0), total_sum=100.0,
                operation=1, merchant_name="M", merchant_inn="1",
                items=[], raw={}))
        H = self._session(client, 86003)
        r = client.post("/api/v1/maxapp/scan", headers=H,
                        json={"qr": "t=20261013T110000&s=100"
                                    "&fn=9900000000088889&i=88889"
                                    "&fp=888889&n=1", "form_ms": 5000})
        assert r.status_code == 200 and r.json()["accepted"] is True

    def test_maxapp_has_photo_button(self):
        html = open("app/static/maxapp/index.html", encoding="utf-8").read()
        assert 'id="btn-photo"' in html and 'id="qr-file"' in html
        js = open("app/static/maxapp/maxapp.js", encoding="utf-8").read()
        assert "uploadPhoto" in js and "scan-image" in js


class TestVersions1706:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.70.6"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and "?v=1.70.5" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.70.5" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
        mapp = open("app/static/maxapp/index.html", encoding="utf-8").read()
        assert f"/maxapp/maxapp.js?v={ver}" in mapp

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.6]") == 1
        assert ch.index("## [1.70.6]") < ch.index("## [1.70.5]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.6':") < js.index("'1.70.5':")


class TestNoRegression1706:
    def test_login_flow_alive(self, client):
        _setup(client, nick="chek.ymaster.bot")
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_worker_gate_alive(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_token", lambda db: "tok")
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_enabled", "1")
            assert max_bot._should_run(s) is True
        finally:
            s.rollback(); s.close()
