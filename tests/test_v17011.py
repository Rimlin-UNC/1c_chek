# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.11 «Закалка»:
#  • Secure-флаг cookie сессии только по HTTPS (схема / X-Forwarded-Proto);
#  • CORS без wildcard-дефолта;
#  • SPA-fallback: явный запрет «..» в пути.
# ======================================================================
import asyncio

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, spa


class TestVersions17011:
    def test_version(self):
        # v1.70.12: строгий замок версии переехал в test_v17012
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 70, 11)

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.11]") == 1
        assert ch.index("## [1.70.11]") < ch.index("## [1.70.10]")

    def test_whatsnew(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.11':") < js.index("'1.70.10':")


class TestCookieSecure:
    def _login(self, base_url, headers=None):
        with TestClient(app, base_url=base_url) as c:
            r = c.post("/api/v1/auth/login",
                       json={"username": "admin", "password": "admin123"},
                       headers=headers or {})
        return r

    def test_secure_on_https(self):
        r = self._login("https://testserver")
        assert r.status_code == 200, r.text
        sc = "; ".join(r.headers.get_list("set-cookie")).lower()
        assert "secure" in sc

    def test_no_secure_on_http(self):
        r = self._login("http://testserver")
        assert r.status_code == 200
        sc = "; ".join(r.headers.get_list("set-cookie")).lower()
        assert "secure" not in sc          # http-превью/локальная сеть работают

    def test_secure_by_forwarded_proto(self):
        r = self._login("http://testserver", {"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        sc = "; ".join(r.headers.get_list("set-cookie")).lower()
        assert "secure" in sc

    def test_httponly_and_samesite_kept(self):
        r = self._login("https://testserver")
        sc = "; ".join(r.headers.get_list("set-cookie")).lower()
        assert "httponly" in sc and "samesite=lax" in sc


class TestCorsNoWildcard:
    def test_default_no_origins(self):
        assert settings.CORS_ORIGINS == []     # env в тестах не задаёт CORS_ORIGINS

    def test_no_wildcard_in_source(self):
        src = open("app/main.py", encoding="utf-8").read()
        assert 'or ["*"]' not in src
        assert "allow_origins=settings.CORS_ORIGINS," in src


class TestSpaTraversalGuard:
    def test_dotdot_rejected(self):
        resp = asyncio.run(spa("../.env"))     # guard возвращает 404-ответ
        assert resp.status_code == 404

    def test_dotdot_encoded_rejected(self):
        resp = asyncio.run(spa("app/..%2fconfig.py"))
        assert resp.status_code == 404

    def test_normal_path_still_works(self):
        resp = asyncio.run(spa("dashboard"))
        assert resp.status_code == 200
