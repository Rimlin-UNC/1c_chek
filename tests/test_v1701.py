# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.1: фиксы связи с MAX.
#  1) username бота — фактический (из MAX API → настройки), не хардкод;
#  2) мини-приложение открывается по адресу без слэша (абсолютный ассет);
#  3) воркер MAX живёт по «Включён + токен», без гейта канала уведомлений.
# ======================================================================
import re

import pytest


# ------------------------------------------------------------------ версии/статика
class TestVersions1701:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.70.1"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and "?v=1.70.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.70.0" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
        mapp = open("app/static/maxapp/index.html", encoding="utf-8").read()
        # v1.70.1: абсолютный путь — страница работает и без слэша (/maxapp)
        assert f'src="/maxapp/maxapp.js?v={ver}"' in mapp
        assert 'src="maxapp.js' not in mapp

    def test_maxapp_page_without_trailing_slash(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.get("/maxapp")                  # как в настройках MAX
            assert r.status_code == 200
            assert 'src="/maxapp/maxapp.js' in r.text

    def test_spa_prefix_slashed(self):
        main = open("app/main.py", encoding="utf-8").read()
        assert '"ws/", "maxapp/")' in main and '"ws/", "maxapp")' not in main

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.1]") == 1
        assert ch.index("## [1.70.1]") < ch.index("## [1.70.0]")


# ------------------------------------------------------------------ username бота
class TestBotUsername:
    def test_refresh_persists_and_no_network_after(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        calls = {"n": 0}

        def fake_me(token):
            calls["n"] += 1
            return {"username": "id4712030034_bot", "name": "chek.ymaster.ru"}

        monkeypatch.setattr(max_bot, "get_me", fake_me)
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
            assert max_bot.refresh_username(s) == "id4712030034_bot"
            assert calls["n"] == 1
            # сохранился в настройках — дальше сеть не нужна
            assert max_bot.bot_username(s) == "id4712030034_bot"
            assert calls["n"] == 1
        finally:
            s.rollback(); s.close()

    def test_fallback_when_unknown(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import max_bot
        monkeypatch.setattr(max_bot, "refresh_username", lambda db: "")
        s = SessionLocal()
        try:
            assert max_bot.bot_username(s, refresh=False) in (
                "chek.ymaster.bot", "id4712030034_bot")
        finally:
            s.rollback(); s.close()

    def test_login_deep_link_uses_real_username(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_username", "id4712030034_bot")
        finally:
            s.rollback(); s.close()
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        assert r.json()["max_link"] == (
            "https://max.ru/id4712030034_bot?startapp=" + r.json()["code"])

    def test_ref_link_uses_real_username(self, client, monkeypatch):
        import hashlib, hmac, json, time, urllib.parse
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_username", "id4712030034_bot")
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
            appsettings.set_setting(s, "pool_enabled", "1")
        finally:
            s.rollback(); s.close()
        token = "700001.ABCDEF"
        ad = int(time.time())
        user = json.dumps({"id": 777001, "first_name": "Реф",
                           "username": "refuser", "language_code": "ru"},
                          ensure_ascii=False)
        pairs = sorted([("auth_date", str(ad)), ("query_id", f"q{ad}"),
                        ("user", user)], key=lambda x: x[0])
        launch = "\n".join(f"{k}={v}" for k, v in pairs)
        sec = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        sig = hmac.new(sec, launch.encode(), hashlib.sha256).hexdigest()
        pairs.append(("hash", sig))
        init_data = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                             for k, v in pairs)
        r = client.post("/api/v1/maxapp/session", json={"init_data": init_data})
        assert r.status_code == 200, r.text
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        d = client.get("/api/v1/maxapp/summary", headers=h).json()
        assert d["ref_link"].startswith(
            "https://max.ru/id4712030034_bot?startapp=ref_")

    def test_about_exposes_bot_username(self, client):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_username", "id4712030034_bot")
        finally:
            s.rollback(); s.close()
        d = client.get("/api/v1/about").json()
        assert d["max_bot_username"] == "id4712030034_bot"


# ------------------------------------------------------------------ воркер
class TestWorkerGate:
    def test_runs_regardless_of_channel(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_token", lambda db: "tok")
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_enabled", "1")
            appsettings.set_setting(s, "assistants_channel", "telegram")
            # v1.70.1: канал уведомлений не глушит бота
            assert max_bot._should_run(s) is True
            appsettings.set_setting(s, "max_enabled", "0")
            assert max_bot._should_run(s) is False
        finally:
            s.rollback(); s.close()
            appsettings.set_setting(s, "max_enabled", "0")

    def test_no_token_no_run(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_token", lambda db: "")
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_enabled", "1")
            assert max_bot._should_run(s) is False
        finally:
            s.rollback(); s.close()


# ------------------------------------------------------------------ замки
class TestNoRegression1701:
    def test_v1700_login_flow_alive(self, client):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
        finally:
            s.rollback(); s.close()
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        d = r.json()
        assert d["max_link"].startswith("https://max.ru/")
        assert d["max_link"].endswith("?startapp=" + d["code"])
        p = client.post("/api/v1/auth/max/poll", json={"code": d["code"]})
        assert p.json()["status"] == "pending"

    def test_password_login_alive(self, client):
        r = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200

    def test_whatsnew_1701(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.1':") < js.index("'1.70.0':")
