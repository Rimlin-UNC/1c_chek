# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.8.2: QR-код приглашения (PNG), права, фикс
# api.download (GET без тела). ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


class TestInviteQr:
    def _invite(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "user", "expires_hours": 24,
                                "max_uses": 3, "note": "QR тест"}).json()
        return hdr, inv

    def test_qr_png(self, client):
        hdr, inv = self._invite(client)
        r = client.get(f"/api/v1/invites/{inv['id']}/qr", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("image/png")
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"          # PNG-магия
        assert len(r.content) > 500
        url = r.headers.get("x-invite-url", "")
        assert "/#/register/" in url and inv["token"] in url

    def test_qr_requires_admin(self, client):
        hdr, inv = self._invite(client)
        client.cookies.clear()
        assert client.get(f"/api/v1/invites/{inv['id']}/qr").status_code in (401, 403)

    def test_qr_user_role_forbidden(self, client):
        hdr, inv = self._invite(client)
        emp = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "user"}).json()
        client.post("/api/v1/auth/register",
                    json={"token": emp["token"], "username": "qruser",
                          "password": "secret2026", "full_name": "Юзер"})
        uhdr = login(client, "qruser", "secret2026")
        r = client.get(f"/api/v1/invites/{inv['id']}/qr", headers=uhdr)
        assert r.status_code in (401, 403)

    def test_qr_revoked(self, client):
        hdr, inv = self._invite(client)
        client.post(f"/api/v1/invites/{inv['id']}/revoke", json={}, headers=hdr)
        r = client.get(f"/api/v1/invites/{inv['id']}/qr", headers=hdr)
        assert r.status_code == 400
        assert "отозвано" in r.json()["detail"]

    def test_qr_unknown(self, client):
        hdr = login(client, "admin", "admin123")
        assert client.get("/api/v1/invites/no-such-id/qr",
                          headers=hdr).status_code == 404


class TestApiDownloadGet:
    def test_download_uses_get_without_body(self):
        js = open("app/static/js/api.js", encoding="utf-8").read()
        assert "b === undefined" in js and "method: 'GET'" in js
        # старое безусловное POST не должно остаться
        assert "method: 'POST', headers, body: JSON.stringify(b || {})" not in js

    def test_ui_has_qr_button(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "openInviteQr" in js and "iv-qr" in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".invite-qr-box" in css

class TestPyBinV18_3:
    def test_py_bin_prefers_venv(self, tmp_path, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "APP_DIR", str(tmp_path))
        assert up._py_bin() == "python3"                 # venv нет → системный
        vbin = tmp_path / "venv" / "bin"
        vbin.mkdir(parents=True)
        (vbin / "python3").write_text("#!/bin/sh\n")
        assert up._py_bin() == str(vbin / "python3")     # venv есть → его python

    def test_health_check_uses_py_bin(self, monkeypatch):
        import app.services.updater as up
        seen = []
        monkeypatch.setattr(up, "_run",
                            lambda cmd, **kw: (seen.append(cmd), (0, "1.8.3"))[1])
        ok_flag, msg = up._health_check()
        assert ok_flag is True
        assert seen[0][0] == up._py_bin()

class TestSelfUpdateV19:
    """v1.9.1: механизм «админ в приложении = суперпользователь»:
    предпроверка готовности, отказ без права на перезапуск,
    уведомление всех пользователей, свежий код у всех."""

    def test_restart_mode_sudoers(self, monkeypatch):
        import app.services.updater as up
        seq = [(0, "active")]
        monkeypatch.setattr(up, "_run",
                            lambda cmd, **kw: (seq.pop(0) if seq else (1, "")))
        ok_flag, mode = up._restart_mode()
        assert ok_flag and mode == "sudoers"

    def test_restart_mode_none(self, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "_run", lambda cmd, **kw: (1, "nope"))
        ok_flag, mode = up._restart_mode()
        assert not ok_flag and mode == ""

    def test_apply_refuses_without_restart_right(self, client, monkeypatch):
        """Без sudoers обновление не начинается — никакого полу-состояния."""
        import app.services.updater as up
        monkeypatch.setattr(up, "pre_flight", lambda db: {
            "can_restart": False, "restart_mode": "", "db_backup_ok": True,
            "github_ok": True, "github_error": "", "ready": False})
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/update/apply", headers=hdr)
        assert r.status_code == 409
        assert "deploy.sh" in r.json()["detail"]

    def test_apply_refuses_without_github(self, client, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "pre_flight", lambda db: {
            "can_restart": True, "restart_mode": "sudoers", "db_backup_ok": True,
            "github_ok": False, "github_error": "blocked", "ready": False})
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/update/apply", headers=hdr)
        assert r.status_code == 502

    def test_preflight_endpoint(self, client, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "pre_flight", lambda db: {
            "can_restart": True, "restart_mode": "sudoers", "db_backup_ok": True,
            "github_ok": True, "github_error": "", "ready": True})
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/preflight", headers=hdr)
        assert r.status_code == 200 and r.json()["ready"] is True

    def test_broadcast_before_restart_in_source(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        assert 'broadcast("server_update"' in src

    def test_ui_handles_server_update_event(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "case 'server_update':" in js

    def test_app_code_always_fresh(self, client):
        """Код (js/css) — no-cache: после рестарта все пользователи сразу
        получают новую версию, а не кэш семидневной давности."""
        r = client.get("/js/app.js")   # v1.10.0: реальный URL из index.html
        assert "no-cache" in r.headers.get("cache-control", "")
        r2 = client.get("/static/js/app.js")
        assert "no-cache" in r2.headers.get("cache-control", "")
        i = client.get("/img/logo.svg")
        assert "max-age=604800" in i.headers.get("cache-control", "")
