# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.2: CSP пропускает Bridge MAX (st.max.ru),
# технический ник бота (idNNNN_bot) не попадает в диплинки, публичный
# ник настраивается в карточке MAX, мини-приложение дожидается Bridge.
# ======================================================================
import re

import pytest


# ------------------------------------------------------------------ CSP
class TestCsp1702:
    def test_csp_allows_max_bridge(self):
        src = open("app/security.py", encoding="utf-8").read()
        assert "st.max.ru" in src and "script-src" in src

    def test_csp_header_on_maxapp_page(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.get("/maxapp/")
            csp = r.headers.get("content-security-policy", "")
            assert "st.max.ru" in csp
            assert "script-src" in csp


# ------------------------------------------------------------------ ник бота
class TestBotNick1702:
    def test_tech_nick_not_used_in_links(self, client):
        """v1.70.6: username бота (даже служебный idNNNN_bot) — единственное
        валидное имя диплинка max.ru/<username>; ссылки ведут к боту из
        токена (прод: отображаемое имя не резолвится)."""
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_username", "id4712030034_bot")
        finally:
            s.rollback(); s.close()
        assert max_bot.bot_username(s) == "id4712030034_bot"

    def test_public_nick_used(self, client):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_username", "chek.ymaster.bot")
        finally:
            s.rollback(); s.close()
        assert max_bot.bot_username(s) == "chek.ymaster.bot"

    def test_refresh_skips_tech_nick(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "id4712030034_bot"})
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
            appsettings.set_setting(s, "max_bot_username", "")
            assert max_bot.refresh_username(s) == "id4712030034_bot"
            # v1.70.6: username токен-бота сохраняется — для диплинков
            assert appsettings.get_setting(
                s, "max_bot_username", "") == "id4712030034_bot"
            assert max_bot.bot_username(s) == "id4712030034_bot"
        finally:
            s.rollback(); s.close()

    def test_refresh_saves_public_nick(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "chek.ymaster.bot"})
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
            appsettings.set_setting(s, "max_bot_username", "")
            max_bot.refresh_username(s)
            assert appsettings.get_setting(
                s, "max_bot_username", "") == "chek.ymaster.bot"
        finally:
            s.rollback(); s.close()

    def test_about_ignores_tech_nick(self, client):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_username", "id4712030034_bot")
        finally:
            s.rollback(); s.close()
        d = client.get("/api/v1/about").json()
        # v1.70.6: /about отдаёт username токен-бота (для диплинков фронта)
        assert d["max_bot_username"] == "id4712030034_bot"

    def test_settings_accept_bot_nick(self, client):
        h = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"})
        hh = {"Authorization": "Bearer " + h.json()["access_token"]}
        r = client.put("/api/v1/settings/max", headers=hh,
                       json={"bot_nick": "chek.ymaster.bot"})
        assert r.status_code == 200, r.text
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            assert appsettings.get_setting(
                s, "max_bot_username", "") == "chek.ymaster.bot"
        finally:
            s.rollback(); s.close()
        bad = client.put("/api/v1/settings/max", headers=hh,
                         json={"bot_nick": "плохой ник!"})
        assert bad.status_code == 422

    def test_start_link_public_nick(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        assert r.json()["max_link"].startswith(
            "https://max.ru/chek.ymaster.bot?startapp=login_")


# ------------------------------------------------------------------ воркер/мини-апп
class TestWorkerAndMiniApp1702:
    def test_settings_report_last_update(self, client):
        from app.services import max_bot
        max_bot.note_update()
        h = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"})
        hh = {"Authorization": "Bearer " + h.json()["access_token"]}
        d = client.get("/api/v1/settings/max", headers=hh).json()
        assert d["last_update_ago"] is not None and d["last_update_ago"] < 60

    def test_maxapp_waits_for_bridge(self):
        src = open("app/static/maxapp/maxapp.js", encoding="utf-8").read()
        assert "waitForBridge" in src
        # v1.70.6: фолбэк без Bridge — честная инструкция и «Повторить»
        assert "mx-retry" in src


# ------------------------------------------------------------------ версии/замки
class TestVersions1702:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.3: строгий замок версии переехал в test_v1703
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 2)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and "?v=1.70.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.70.1" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
        mapp = open("app/static/maxapp/index.html", encoding="utf-8").read()
        assert f"/maxapp/maxapp.js?v={ver}" in mapp

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.2]") == 1
        assert ch.index("## [1.70.2]") < ch.index("## [1.70.1]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.2':") < js.index("'1.70.1':")


# ------------------------------------------------------------------ замки
class TestNoRegression1702:
    def test_login_flow_alive(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_password_login_alive(self, client):
        r = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        assert "ymaster_token" in r.cookies
