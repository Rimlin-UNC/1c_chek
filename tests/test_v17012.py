# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.12 «Telegram сквозь блокировки»:
#  • сменный адрес Bot API (мост/шлюз) во всех путях бота;
#  • мост deploy/tg-bridge: e2e на локальных портах (без внешней сети);
#  • настройки/UI/диагностика.
# ======================================================================
import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import telegram_bot as tg


# ---------------------------------------------------------------- версии
class TestVersions17012:
    def test_version(self):
        # v1.70.13: строгий замок версии переехал в test_v17013
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 70, 12)

    def test_env_api_base(self):
        assert settings.TELEGRAM_API_BASE == "https://api.telegram.org"

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.12]") == 1
        assert ch.index("## [1.70.12]") < ch.index("## [1.70.11]")

    def test_whatsnew(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.12':") < js.index("'1.70.11':")


# ---------------------------------------------------------------- адрес API в боте
class TestApiBase:
    def test_consts(self):
        assert tg.SETT_API_BASE == "telegram_api_base"
        assert tg.TG_API_BASE_DEFAULT == "https://api.telegram.org"

    def test_get_api_base_default(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_API_BASE, "")
            monkeypatch.setattr(settings, "TELEGRAM_API_BASE", "")
            assert tg.get_api_base(db) == tg.TG_API_BASE_DEFAULT
        finally:
            db.close()

    def test_get_api_base_setting_wins(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_API_BASE, "http://127.0.0.1:8077")
            assert tg.get_api_base(db) == "http://127.0.0.1:8077"
        finally:
            db.close()

    def test_get_api_base_env_fallback(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_API_BASE, "")
            monkeypatch.setattr(settings, "TELEGRAM_API_BASE", "http://env-gw:9")
            assert tg.get_api_base(db) == "http://env-gw:9"
        finally:
            db.close()

    def test_get_api_base_dash_resets(self):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_API_BASE, "-")
            assert tg.get_api_base(db) == tg.TG_API_BASE_DEFAULT
            appsettings.set_setting(db, tg.SETT_API_BASE, "")
        finally:
            db.close()

    def test_api_uses_base(self, monkeypatch):
        seen = {}

        def fake_post(url, json=None, timeout=None, proxy=None):
            seen["url"] = url
            seen["proxy"] = proxy

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "result": {}}
            return R()

        monkeypatch.setattr(tg.httpx, "post", fake_post)
        tg.api("TOK", "getMe", base="http://127.0.0.1:8077/")
        assert seen["url"] == "http://127.0.0.1:8077/botTOK/getMe"

    def test_api_default_official(self, monkeypatch):
        seen = {}

        def fake_post(url, json=None, timeout=None, proxy=None):
            seen["url"] = url

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "result": {}}
            return R()

        monkeypatch.setattr(tg.httpx, "post", fake_post)
        tg.api("TOK", "getMe")
        assert seen["url"] == "https://api.telegram.org/botTOK/getMe"

    def test_send_message_passes_base(self, monkeypatch):
        seen = {}

        def fake_api(token, method, payload=None, timeout=35.0, proxy="", base=""):
            seen.update(proxy=proxy, base=base, method=method)

        monkeypatch.setattr(tg, "api", fake_api)
        tg.send_message("T", 42, "привет", proxy="socks5://p", base="http://gw")
        assert seen == {"proxy": "socks5://p", "base": "http://gw",
                        "method": "sendMessage"}

    def test_poll_once_uses_base(self, monkeypatch):
        from app.database import SessionLocal
        seen = {}
        monkeypatch.setattr(tg, "get_token", lambda db: "T")
        monkeypatch.setattr(tg, "get_proxy", lambda db: "socks5://x")
        monkeypatch.setattr(tg, "get_api_base", lambda db: "http://gw:8077")

        def fake_api(token, method, payload=None, timeout=None, proxy="", base=""):
            seen.update(proxy=proxy, base=base, method=method)
            return []

        monkeypatch.setattr(tg, "api", fake_api)
        tg._poll_once([0])
        assert seen == {"proxy": "socks5://x", "base": "http://gw:8077",
                        "method": "getUpdates"}
        _ = SessionLocal  # сессия короткая внутри, наружу не нужна

    def test_probe_with_base(self, monkeypatch):
        seen = {}

        def fake_post(url, json=None, timeout=None, proxy=None):
            seen["url"] = url
            raise RuntimeError("no net")

        monkeypatch.setattr(tg.httpx, "post", fake_post)
        r = tg._probe("", base="http://gw:8077")
        assert r["ok"] is False
        assert seen["url"] == "http://gw:8077/bot0:x/getMe"

    def test_diagnose_reports_via_base(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_API_BASE, "http://gw:8077")
            monkeypatch.setattr(tg, "_probe",
                                lambda p, base="": {"ok": bool(base), "error": ""})
            d = tg.diagnose(db)
            assert d["via_base"]["ok"] is True
            assert d["verdict"] == "ok"
            assert d["api_base"] == "http://gw:8077"
            appsettings.set_setting(db, tg.SETT_API_BASE, "")
        finally:
            db.close()


