# ==============================================================================
#  Ямастер Чек — тесты v1.70.16 «Мастер AdGuard без пароля»
#  Пароль сервера (sudo) исключён из мастера как класс: привилегированные шаги
#  делает один помощник deploy/adguard-vpn/ymaster-adguard-setup.sh,
#  разрешённый узким sudoers-правилом на фиксированные фазы. Проверяем:
#    • версии/журнал/What’s New синхронны;
#    • ни в сервисе, ни в маршруте, ни в UI пароля больше нет;
#    • помощник: синтаксис, фазы, права файла, отсутствие eval,
#      sudoers-правило перечисляет фазы и проверяется visudo;
#    • определение прав: root | sudo -n | помощник | нет прав (→ одна команда);
#    • шаги мастера в режиме помощника идут через run_helper, а не sudo -S;
#    • deploy.sh ставит помощник и правило (блок защищён);
#    • живой прогон помощника: rights/неизвестная фаза/без root.
#  Сетевых вызовов нет: subprocess и логин подменяются.
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import inspect
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from fastapi import status
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import adguard_setup as ag
from app.services import telegram_bot as tg
from tests.test_v1121 import login

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
APP_JS = "app/static/js/app.js"


def _read(path):
    return open(path, encoding="utf-8").read()


def _adguard_panel(js: str) -> str:
    """Куски app.js про панель AdGuard: разметка + обработчики кнопок.

    Пароль окна обновления (своя механика, v1.17.0) сюда не попадает —
    проверяем только то, что относится к мастеру AdGuard."""
    parts = []
    for pat in (r'<div id="ag-setup".*?id="tg-ping"',
                r"const tga = \$\('#tg-adguard'\).*?const tgp = \$\('#tg-ping'\)"):
        m = re.search(pat, js, re.S)
        assert m, f"панель AdGuard не найдена в app.js: {pat[:40]}"
        parts.append(m.group(0))
    return "\n".join(parts)


@pytest.fixture()
def fresh_job(tmp_path, monkeypatch):
    """Чистое задание И изолированный файл состояния (v1.70.20).

    Без изоляции `_state_path` все тесты мастера писали в общий
    `data/adguard_setup.json`, а `setup_status()` при не-живом задании читает
    состояние С ДИСКА — поэтому отдавал результат чужого теста. Падение
    «плавало» между воркерами xdist в зависимости от раскладки файлов
    (test_status_endpoint_exposes_one_time_cmd видел one_time_cmd="").
    """
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield ag.job
    ag.job.reset()


@pytest.fixture(autouse=True)
def _restore_proxy_setting():
    """Полный прогон мастера пишет telegram_proxy в общую тестовую БД —
    возвращаем прежнее значение, иначе тесты прокси (v1.29.0/v1.70.14)
    видят «прокси из БД» вместо своего сценария."""
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


# ==============================================================================
#  Версии и документы
# ==============================================================================
class TestVersions17016:
    def test_version(self):
        # v1.70.17: пин ослаблен (точную версию проверяет свой тест)
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) \
            >= (1, 70, 16)

    def test_changelog(self):
        ch = _read("CHANGELOG.md")
        assert ch.count("## [1.70.16]") == 1
        assert ch.index("## [1.70.16]") < ch.index("## [1.70.15]")
        block = ch[ch.index("## [1.70.16]"):ch.index("## [1.70.15]")]
        assert "без пароля" in block.lower()
        assert "ymaster-adguard-setup" in block
        assert "/etc/sudoers.d/ymaster-adguard" in block

    def test_whatsnew(self):
        js = _read(APP_JS)
        assert "'1.70.16':" in js
        assert js.index("'1.70.16':") < js.index("'1.70.15':")

    def test_versions_everywhere(self):
        # v1.70.17: сверяем с текущей версией из config (точки синхронны)
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert settings.APP_VERSION in _read(f), f

    def test_service_header_mentions_passwordless(self):
        src = _read("app/services/adguard_setup.py")
        assert "v1.70.16" in src and "БЕЗ пароля" in src


