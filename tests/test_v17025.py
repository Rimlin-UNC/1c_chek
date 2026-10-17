# -*- coding: utf-8 -*-
"""v1.70.25 — AdGuard VPN: одна кнопка-переключатель (включить/выключить).

Что здесь доказано:
  1) ЖИВЫМ прогоном фазы `off` на stub-`systemctl`: автопилот гасится РАНЬШЕ
     туннеля. Порядок принципиален — иначе tg-health.sh в 15:10 увидел бы
     мёртвый путь и сам выполнил `connect -l`, включив VPN обратно за спиной
     владельца;
  2) фаза `off` честно падает, если туннель не остановился, и не рапортует
     успех;
  3) `vpn_status()` берёт цвет из фактического состояния юнита, а при
     недоступном systemd отвечает `known: false` вместо ложного «выключен»;
  4) `turn_off()` не работает во время установки, без прав отдаёт одну команду
     для терминала, а не «sudo: not allowed»;
  5) лишнее вычищено: нет отдельного шага `proxy`, нет фазы-синонима `socks`,
     нет мёртвой ветки `need_password` в обработчике кнопки;
  6) кнопка красится в btn-ok/btn-bad и зовёт действие `off`.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from fastapi import status
from app.config import settings
from app.main import app
from app.services import adguard_setup as ag
from tests.test_v1121 import login

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
README = "deploy/adguard-vpn/README.md"
APP_JS = "app/static/js/app.js"
VER = "1.70.25"
PREV = "1.70.24"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Подставной systemctl: пишет аргументы в лог, на is-active отвечает то, что
# попросил тест (через STUB_STATE). Так доказывается ПОРЯДОК команд выключения.
SYSTEMCTL_STUB = r"""#!/bin/sh
printf '%s\n' "systemctl $*" >> "$STUB_LOG"
if [ "$1" = "is-active" ]; then
  cat "$STUB_STATE" 2>/dev/null || echo inactive
fi
exit 0
"""

CLI_STUB = r"""#!/bin/sh
printf '%s\n' "cli $*" >> "$STUB_LOG"
exit 0
"""


def _read(path):
    return open(path, encoding="utf-8").read()


def _sudo_ok():
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True,
                              timeout=20,
                              stdin=subprocess.DEVNULL).returncode == 0
    except Exception:                                        # noqa: BLE001
        return False


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Изолированный файл состояния (приём v1.70.20) + чистое задание."""
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield tmp_path
    ag.job.reset()


