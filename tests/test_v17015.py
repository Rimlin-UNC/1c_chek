# ==============================================================================
#  Ямастер Чек — тесты v1.70.15 «AdGuard VPN: автоустановка из программы»
#  Мастер в карточке Telegram: CLI -> ссылка входа -> SOCKS5 -> службы ->
#  прокси -> контрольный замер. Механика — как у механизма обновления.
#  Сетевых вызовов нет: subprocess и логин подменяются.
# ==============================================================================
import os
import sys
import time

import pytest

sys.path.insert(0, os.getcwd())

from fastapi import status
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import adguard_setup as ag
from app.services import telegram_bot as tg
from tests.test_v1121 import login


class TestVersions17015:
    def test_version(self):
        # v1.70.16: пин ослаблен (точную версию проверяет свой тест)
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) \
            >= (1, 70, 15)

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.15]") == 1
        assert ch.index("## [1.70.15]") < ch.index("## [1.70.14]")

    def test_whatsnew(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.70.15':" in js
        assert js.index("'1.70.15':") < js.index("'1.70.14':")

    def test_versions_everywhere(self):
        # v1.70.16: проверяем синхронность точек с текущей версией из config
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert settings.APP_VERSION in open(f, encoding="utf-8").read(), f


class TestLoginUrl:
    def test_official_link_accepted(self):
        line = ("You need to authorize in your browser. The following link "
                "will be available for 1799 seconds: "
                "https://auth.adguard.io/device_code?user_code=ABCD-1234")
        assert ag.extract_login_url(line) == \
            "https://auth.adguard.io/device_code?user_code=ABCD-1234"

    def test_foreign_link_rejected(self):
        # защита от подмены домена (issue #188: CLI мог вернуть чужой домен)
        assert ag.extract_login_url(
            "open https://auctionassociation.space/device_code?user_code=X"
        ) == ""
        assert ag.extract_login_url("no link here") == ""


@pytest.fixture()
def fresh_job(tmp_path, monkeypatch):
    """Чистое задание И изолированный файл состояния (v1.70.20).

    Без изоляции `_state_path` тесты писали в общий `data/adguard_setup.json`,
    а `setup_status()` при не-живом задании читает состояние С ДИСКА — отдавал
    результат чужого теста (падение «плавало» между воркерами xdist).
    """
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield ag.job
    ag.job.reset()


@pytest.fixture(autouse=True)
def _restore_proxy_setting():
    """v1.70.16: прогон мастера пишет telegram_proxy в общую тестовую БД —
    возвращаем как было (иначе тесты прокси v1.29.0 читают чужое значение)."""
    from app.database import SessionLocal
    from app.services import appsettings

    def _current():
        db = SessionLocal()
        try:
            return appsettings.get_setting(db, ag.SETT_PROXY, "")
        finally:
            db.close()

    before = _current()
    yield
    if _current() != before:
        db = SessionLocal()
        try:
            appsettings.set_setting(db, ag.SETT_PROXY, before)
        finally:
            db.close()


@pytest.fixture()
def kit_dir(tmp_path):
    d = tmp_path / "kit"
    d.mkdir()
    for n in ag.UNIT_MAP:
        (d / n).write_text("# unit stub\n", encoding="utf-8")
    return d


def _patch_dests(monkeypatch, tmp_path):
    """Юниты «ставим» во временные каталоги — не трогаем реальную систему."""
    dests = {}
    for key, name in (("etc", "etc"), ("sbin", "sbin")):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        dests[key] = str(d)
    monkeypatch.setattr(ag, "DEST_DIRS", dests)


def _patch_happy(monkeypatch, kit_dir, calls, tmp_path=None):
    """Полный happy-path без сети: root, CLI есть, логин ок, всё rc=0."""
    monkeypatch.setattr(ag, "DEPLOY_DIR", str(kit_dir))
    if tmp_path is not None:
        _patch_dests(monkeypatch, tmp_path)
    monkeypatch.setattr(ag, "_find_cli",
                        lambda: "/usr/local/bin/adguardvpn-cli")
    monkeypatch.setattr(ag.os, "geteuid", lambda: 0)

    def fake_run(cmd, timeout=ag.CMD_TIMEOUT, shell=False):
        calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return 0, "ok"

    monkeypatch.setattr(ag, "run_cmd", fake_run)
    # v1.70.24: шаг режима (TUN + selective) унифицирован — он всегда идёт
    # через помощника, во всех режимах прав (root — `bash <сценарий> <фаза>`,
    # nopass/helper — `sudo -n`). Раньше в root/nopass команда `set-mode`
    # выполнялась напрямую через run_cmd, поэтому фикстура его и не ставила.
    monkeypatch.setattr(ag, "run_helper",
                        lambda phase, timeout=ag.CMD_TIMEOUT, stream=False:
                        (calls.append("helper:" + phase) or (0, "ok\nготово")))
    monkeypatch.setattr(ag, "_login_worker",
                        lambda cli: (0, "Successfully logged in", ""))
    monkeypatch.setattr(tg, "ping_proxy",
                        lambda db, samples=3, scheduled=False:
                        {"ok": True, "ms": 42, "ok_samples": 3,
                         "samples": 3, "at": "2026-10-17 15:00",
                         "proxy": True})


class TestSetupService:
    def test_success_path(self, monkeypatch, fresh_job, kit_dir, tmp_path):
        from app.database import SessionLocal
        from app.services import appsettings
        calls: list = []
        _patch_happy(monkeypatch, kit_dir, calls, tmp_path)
        db = SessionLocal()
        try:
            ag._run_steps(db)
            assert fresh_job.finished and fresh_job.success
            assert fresh_job.error == ""
            assert fresh_job.ping_ms == 42
            statuses = {s: fresh_job.steps[s]["status"] for s in ag.STEP_ORDER}
            assert all(v in ("done", "skip") for v in statuses.values()), \
                statuses
            assert appsettings.get_setting(db, ag.SETT_PROXY, "") == \
                ag.PROXY_VALUE
            # systemd-юниты ставились: daemon-reload был
            assert any("daemon-reload" in c for c in calls)
            # лог есть, последняя строка — про готовность
            assert fresh_job.log and "Готово" in fresh_job.log[-1]
        finally:
            db.close()

    def test_public_shape(self, monkeypatch, fresh_job, kit_dir, tmp_path):
        from app.database import SessionLocal
        calls: list = []
        _patch_happy(monkeypatch, kit_dir, calls, tmp_path)
        db = SessionLocal()
        try:
            ag._run_steps(db)
            pub = ag.job.public()
            for key in ("running", "finished", "success", "error", "step",
                        "progress", "steps", "log", "login_url", "ping_ms",
                        "elapsed_s"):
                assert key in pub, key
            assert pub["progress"] == 100
            assert len(pub["steps"]) == len(ag.STEP_ORDER)
            assert all("title" in s and "status" in s for s in pub["steps"])
        finally:
            db.close()

    def test_cli_already_installed_skips_installer(self, monkeypatch,
                                                   fresh_job):
        calls: list = []
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)

        def fake_run(cmd, timeout=ag.CMD_TIMEOUT, shell=False):
            calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
            return 0, "adguardvpn-cli version 1.7.12"

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        monkeypatch.setattr(ag, "_find_cli",
                            lambda: "/usr/local/bin/adguardvpn-cli")
        ag._step_cli()
        assert fresh_job.steps["cli"]["status"] == "skip"
        assert not any("install.sh" in c for c in calls)   # ставку не качали

    def test_installer_link_repair(self, monkeypatch, fresh_job):
        """Установщик отработал, но ссылки /usr/local/bin нет — чиним сами."""
        calls: list = []
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)
        state = {"linked": False}
        real_exists = os.path.exists

        def fake_exists(path):
            if "adguardvpn-cli" in str(path):
                return True                     # бинарник в /opt «есть»
            return real_exists(path)

        def fake_run(cmd, timeout=ag.CMD_TIMEOUT, shell=False):
            c = cmd if isinstance(cmd, str) else " ".join(cmd)
            calls.append(c)
            if "ln -sf" in c:
                state["linked"] = True
                return 0, ""
            if "--version" in c:
                return (0, "1.7.12") if state["linked"] else (1, "")
            return 0, "installed"

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        monkeypatch.setattr(ag.os.path, "exists", fake_exists)
        monkeypatch.setattr(ag, "_find_cli",
                            lambda: "/usr/local/bin/adguardvpn-cli")
        ag._step_cli()
        assert fresh_job.steps["cli"]["status"] == "done"
        assert any("ln -sf /opt/adguardvpn_cli" in c for c in calls)

    def test_login_failure_is_clear_error(self, monkeypatch, fresh_job):
        from app.database import SessionLocal
        calls: list = []
        _patch_happy(monkeypatch, None, calls)
        monkeypatch.setattr(ag, "_login_worker",
                            lambda cli: (1, "link expired", ""))
        monkeypatch.setattr(ag, "DEPLOY_DIR", "/nonexistent-kit")
        db = SessionLocal()
        try:
            monkeypatch.setattr("app.database.SessionLocal", lambda: db)
            ag._worker()
            assert fresh_job.finished and not fresh_job.success
            assert "Вход не завершён" in fresh_job.error
            assert any("failed" == s["status"]
                       for s in fresh_job.public()["steps"])
        finally:
            db.close()

    def test_verify_failure_honest_error(self, monkeypatch, fresh_job,
                                         kit_dir, tmp_path):
        from app.database import SessionLocal
        calls: list = []
        _patch_happy(monkeypatch, kit_dir, calls, tmp_path)
        monkeypatch.setattr(tg, "ping_proxy",
                            lambda db, samples=3, scheduled=False:
                            {"ok": False, "ms": None})
        db = SessionLocal()
        try:
            monkeypatch.setattr("app.database.SessionLocal", lambda: db)
            ag._worker()
            assert fresh_job.finished and not fresh_job.success
            assert "не отвечает" in fresh_job.error
            assert fresh_job.steps["verify"]["status"] == "failed"
        finally:
            db.close()

    def test_cancel(self, monkeypatch, fresh_job):
        from app.database import SessionLocal
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)

        def fake_run(cmd, timeout=ag.CMD_TIMEOUT, shell=False):
            deadline = time.time() + 5
            while not fresh_job.cancel_requested and time.time() < deadline:
                time.sleep(0.02)
            return 0, "ok"

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        monkeypatch.setattr(ag, "_find_cli",
                            lambda: "/usr/local/bin/adguardvpn-cli")
        db = SessionLocal()
        try:
            monkeypatch.setattr("app.database.SessionLocal", lambda: db)
            ag.start_setup(db)
            assert ag.cancel_setup() is True
            deadline = time.time() + 10
            while ag.job.running and time.time() < deadline:
                time.sleep(0.05)
            assert ag.job.finished and not ag.job.success
            assert "Отменено" in ag.job.error
            assert ag.cancel_setup() is False      # уже не выполняется
        finally:
            db.close()
            ag.job.reset()