# ==============================================================================
#  Пароля нет нигде: сервис, маршрут, UI, состояние на диске
# ==============================================================================
class TestNoPasswordAnywhere:
    def test_service_dropped_sudo_password(self):
        src = _read("app/services/adguard_setup.py")
        assert "_sudo_pass" not in src
        assert "sudo_password" not in src
        assert '"-S"' not in src, "sudo -S (пароль через stdin) удалён"
        assert "sudo -S" not in src

    def test_start_setup_signature(self):
        params = list(inspect.signature(ag.start_setup).parameters)
        assert params == ["db"], params

    def test_route_ignores_sudo_password(self, client, monkeypatch):
        """Устаревший клиент из кэша мог прислать sudo_password — игнорируем
        (не ошибка), но предупреждаем и в задание ничего не передаём."""
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        seen = {}
        monkeypatch.setattr(ag, "start_setup",
                            lambda db: seen.setdefault("called", True))
        try:
            r = client.post("/api/v1/settings/telegram/adguard/setup",
                            headers=adm,
                            json={"action": "start",
                                  "sudo_password": "secret-pw"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True and body["passwordless"] is True
            assert "secret-pw" not in json.dumps(body, ensure_ascii=False)
            assert "warning" in body and "без пароля" in body["warning"]
            assert seen.get("called") is True
        finally:
            ag.job.reset()

    def test_route_source_has_no_password(self):
        src = _read("app/routers/settings_routes.py")
        block = src[src.index("adguard/setup"):]
        assert "sudo_password=(body" not in src
        assert "ag.start_setup(db)" in block

    def test_ui_has_no_password_field(self):
        """Поле пароля убрано именно из панели AdGuard (в окне обновления
        свой пароль — его не трогаем)."""
        js = _read(APP_JS)
        assert "ag-sudo-pw" not in js
        assert "ag-sudo-row" not in js
        assert "agNeedPassword" not in js
        panel = _adguard_panel(js)
        assert "sudo_password" not in panel
        assert 'type="password"' not in panel
        assert "Пароль сервера" not in panel

    def test_ui_has_one_time_command(self):
        js = _read(APP_JS)
        for token in ('id="ag-rule-row"', 'id="ag-cmd"', 'id="ag-copy-cmd"',
                      'id="ag-recheck"', "one_time_cmd", "agNeedRule",
                      "ymaster-adguard-setup",
                      "/etc/sudoers.d/ymaster-adguard", "Проверить снова"):
            assert token in js, token

    def test_job_public_has_one_time_cmd_and_no_secrets(self, fresh_job):
        pub = fresh_job.public()
        assert "one_time_cmd" in pub and "sudo_mode" in pub
        blob = json.dumps(pub, ensure_ascii=False)
        for bad in ("sudo_pass", "password"):
            assert bad not in blob, bad

    def test_state_file_never_gets_password(self, fresh_job, monkeypatch,
                                            tmp_path):
        monkeypatch.setattr(ag, "_state_path",
                            lambda: str(tmp_path / "adguard_setup.json"))
        ag._state_write()
        blob = (tmp_path / "adguard_setup.json").read_text(encoding="utf-8")
        assert "one_time_cmd" in blob and "password" not in blob


# ==============================================================================
#  Помощник: сценарий, фазы, sudoers-правило
# ==============================================================================
class TestHelperScript:
    def test_exists_and_executable(self):
        assert os.path.exists(HELPER_SH), HELPER_SH
        assert os.access(HELPER_SH, os.X_OK), "помощник обязан быть +x"

    def test_shebang_and_strict_mode(self):
        sh = _read(HELPER_SH)
        assert sh.startswith("#!/usr/bin/env bash")
        assert "set -u" in sh

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, (r.stdout + r.stderr)[:2000]

    def test_no_eval_and_no_shell_from_args(self):
        sh = _read(HELPER_SH)
        assert "eval" not in sh
        assert "sh -c \"$1\"" not in sh and "bash -c \"$1\"" not in sh

    def test_all_service_phases_implemented(self):
        sh = _read(HELPER_SH)
        for phase in ag.HELPER_PHASES:
            assert re.search(rf"^\s+{phase}\)", sh, re.M), phase

    def test_extra_phases_for_terminal(self):
        sh = _read(HELPER_SH)
        for phase in ("install-rule", "remove-rule"):
            assert re.search(rf"^\s+{phase}\)", sh, re.M), phase

    def test_unknown_phase_rejected(self):
        sh = _read(HELPER_SH)
        assert "неизвестная фаза" in sh

    def test_paths(self):
        sh = _read(HELPER_SH)
        assert ag.HELPER_PATH in sh                 # /usr/local/sbin/…
        assert ag.HELPER_RULE in sh                 # /etc/sudoers.d/…
        assert "/usr/local/share/ymaster-adguard" in sh

    def test_sudoers_rule_lists_phases(self):
        """NOPASSWD — не на весь сценарий, а на явный список фаз;
        install-rule/remove-rule из-под приложения недоступны."""
        sh = _read(HELPER_SH)
        m = re.search(r'^ALLOWED_PHASES="([^"]+)"', sh, re.M)
        assert m, "не найден список разрешённых фаз"
        allowed = m.group(1).split()
        for phase in ("preflight", "install", "login", "tun", "off",
                      "units", "autopilot", "version", "status", "rights"):
            assert phase in allowed, phase
        assert "install-rule" not in allowed and "remove-rule" not in allowed
        assert set(allowed) == set(ag.HELPER_PHASES), allowed
        # правило собирается из этого списка и проверяется visudo
        assert re.search(r'rules="\$user ALL=\(root\) NOPASSWD:"', sh)
        assert "for ph in $ALLOWED_PHASES" in sh
        assert '$HELPER $ph,' in sh

    def test_rule_validated_by_visudo_and_removed_on_failure(self):
        sh = _read(HELPER_SH)
        assert "visudo -cf" in sh
        assert re.search(r"rm -f \"\$SUDOERS_FILE\"", sh)

    def test_root_only_phases_guarded(self):
        sh = _read(HELPER_SH)
        assert "need_root" in sh
        # v1.70.24: фаза «socks» стала синонимом «tun» (TUN + selective
        # вместо локального SOCKS-прокси), поэтому guard стоит на «tun».
        for phase in ("install", "login", "tun", "units", "autopilot"):
            assert re.search(rf"need_root \"{phase}\"", sh), phase

    def test_kit_copied_from_repo_not_from_app_runtime(self):
        """Юниты раскладываются из root-копии /usr/local/share — путь из-под
        приложения в фазы мастера не передаётся."""
        sh = _read(HELPER_SH)
        body = sh[sh.index("phase_units()"):]
        assert "$KIT_DIR" in body and "install -m 0644" in body
        assert "ymaster-adguard-setup" not in body.split("phase_autopilot")[0]

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
    def test_run_rights_phase(self):
        r = subprocess.run(["bash", HELPER_SH, "rights"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.strip() == "ok"

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
    def test_run_unknown_phase_fails(self):
        r = subprocess.run(["bash", HELPER_SH, "rm", "-rf", "/"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode != 0 and "неизвестная фаза" in (r.stdout + r.stderr)

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
    def test_run_without_phase_prints_usage(self):
        r = subprocess.run(["bash", HELPER_SH], capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 2 and "использование" in (r.stdout + r.stderr)

    @pytest.mark.skipif(os.geteuid() == 0 or shutil.which("bash") is None,
                        reason="нужен запуск не от root")
    def test_install_rule_requires_root(self):
        r = subprocess.run(["bash", HELPER_SH, "install-rule", "/opt/x"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode != 0
        assert "только от root" in (r.stdout + r.stderr)


# ==============================================================================
#  Определение прав: root | sudo -n | помощник | нет прав
# ==============================================================================
class _R:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout, self.stderr = out, ""


class TestRightsDetection:
    def test_modes_are_passwordless(self):
        assert ag.RIGHTS_MODES == ("root", "nopass", "helper")
        assert "pass" not in ag.RIGHTS_MODES

    def test_root_mode(self, monkeypatch, fresh_job):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)
        mode, note = ag._detect_rights()
        assert mode == "root" and "root" in note

    def test_nopass_mode(self, monkeypatch, fresh_job):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: _R(0))
        mode, _note = ag._detect_rights()
        assert mode == "nopass"

    def test_helper_mode(self, monkeypatch, fresh_job):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: _R(1))
        monkeypatch.setattr(ag, "helper_allowed", lambda: True)
        mode, note = ag._detect_rights()
        assert mode == "helper" and "пароль не нужен" in note

    def test_no_rights_is_empty_mode(self, monkeypatch, fresh_job):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: _R(1))
        monkeypatch.setattr(ag, "helper_allowed", lambda: False)
        assert ag._detect_rights() == ("", "")

    def test_helper_allowed_needs_installed_helper(self, monkeypatch):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.os.path, "exists", lambda p: False)
        assert ag.helper_allowed() is False      # sudo даже не дёргаем

    def test_helper_allowed_uses_sudo_n_only(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)

        def fake_run(argv, **k):
            seen["argv"], seen["kw"] = argv, k
            return _R(0)

        monkeypatch.setattr(ag.subprocess, "run", fake_run)
        assert ag.helper_allowed() is True
        assert seen["argv"] == ["sudo", "-n", ag.HELPER_PATH, "rights"]
        assert "-S" not in seen["argv"] and "-p" not in seen["argv"]

    def test_helper_allowed_false_on_sudo_error(self, monkeypatch):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)

        def boom(*a, **k):
            raise OSError("sudo не найден")

        monkeypatch.setattr(ag.subprocess, "run", boom)
        assert ag.helper_allowed() is False

    def test_precheck_stops_with_one_time_cmd(self, monkeypatch, fresh_job):
        """Прав нет — мастер честно останавливается и даёт ОДНУ команду."""
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/curl")
        monkeypatch.setattr(ag, "_detect_rights", lambda: ("", ""))
        with pytest.raises(RuntimeError) as ei:
            ag._step_precheck()
        msg = str(ei.value)
        assert ag.HELPER_SRC in msg and "install-rule" in msg
        assert fresh_job.one_time_cmd == ag.one_time_cmd()
        assert "пароль" in msg.lower()
        assert fresh_job.sudo_mode == "none"

    def test_precheck_helper_mode_runs_helper_preflight(self, monkeypatch,
                                                        fresh_job):
        calls = []
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/curl")
        monkeypatch.setattr(ag, "_detect_rights",
                            lambda: ("helper", "Права: помощник."))
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=0, **k:
                            calls.append(phase) or (0, "ready"))
        ag._step_precheck()
        assert fresh_job.sudo_mode == "helper"
        assert calls == ["preflight"]
        assert fresh_job.steps["precheck"]["status"] == "done"

    def test_precheck_helper_failure_is_honest(self, monkeypatch, fresh_job):
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/curl")
        monkeypatch.setattr(ag, "_detect_rights",
                            lambda: ("helper", "Права: помощник."))
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=0, **k:
                            (1, "комплект не разложен"))
        with pytest.raises(RuntimeError) as ei:
            ag._step_precheck()
        assert "Помощник не готов" in str(ei.value)


# ==============================================================================
#  run_cmd / run_helper: в режиме помощника произвольного root нет
# ==============================================================================
class TestRunHelper:
    def test_run_cmd_refuses_in_helper_mode(self, fresh_job, monkeypatch):
        fresh_job.sudo_mode = "helper"

        def boom(*a, **k):
            raise AssertionError("в режиме помощника run_cmd вызывать нельзя")

        monkeypatch.setattr(ag.subprocess, "run", boom)
        with pytest.raises(RuntimeError):
            ag.run_cmd(["systemctl", "daemon-reload"])

    def test_run_cmd_nopass_mode_unchanged(self, monkeypatch, fresh_job):
        seen = {}
        monkeypatch.setattr(ag.subprocess, "run",
                            lambda full, **k: seen.update(full=full, k=k)
                            or _R(0, "ok"))
        fresh_job.sudo_mode = "nopass"
        rc, _out = ag.run_cmd(["cp", "a", "b"])
        assert rc == 0 and seen["full"] == ["sudo", "-n", "cp", "a", "b"]
        assert seen["k"].get("input") is None

    def test_run_helper_unknown_phase(self, fresh_job):
        with pytest.raises(RuntimeError):
            ag.run_helper("reboot")

    def test_run_helper_argv_and_stdin_closed(self, monkeypatch, fresh_job):
        seen = {}
        fresh_job.sudo_mode = "helper"
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)
        monkeypatch.setattr(ag.subprocess, "run",
                            lambda argv, **k: seen.update(argv=argv, k=k)
                            or _R(0, "готово"))
        rc, out = ag.run_helper("tun")
        assert rc == 0 and out == "готово"
        assert seen["argv"] == ["sudo", "-n", ag.HELPER_PATH, "tun"]
        assert seen["k"]["stdin"] == subprocess.DEVNULL

    def test_helper_argv_root_mode_runs_script_directly(self, monkeypatch,
                                                        fresh_job):
        fresh_job.sudo_mode = "root"
        monkeypatch.setattr(ag.os.path, "exists", lambda p: False)
        argv = ag.helper_argv("install")
        assert argv[0] == "bash" and argv[-1] == "install"
        assert argv[1].endswith(ag.HELPER_SRC)

    def test_login_argv_helper_mode(self, monkeypatch, fresh_job):
        fresh_job.sudo_mode = "helper"
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)
        argv = ag._login_argv("/usr/local/bin/adguardvpn-cli")
        assert argv == ["sudo", "-n", ag.HELPER_PATH, "login"]

    def test_login_worker_closes_stdin(self, monkeypatch, fresh_job):
        """Пароль через stdin больше не передаём — stdin закрыт вовсе."""
        seen = {}

        class P:
            returncode = 0
            stdout = iter([])

            def poll(self):
                return 0

            def kill(self):
                pass

        def fake_popen(argv, **k):
            seen.update(argv=argv, k=k)
            return P()

        monkeypatch.setattr(ag.subprocess, "Popen", fake_popen)
        fresh_job.sudo_mode = "root"
        ag._login_worker("/usr/local/bin/adguardvpn-cli")
        assert seen["k"]["stdin"] == subprocess.DEVNULL

    def test_one_time_cmd_shape(self):
        cmd = ag.one_time_cmd()
        assert cmd.startswith("sudo bash ")
        assert ag.HELPER_SRC in cmd and "install-rule" in cmd
        assert "ymaster-adguard-setup.sh" in cmd
        assert "\n" not in cmd and ";" not in cmd and "|" not in cmd