@pytest.fixture()
def stubs(tmp_path):
    """Каталог со stub-systemctl и stub-adguardvpn-cli + лог вызовов."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("systemctl", SYSTEMCTL_STUB),
                       ("adguardvpn-cli", CLI_STUB)):
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    log = tmp_path / "calls.log"
    state = tmp_path / "unit_state"
    log.write_text("", encoding="utf-8")
    state.write_text("inactive\n", encoding="utf-8")
    return {"bindir": str(bindir), "log": str(log), "state": str(state)}


def _run_off(stubs, cwd, extra_env=None):
    """Живой прогон фазы `off` от root со stub-ом в PATH."""
    env_path = stubs["bindir"] + ":" + os.environ.get("PATH", "")
    cmd = ["sudo", "-n", "env",
           "PATH=" + env_path,
           "STUB_LOG=" + stubs["log"],
           "STUB_STATE=" + stubs["state"]]
    for k, v in (extra_env or {}).items():
        cmd.append(f"{k}={v}")
    cmd += ["bash", os.path.abspath(HELPER_SH), "off"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=cwd, stdin=subprocess.DEVNULL)


# ==============================================================================
#  1) Живой прогон фазы off: порядок команд и честность отказа
# ==============================================================================
@pytest.mark.skipif(not _sudo_ok(), reason="нет passwordless sudo")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
class TestPhaseOffLive:
    def test_timer_killed_before_tunnel(self, stubs, tmp_path):
        """Главное свойство выключения: СНАЧАЛА автопилот, ПОТОМ туннель.

        Если наоборот, то между остановкой туннеля и остановкой таймера
        остаётся окно, в котором tg-health.sh может отработать и вернуть VPN.
        """
        r = _run_off(stubs, str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        lines = [l for l in _read(stubs["log"]).splitlines()
                 if l.startswith("systemctl disable")]
        assert len(lines) == 2, lines
        assert "tg-health.timer" in lines[0], \
            "первым должен гаситься автопилот: " + str(lines)
        assert "adguard-vpn-telegram" in lines[1], lines

    def test_disconnect_called(self, stubs, tmp_path):
        """Ручной сеанс (connect вне юнита) тоже снимается."""
        r = _run_off(stubs, str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "cli disconnect" in _read(stubs["log"])

    def test_reports_success_only_when_stopped(self, stubs, tmp_path):
        open(stubs["state"], "w").write("inactive\n")
        r = _run_off(stubs, str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "выключен" in r.stdout

    def test_honest_failure_when_tunnel_survives(self, stubs, tmp_path):
        """Юнит остался active — фаза обязана упасть, а не отрапортовать."""
        open(stubs["state"], "w").write("active\n")
        r = _run_off(stubs, str(tmp_path))
        assert r.returncode != 0
        assert "не остановился" in (r.stdout + r.stderr)
        assert "выключен" not in r.stdout

    def test_credentials_not_removed(self, stubs, tmp_path):
        """Выключение не удаляет CLI и не выходит из аккаунта: повторное
        включение должно идти без ссылки регистрации."""
        r = _run_off(stubs, str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        log = _read(stubs["log"])
        assert "logout" not in log
        assert "uninstall" not in log
        assert "-u" not in log.split()


# ==============================================================================
#  2) Статика помощника
# ==============================================================================
class TestHelperStatic:
    def test_off_phase_exists_and_guarded(self):
        sh = _read(HELPER_SH)
        assert re.search(r"^phase_off\(\) \{$", sh, re.M)
        assert re.search(r'^\s+off\)\s+phase_off ;;', sh, re.M)
        assert 'need_root "off"' in sh

    def test_allowed_phases(self):
        sh = _read(HELPER_SH)
        m = re.search(r'^ALLOWED_PHASES="([^"]+)"', sh, re.M)
        assert m
        allowed = m.group(1).split()
        assert "off" in allowed and "tun" in allowed
        assert "socks" not in allowed, \
            "v1.70.25: синоним фазы режима вычищен"
        assert "install-rule" not in allowed and "remove-rule" not in allowed
        assert set(allowed) == set(ag.HELPER_PHASES), allowed

    def test_no_socks_phase_left(self):
        sh = _read(HELPER_SH)
        assert not re.search(r"^\s+socks\)", sh, re.M)
        assert "phase_socks" not in sh

    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  3) Сервис: состояние VPN и выключение
# ==============================================================================
class TestVpnStatus:
    def test_active_means_on(self, monkeypatch):
        monkeypatch.setattr(ag.subprocess, "run",
                            lambda *a, **k: type("R", (), {
                                "stdout": "active\n", "returncode": 0})())
        st = ag.vpn_status()
        assert st["on"] is True and st["known"] is True
        assert st["unit"] == ag.TUNNEL_UNIT

    def test_inactive_means_off(self, monkeypatch):
        monkeypatch.setattr(ag.subprocess, "run",
                            lambda *a, **k: type("R", (), {
                                "stdout": "inactive\n", "returncode": 3})())
        st = ag.vpn_status()
        assert st["on"] is False and st["known"] is True

    def test_unknown_systemd_is_not_reported_as_off(self, monkeypatch):
        """systemd недоступен (контейнер/песочница) → known=False.

        Кнопка не имеет права утверждать «выключен», если состояние
        определить не удалось: владелец увидел бы красный на работающем VPN.
        """
        def _boom(*a, **k):
            raise OSError("Failed to connect to bus")
        monkeypatch.setattr(ag.subprocess, "run", _boom)
        st = ag.vpn_status()
        assert st["known"] is False
        assert st["on"] is False
        assert st["state"] == "unknown"

    def test_failed_unit_is_not_on(self, monkeypatch):
        monkeypatch.setattr(ag.subprocess, "run",
                            lambda *a, **k: type("R", (), {
                                "stdout": "failed\n", "returncode": 3})())
        assert ag.vpn_status()["on"] is False

    def test_uses_is_active_without_sudo(self, monkeypatch):
        """Состояние читается без root — значит и без правила помощника."""
        seen = {}

        def fake(argv, **k):
            seen["argv"] = argv
            return type("R", (), {"stdout": "active\n", "returncode": 0})()
        monkeypatch.setattr(ag.subprocess, "run", fake)
        ag.vpn_status()
        assert seen["argv"][:2] == ["systemctl", "is-active"]
        assert "sudo" not in seen["argv"]


class TestTurnOff:
    def test_refuses_while_installing(self, state_dir):
        ag.job.running = True
        try:
            r = ag.turn_off()
            assert r["ok"] is False
            assert "установка" in r["error"].lower()
        finally:
            ag.job.reset()

    def test_no_rights_returns_one_command(self, state_dir, monkeypatch):
        monkeypatch.setattr(ag, "_detect_rights", lambda: ("", ""))
        r = ag.turn_off()
        assert r["ok"] is False and r["need_rule"] is True
        assert "install-rule" in r["one_time_cmd"]
        assert "sudo" not in r["error"].split("install-rule")[0].lower() \
            or "not allowed" not in r["error"]

    def test_success_calls_off_phase_with_explicit_mode(self, state_dir,
                                                        monkeypatch):
        seen = {}
        monkeypatch.setattr(ag, "_detect_rights", lambda: ("nopass", "ок"))
        monkeypatch.setattr(ag, "vpn_status",
                            lambda: {"on": False, "known": True,
                                     "state": "inactive",
                                     "unit": ag.TUNNEL_UNIT})

        def fake(phase, timeout=ag.CMD_TIMEOUT, stream=False, mode=None):
            seen.update(phase=phase, mode=mode, timeout=timeout)
            return 0, "VPN выключен"
        monkeypatch.setattr(ag, "run_helper", fake)
        r = ag.turn_off()
        assert r["ok"] is True
        assert seen["phase"] == "off"
        # режим прав передаётся ЯВНО: job.sudo_mode вне установки не определён
        assert seen["mode"] == "nopass"

    def test_does_not_touch_job_sudo_mode(self, state_dir, monkeypatch):
        """Выключение не подменяет состояние живого задания."""
        monkeypatch.setattr(ag, "_detect_rights", lambda: ("root", "ок"))
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=0, stream=False, mode=None:
                            (0, "ok"))
        monkeypatch.setattr(ag, "vpn_status", lambda: {"on": False})
        before = ag.job.sudo_mode
        ag.turn_off()
        assert ag.job.sudo_mode == before

    def test_helper_failure_is_reported_with_reason(self, state_dir,
                                                    monkeypatch):
        monkeypatch.setattr(ag, "_detect_rights", lambda: ("root", "ок"))
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=0, stream=False, mode=None:
                            (1, ""))
        r = ag.turn_off()
        assert r["ok"] is False
        # _reason() никогда не оставляет пустую «причину» после двоеточия
        assert not r["error"].rstrip().endswith(":")


class TestHelperArgvMode:
    def test_explicit_mode_wins_over_job(self, state_dir, monkeypatch):
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True)
        ag.job.sudo_mode = "helper"
        try:
            assert ag.helper_argv("off", "root")[0] == "bash"
            assert ag.helper_argv("off", "nopass")[:2] == ["sudo", "-n"]
            # без явного режима — прежнее поведение по job.sudo_mode
            assert ag.helper_argv("off")[:2] == ["sudo", "-n"]
        finally:
            ag.job.reset()


# ==============================================================================
#  4) Вычищенное лишнее
# ==============================================================================
class TestCleanup:
    def test_no_separate_proxy_step(self):
        assert "proxy" not in ag.STEP_ORDER
        assert "proxy" not in ag.STEP_TITLE
        assert not hasattr(ag, "_step_proxy")
        assert not hasattr(ag, "_step_socks")

    def test_step_tun_takes_db(self):
        import inspect
        assert list(inspect.signature(ag._step_tun).parameters) == ["db"]

    def test_ui_has_no_dead_password_branch(self):
        js = _read(APP_JS)
        assert "pf.need_password" not in js, \
            "поле need_password с v1.70.16 всегда false — ветка мертва"
        assert "ag-sudo-pw" not in js

    def test_unit_names_are_constants(self):
        assert ag.TUNNEL_UNIT == "adguard-vpn-telegram"
        assert ag.TIMER_UNIT == "tg-health.timer"
        src = _read("app/services/adguard_setup.py")
        assert 'TUNNEL_UNIT = "adguard-vpn-telegram"' in src


# ==============================================================================
#  5) Маршруты
# ==============================================================================
class TestRoutes:
    def test_status_returns_vpn_state(self, client):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/settings/telegram/adguard/setup", headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert "vpn" in body and "on" in body["vpn"] and "known" in body["vpn"]

    def test_off_requires_admin(self):
        from fastapi.testclient import TestClient
        c = TestClient(app)
        r = c.post("/api/v1/settings/telegram/adguard/setup",
                   json={"action": "off"})
        assert r.status_code in (401, 403)

    def test_off_while_installing_is_conflict(self, client, monkeypatch):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        ag.job.running = True
        try:
            r = client.post("/api/v1/settings/telegram/adguard/setup",
                            headers=adm, json={"action": "off"})
            assert r.status_code == status.HTTP_409_CONFLICT
        finally:
            ag.job.reset()

    def test_off_success(self, client, monkeypatch):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag, "turn_off",
                            lambda: {"ok": True,
                                     "vpn": {"on": False, "known": True},
                                     "message": "VPN выключен"})
        r = client.post("/api/v1/settings/telegram/adguard/setup",
                        headers=adm, json={"action": "off"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["vpn"]["on"] is False

    def test_off_without_rule_returns_command_not_409(self, client,
                                                      monkeypatch):
        """Нет правила помощника → это не конфликт, а «нужна одна команда»."""
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag, "turn_off",
                            lambda: {"ok": False, "need_rule": True,
                                     "one_time_cmd": "sudo bash … install-rule",
                                     "error": "нет прав"})
        r = client.post("/api/v1/settings/telegram/adguard/setup",
                        headers=adm, json={"action": "off"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["need_rule"] is True
        assert "install-rule" in body["one_time_cmd"]

    def test_off_failure_is_conflict(self, client, monkeypatch):
        ag.job.reset()
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(ag, "turn_off",
                            lambda: {"ok": False, "error": "туннель не встал"})
        r = client.post("/api/v1/settings/telegram/adguard/setup",
                        headers=adm, json={"action": "off"})
        assert r.status_code == status.HTTP_409_CONFLICT


# ==============================================================================
#  6) UI: переключатель
# ==============================================================================
class TestUi:
    def test_paint_function_exists(self):
        js = _read(APP_JS)
        assert "function agPaint(" in js
        assert "let agVpnOn" in js

    def test_button_colored_by_state(self):
        js = _read(APP_JS)
        assert "b.classList.toggle('btn-ok', agVpnOn && !busy)" in js
        assert "b.classList.toggle('btn-bad', !agVpnOn && !busy)" in js

    def test_unknown_state_is_labeled_honestly(self):
        js = _read(APP_JS)
        assert "состояние неизвестно" in js

    def test_click_branches_on_state(self):
        js = _read(APP_JS)
        assert "if (agVpnOn) { await agTurnOff(); return; }" in js
        assert "async function agTurnOff()" in js
        assert "{ action: 'off' }" in js

    def test_paint_called_on_poll_and_on_load(self):
        js = _read(APP_JS)
        assert "agPaint(r.vpn, !!st.running)" in js
        assert js.count("agPaint(") >= 4

    def test_no_proxy_prefill(self):
        js = _read(APP_JS)
        assert "prx.value = 'socks5://127.0.0.1:1080'" not in js

    def test_hint_has_no_password_label(self):
        """Фраза «Пароль сервера» дословно = метка удалённого поля пароля;
        на неё завязан тест-страховка test_ui_has_no_password_field."""
        js = _read(APP_JS)
        panel = js[js.index('<div id="ag-setup"'):js.index("function agRender")]
        assert "Пароль сервера" not in panel

    def test_whats_new_entry(self):
        assert f"'{VER}':" in _read(APP_JS)

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  7) Документация
# ==============================================================================
class TestDocs:
    def test_readme_describes_toggle(self):
        rd = _read(README)
        assert "кнопка-переключатель" in rd
        assert "выключить" in rd and "включить" in rd

    def test_readme_explains_off_order(self):
        rd = _read(README)
        assert "сначала автопилот" in rd

    def test_readme_says_credentials_survive(self):
        rd = _read(README)
        assert "без ссылки" in rd

    def test_changelog(self):
        ch = _read("CHANGELOG.md")
        assert ch.count(f"## [{VER}]") == 1
        top = ch.split(f"## [{PREV}]")[0]
        for needle in ("переключател", "phase", "off", "автопилот",
                       "фактического состояния"):
            assert needle.lower() in top.lower(), needle


# ==============================================================================
#  8) Версии
# ==============================================================================
class TestVersions17025:
    def test_version_at_least(self):
        parts = tuple(int(x) for x in settings.APP_VERSION.split("."))
        assert parts >= (1, 70, 25), settings.APP_VERSION

    def test_static_pins_follow_current_version(self):
        for path in SYNC_POINTS:
            assert settings.APP_VERSION in _read(path), path

    def test_no_previous_version_left(self):
        for path in SYNC_POINTS:
            assert PREV not in _read(path), path

    def test_changelog_and_whatsnew(self):
        assert _read("CHANGELOG.md").count(f"## [{VER}]") == 1
        assert f"'{VER}':" in _read(APP_JS)
        assert f"'{VER}':" in _read("tests/ui_harness.js")
