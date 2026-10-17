# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.14 «Автопилот AdGuard»:
#  • ping_proxy: 3 запроса, среднее, сохранение, режим «путь мёртв»;
#  • _ping_due: окно 15:00–15:20 МСК, раз в сутки;
#  • маршрут /settings/telegram/ping и поля пинга в GET-настройках;
#  • UI-кнопка «📏 Пинг»; комплект автопилота (скрипт/таймер/юнит).
# ======================================================================
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import telegram_bot as tg


class TestVersions17014:
    def test_version(self):
        # v1.70.15: пин ослаблен до минимума (точную версию проверяет свой тест)
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 70, 14)

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.14]") == 1
        assert ch.index("## [1.70.14]") < ch.index("## [1.70.13]")

    def test_whatsnew(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.14':") < js.index("'1.70.13':")

    def test_consts(self):
        assert tg.PING_TIME == (15, 0)
        assert tg.SETT_PING_MS == "telegram_ping_ms"


class TestPingProxy:
    def _ok_resp(self):
        class R:
            status_code = 200
        return R()

    def test_three_requests_avg_saved(self, monkeypatch):
        from app.database import SessionLocal
        calls = {"n": 0}

        def fake_get(url, timeout=None, proxy=None):
            calls["n"] += 1
            return self._ok_resp()

        monkeypatch.setattr(tg.httpx, "get", fake_get)
        monkeypatch.setattr(tg.time, "sleep", lambda s: None)
        db = SessionLocal()
        try:
            r = tg.ping_proxy(db)
            assert calls["n"] == 3
            assert r["ok"] is True and r["samples"] == 3
            assert isinstance(r["ms"], int) and r["ms"] >= 0
            assert tg._ping_ms_or_none(db) == r["ms"]
            from app.services import appsettings
            assert appsettings.get_setting(db, tg.SETT_PING_OK, "") == "1"
        finally:
            db.close()

    def test_dead_path(self, monkeypatch):
        from app.database import SessionLocal

        def fake_get(url, timeout=None, proxy=None):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(tg.httpx, "get", fake_get)
        monkeypatch.setattr(tg.time, "sleep", lambda s: None)
        db = SessionLocal()
        try:
            r = tg.ping_proxy(db)
            assert r["ok"] is False and r["ms"] is None
            from app.services import appsettings
            assert appsettings.get_setting(db, tg.SETT_PING_OK, "") == "0"
            assert tg._ping_ms_or_none(db) is None
        finally:
            db.close()

    def test_scheduled_marks_day(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        monkeypatch.setattr(tg.httpx, "get", lambda u, timeout=None, proxy=None: self._ok_resp())
        monkeypatch.setattr(tg.time, "sleep", lambda s: None)
        db = SessionLocal()
        try:
            tg.ping_proxy(db, scheduled=True)
            assert appsettings.get_setting(db, "tg_last_ping", "") == \
                datetime.now().strftime("%Y-%m-%d")
            appsettings.set_setting(db, "tg_last_ping", "")
        finally:
            db.close()

    def test_worker_hook_present(self):
        src = open("app/services/telegram_bot.py", encoding="utf-8").read()
        assert "ping_proxy(db, scheduled=True)" in src


class TestPingDue:
    def test_in_window_due(self):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            now = datetime(2026, 10, 17, 15, 5)
            assert tg._ping_due(db, now=now) is True
        finally:
            db.close()

    def test_outside_window(self):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            assert tg._ping_due(db, now=datetime(2026, 10, 17, 14, 0)) is False
            assert tg._ping_due(db, now=datetime(2026, 10, 17, 15, 45)) is False
        finally:
            db.close()

    def test_already_done_today(self):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, "tg_last_ping", "2026-10-17")
            assert tg._ping_due(db, now=datetime(2026, 10, 17, 15, 5)) is False
            appsettings.set_setting(db, "tg_last_ping", "")
        finally:
            db.close()


class TestRouteAndCard:
    def test_ping_route(self, client, monkeypatch):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(tg, "ping_proxy",
                            lambda db, samples=3, scheduled=False:
                            {"ok": True, "ms": 42, "ok_samples": 3,
                             "samples": 3, "at": "2026-10-17 15:00",
                             "proxy": True})
        r = client.post("/api/v1/settings/telegram/ping", headers=adm)
        assert r.status_code == 200
        assert r.json()["ms"] == 42 and r.json()["ok_samples"] == 3

    def test_settings_include_ping(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        d = client.get("/api/v1/settings/telegram", headers=adm).json()
        assert "ping_ms" in d and "ping_at" in d and "ping_ok" in d

    def test_ui_button_and_handler(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="tg-ping"' in js and 'id="tg-ping-out"' in js
        assert "settings/telegram/ping" in js
        assert "путь недоступен" in js


class TestAutopilotKit:
    def test_script(self):
        s = open("deploy/adguard-vpn/tg-health.sh", encoding="utf-8").read()
        assert "socks5h://127.0.0.1:1080" in s
        assert "list-locations" in s
        assert "germany|герман" in s
        assert '"${TARGET_MS:-30}"' in s or "TARGET_MS:-30" in s
        assert "connect -l" in s

    def test_timer_and_service(self):
        timer = open("deploy/adguard-vpn/tg-health.timer", encoding="utf-8").read()
        assert "OnCalendar=*-*-* 15:10:00" in timer and "Persistent=true" in timer
        svc = open("deploy/adguard-vpn/tg-health.service", encoding="utf-8").read()
        assert "Type=oneshot" in svc and "tg-health.sh" in svc

    def test_readme_section(self):
        rd_txt = open("deploy/adguard-vpn/README.md", encoding="utf-8").read()
        assert "Автопилот" in rd_txt and "15:10" in rd_txt
        assert "Германию" in rd_txt or "Германия" in rd_txt

    def test_manual_updated(self):
        src = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Автопилот" in src


class TestHarness17014:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.14: пинг пути Telegram в карточке" in h