# ==============================================================================
#  preflight: контракт для UI
# ==============================================================================
class TestPreflight:
    KEYS = ("root", "sudo_nopass", "helper_ready", "mode", "ready",
            "need_password", "one_time_cmd", "hint", "helper_path",
            "sudoers_rule", "helper_installed")

    def test_shape_no_rights(self, monkeypatch):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: _R(1))
        monkeypatch.setattr(ag, "helper_allowed", lambda: False)
        pf = ag.preflight()
        for k in self.KEYS:
            assert k in pf, k
        assert pf["need_password"] is False       # пароля нет вовсе
        assert pf["ready"] is False and pf["mode"] == "none"
        assert "install-rule" in pf["one_time_cmd"]
        assert "помощник" in pf["hint"] or "команд" in pf["hint"]

    def test_shape_helper_ready(self, monkeypatch):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: _R(1))
        monkeypatch.setattr(ag, "helper_allowed", lambda: True)
        pf = ag.preflight()
        assert pf["ready"] is True and pf["mode"] == "helper"
        assert pf["helper_ready"] is True and pf["need_password"] is False
        assert pf["one_time_cmd"] == ""

    def test_shape_root(self, monkeypatch):
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)
        pf = ag.preflight()
        assert pf == {**pf, "root": True, "mode": "root", "ready": True,
                      "need_password": False, "one_time_cmd": ""}

    def test_endpoint(self, client, monkeypatch):
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: _R(1))
        monkeypatch.setattr(ag, "helper_allowed", lambda: False)
        r = client.get("/api/v1/settings/telegram/adguard/setup/preflight",
                       headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert body["need_password"] is False
        assert body["ready"] is False
        assert "install-rule" in body["one_time_cmd"]
        assert "hint" in body and body["hint"]

    def test_endpoint_ready(self, client, monkeypatch):
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag.os, "geteuid", lambda: 0)
        r = client.get("/api/v1/settings/telegram/adguard/setup/preflight",
                       headers=adm)
        body = r.json()
        assert body["ready"] is True and body["mode"] == "root"
        assert body["need_password"] is False

    def test_endpoint_requires_admin(self):
        c = TestClient(app)
        r = c.get("/api/v1/settings/telegram/adguard/setup/preflight")
        assert r.status_code in (401, 403)

    def test_status_endpoint_exposes_one_time_cmd(self, client, fresh_job):
        adm = login(client, "admin", "admin123")
        ag.job.one_time_cmd = ag.one_time_cmd()
        try:
            r = client.get("/api/v1/settings/telegram/adguard/setup",
                           headers=adm)
            assert r.status_code == 200
            assert "install-rule" in r.json()["job"]["one_time_cmd"]
        finally:
            ag.job.reset()