class TestSetupRoutes:
    def test_status_requires_admin(self):
        # чистый клиент: без cookie-сессий других тестов и без lifespan —
        # проверяем только требование прав у маршрута
        c = TestClient(app)
        r = c.get("/api/v1/settings/telegram/adguard/setup")
        assert r.status_code in (401, 403)

    def test_status_idle_shape(self, client):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/settings/telegram/adguard/setup", headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is False
        assert "job" in body and "steps" in body["job"]
        assert "login_url" in body["job"] and "log" in body["job"]

    def test_start_conflict_and_cancel_conflict(self, client):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        ag.job.running = True                      # «установка уже идёт»
        try:
            r = client.post("/api/v1/settings/telegram/adguard/setup",
                            headers=adm, json={"action": "start"})
            assert r.status_code == status.HTTP_409_CONFLICT
            ag.job.running = False
            r = client.post("/api/v1/settings/telegram/adguard/setup",
                            headers=adm, json={"action": "cancel"})
            assert r.status_code == status.HTTP_409_CONFLICT
        finally:
            ag.job.reset()

    def test_preflight_endpoint(self, client, monkeypatch):
        """v1.70.16: пароль не запрашивается никогда — вместо него
        готовность (ready/mode) и одна команда для терминала."""
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 1})())
        monkeypatch.setattr(ag, "helper_allowed", lambda: False)
        r = client.get("/api/v1/settings/telegram/adguard/setup/preflight",
                       headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert body["need_password"] is False and "hint" in body
        assert body["ready"] is False and "install-rule" in body["one_time_cmd"]
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)
        r = client.get("/api/v1/settings/telegram/adguard/setup/preflight",
                       headers=adm)
        body = r.json()
        assert body["need_password"] is False
        assert body["ready"] is True and body["mode"] == "root"

    def test_start_ignores_sudo_password(self, client, monkeypatch):
        # v1.70.16: пароля у мастера нет — поле игнорируется (не ошибка)
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        seen = {}

        def fake_start(db):
            seen["called"] = True

        monkeypatch.setattr(ag, "start_setup", fake_start)
        r = client.post("/api/v1/settings/telegram/adguard/setup",
                        headers=adm,
                        json={"action": "start", "sudo_password": "pw1"})
        assert r.status_code == 200
        assert seen.get("called") is True
        assert r.json()["passwordless"] is True
        assert "pw1" not in r.text

    def test_start_spawns_job(self, client, monkeypatch):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag, "_worker", lambda db=None: None)
        try:
            r = client.post("/api/v1/settings/telegram/adguard/setup",
                            headers=adm, json={"action": "start"})
            assert r.status_code == 200 and r.json()["ok"] is True
            assert ag.job.running is True
            r = client.post("/api/v1/settings/telegram/adguard/setup",
                            headers=adm, json={"action": "start"})
            assert r.status_code == status.HTTP_409_CONFLICT
        finally:
            ag.job.reset()