# ---------------------------------------------------------------- настройки/UI
class TestSettingsAndUi:
    def test_put_api_base_save_and_clear(self, client, monkeypatch):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(tg, "get_me",
                            lambda token, proxy="", base="": {"username": "b"})
        monkeypatch.setattr(tg, "refresh_username", lambda db: "b")
        # неверная схема → 422
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"api_base": "ftp://x"})
        assert r.status_code == 422
        # сохранить мост
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"api_base": "http://127.0.0.1:8077/"})
        assert r.status_code == 200
        d = client.get("/api/v1/settings/telegram", headers=adm).json()
        assert d["api_base"] == "http://127.0.0.1:8077"
        # «-» возвращает стандартный
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"api_base": "-"})
        assert r.status_code == 200
        d = client.get("/api/v1/settings/telegram", headers=adm).json()
        assert d["api_base"] == ""

    def test_ui_has_apibase_field(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="tg-apibase"' in js
        assert "body.api_base = apb" in js
        assert "через адрес API" in js          # диагностика: третья проба

    def test_manual_section(self):
        src = open("app/services/manual_content.py", encoding="utf-8").read()
        assert '"id": "telegram-bridge"' in src
        assert '"/img/manual/tggateway.jpg"' in src

    def test_manual_image_served(self):
        with TestClient(app) as c:
            r = c.get("/img/manual/tggateway.jpg")
            assert r.status_code == 200
            assert r.content[:2] == b"\xff\xd8"


# ---------------------------------------------------------------- мост: e2e на локальных портах
class _Upstream(BaseHTTPRequestHandler):
    """Мини-двойник api.telegram.org (в отдельном потоке, без сети)."""

    def log_message(self, *a):
        pass

    def _answer(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/bot0:x/getMe"):
            self._answer(200, {"ok": True, "result": {"username": "up_bot"}})
        else:
            self._answer(404, {"ok": False, "description": "not found"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(ln)
        if self.path.startswith("/bot0:x/sendMessage"):
            self._answer(200, {"ok": True, "result": {"message_id": 7}})
        else:
            self._answer(404, {"ok": False, "description": "not found"})


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "tg_bridge_mod", "deploy/tg-bridge/tg_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="class")
def bridge_env():
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    mod = _load_bridge()
    mod.UPSTREAM = f"http://127.0.0.1:{up.server_address[1]}"   # подменяем «Телеграм»
    br = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=br.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{br.server_address[1]}"
    yield base
    br.shutdown()
    up.shutdown()


class TestBridgeE2E:
    def test_health(self, bridge_env):
        r = httpx.get(bridge_env + "/health", timeout=10)
        assert r.status_code == 200 and r.json() == {"ok": True}

    def test_get_me_relayed(self, bridge_env):
        r = httpx.get(bridge_env + "/bot0:x/getMe", timeout=10)
        assert r.status_code == 200
        assert r.json()["result"]["username"] == "up_bot"

    def test_send_message_relayed(self, bridge_env):
        r = httpx.post(bridge_env + "/bot0:x/sendMessage",
                       json={"chat_id": 1, "text": "привет"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["result"]["message_id"] == 7

    def test_whitelist_blocks_foreign_host(self, bridge_env):
        r = httpx.get(bridge_env + "/proxy/evil.example.com/x", timeout=10)
        assert r.status_code == 403

    def test_only_bot_paths(self, bridge_env):
        r = httpx.get(bridge_env + "/other/path", timeout=10)
        assert r.status_code == 404

    def test_deploy_files_complete(self):
        src = open("deploy/tg-bridge/tg_bridge.py", encoding="utf-8").read()
        assert "ALLOWED_HOSTS" in src and "api.telegram.org" in src
        unit = open("deploy/tg-bridge/tg-bridge.service", encoding="utf-8").read()
        assert "Restart=always" in unit and "tg_bridge.py" in unit
        rd = open("deploy/tg-bridge/README.md", encoding="utf-8").read()
        assert "Адрес API" in rd and "SSH" in rd


# ---------------------------------------------------------------- харнесс
class TestHarness17012:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.12: Telegram — сменный адрес API (мост/шлюз)" in h