# ==============================================================================
#  Шаги мастера в режиме помощника
# ==============================================================================
def _patch_helper_mode(monkeypatch, tmp_path, calls):
    """Happy-path без сети в режиме помощника: все шаги — run_helper."""
    monkeypatch.setattr(ag.job, "sudo_mode", "helper")
    monkeypatch.setattr(ag, "_find_cli",
                        lambda: "/usr/local/bin/adguardvpn-cli")

    def fake_helper(phase, timeout=ag.CMD_TIMEOUT, stream=False):
        calls.append("helper:" + phase)
        if phase == "version":
            return 0, "adguardvpn-cli 1.7.12"
        return 0, "ok\nготово"

    def fake_cmd(cmd, timeout=ag.CMD_TIMEOUT, shell=False, input_text=""):
        calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return 0, "ok"

    monkeypatch.setattr(ag, "run_helper", fake_helper)
    monkeypatch.setattr(ag, "run_cmd", fake_cmd)
    monkeypatch.setattr(ag, "_login_worker",
                        lambda cli: (0, "Successfully logged in", ""))
    monkeypatch.setattr(tg, "ping_proxy",
                        lambda db, samples=3, scheduled=False:
                        {"ok": True, "ms": 27, "ok_samples": 3, "samples": 3,
                         "at": "2026-10-17 15:00", "proxy": True})