class TestSudoRights:
    """Программа не от root: sudo -n либо помощник (v1.70.16). Режим
    «pass» (sudo -S с паролем из браузера) в v1.70.16 удалён — подробные
    проверки беспарольных режимов живут в tests/test_v17016.py."""

    def test_run_cmd_pass_mode_removed(self, monkeypatch):
        # v1.70.16: пароля у мастера нет — stdin не используется вовсе
        seen = {}

        class R:
            returncode = 0
            stdout, stderr = "ok", ""

        def fake_run(full, shell=False, capture_output=False, text=False,
                     timeout=None, input=None):
            seen["full"], seen["input"] = full, input
            return R()

        monkeypatch.setattr(ag.subprocess, "run", fake_run)
        ag.job.reset()
        assert "pass" not in ag.RIGHTS_MODES
        ag.job.sudo_mode = "nopass"
        rc, _out = ag.run_cmd(["systemctl", "daemon-reload"])
        assert rc == 0
        assert seen["full"] == ["sudo", "-n", "systemctl", "daemon-reload"]
        assert seen["input"] is None               # никакого пароля в stdin
        assert not hasattr(ag.job, "_sudo_pass")

    def test_run_cmd_nopass_mode(self, monkeypatch):
        seen = {}

        class R:
            returncode = 0
            stdout, stderr = "ok", ""

        def fake_run(full, shell=False, capture_output=False, text=False,
                     timeout=None, input=None):
            seen["full"] = full
            return R()

        monkeypatch.setattr(ag.subprocess, "run", fake_run)
        ag.job.reset()
        ag.job.sudo_mode = "nopass"
        ag.run_cmd(["cp", "a", "b"])
        assert seen["full"] == ["sudo", "-n", "cp", "a", "b"]

    def test_precheck_without_rights_shows_one_command(self, monkeypatch):
        """v1.70.16: прав нет — не «введите пароль», а одна команда для SSH."""
        class R:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout, self.stderr = "", ""

        def fake_run(full, **k):
            # systemctl есть (0), sudo -n true — нет (1)
            lst = list(full) if not isinstance(full, str) else [full]
            return R(0 if lst and lst[0] == "systemctl" else 1)

        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", fake_run)
        monkeypatch.setattr(ag, "helper_allowed", lambda: False)
        ag.job.reset()
        with pytest.raises(RuntimeError) as ei:
            ag._step_precheck()
        msg = str(ei.value)
        assert "install-rule" in msg and "Скопировать" in msg
        assert ag.job.one_time_cmd != ""
        assert "sudo -S" not in msg

    def test_precheck_helper_mode_ok(self, monkeypatch):
        calls = []

        class R:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout, self.stderr = "ok", ""

        def fake_run(full, **k):
            calls.append(list(full) if not isinstance(full, str) else full)
            return R(1 if list(full) == ["sudo", "-n", "true"] else 0)

        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", fake_run)
        monkeypatch.setattr(ag, "helper_allowed", lambda: True)
        monkeypatch.setattr(ag, "run_helper", lambda phase, timeout=0, **k:
                            (0, "ready"))
        ag.job.reset()
        ag._step_precheck()
        assert ag.job.sudo_mode == "helper"
        # пароль через stdin не передавался ни разу
        assert all("-S" not in c for c in calls)

    def test_preflight_modes(self, monkeypatch):
        class R:
            def __init__(self, rc):
                self.returncode = rc

        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)
        pf = ag.preflight()
        assert pf["root"] is True and pf["sudo_nopass"] is False
        assert pf["need_password"] is False and pf["ready"] is True
        assert pf["mode"] == "root" and pf["one_time_cmd"] == ""
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: R(1))
        monkeypatch.setattr(ag, "helper_allowed", lambda: False)
        pf = ag.preflight()
        assert pf["need_password"] is False      # v1.70.16: пароля нет вовсе
        assert pf["ready"] is False and pf["one_time_cmd"]

    def test_login_argv_modes(self, monkeypatch):
        ag.job.reset()
        assert ag._login_argv("cli") == ["cli", "login"]
        ag.job.sudo_mode = "nopass"
        assert ag._login_argv("cli") == ["sudo", "-n", "cli", "login"]
        # v1.70.16: вместо sudo -S — помощник (его фаза login)
        ag.job.sudo_mode = "helper"
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)
        assert ag._login_argv("cli") == ["sudo", "-n", ag.HELPER_PATH,
                                         "login"]
        ag.job.reset()

    def test_password_never_in_public(self):
        ag.job.reset()
        import json as _json
        blob = _json.dumps(ag.job.public(), ensure_ascii=False)
        assert "sudo_pass" not in blob and "password" not in blob
        assert not hasattr(ag.job, "_sudo_pass")   # поля пароля больше нет
        ag.job.reset()


