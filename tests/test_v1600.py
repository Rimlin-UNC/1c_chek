# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.60.0: единая работа со временем.
# Сервер отдаёт все моменты времени в UTC с меткой «Z» (браузер показывает
# местную зону пользователя — приложение работает из любой страны/города).
# Бизнес-времена (дата чека с кассы) остаются без «Z» намеренно.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1600:
    """Строгий версионный замок релиза v1.60.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.61.0: структурная проверка — строгий замок версии в test_v1610
        assert tuple(int(x) for x in ver.split(".")) >= (1, 60, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.6" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.60.0':") < js.index("'1.59.6':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.60.0]") == 1
        assert ch.index("## [1.60.0]") < ch.index("## [1.59.6]")

    def test_updates_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in reg["Обновления"].split(".")) >= (1, 60, 0)


class TestApiInstantsAreUtcZ:
    """API отдаёт моменты с «Z»; бизнес-времена — без."""

    def test_invite_expires_at_z(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        r = client.post("/api/v1/invites", headers=adm, json={
            "role": "user", "expires_hours": 72, "max_uses": 1})
        assert r.status_code == 200
        assert r.json()["expires_at"].endswith("Z")

    def test_me_created_at_z(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=adm).json()
        assert me["created_at"] and me["created_at"].endswith("Z")

    def test_users_created_at_z(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        users = client.get("/api/v1/users", headers=adm).json()
        assert users and users[0]["created_at"].endswith("Z")

    def test_dashboard_generated_at_z(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        d = client.get("/api/v1/dashboard/stats", headers=adm).json()
        assert d["generated_at"].endswith("Z")

    def test_receipt_instants_z_business_naive(self, client):
        """Чек: created_at — момент (Z); receipt_date — бизнес-время (без Z)."""
        from app.models import Receipt, iso_z
        src = open("app/models.py", encoding="utf-8").read()
        assert "iso_z(self.created_at)" in src
        assert "iso_z(self.fns_checked_at)" in src
        # receipt_date остаётся наивным (время по кассе)
        assert '"receipt_date": self.receipt_date.isoformat()' in src
        assert iso_z(None) is None
        from app.models import utcnow
        assert iso_z(utcnow()).endswith("Z")

    def test_pool_receipt_public_created_at_z(self, client):
        """Гостю «Чек-Пула» created_at тоже приходит с «Z»."""
        src = open("app/pool/router_public.py", encoding="utf-8").read()
        assert "iso_z(r.created_at)" in src
        # receipt_date у пула — бизнес-время, без «Z»
        assert 'r.receipt_date.isoformat() if r.receipt_date else None' in src

    def test_onec_server_time_z(self, client):
        src = open("app/routers/onec.py", encoding="utf-8").read()
        assert 'isoformat() + "Z"' in src


class TestUiLocalZone:
    """Фронт: строки журнала и «Z»-время переводятся в местную зону."""

    def test_log_line_helper(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function updLogLine(" in js
        assert ".map(updLogLine)" in js

    def test_fmt_date_uses_date_object(self):
        ui = open("app/static/js/ui.js", encoding="utf-8").read()
        assert "new Date(iso)" in ui and "toLocaleString('ru-RU'" in ui