class TestStepsInHelperMode:
    def test_cli_skips_when_version_ok(self, monkeypatch, fresh_job, tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        ag._step_cli()
        assert calls == ["helper:version"]
        assert fresh_job.steps["cli"]["status"] == "skip"
        assert fresh_job.cli_version.startswith("adguardvpn-cli")

    def test_cli_installs_through_helper(self, monkeypatch, fresh_job,
                                         tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        state = {"installed": False}

        def fake_helper(phase, timeout=ag.CMD_TIMEOUT, stream=False):
            calls.append("helper:" + phase)
            if phase == "version" and not state["installed"]:
                return 1, "не установлен"
            if phase == "install":
                state["installed"] = True
                return 0, "CLI готов: 1.7.12"
            return 0, "adguardvpn-cli 1.7.12"

        monkeypatch.setattr(ag, "run_helper", fake_helper)
        ag._step_cli()
        assert calls == ["helper:version", "helper:install", "helper:version"]
        assert fresh_job.steps["cli"]["status"] == "done"
        # установщик из приложения не запускался (нет curl|sh в режиме помощника)
        assert not any("install.sh" in c for c in calls)

    def test_cli_install_failure_is_honest(self, monkeypatch, fresh_job,
                                           tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=0, **k:
                            (1, "установщик упал") if phase == "version"
                            else (1, "установщик упал"))
        with pytest.raises(RuntimeError) as ei:
            ag._step_cli()
        assert "Установщик AdGuard" in str(ei.value)

    def test_tun_through_helper(self, monkeypatch, fresh_job, tmp_path):
        # v1.70.24: шаг режима называется «tun» (TUN + selective вместо
        # SOCKS5). v1.70.25: фаза помощника тоже «tun» (исторический синоним
        # «socks» вычищен), и шаг принимает db — он поглотил очистку прокси.
        from app.database import SessionLocal
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        db = SessionLocal()
        try:
            ag._step_tun(db)
        finally:
            db.close()
        assert calls == ["helper:tun"]
        assert fresh_job.steps["tun"]["status"] == "done"

    def test_tunnel_through_helper(self, monkeypatch, fresh_job, tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        ag._step_tunnel()
        assert calls == ["helper:units"]
        assert fresh_job.steps["tunnel"]["status"] == "done"
        # юниты из-под приложения в /etc не копировались — это работа помощника
        assert not any(c.startswith("cp ") for c in calls)

    def test_tunnel_failure_is_honest(self, monkeypatch, fresh_job, tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=0, **k:
                            (1, "туннель не поднялся за 20 с"))
        with pytest.raises(RuntimeError) as ei:
            ag._step_tunnel()
        assert "туннеля" in str(ei.value) or "туннель" in str(ei.value)

    def test_autopilot_through_helper(self, monkeypatch, fresh_job, tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        ag._step_autopilot()
        assert calls == ["helper:autopilot"]
        assert fresh_job.steps["autopilot"]["status"] == "done"

    def test_full_run_helper_mode(self, monkeypatch, fresh_job, tmp_path):
        """Полный прогон в режиме помощника: успех, прокси записан,
        привилегированные шаги — только через помощника."""
        from app.database import SessionLocal
        from app.services import appsettings
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        monkeypatch.setattr(ag, "_step_precheck",
                            lambda: fresh_job.say("precheck", "Сервер готов.",
                                                  "done", 8))
        fresh_job.sudo_mode = "helper"
        db = SessionLocal()
        try:
            ag._run_steps(db)
            assert fresh_job.finished and fresh_job.success
            assert fresh_job.error == "" and fresh_job.ping_ms == 27
            statuses = {s: fresh_job.steps[s]["status"] for s in ag.STEP_ORDER}
            assert all(v in ("done", "skip") for v in statuses.values()), \
                statuses
            assert appsettings.get_setting(db, ag.SETT_PROXY, "") == \
                ag.PROXY_VALUE
            priv = [c for c in calls if c.startswith("helper:")]
            assert [p.split(":")[1] for p in priv] == \
                ["version", "tun", "units", "autopilot"]
            assert not any("sudo" in c for c in calls)
        finally:
            db.close()

    def test_login_step_uses_helper_argv(self, monkeypatch, fresh_job,
                                         tmp_path):
        calls = []
        _patch_helper_mode(monkeypatch, tmp_path, calls)
        seen = {}
        monkeypatch.setattr(ag, "_login_worker",
                            lambda cli: seen.setdefault("argv",
                                                        ag._login_argv(cli))
                            and (0, "ok", ""))
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)
        ag._step_login()
        assert seen["argv"] == ["sudo", "-n", ag.HELPER_PATH, "login"]
        assert fresh_job.steps["login"]["status"] in ("done", "skip")


# ==============================================================================
#  deploy.sh, README, инструкция
# ==============================================================================
class TestDeployAndDocs:
    def test_deploy_sh_installs_helper(self):
        sh = _read("deploy.sh")
        assert "ymaster-adguard-setup.sh" in sh
        assert "install-rule" in sh
        assert "/etc/sudoers.d/ymaster-adguard" in sh or \
            "ymaster-adguard-setup" in sh

    def test_deploy_block_is_guarded(self):
        """Ошибка блока не должна ронять деплой (как у sudoers v1.8.3)."""
        sh = _read("deploy.sh")
        block = sh[sh.index("AG_HELPER_SRC="):]
        block = block[:block.index("bold \"8/9")]
        assert "if [[" in block and "warn" in block
        assert "set -e" not in block

    def test_deploy_passes_app_dir(self):
        sh = _read("deploy.sh")
        assert re.search(r'install-rule "\$APP_DIR"', sh)

    def test_readme_documents_passwordless(self):
        rd = _read("deploy/adguard-vpn/README.md")
        assert "Пароль сервера не нужен" in rd
        assert "install-rule" in rd and "remove-rule" in rd
        assert "/usr/local/share/ymaster-adguard" in rd
        assert "visudo -c" in rd
        # ручная инструкция сохранилась
        assert "config set-mode SOCKS" in rd

    def test_manual_updated(self):
        m = _read("app/services/manual_content.py")
        assert "v1.70.16" in m and "без пароля" in m
        assert "ymaster-adguard-setup" in m

    def test_kit_files_still_complete(self):
        for name in ag.UNIT_MAP:
            assert os.path.exists(os.path.join(ag.DEPLOY_DIR, name)), name


# ==============================================================================
#  UI-харнесс
# ==============================================================================
class TestHarness17016:
    def test_harness_check_added(self):
        h = _read("tests/ui_harness.js")
        assert "1.70.16" in h and "ag-cmd" in h
        assert "adguard/setup/preflight" in h

    def test_harness_dropped_password_check(self):
        """Харнесс больше не требует поле пароля — только запрещает его."""
        h = _read("tests/ui_harness.js")
        assert "!bundle.includes('ag-sudo-pw')" in h
        # положительного требования (ok(bundle.includes('ag-sudo-pw'))) нет
        assert not re.search(r"(?<!!)bundle\.includes\('ag-sudo-pw'\)", h)
        assert not re.search(r"(?<!!)bundle\.includes\('agNeedPassword'\)", h)