class TestKitAndUi:
    def test_kit_files_complete(self):
        for name, (dst, mode) in ag.UNIT_MAP.items():
            path = os.path.join(ag.DEPLOY_DIR, name)
            assert os.path.exists(path), path
            if mode == 0o755:
                assert os.access(path, os.X_OK), name

    def test_health_handover_to_unit(self):
        sh = open("deploy/adguard-vpn/tg-health.sh", encoding="utf-8").read()
        assert "try-restart adguard-vpn-telegram" in sh

    def test_ui_wizard(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "/telegram/adguard/setup" in js
        assert 'id="ag-setup"' in js and 'id="ag-setup-log"' in js
        assert "Подтвердить вход в AdGuard" in js
        assert "action: 'cancel'" in js or 'action: "cancel"' in js
        # v1.70.16: вместо поля пароля — одна команда для терминала
        assert "preflight" in js
        assert "ag-cmd" in js and "ag-recheck" in js
        assert "ag-sudo-pw" not in js

    def test_readme_quick_path(self):
        rd = open("deploy/adguard-vpn/README.md", encoding="utf-8").read()
        assert "Быстрый путь" in rd
        assert "ссылку для подтверждения входа" in rd
        # ручная инструкция сохранилась (полный контроль)
        assert "config set-mode SOCKS" in rd

    def test_manual_updated(self):
        m = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "установка автоматическая" in m


class TestHarness17015:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "1.70.15" in h and "adguard/setup" in h
