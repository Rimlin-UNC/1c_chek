# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.5: проверка токена в один клик + код входа
# не сгорает при неудачной регистрации.
# ======================================================================
import re

import pytest

BOT_TOKEN = "700001.ABCDEF"


def _setup(client, nick="chek.ymaster.bot"):
    from app.database import SessionLocal
    from app.services import appsettings
    s = SessionLocal()
    try:
        appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
        appsettings.set_setting(s, "max_bot_username", nick)
    finally:
        s.rollback(); s.close()


class TestCheckToken1705:
    def test_check_token_ok_and_mismatch(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        _setup(client)
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "id4712030034_bot"})
        st, a = client.post("/api/v1/auth/login", json={
            "username": "admin", "password": "admin123"}).status_code, None
        h = {"Authorization": "Bearer " + client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin123"}
        ).json()["access_token"]}
        d = client.get("/api/v1/settings/max/check-token", headers=h).json()
        assert d["ok"] is True and d["bot"] == "id4712030034_bot"
        assert d["public_nick"] == "chek.ymaster.bot"
        assert d["mismatch"] is True
        # теперь токен «правильного» бота
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "chek.ymaster.bot"})
        max_bot._username_cache = ""
        d2 = client.get("/api/v1/settings/max/check-token", headers=h).json()
        assert d2["mismatch"] is False and d2["bot"] == "chek.ymaster.bot"

    def test_check_token_requires_admin(self, client):
        client.cookies.clear()   # client живёт на модуль — сбросим авторизацию
        r = client.get("/api/v1/settings/max/check-token")
        assert r.status_code in (401, 403)

    def test_check_token_no_token_400(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_token", lambda db: "")
        h = {"Authorization": "Bearer " + client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin123"}
        ).json()["access_token"]}
        r = client.get("/api/v1/settings/max/check-token", headers=h)
        assert r.status_code == 400

    def test_card_has_button_and_steps(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "mx-who" in js and "check-token" in js
        assert "business.max.ru" in js


class TestPollOrder1705:
    def _code(self, client):
        return client.post("/api/v1/auth/max/start", json={}).json()["code"]

    def _confirm(self, client, code, uid=85001):
        import hashlib, hmac, json, time, urllib.parse
        ad = int(time.time())
        raw = [("auth_date", str(ad)), ("query_id", f"q{uid}"),
               ("user", json.dumps({"id": uid, "username": "pollorder"}))]
        launch = "\n".join(f"{k}={v}" for k, v in sorted(raw))
        sec = hmac.new(b"WebAppData", BOT_TOKEN.encode(),
                       hashlib.sha256).digest()
        sig = hmac.new(sec, launch.encode(), hashlib.sha256).hexdigest()
        pairs = [(k, urllib.parse.quote(v, safe="")) for k, v in raw]
        pairs.append(("hash", sig))
        init_data = "&".join(f"{k}={v}" for k, v in pairs)
        _setup(client)
        sess = client.post("/api/v1/maxapp/session",
                           json={"init_data": init_data}).json()
        r = client.post("/api/v1/maxapp/confirm-login", headers={
            "Authorization": "Bearer " + sess["access_token"]},
            json={"code": code})
        assert r.status_code == 200, r.text

    def test_code_not_consumed_on_failed_register(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        code = self._code(client)
        self._confirm(client, code)
        # регистрация выключена → poll 403
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_web_register", "0")
        finally:
            s.rollback(); s.close()
        r = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert r.status_code == 403
        # включаем — код всё ещё жив (не сгорел), регистрация проходит
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_web_register", "1")
        finally:
            s.rollback(); s.close()
        p = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p.status_code == 200 and p.json()["status"] == "ok"


class TestVersions1705:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.6: строгий замок версии переехал в test_v1706
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 5)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and '?v=1.70.4"' not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "-v1.70.4'" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.5]") == 1
        assert ch.index("## [1.70.5]") < ch.index("## [1.70.4]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.5':") < js.index("'1.70.4':")


class TestNoRegression1705:
    def test_login_flow_alive(self, client):
        _setup(client)
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_diag_fields_alive(self, client):
        from app.database import SessionLocal
        from app.services import max_bot
        st = max_bot.bot_settings(SessionLocal())
        assert "token_bot" in st and "token_mismatch" in st
