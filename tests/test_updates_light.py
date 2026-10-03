# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.6.0: мультиканальная проверка обновлений,
# семвер «только вперёд», источник репозитория, лёгкий клиент (gzip/кэш).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import app.services.updater as updater
from app.config import settings
from tests.conftest import login


class TestSemver:
    def test_forward_only(self):
        assert updater._is_newer("1.6.0", "1.5.0") is True
        assert updater._is_newer("1.10.0", "1.9.0") is True      # 10 > 9 числом
        assert updater._is_newer("1.6.0", "1.6.0") is False      # равные — не обновление
        assert updater._is_newer("1.5.0", "1.6.0") is False      # назад не катимся
        assert updater._is_newer("2.0", "1.9.9") is True

    def test_parse_ver(self):
        assert updater._parse_ver("1.6.0") == (1, 6, 0)
        assert updater._parse_ver("2.0") == (2, 0)
        assert updater._parse_ver("x.y") == (0, 0)


class TestMultiSource:
    def test_fallback_api_to_raw(self, monkeypatch):
        """api.github.com упал → берём raw.githubusercontent.com."""
        class R:
            status_code = 200
            text = 'APP_VERSION: str = "9.9.9"'
            def raise_for_status(self): pass

        calls = []
        def fake_get(url, **kw):
            calls.append(url)
            if "api.github.com" in url:
                raise updater.httpx.ConnectError("blocked")
            return R()
        monkeypatch.setattr(updater.httpx, "get", fake_get)
        text, source = updater._fetch_remote_file("o/r", "b", "app/config.py")
        assert source == "raw.githubusercontent.com"
        assert "9.9.9" in text

    def test_git_channel_when_all_http_blocked(self, monkeypatch):
        """Оба HTTP-канала недоступны → git-протокол."""
        def fake_get(url, **kw):
            raise updater.httpx.ConnectError("blocked")
        monkeypatch.setattr(updater.httpx, "get", fake_get)
        monkeypatch.setattr(updater, "_run", lambda *a, **kw: (0, ""))
        monkeypatch.setattr(updater, "_run_full",
                            lambda *a, **kw: (0, 'APP_VERSION: str = "8.8.8"'))
        text, source = updater._fetch_remote_file("o/r", "b", "app/config.py")
        assert source == "git (github.com:443)"

    def test_all_channels_error_message(self, monkeypatch):
        def fake_get(url, **kw):
            raise updater.httpx.ConnectError("blocked")
        monkeypatch.setattr(updater.httpx, "get", fake_get)
        monkeypatch.setattr(updater, "_run", lambda *a, **kw: (1, "TLS EOF"))
        monkeypatch.setattr(updater, "_run_full", lambda *a, **kw: (1, "TLS EOF"))
        try:
            updater._fetch_remote_file("o/r", "b", "app/config.py")
            assert False, "должна быть ошибка"
        except RuntimeError as e:
            assert "deploy.sh --update" in str(e)
            assert "api.github.com" in str(e)


class TestCheckEndpoint:
    def test_check_ok_false_when_github_down(self, client, monkeypatch):
        hdr = login(client, "admin", "admin123")
        def boom(*a, **kw):
            raise RuntimeError("GitHub недоступен с сервера (тест)")
        monkeypatch.setattr("app.routers.admin.check_update", boom)
        r = client.get("/api/v1/admin/update/check", headers=hdr)
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "GitHub" in r.json()["error"]

    def test_check_wraps_ok(self, client, monkeypatch):
        hdr = login(client, "admin", "admin123")
        monkeypatch.setattr("app.routers.admin.check_update", lambda *a, **kw: {
            "current_version": "1.6.0", "remote_version": "1.6.0",
            "update_available": False, "branch": "b", "local_commit": "x",
            "checked_at": "now", "changelog_excerpt": "", "source": "api.github.com"})
        r = client.get("/api/v1/admin/update/check", headers=hdr)
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_update_repo_endpoint(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/admin/update/repo", headers=hdr,
                       json={"repo_branch": settings.DEFAULT_BRANCH})
        assert r.status_code == 200 and r.json()["ok"] is True
        bad = client.put("/api/v1/admin/update/repo", headers=hdr,
                         json={"repo_url": "not a repo!!"})
        assert bad.status_code == 422

    def test_system_has_repo(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/system", headers=hdr)
        assert r.status_code == 200
        assert "repo_url" in r.json() and "branch" in r.json()


class TestLightClient:
    def test_static_cached(self, client):
        # v1.9.1: код (js/css) — no-cache (всегда свежий после рестарта),
        # картинки — долгий кэш.
        # v1.10.0: код отдаётся и с корня SPA (/js/app.js, /css/app.css) —
        # именно эти URL грузит index.html; no-cache обязателен для ЛЮБОГО префикса.
        for url in ("/static/css/app.css", "/css/app.css", "/js/app.js"):
            r = client.get(url)
            assert "no-cache" in r.headers.get("cache-control", ""), url
        i = client.get("/img/logo.svg")
        assert "max-age=604800" in i.headers.get("cache-control", "")

    def test_spa_deeplink_fresh(self, client):
        # v1.10.0: оболочка SPA и deep-link'и — тоже всегда свежие
        for url in ("/", "/receipts", "/scan"):
            r = client.get(url)
            assert "no-cache" in r.headers.get("cache-control", ""), url

    def test_sw_not_cached(self, client):
        r = client.get("/sw.js")
        assert "no-cache" in r.headers.get("cache-control", "")

    def test_gzip_enabled(self):
        from app.main import app
        mids = [m.cls.__name__ for m in app.user_middleware]
        assert "GZipMiddleware" in mids


class TestRepoNormalize:
    def test_normalize_all_forms(self):
        f = updater._normalize_repo
        assert f("https://github.com/Rimlin-UNC/1c_chek.git") == "Rimlin-UNC/1c_chek"
        assert f("http://github.com/o/r/") == "o/r"
        assert f("git@github.com:o/r.git") == "o/r"
        assert f("o/r") == "o/r"
        assert f("https://gitlab.com/o/r.git") == "o/r"
        assert f("мусор") == ""
        assert f("") == ""

    def test_guess_repo_normalized(self, monkeypatch):
        monkeypatch.setattr(updater, "_git", lambda *a, **kw: (0, "https://github.com/Rimlin-UNC/1c_chek.git\n"))
        assert updater._guess_repo() == "Rimlin-UNC/1c_chek"

    def test_check_uses_normalized(self, client, monkeypatch):
        """Даже с полным URL клона в настройках запрос идёт на owner/repo."""
        seen = {}
        def fake_fetch(repo, branch, path):
            seen["repo"] = repo
            return 'APP_VERSION: str = "1.6.0"', "raw.githubusercontent.com"
        monkeypatch.setattr(updater, "_fetch_remote_file", fake_fetch)
        hdr = login(client, "admin", "admin123")
        client.put("/api/v1/admin/update/repo", headers=hdr,
                   json={"repo_url": "https://github.com/Rimlin-UNC/1c_chek.git"})
        r = client.get("/api/v1/admin/update/check", headers=hdr)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen["repo"] == "Rimlin-UNC/1c_chek"
