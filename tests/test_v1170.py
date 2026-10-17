# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.17.0: обновление без пароля (re-exec), сброс
# кэша клиентов, информативные блоки настроек (админ/бухгалтер/сотрудник).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import os
import re

from tests.conftest import login


class TestReexecUpdate:
    def test_reexec_disabled_in_tests(self):
        """В тестах re-exec запрещён — иначе тестовый процесс заменит себя."""
        from app.services.updater import _reexec_allowed
        assert _reexec_allowed() is False

    def test_self_reexec_function_exists_and_safe(self, monkeypatch, tmp_path):
        """_self_reexec возвращает False при невозможности exec (не роняет поток)."""
        import app.services.updater as up
        # не запускаем настоящий execv: подменяем os.execv
        calls = []

        def fake_execv(path, argv):
            calls.append((path, argv))
            raise OSError("boom")

        monkeypatch.setattr(up.os, "execv", fake_execv)
        monkeypatch.setattr(up, "_reexec_allowed", lambda: True)
        ok = up._self_reexec()
        assert ok is False and len(calls) == 1

    def test_preflight_reports_reexec(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/preflight", headers=hdr)
        assert r.status_code == 200
        d = r.json()
        assert "reexec_ok" in d and d["reexec_ok"] is False   # тестовый режим
        assert d["restart_mode"] in ("", "sudoers", "systemctl", "reexec")

    def test_restart_chain_prefers_passwordless(self):
        """sudo -S с паролем — НЕ первый способ; sudoers и re-exec раньше."""
        src = open("app/services/updater.py", encoding="utf-8").read()
        i_sudo_n = src.index('["sudo", "-n", "systemctl", "restart"')
        i_reexec = src.index("if _reexec_allowed():", i_sudo_n)
        i_pw = src.index('["sudo", "-S", "-p", ""', i_reexec)
        assert i_reexec < i_pw                # re-exec раньше пароля
        assert "СЛУЖЕБНОГО" in src            # объяснение в докстринге


class TestCacheReset:
    def test_frontend_has_hard_reset_and_watchdog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "async function hardReset" in js
        assert "caches.delete(k)" in js
        assert "server_update" in js and "hardReset" in js
        # сторож версии: интервал + visibilitychange
        assert js.count("api.get('/api/v1/about', { retries: 1 })") >= 2
        assert "visibilitychange" in js
        assert "btn-cache-reset" in js

    def test_sw_cleans_old_caches(self):
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "caches.delete" in sw          # активация чистит старые кэши


class TestAdminBlocks:
    def test_system_info_extended(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/system", headers=hdr)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("python", "uptime_s", "service_user", "unit",
                  "sudoers_cmd", "reexec", "counts"):
            assert k in d, k
        assert "sudo tee /etc/sudoers.d/" in d["sudoers_cmd"]
        assert "NOPASSWD" in d["sudoers_cmd"]
        assert "companies" in d["counts"]

    def test_ui_commands_block(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Сервер и команды" in js
        assert "copyCmd" in js and "cmd-copy" in js
        assert "sudoers_cmd" in js
        # предзагрузка system в viewSettings
        assert "api.get('/api/v1/admin/system')" in js


class TestRoleBlocks:
    def test_app_summary_for_all_roles(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": "ООО Блоки 1170"}, headers=hdr).json()
        # бухгалтер
        inv = client.post("/api/v1/invites", json={
            "role": "accountant", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "blocks_acc",
            "password": "parol123", "full_name": "Бэла Блоковна"})
        ha = {"Authorization": "Bearer " + reg.json()["access_token"]}
        r = client.get("/api/v1/settings/app/summary", headers=ha)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["advance_deadline_days"] >= 1
        assert "auto_verify" in d and "version" in d
        # сотрудник
        inv2 = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=hdr).json()
        reg2 = client.post("/api/v1/auth/register", json={
            "token": inv2["token"], "username": "blocks_user",
            "password": "parol123", "full_name": "Богдан Блоков"})
        hu = {"Authorization": "Bearer " + reg2.json()["access_token"]}
        assert client.get("/api/v1/settings/app/summary", headers=hu).status_code == 200
        # /me отдаёт company_inn бухгалтеру
        me = client.get("/api/v1/auth/me", headers=ha).json()
        assert "company_inn" in me

    def test_ui_role_blocks(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Ваша компания" in js and "Мои чеки" in js
        assert "Обслуживание устройства" in js
        assert "app/summary" in js and "company_inn" in js


class TestVersion1170:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        from app.config import settings
        assert settings.APP_VERSION == ver
        assert settings.SELF_REEXEC is True

    def test_css_responsive(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".cmd-line" in css
        # мобильная перестройка командной строки
        assert "flex-direction: column" in css
