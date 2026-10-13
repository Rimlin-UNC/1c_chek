# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.8: SSL к MAX Bot API через сертификат
# Минцифры (Russian Trusted CA) + запасной путь без проверки.
# ======================================================================
import re
import ssl

import httpx
import pytest


class TestCaBundle1708:
    def test_cert_shipped_and_parses(self):
        import os
        p = os.path.join("app", "certs", "russian_trusted_ca.pem")
        data = open(p, "rb").read()
        assert b"BEGIN CERTIFICATE" in data
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=p)          # валидный PEM
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_max_ssl_ctx_loads_ru_root(self):
        from app.services import max_bot
        ctx = max_bot._max_ssl()
        assert isinstance(ctx, ssl.SSLContext)
        subj = None
        for cert, _ in ctx.get_ca_certs() and []:
            pass
        # корневой сертификат Минцифры реально загружен
        import base64
        ok = False
        der = None
        import subprocess
        # извлекаем subject из файла штатным ssl-механизмом нельзя —
        # проверяем количество доверенных корней выросло относительно базы
        base = ssl.create_default_context()
        assert len(ctx.get_ca_certs()) > len(base.get_ca_certs())


class TestApiSsl1708:
    def test_uses_ssl_context(self, monkeypatch):
        from app.services import max_bot
        seen = {}
        fake = httpx.Response(200, json={"ok": True})

        def fake_get(url, **kw):
            seen["verify"] = kw.get("verify")
            return fake

        monkeypatch.setattr(max_bot.httpx, "get", fake_get)
        monkeypatch.setattr(max_bot.httpx, "post", fake_get)
        max_bot.api("tok", "me")
        assert isinstance(seen["verify"], ssl.SSLContext)

    def test_fallback_without_verify(self, monkeypatch):
        from app.services import max_bot
        calls = []

        def fake_get(url, **kw):
            calls.append(kw.get("verify"))
            if len(calls) == 1:
                raise httpx.ConnectError(
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
                    "failed: unable to get local issuer certificate")
            return httpx.Response(200, json={"username": "id1_bot"})

        monkeypatch.setattr(max_bot.httpx, "get", fake_get)
        d = max_bot.api("tok", "me")
        assert d["username"] == "id1_bot"
        assert calls[0] is not False and calls[1] is False

    def test_other_errors_raise_maxerror(self, monkeypatch):
        from app.services import max_bot

        def fake_get(url, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(max_bot.httpx, "get", fake_get)
        with pytest.raises(max_bot.MaxError):
            max_bot.api("tok", "me")


class TestVersions1708:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.70.8"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and "?v=1.70.7" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.70.7" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.8]") == 1
        assert ch.index("## [1.70.8]") < ch.index("## [1.70.7]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.8':") < js.index("'1.70.7':")


class TestNoRegression1708:
    def test_login_flow_alive(self, client):
        from app.database import SessionLocal
        from app.services import appsettings, max_login
        max_login._ip_times.clear()
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
        finally:
            s.rollback(); s.close()
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_worker_loop_alive(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_token", lambda db: "tok")
        monkeypatch.setattr(max_bot, "refresh_username", lambda db: "")
        monkeypatch.setattr(
            max_bot, "api",
            lambda token, method, params=None, payload=None, timeout=30:
            {"marker": None, "updates": []})
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_enabled", "1")
            appsettings.set_setting(s, "assistants_channel", "max")
            assert max_bot.start_worker() in (True, False)  # не падает
        finally:
            s.rollback(); s.close()
            max_bot.stop_worker()
