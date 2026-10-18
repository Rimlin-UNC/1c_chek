# -*- coding: utf-8 -*-
"""v1.70.26 — проверки режима VPN; с v1.70.27 они НЕ блокируют установку.

История вопроса. В v1.70.26 расхождение показаний CLI считалось фатальным:
фаза падала. Это было ошибкой — контракт у AdGuard документированный и это
КОД ВОЗВРАТА команды, а формат её текстового вывода нам не подконтролен.
Падать из-за нераспознанной строки — значит мешать запуску VPN на сервере.

С v1.70.27 все такие проверки дают ПРЕДУПРЕЖДЕНИЕ в живой журнал и продолжают
работу. Фатальными остались только отказы самих документированных команд
(`set-mode TUN`, `site-exclusions mode selective`, `connect`) и отсутствие
`/dev/net/tun` в фазе tun — там TUN действительно невозможен.

Что здесь доказано ЖИВЫМИ прогонами помощника и автопилота:

  1) режим исключений перечитывается, и расхождение ВИДНО владельцу
     (`general` = противоположный смысл списка: в VPN всё, КРОМЕ Telegram),
     но установка при этом НЕ прерывается;
  2) слушатель на 127.0.0.1:1080 предупреждает, а не блокирует: порт может
     быть занят посторонним сервисом;
  3) выключение VPN не блокируется состоянием автопилота, а от воскрешения
     VPN защищает сам `tg-health.sh` (выходит при disabled/masked юните);
  4) `/dev/net/tun` проверяется рано — и как предупреждение, потому что узел
     может появиться позже (модуль грузится по требованию);
  5) `install-rule` освежает уже разложенные юниты через `try-restart`;
  6) набор фаз помощника не изменился — установленное на сервере sudoers-
     правило продолжает работать;
  7) документированная последовательность команд сохранена целиком.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import shutil
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.services import adguard_setup as ag

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
HEALTH_SH = "deploy/adguard-vpn/tg-health.sh"
README = "deploy/adguard-vpn/README.md"

# Тот же набор фаз, что и в v1.70.25/26. sudoers-правило перечисляет фазы
# явно, поэтому релиз не должен требовать его переустановки.
PHASES = "preflight rights install login tun off units autopilot version status"

# Документированная последовательность (README AdGuardVPNCLI + БЗ AdGuard):
# режим TUN -> маршрутизация AUTO -> selective -> домены.
DOC_SEQUENCE = ["config set-mode TUN",
                "config set-tun-routing-mode AUTO",
                "site-exclusions mode selective"]


def _read(path):
    return open(path, encoding="utf-8").read()


def _sudo_ok():
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True,
                              timeout=20,
                              stdin=subprocess.DEVNULL).returncode == 0
    except Exception:                                        # noqa: BLE001
        return False


HAS_SUDO = _sudo_ok()
HAS_BASH = shutil.which("bash") is not None
HAS_TUN = os.path.exists("/dev/net/tun")

# CLI-заглушка, моделирующая настоящее поведение (README AdGuardVPNCLI):
# `site-exclusions mode` без аргументов печатает текущий режим.
CLI_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$AG_STUB_LOG"
if [ "$1" = "site-exclusions" ] && [ "$2" = "add" ]; then
  shift 2
  for d in "$@"; do printf '%s\n' "$d" >> "$AG_STUB_STATE"; done
fi
if [ "$1" = "site-exclusions" ] && [ "$2" = "show" ]; then
  [ -f "$AG_STUB_STATE" ] && cat "$AG_STUB_STATE"
fi
if [ "$1" = "site-exclusions" ] && [ "$2" = "mode" ]; then
  if [ "${3:-}" = "selective" ] || [ "${3:-}" = "general" ]; then
    if [ -z "${AG_STUB_IGNORE_SET:-}" ]; then
      printf '%s\n' "$3" > "$AG_STUB_MODE"
    fi
  else
    m=$(cat "$AG_STUB_MODE" 2>/dev/null)
    printf '%s\n' "${m:-general}"
  fi
fi
if [ "$1" = "--version" ]; then echo "adguardvpn-cli 1.7.12"; fi
exit 0
"""

