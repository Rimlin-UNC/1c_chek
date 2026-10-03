# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.8.1: обновление из приложения применяет себя
# (sudoers-рестарт, маркер успеха), /update/status: last_success.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


class TestRestartFromApp:
    def test_restart_prefers_sudo(self, monkeypatch):
        """На боевом сервере sudo -n systemctl (правило NOPASSWD) срабатывает."""
        import app.services.updater as up
        calls = []
        monkeypatch.setattr(up, "_run",
                            lambda cmd, **kw: (calls.append(cmd), (0, ""))[1])
        ok_flag, msg = up._restart_service()
        assert ok_flag is True
        assert calls[0][:3] == ["sudo", "-n", "systemctl"]

    def test_restart_fallback_plain(self, monkeypatch):
        """Без sudoers (dev) — обычный systemctl."""
        import app.services.updater as up
        seq = [(124, "no sudo"), (0, "ok")]
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return seq.pop(0)
        monkeypatch.setattr(up, "_run", fake_run)
        ok_flag, _ = up._restart_service()
        assert ok_flag is True
        assert calls[0][0] == "sudo" and calls[1][0] == "systemctl"

    def test_restart_failure_message(self, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "_run", lambda cmd, **kw: (1, "fail"))
        ok_flag, msg = up._restart_service()
        assert ok_flag is False and "systemctl restart" in msg

    def test_marker_roundtrip(self, tmp_path, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "APP_DIR", str(tmp_path))
        assert up.load_last_update() is None
        up.write_last_update({"from": "1.8.0", "to": "1.8.1",
                              "at": "2026-10-03T09:00:00Z", "backup": "x.db"})
        got = up.load_last_update()
        assert got["to"] == "1.8.1" and got["backup"] == "x.db"

    def test_status_has_last_success(self, client, monkeypatch):
        import app.routers.admin as admin_mod
        monkeypatch.setattr(admin_mod, "load_last_update",
                            lambda: {"from": "1.8.0", "to": "1.8.1",
                                     "at": "2026-10-03T09:00:00Z"})
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/status", headers=hdr)
        assert r.status_code == 200
        assert r.json()["last_success"]["to"] == "1.8.1"

    def test_sudoers_rule_in_deploy(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert "sudoers.d/ymaster-check" in d
        assert "NOPASSWD: /usr/bin/systemctl restart ymaster-check" in d
        assert "visudo -cf" in d                      # правило проверяется перед применением

    def test_version_bump(self):
        c = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "1.8.1"' in c
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v1.8.1" in sw

    def test_marker_success_flow_in_ui(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "last_success" in js and "upd-last" in js
