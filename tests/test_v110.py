# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.10.0: меню, адаптив/поворот, фискальный QR чека,
# авансовый отчёт, Windows-установка, пароль сервера (sudo -S), зависимости.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login

QR = ("t=20260927T1529&s=2150.00&fn=7381440700130934"
      "&i=33079&fp=3673437411&n=1")


class TestMenuAndResponsive:
    def test_menu_close_ways(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "closeSidebar" in js and "sidebar-backdrop" in js
        assert "Escape" in js                       # клавиша Esc
        h = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="sidebar-backdrop"' in h and 'id="sb-close"' in h

    def test_orientation_media(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "orientation: landscape" in css
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert '"orientation"' not in mf            # поворот экрана разрешён


class TestFiscalQrAndAO1:
    def test_receipt_qr_png(self, client):
        hdr = login(client, "admin", "admin123")
        rid = client.post("/api/v1/receipts/scan",
                          json={"qr_data": QR, "source": "manual"},
                          headers=hdr).json()["receipt"]["id"]
        r = client.get(f"/api/v1/receipts/{rid}/qr.png", headers=hdr)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_ui_has_ao1_and_qr_in_print(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "openAO1Modal" in js and "btn-ao1" in js
        assert "АВАНСОВЫЙ ОТЧЁТ" in js
        assert "qrUrls" in js and "/qr.png" in js   # QR в печатных чеках
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".ao-page" in css and ".ao-table" in css


class TestWindowsPwa:
    def test_windows_branch_and_shortcuts(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "isWindows" in js and "Установить приложение" in js
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert "shortcuts" in mf and "display_override" in mf


class TestSudoPassword:
    def test_run_passes_stdin(self, monkeypatch):
        import subprocess
        import app.services.updater as up
        seen = {}
        def fake_run(cmd, cwd=None, timeout=180, input_text=None):
            seen["cmd"], seen["input"] = cmd, input_text
            return 0, ""
        monkeypatch.setattr(up.subprocess, "run",
                            lambda **kw: seen.update(kw) or type("P", (), {
                                "returncode": 0, "stdout": "", "stderr": ""})())
        rc, _ = up._run(["sudo", "-S", "systemctl", "restart", "x"],
                        input_text="secret\n")
        assert rc == 0 and seen["input"] == "secret\n"

    def test_restart_chain_sudo_s(self, monkeypatch):
        import app.services.updater as up
        calls = []
        def fake_run(cmd, **kw):
            calls.append((cmd[0:2], kw.get("input_text")))
            if cmd[1] == "-S":
                return 0, ""
            return 1, ""
        monkeypatch.setattr(up, "_run", fake_run)
        ok_flag, msg = up._restart_service("пароль123")
        assert ok_flag is True
        assert calls[0][0] == ["sudo", "-n"]           # сначала sudoers
        assert calls[1][0] == ["sudo", "-S"]           # затем по паролю
        assert calls[1][1] == "пароль123\n"
        assert "пароль" not in msg                     # пароль не светится в сообщениях

    def test_password_never_in_apply_logs(self, client, monkeypatch):
        """Пароль не сохраняется: в job-логе/файле маркера его быть не должно."""
        import app.services.updater as up
        captured = {}
        monkeypatch.setattr(up, "_restart_service",
                            lambda pw: captured.update(pw=pw) or (True, "ok"))
        monkeypatch.setattr(up, "write_last_update", lambda payload: captured.update(
            marker=str(payload)))
        ok_flag, msg = up._restart_service("секрет")
        up.write_last_update({"to": "1.10.0"})
        assert captured["pw"] == "секрет"              # дошел только в память вызова
        assert "секрет" not in captured["marker"]      # в маркер не попал

    def test_apply_without_restart_right_needs_password(self, client, monkeypatch):
        import app.services.updater as up
        monkeypatch.setattr(up, "pre_flight", lambda db: {
            "can_restart": False, "restart_mode": "", "db_backup_ok": True,
            "github_ok": True, "github_error": "", "ready": False})
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/update/apply", headers=hdr)
        assert r.status_code == 409                    # без пароля — отказ
        assert "пароль" in r.json()["detail"].lower()


class TestDependencyGuard:
    def test_requirements_complete(self):
        req = open("requirements.txt", encoding="utf-8").read().lower()
        for pkg in ("qrcode", "pillow", "fastapi", "uvicorn", "sqlalchemy",
                    "httpx", "pyjwt", "python-multipart",
                    "opencv-python-headless", "numpy"):
            assert pkg in req, f"нет {pkg}"

    def test_deploy_pip_check(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert "pip check" in d