SYSTEMCTL_STUB = r"""#!/bin/sh
printf '%s\n' "systemctl $*" >> "$STUB_LOG"
if [ "$1" = "is-active" ]; then
  if [ "$2" = "tg-health.timer" ] && [ -f "${STUB_TIMER_ACTIVE:-/nonexistent}" ]; then
    cat "$STUB_TIMER_ACTIVE"
  else
    cat "$STUB_STATE" 2>/dev/null || echo inactive
  fi
elif [ "$1" = "is-enabled" ]; then
  if [ "$2" = "tg-health.timer" ] && [ -f "${STUB_TIMER_ENABLED:-/nonexistent}" ]; then
    cat "$STUB_TIMER_ENABLED"
  else
    cat "$STUB_ENABLED" 2>/dev/null || echo disabled
  fi
fi
exit 0
"""

# visudo в песочнице нет, а install-rule проверяет им sudoers-правило.
VISUDO_STUB = "#!/bin/sh\nexit 0\n"


@pytest.fixture()
def stubs(tmp_path):
    """Каталог заглушек + файлы состояния. Root-файлы чистятся через sudo."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("adguardvpn-cli", CLI_STUB),
                       ("systemctl", SYSTEMCTL_STUB),
                       ("visudo", VISUDO_STUB)):
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    files = {"bindir": str(bindir)}
    for key, initial in (("log", ""), ("state", "inactive\n"), ("mode", ""),
                         ("excl", ""), ("enabled", "disabled\n"),
                         ("timer_active", "inactive\n"),
                         ("timer_enabled", "disabled\n")):
        f = tmp_path / (key + ".txt")
        f.write_text(initial, encoding="utf-8")
        files[key] = str(f)
    yield files
    if HAS_SUDO:
        subprocess.run(["sudo", "-n", "chown", "-R",
                        "%d:%d" % (os.getuid(), os.getgid()), str(tmp_path)],
                       capture_output=True, timeout=60, stdin=subprocess.DEVNULL)


def _sudo_run(stubs, argv, phase, cwd=None, extra_env=None, script=None):
    """Живой прогон фазы помощника от root со заглушками в PATH."""
    env_path = stubs["bindir"] + ":" + os.environ.get("PATH", "")
    cmd = ["sudo", "-n", "env",
           "PATH=" + env_path,
           "AG_STUB_LOG=" + stubs["log"],
           "AG_STUB_STATE=" + stubs["excl"],
           "AG_STUB_MODE=" + stubs["mode"],
           "STUB_LOG=" + stubs["log"],
           "STUB_STATE=" + stubs["state"],
           "STUB_ENABLED=" + stubs["enabled"],
           "STUB_TIMER_ACTIVE=" + stubs["timer_active"],
           "STUB_TIMER_ENABLED=" + stubs["timer_enabled"]]
    for k, v in (extra_env or {}).items():
        cmd.append(f"{k}={v}")
    cmd += ["bash", os.path.abspath(script or HELPER_SH), phase] + list(argv)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          cwd=cwd or os.getcwd(), stdin=subprocess.DEVNULL)


@pytest.fixture()
def redirected(stubs, tmp_path):
    """Копия помощника с перенаправленными системными каталогами.

    Фазы units/install-rule раскладывают файлы по /etc/systemd/system и
    /usr/local/sbin — в тесте их трогать нельзя. Подменяются ТОЛЬКО пути,
    проверяемая логика остаётся прежней.
    """
    kit = tmp_path / "kit"
    etc = tmp_path / "etc"
    sbin = tmp_path / "sbin"
    for d in (kit, etc, sbin):
        d.mkdir()
    for n in ("adguard-vpn-telegram.service", "tg-health.service",
              "tg-health.timer", "tg-health.sh", "ymaster-adguard-setup.sh"):
        (kit / n).write_text("[Unit]\nDescription=kit\n", encoding="utf-8")
    src = _read(HELPER_SH)
    src = src.replace("/usr/local/share/ymaster-adguard", str(kit))
    src = src.replace("/etc/systemd/system", str(etc))
    src = src.replace("/usr/local/sbin", str(sbin))
    src = src.replace("/etc/sudoers.d/ymaster-adguard", str(tmp_path / "sudoers"))
    script = tmp_path / "helper.sh"
    script.write_text(src, encoding="utf-8")
    return {"script": str(script), "etc": etc, "sbin": sbin, "kit": kit}


# ==============================================================================
#  1) Режим исключений: расхождение видно, но установку НЕ прерывает
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
@pytest.mark.skipif(not HAS_TUN, reason="нет /dev/net/tun")
class TestSelectiveModeReadBack:
    def _tun(self, stubs, tmp_path, name="cwd", **env):
        cwd = tmp_path / name
        cwd.mkdir(exist_ok=True)
        return _sudo_run(stubs, [], "tun", cwd=str(cwd), extra_env=env)

    def test_documented_sequence_is_intact(self, stubs, tmp_path):
        """Документированная последовательность команд сохранена целиком."""
        r = self._tun(stubs, tmp_path, "seq")
        assert r.returncode == 0, r.stdout + r.stderr
        lines = _read(stubs["log"]).splitlines()
        idx = [lines.index(c) for c in DOC_SEQUENCE]
        assert idx == sorted(idx), lines           # именно в этом порядке
        assert "site-exclusions add api.telegram.org" in lines

    def test_happy_path_confirms_selective(self, stubs, tmp_path):
        r = self._tun(stubs, tmp_path, "ok")
        assert r.returncode == 0, r.stdout + r.stderr
        lines = _read(stubs["log"]).splitlines()
        assert "site-exclusions mode" in lines      # перечитывание выполнено
        assert _read(stubs["mode"]).strip() == "selective"
        assert "selective" in r.stdout
        assert "ВНИМАНИЕ" not in r.stdout

    def test_general_warns_but_does_not_block(self, stubs, tmp_path):
        """ГЛАВНОЕ свойство v1.70.27: не блокируем запуск VPN.

        Расхождение показываем владельцу (в general смысл списка
        противоположный), но установка продолжается: домены добавляются,
        фаза завершается успешно.
        """
        r = self._tun(stubs, tmp_path, "gen", AG_STUB_IGNORE_SET="1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ВНИМАНИЕ" in r.stdout
        assert "general" in r.stdout
        assert "site-exclusions add api.telegram.org" in _read(stubs["log"])

    def test_general_warning_is_actionable(self, stubs, tmp_path):
        """В предупреждении есть команда для проверки — не «неизвестная ошибка»."""
        r = self._tun(stubs, tmp_path, "act", AG_STUB_IGNORE_SET="1")
        assert "site-exclusions mode" in r.stdout

    def test_unrecognized_answer_warns_not_fails(self, stubs, tmp_path):
        """Нераспознанный вывод CLI — предупреждение. Формат вывода vendor'а
        нам не подконтролен, и падать из-за него нельзя."""
        r = self._tun(stubs, tmp_path, "unk", AG_STUB_IGNORE_SET="1")
        open(stubs["mode"], "w").write("Exclusion mode: что-то своё\n")
        r2 = self._tun(stubs, tmp_path, "unk2", AG_STUB_IGNORE_SET="1")
        for res in (r, r2):
            assert res.returncode == 0, res.stdout + res.stderr

    def test_domain_in_list_does_not_hide_mode_warning(self, stubs, tmp_path):
        """Домен лежит в списке (в общем) — прежняя проверка прошла бы, а
        предупреждение о режиме всё равно показывается."""
        open(stubs["excl"], "w").write("api.telegram.org\n")
        r = self._tun(stubs, tmp_path, "dom", AG_STUB_IGNORE_SET="1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ВНИМАНИЕ" in r.stdout

    def test_script_routing_mode_never_used(self, stubs, tmp_path):
        """Режим SCRIPT не вызывается: он не работает (#69, #190)."""
        r = self._tun(stubs, tmp_path, "scr")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SCRIPT" not in _read(stubs["log"])
        assert "create-route-script" not in _read(stubs["log"])


# ==============================================================================
#  2) Туннель: посторонний слушатель на 1080 предупреждает, не блокирует
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
class TestTunnelChecks:
    def test_socks_listener_warns_but_units_succeed(self, stubs, tmp_path,
                                                    redirected):
        """Порт может быть занят посторонним сервисом — блокировать нельзя."""
        open(stubs["state"], "w").write("active\n")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 1080))
            srv.listen(1)
            r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                          script=redirected["script"])
        finally:
            srv.close()
        assert r.returncode == 0, r.stdout + r.stderr
        assert "внимание" in r.stdout
        assert "1080" in r.stdout
        assert "туннель активен" in r.stdout

    def test_clean_units_succeed_silently(self, stubs, tmp_path, redirected):
        open(stubs["state"], "w").write("active\n")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        assert probe.connect_ex(("127.0.0.1", 1080)) != 0
        probe.close()
        r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                      script=redirected["script"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "туннель активен" in r.stdout
        assert "1080" not in r.stdout

    def test_units_are_deployed_from_kit(self, stubs, tmp_path, redirected):
        open(stubs["state"], "w").write("active\n")
        r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                      script=redirected["script"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert (redirected["etc"] / "adguard-vpn-telegram.service").exists()
        assert (redirected["sbin"] / "tg-health.sh").exists()

    def test_dead_tunnel_message_mentions_license(self, stubs, tmp_path,
                                                  redirected):
        """Единственный фатальный исход фазы — туннель действительно не поднялся."""
        open(stubs["state"], "w").write("inactive\n")
        r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                      script=redirected["script"])
        assert r.returncode != 0
        assert "лиценз" in (r.stdout + r.stderr)

    def test_wait_extended_to_60s(self):
        """Первое подключение в TUN на медленном канале в 20 с не укладывается."""
        sh = _read(HELPER_SH)
        units = sh[sh.index("phase_units()"):sh.index("phase_autopilot()")]
        assert "seq 1 30" in units
        assert "за 60 с" in units
        src = _read("app/services/adguard_setup.py")
        fn = src[src.index("def _step_tunnel"):]
        fn = fn[:fn.index("\ndef ")]
        assert "range(30)" in fn


# ==============================================================================
#  3) Выключение: не блокируется, а от воскрешения защищает tg-health.sh
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
class TestOffIsNotBlocked:
    def test_off_ok_when_timer_gone(self, stubs, tmp_path):
        open(stubs["state"], "w").write("inactive\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "VPN выключен" in r.stdout
        assert "внимание" not in r.stdout

    def test_off_succeeds_even_if_timer_survives(self, stubs, tmp_path):
        """Выключение не должно блокироваться: владелец нажал «выключить» —
        VPN выключается. Состояние автопилота показываем предупреждением."""
        open(stubs["state"], "w").write("inactive\n")
        open(stubs["timer_enabled"], "w").write("enabled\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "VPN выключен" in r.stdout
        assert "внимание" in r.stdout

    def test_off_warning_is_actionable(self, stubs, tmp_path):
        open(stubs["state"], "w").write("inactive\n")
        open(stubs["timer_enabled"], "w").write("enabled\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert "disable --now tg-health.timer" in r.stdout

    def test_tunnel_still_guarded(self, stubs, tmp_path):
        """Единственный фатальный исход — туннель действительно не остановился."""
        open(stubs["state"], "w").write("active\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode != 0
        assert "не остановился" in (r.stdout + r.stderr)

    def test_timer_killed_before_tunnel(self, stubs, tmp_path):
        """Порядок из v1.70.25 сохранён: сначала автопилот, потом туннель."""
        open(stubs["state"], "w").write("inactive\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        dis = [l for l in _read(stubs["log"]).splitlines()
               if l.startswith("systemctl disable")]
        assert len(dis) == 2, dis
        assert "tg-health.timer" in dis[0], dis
        assert "adguard-vpn-telegram" in dis[1], dis

    def test_credentials_not_removed(self, stubs, tmp_path):
        """Вход и лицензия сохраняются — повторное включение без ссылки."""
        open(stubs["state"], "w").write("inactive\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        log = _read(stubs["log"])
        assert "logout" not in log and "uninstall" not in log


# ==============================================================================
#  4) Автопилот не воскрешает выключенный VPN (теперь это главная защита)
# ==============================================================================
class TestAutopilotRespectsOffSwitch:
    def _run_health(self, stubs, tmp_path, enabled_answer):
        open(stubs["enabled"], "w").write(enabled_answer + "\n")
        env = dict(os.environ)
        env.update({"PATH": stubs["bindir"] + ":" + env.get("PATH", ""),
                    "STUB_LOG": stubs["log"],
                    "STUB_STATE": stubs["state"],
                    "STUB_ENABLED": stubs["enabled"],
                    "CLI": "/nonexistent/adguardvpn-cli",
                    "TARGET_MS": "30"})
        return subprocess.run(["bash", os.path.abspath(HEALTH_SH)],
                              capture_output=True, text=True, timeout=180,
                              env=env, stdin=subprocess.DEVNULL,
                              cwd=str(tmp_path))

    @pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
    def test_disabled_tunnel_means_skip(self, stubs, tmp_path):
        r = self._run_health(stubs, tmp_path, "disabled")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "выключен владельцем" in r.stdout
        assert "connect" not in _read(stubs["log"])

    @pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
    def test_masked_tunnel_means_skip(self, stubs, tmp_path):
        r = self._run_health(stubs, tmp_path, "masked")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "выключен владельцем" in r.stdout

    @pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
    def test_enabled_tunnel_proceeds(self, stubs, tmp_path):
        """Штатный режим (мастер делает enable) — автопилот работает."""
        r = self._run_health(stubs, tmp_path, "enabled")
        assert "выключен владельцем" not in r.stdout

    @pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
    def test_unknown_answer_does_not_disable_autopilot(self, stubs, tmp_path):
        """Ответ не распознан (systemctl недоступен) — работаем как раньше."""
        r = self._run_health(stubs, tmp_path, "static")
        assert "выключен владельцем" not in r.stdout
        assert "подбираем локацию" in r.stdout or "недоступен" in r.stdout

    def test_guard_checks_answer_not_exit_code(self):
        sh = _read(HEALTH_SH)
        assert re.search(r'systemctl is-enabled adguard-vpn-telegram', sh)
        assert '"$EN" = "disabled"' in sh
        assert '"$EN" = "masked"' in sh


# ==============================================================================
#  5) /dev/net/tun: рано, но как предупреждение
# ==============================================================================
class TestTunDeviceWarning:
    def test_preflight_warns_not_dies(self):
        sh = _read(HELPER_SH)
        pf = sh[sh.index("phase_preflight()"):sh.index("phase_version()")]
        assert "/dev/net/tun" in pf
        # предупреждение идёт раньше проверки комплекта
        assert pf.index("/dev/net/tun") < pf.index("KIT_DIR")
        tail = pf[pf.index("/dev/net/tun"):]
        assert "die" not in tail.split("\n")[0] + tail.split("\n")[1]

    def test_tun_phase_keeps_hard_guard(self):
        """В самой фазе tun guard остаётся фатальным: там TUN действительно
        невозможен, и это не догадка, а факт."""
        sh = _read(HELPER_SH)
        tun = sh[sh.index("phase_tun()"):sh.index("phase_units()")]
        assert re.search(r'\[ -e /dev/net/tun \] \|\| die', tun)

    def test_python_precheck_warns_not_raises(self, monkeypatch):
        real = os.path.exists
        monkeypatch.setattr(ag.os.path, "exists",
                            lambda p: False if p == "/dev/net/tun" else real(p))
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(ag, "_detect_rights",
                            lambda: ("root", "Права: root — хорошо."))
        ag.job.reset()
        try:
            ag._step_precheck()                    # НЕ бросает исключение
            log = "\n".join(ag.job.log)
        finally:
            ag.job.reset()
        assert "/dev/net/tun" in log
        assert "Внимание" in log

    def test_python_precheck_silent_when_tun_present(self, monkeypatch):
        real = os.path.exists
        monkeypatch.setattr(ag.os.path, "exists",
                            lambda p: True if p == "/dev/net/tun" else real(p))
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(ag, "_detect_rights",
                            lambda: ("root", "Права: root — хорошо."))
        ag.job.reset()
        try:
            ag._step_precheck()
            log = "\n".join(ag.job.log)
        finally:
            ag.job.reset()
        assert "/dev/net/tun" not in log


# ==============================================================================
#  6) install-rule освежает разложенные юниты
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
class TestInstallRuleRefresh:
    def _app(self, tmp_path, name, deployed):
        kit = tmp_path / (name + "-kit")
        etc = tmp_path / (name + "-etc")
        sbin = tmp_path / (name + "-sbin")
        appdir = tmp_path / name
        for d in (kit, etc, sbin, appdir / "deploy" / "adguard-vpn"):
            d.mkdir(parents=True)
        src = _read(HELPER_SH)
        src = src.replace("/usr/local/share/ymaster-adguard", str(kit))
        src = src.replace("/etc/systemd/system", str(etc))
        src = src.replace("/usr/local/sbin", str(sbin))
        src = src.replace("/etc/sudoers.d/ymaster-adguard",
                          str(tmp_path / (name + "-sudoers")))
        script = appdir / "deploy" / "adguard-vpn" / "ymaster-adguard-setup.sh"
        script.write_text(src, encoding="utf-8")
        marker = "[Unit]\nDescription=NEW-FROM-KIT\n"
        for n in ("adguard-vpn-telegram.service", "tg-health.service",
                  "tg-health.timer"):
            (script.parent / n).write_text(marker, encoding="utf-8")
            if deployed:
                (etc / n).write_text("[Unit]\nDescription=OLD\n",
                                     encoding="utf-8")
        (script.parent / "tg-health.sh").write_text("#!/bin/sh\n# NEW\n",
                                                    encoding="utf-8")
        if deployed:
            (sbin / "tg-health.sh").write_text("#!/bin/sh\n# OLD\n",
                                               encoding="utf-8")
        return {"appdir": appdir, "script": script, "etc": etc, "sbin": sbin}

    def test_refresh_when_already_installed(self, stubs, tmp_path):
        ctx = self._app(tmp_path, "app1", deployed=True)
        r = _sudo_run(stubs, [str(ctx["appdir"])], "install-rule",
                      cwd=str(tmp_path), script=str(ctx["script"]))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "освежаю" in r.stdout
        assert "NEW-FROM-KIT" in (ctx["etc"] / "adguard-vpn-telegram.service"
                                  ).read_text(encoding="utf-8")
        assert "# NEW" in (ctx["sbin"] / "tg-health.sh").read_text(
            encoding="utf-8")

    def test_no_refresh_on_fresh_install(self, stubs, tmp_path):
        ctx = self._app(tmp_path, "app2", deployed=False)
        r = _sudo_run(stubs, [str(ctx["appdir"])], "install-rule",
                      cwd=str(tmp_path), script=str(ctx["script"]))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "освежаю" not in r.stdout
        assert not (ctx["etc"] / "adguard-vpn-telegram.service").exists()

    def test_uses_try_restart_not_restart(self):
        """VPN, выключенный владельцем, включать обратно нельзя."""
        sh = _read(HELPER_SH)
        ir = sh[sh.index("phase_install_rule()"):sh.index("phase_remove_rule()")]
        assert "try-restart" in ir
        assert not re.search(r'systemctl restart "\$TUNNEL_UNIT"', ir)

    def test_refresh_is_non_fatal(self):
        """Каждая команда освежения допускает отказ: install-rule не должен
        падать из-за этого (иначе deploy.sh --update сломался бы)."""
        sh = _read(HELPER_SH)
        ir = sh[sh.index("phase_install_rule()"):sh.index("phase_remove_rule()")]
        tail = ir[ir.index("VPN уже был установлен"):]
        assert tail.count("die ") == 0, tail


# ==============================================================================
#  7) Набор фаз не изменился + документация
# ==============================================================================
class TestPhaseSetAndDocs:
    def test_allowed_phases_unchanged(self):
        m = re.search(r'ALLOWED_PHASES="([^"]+)"', _read(HELPER_SH))
        assert m and m.group(1) == PHASES

    def test_helper_phases_unchanged(self):
        assert tuple(ag.HELPER_PHASES) == tuple(PHASES.split())

    def test_dispatcher_matches_allowed(self):
        """Новых фаз нет — переустановка sudoers-правила не требуется."""
        sh = _read(HELPER_SH)
        allowed = set(re.search(r'ALLOWED_PHASES="([^"]+)"', sh).group(1).split())
        dispatched = set(re.findall(r'^\s{4}([a-z-]+)\)\s+phase_', sh, re.M))
        assert dispatched - {"install-rule", "remove-rule"} == allowed

    def test_step_order_unchanged(self):
        assert ag.STEP_ORDER == ["precheck", "cli", "login", "tun", "tunnel",
                                 "autopilot", "verify", "done"]

    def test_readme_lists_real_phases(self):
        line = [l for l in _read(README).splitlines()
                if "ymaster-adguard-setup preflight" in l]
        assert line
        assert "socks" not in line[0]
        for ph in ("tun", "off", "units", "autopilot"):
            assert ph in line[0], line[0]

    def test_readme_says_checks_do_not_block(self):
        """Инструкция честно говорит: наблюдения установку не прерывают."""
        md = _read(README)
        assert "не прерывают установку" in md
        assert "не прерывается" in md

    def test_helper_has_no_stale_synonym_claim(self):
        sh = _read(HELPER_SH)
        assert "по-прежнему вызывает фазу" not in sh
        assert "принято как синоним" not in sh

    def test_autopilot_no_redundant_enable(self):
        src = _read("app/services/adguard_setup.py")
        fn = src[src.index("def _step_autopilot"):]
        fn = fn[:fn.index("\ndef ")]
        assert fn.count('"enable"') == 1, fn
        assert '"--now"' in fn

    def test_no_unit_name_literals_left(self):
        src = _read("app/services/adguard_setup.py")
        for fn_name in ("_step_tunnel", "_step_autopilot"):
            fn = src[src.index("def " + fn_name):]
            fn = fn[:fn.index("\ndef ")]
            assert '"adguard-vpn-telegram"' not in fn, fn_name
            assert '"tg-health.timer"' not in fn, fn_name
