# -*- coding: utf-8 -*-
"""v1.70.26 — «ничего не забыли»: успех VPN доказывается, а не предполагается.

Что здесь доказано ЖИВЫМИ прогонами помощника и автопилота:

  1) Фаза `tun` ПЕРЕЧИТЫВАЕТ режим исключений и падает, если он остался
     `general`. Это не косметика: в general-режиме тот же список работает
     НАОБОРОТ (в VPN уходит всё, КРОМЕ Telegram), а `api.telegram.org` при
     этом всё равно присутствует в выводе `site-exclusions show` — поэтому
     прежняя проверка «домен есть в списке» инверсию требования «туннелим
     только Telegram» не ловила;
  2) фаза `units` не считает `active` доказательством TUN: если слушается
     127.0.0.1:1080, сервер остался в SOCKS и трафик Telegram в VPN не
     уходит — фаза падает вместо рапорта;
  3) фаза `off` падает, если автопилот не снят: выживший таймер в 15:10
     поднял бы VPN обратно, а красная кнопка в панели врала бы;
  4) `tg-health.sh` не поднимает VPN, который владелец выключил
     (`disabled`/`masked`), и при недоступном systemctl сохраняет прежнее
     поведение — иначе автопилот молча переставал бы работать;
  5) `/dev/net/tun` проверяется ДО установки CLI и до ссылки входа;
  6) `install-rule` освежает уже разложенные по системе юниты через
     `try-restart` (выключенный владельцем VPN обратно не включается);
  7) набор фаз помощника НЕ изменился — значит уже установленное на сервере
     sudoers-правило продолжает работать и переустановка правила ради этого
     релиза не требуется;
  8) из инструкции убрана несуществующая фаза `socks`, добавлены `tun`/`off`.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
HEALTH_SH = "deploy/adguard-vpn/tg-health.sh"
README = "deploy/adguard-vpn/README.md"
APP_JS = "app/static/js/app.js"
CHANGELOG = "CHANGELOG.md"
VER = "1.70.26"
PREV = "1.70.25"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Тот же набор фаз, что и в v1.70.25. ВАЖНО: sudoers-правило перечисляет фазы
# явно, поэтому новый релиз не должен требовать его переустановки.
PHASES_17025 = "preflight rights install login tun off units autopilot version status"


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

# CLI-заглушка, моделирующая НАСТОЯЩее поведение (README AdGuardVPNCLI):
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
    if [ -n "${AG_STUB_IGNORE_SET:-}" ]; then
      # клиника: команда принята (код 0), но режим НЕ применился
      :
    else
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

# systemctl-заглушка: is-active/is-enabled отвечают содержимым файлов состояния.
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
# Заглушка просто подтверждает корректность — проверяем не visudo, а то, что
# install-rule освежает уже разложенные юниты.
VISUDO_STUB = r"""#!/bin/sh
exit 0
"""


@pytest.fixture()
def stubs(tmp_path):
    """Каталог заглушек + файлы состояния. Корневые файлы чистятся через sudo."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("adguardvpn-cli", CLI_STUB),
                       ("systemctl", SYSTEMCTL_STUB),
                       ("visudo", VISUDO_STUB)):
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    files = {}
    for key, initial in (("log", ""), ("state", "inactive\n"), ("mode", ""),
                         ("excl", ""), ("enabled", "disabled\n"),
                         ("timer_active", "inactive\n"),
                         ("timer_enabled", "disabled\n")):
        f = tmp_path / (key + ".txt")
        f.write_text(initial, encoding="utf-8")
        files[key] = str(f)
    files["bindir"] = str(bindir)
    yield files
    # фазы под sudo оставляют root-файлы: снимаем их до очистки tmp_path
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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=cwd or os.getcwd(), stdin=subprocess.DEVNULL)


# ==============================================================================
#  1) Режим исключений перечитывается: general НЕ принимается за selective
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
@pytest.mark.skipif(not os.path.exists("/dev/net/tun"), reason="нет /dev/net/tun")
class TestSelectiveModeVerifiedLive:
    def _tun(self, stubs, tmp_path, **env):
        cwd = tmp_path / "cwd"
        cwd.mkdir(exist_ok=True)
        return _sudo_run(stubs, [], "tun", cwd=str(cwd), extra_env=env)

    def test_happy_path_reads_mode_back(self, stubs, tmp_path):
        """В штатном режиме фаза успешна, а перечитывание реально выполнено."""
        r = self._tun(stubs, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        lines = _read(stubs["log"]).splitlines()
        # именно БЕЗ аргумента — это и есть запрос текущего режима
        assert "site-exclusions mode" in lines, lines
        assert _read(stubs["mode"]).strip() == "selective"

    def test_general_mode_is_rejected(self, stubs, tmp_path):
        """ГЛАВНОЕ: режим не применился → фаза падает, а не рапортует успех.

        Иначе через VPN шёл бы весь трафик сервера КРОМЕ Telegram, то есть
        сайт, ФНС, MAX и 1С уехали бы в VPN, а Telegram остался прямым.
        """
        r = self._tun(stubs, tmp_path, AG_STUB_IGNORE_SET="1")
        assert r.returncode != 0
        out = r.stdout + r.stderr
        assert "GENERAL" in out or "general" in out
        assert "только Telegram" not in r.stdout      # успех не заявлен
        assert "TUN + selective" not in r.stdout

    def test_general_rejection_names_the_fix(self, stubs, tmp_path):
        """Сообщение содержит готовую команду, а не «неизвестная ошибка»."""
        r = self._tun(stubs, tmp_path, AG_STUB_IGNORE_SET="1")
        assert "site-exclusions mode selective" in (r.stdout + r.stderr)

    def test_api_domain_alone_does_not_prove_selective(self, stubs, tmp_path):
        """Доказательство, что прежней проверки было НЕДОСТАТОЧНО.

        Домен уже лежит в списке исключений (в общем), `site-exclusions show`
        его показывает — прежняя обязательная проверка прошла бы. Но режим
        general, а значит смысл списка противоположный: в VPN уходит всё,
        КРОМЕ Telegram. Фаза обязана упасть именно по режиму.
        """
        open(stubs["excl"], "w").write("api.telegram.org\n")
        r = self._tun(stubs, tmp_path, AG_STUB_IGNORE_SET="1")
        assert r.returncode != 0
        out = r.stdout + r.stderr
        assert "GENERAL" in out or "general" in out

    def test_fails_before_half_configuring(self, stubs, tmp_path):
        """Отказ по режиму — ДО добавления доменов: сервер не остаётся
        наполовину настроенным."""
        r = self._tun(stubs, tmp_path, AG_STUB_IGNORE_SET="1")
        assert r.returncode != 0
        assert "site-exclusions add" not in _read(stubs["log"])

    def test_set_mode_and_routing_still_applied(self, stubs, tmp_path):
        """Проверка режима не отменила саму настройку TUN."""
        r = self._tun(stubs, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        lines = _read(stubs["log"]).splitlines()
        assert "config set-mode TUN" in lines
        assert "config set-tun-routing-mode AUTO" in lines
        assert "SCRIPT" not in _read(stubs["log"])

    def test_empty_mode_answer_is_rejected(self, stubs, tmp_path):
        """Пустой/невнятный ответ CLI — тоже отказ (нельзя догадываться)."""
        open(stubs["mode"], "w").write("какая-то ерунда\n")
        cwd = tmp_path / "cwd2"
        cwd.mkdir(exist_ok=True)
        # AG_STUB_IGNORE_SET запрещает перезапись, ответ остаётся невнятным
        r = _sudo_run(stubs, [], "tun", cwd=str(cwd),
                      extra_env={"AG_STUB_IGNORE_SET": "1"})
        assert r.returncode != 0
        assert "selective" in (r.stdout + r.stderr)


# ==============================================================================
#  2) active != TUN: оставшийся SOCKS-слушатель ловится
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
class TestTunnelModeVerifiedLive:
    @pytest.fixture()
    def redirected(self, stubs, tmp_path):
        """Копия помощника с перенаправленными системными каталогами.

        Фаза units раскладывает юниты по /etc/systemd/system и
        /usr/local/sbin — в тесте их трогать нельзя. Подменяются ТОЛЬКО пути,
        проверяемая логика остаётся той же.
        """
        kit = tmp_path / "kit"
        etc = tmp_path / "etc"
        sbin = tmp_path / "sbin"
        for d in (kit, etc, sbin):
            d.mkdir()
        for n in ("adguard-vpn-telegram.service", "tg-health.service",
                  "tg-health.timer", "tg-health.sh",
                  "ymaster-adguard-setup.sh"):
            (kit / n).write_text("[Unit]\nDescription=stub\n", encoding="utf-8")
        src = _read(HELPER_SH)
        src = src.replace("/usr/local/share/ymaster-adguard", str(kit))
        src = src.replace("/etc/systemd/system", str(etc))
        src = src.replace("/usr/local/sbin", str(sbin))
        src = src.replace("/etc/sudoers.d/ymaster-adguard", str(tmp_path / "sudoers"))
        script = tmp_path / "helper.sh"
        script.write_text(src, encoding="utf-8")
        return {"script": str(script), "etc": etc, "sbin": sbin, "kit": kit}

    def test_socks_listener_left_behind_is_fatal(self, stubs, tmp_path, redirected):
        """Юнит active, но порт 1080 слушается → сервер остался в SOCKS.

        В этом режиме прозрачно в туннель не уходит ничего, а поле «Прокси»
        мастер уже очистил — бот пошёл бы напрямую, то есть в блокировку.
        """
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
        assert r.returncode != 0
        assert "SOCKS" in (r.stdout + r.stderr)
        assert "1080" in (r.stdout + r.stderr)

    def test_no_socks_listener_units_succeed(self, stubs, tmp_path, redirected):
        """Без слушателя на 1080 фаза units проходит и рапортует успех."""
        open(stubs["state"], "w").write("active\n")
        # порт гарантированно свободен
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        assert probe.connect_ex(("127.0.0.1", 1080)) != 0
        probe.close()
        r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                      script=redirected["script"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "туннель активен" in r.stdout

    def test_units_are_deployed_from_kit(self, stubs, tmp_path, redirected):
        """Юниты действительно раскладываются, а не только проверяются."""
        open(stubs["state"], "w").write("active\n")
        r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                      script=redirected["script"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert (redirected["etc"] / "adguard-vpn-telegram.service").exists()
        assert (redirected["sbin"] / "tg-health.sh").exists()

    def test_dead_tunnel_message_mentions_license(self, stubs, tmp_path, redirected):
        """Частая причина «туннель не поднялся» — нет лицензии: говорим о ней."""
        open(stubs["state"], "w").write("inactive\n")
        r = _sudo_run(stubs, [], "units", cwd=str(tmp_path),
                      script=redirected["script"])
        assert r.returncode != 0
        assert "лиценз" in (r.stdout + r.stderr)


# ==============================================================================
#  3) Выключение: автопилот обязан быть снят, иначе красная кнопка врёт
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
class TestOffVerifiesAutopilotLive:
    def test_off_ok_when_timer_really_gone(self, stubs, tmp_path):
        open(stubs["state"], "w").write("inactive\n")
        open(stubs["timer_active"], "w").write("inactive\n")
        open(stubs["timer_enabled"], "w").write("disabled\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "выключен" in r.stdout

    def test_off_fails_when_timer_still_enabled(self, stubs, tmp_path):
        """Таймер выжил → в 15:10 VPN поднялся бы сам. Молчать нельзя."""
        open(stubs["state"], "w").write("inactive\n")
        open(stubs["timer_active"], "w").write("inactive\n")
        open(stubs["timer_enabled"], "w").write("enabled\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode != 0
        out = r.stdout + r.stderr
        assert "автопилот не отключился" in out
        assert "выключен" not in r.stdout

    def test_off_fails_when_timer_still_active(self, stubs, tmp_path):
        open(stubs["state"], "w").write("inactive\n")
        open(stubs["timer_active"], "w").write("active\n")
        open(stubs["timer_enabled"], "w").write("disabled\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode != 0
        assert "автопилот" in (r.stdout + r.stderr)

    def test_off_failure_names_the_fix(self, stubs, tmp_path):
        open(stubs["state"], "w").write("inactive\n")
        open(stubs["timer_enabled"], "w").write("enabled\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert "systemctl disable --now tg-health.timer" in (r.stdout + r.stderr)

    def test_timer_still_killed_before_tunnel(self, stubs, tmp_path):
        """Порядок из v1.70.25 сохранён: сначала автопилот, потом туннель."""
        open(stubs["state"], "w").write("inactive\n")
        r = _sudo_run(stubs, [], "off", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        dis = [l for l in _read(stubs["log"]).splitlines()
               if l.startswith("systemctl disable")]
        assert len(dis) == 2, dis
        assert "tg-health.timer" in dis[0], dis
        assert "adguard-vpn-telegram" in dis[1], dis


# ==============================================================================
#  4) Автопилот не воскрешает выключенный владельцем VPN
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
        # путь заведомо мёртв: автопилот ОБЯЗАН был бы менять локацию
        return subprocess.run(["bash", os.path.abspath(HEALTH_SH)],
                              capture_output=True, text=True, timeout=120,
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
    def test_unknown_answer_does_not_disable_autopilot(self, stubs, tmp_path):
        """Ответ не распознан (systemctl недоступен) — работаем как раньше.

        Иначе автопилот молча переставал бы делать хоть что-то.
        """
        r = self._run_health(stubs, tmp_path, "static")
        assert "выключен владельцем" not in r.stdout
        # с несуществующим CLI скрипт доходит до подбора локации и честно
        # сообщает, что список локаций недоступен
        assert "подбираем локацию" in r.stdout or "недоступен" in r.stdout

    def test_header_describes_tun_path(self):
        """Шапка больше не утверждает, что замер идёт через локальный прокси."""
        sh = _read(HEALTH_SH)
        head = sh[:sh.index("set -u")]
        assert "TUN" in head
        assert "через локальный прокси\n#      AdGuard VPN" not in head

    def test_guard_checks_answer_not_exit_code(self):
        """Проверка по ТЕКСТУ ответа: код возврата systemctl не повод молчать."""
        sh = _read(HEALTH_SH)
        assert re.search(r'systemctl is-enabled adguard-vpn-telegram', sh)
        assert '"$EN" = "disabled"' in sh
        assert '"$EN" = "masked"' in sh


# ==============================================================================
#  5) /dev/net/tun — до установки CLI и до ссылки входа
# ==============================================================================
class TestTunDeviceCheckedEarly:
    def test_preflight_phase_checks_tun(self):
        sh = _read(HELPER_SH)
        pf = sh[sh.index("phase_preflight()"):sh.index("phase_version()")]
        assert "/dev/net/tun" in pf

    def test_preflight_checks_tun_before_kit(self):
        """Проверка TUN идёт раньше «комплект не разложен» — она важнее."""
        sh = _read(HELPER_SH)
        pf = sh[sh.index("phase_preflight()"):sh.index("phase_version()")]
        assert pf.index("/dev/net/tun") < pf.index("KIT_DIR")

    def test_python_precheck_checks_tun(self, monkeypatch):
        """Своя проверка в программе: фаза помощника зовётся лишь в режиме
        helper, а сказать владельцу надо при любом режиме прав."""
        real = os.path.exists

        def fake(p):
            return False if p == "/dev/net/tun" else real(p)

        monkeypatch.setattr(ag.os.path, "exists", fake)
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/" + n)
        ag.job.reset()
        try:
            with pytest.raises(RuntimeError) as ei:
                ag._step_precheck()
        finally:
            ag.job.reset()
        assert "/dev/net/tun" in str(ei.value)

    def test_python_precheck_passes_when_tun_present(self, monkeypatch):
        """Небезразличность: при наличии устройства шаг не падает по этой
        причине (дальше он упрётся в права, которых в тесте нет)."""
        real = os.path.exists
        monkeypatch.setattr(ag.os.path, "exists", lambda p: True
                            if p == "/dev/net/tun" else real(p))
        monkeypatch.setattr(ag, "sys_platform_linux", lambda: True)
        monkeypatch.setattr(ag, "run_cmd", lambda *a, **k: (0, "systemd 255"))
        monkeypatch.setattr(ag.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(ag, "_detect_rights", lambda: ("", ""))
        ag.job.reset()
        try:
            with pytest.raises(RuntimeError) as ei:
                ag._step_precheck()
        finally:
            ag.job.reset()
        assert "/dev/net/tun" not in str(ei.value)

    def test_tun_phase_keeps_its_own_guard(self):
        """Страховка в самой фазе tun осталась (защита в глубину)."""
        sh = _read(HELPER_SH)
        tun = sh[sh.index("phase_tun()"):sh.index("phase_units()")]
        assert "/dev/net/tun" in tun


# ==============================================================================
#  6) install-rule освежает разложенные юниты (deploy.sh --update)
# ==============================================================================
class TestInstallRuleRefreshesDeployedUnits:
    def test_refresh_block_present_and_guarded(self):
        sh = _read(HELPER_SH)
        ir = sh[sh.index("phase_install_rule()"):sh.index("phase_remove_rule()")]
        assert "/etc/systemd/system/${TUNNEL_UNIT}.service" in ir
        assert "UNITS_ETC" in ir and "UNITS_SBIN" in ir

    def test_uses_try_restart_not_restart(self):
        """VPN, выключенный владельцем, включать обратно нельзя."""
        sh = _read(HELPER_SH)
        ir = sh[sh.index("phase_install_rule()"):sh.index("phase_remove_rule()")]
        assert "try-restart" in ir
        assert not re.search(r'systemctl restart "\$TUNNEL_UNIT"', ir)

    def test_refresh_only_when_already_installed(self):
        """Первая установка не трогает систему раньше времени."""
        sh = _read(HELPER_SH)
        ir = sh[sh.index("phase_install_rule()"):sh.index("phase_remove_rule()")]
        guard = ir.index('if [ -f "/etc/systemd/system/${TUNNEL_UNIT}.service" ]')
        assert ir.index("UNITS_ETC", guard) > guard

    @pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
    @pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
    def test_refresh_happens_live(self, stubs, tmp_path):
        """Живой прогон: юнит уже стоит → файл обновляется содержимым комплекта."""
        kit = tmp_path / "kit"
        etc = tmp_path / "etc"
        sbin = tmp_path / "sbin"
        appdir = tmp_path / "app"
        for d in (kit, etc, sbin, appdir / "deploy" / "adguard-vpn"):
            d.mkdir(parents=True)
        marker = "[Unit]\nDescription=NEW-FROM-KIT\n"
        for n in ("adguard-vpn-telegram.service", "tg-health.service",
                  "tg-health.timer"):
            (etc / n).write_text("[Unit]\nDescription=OLD\n", encoding="utf-8")
        (sbin / "tg-health.sh").write_text("#!/bin/sh\n# OLD\n", encoding="utf-8")
        src = _read(HELPER_SH)
        src = src.replace("/usr/local/share/ymaster-adguard", str(kit))
        src = src.replace("/etc/systemd/system", str(etc))
        src = src.replace("/usr/local/sbin", str(sbin))
        src = src.replace("/etc/sudoers.d/ymaster-adguard", str(tmp_path / "sudoers"))
        script = appdir / "deploy" / "adguard-vpn" / "ymaster-adguard-setup.sh"
        script.write_text(src, encoding="utf-8")
        for n in ("adguard-vpn-telegram.service", "tg-health.service",
                  "tg-health.timer"):
            (script.parent / n).write_text(marker, encoding="utf-8")
        (script.parent / "tg-health.sh").write_text("#!/bin/sh\n# NEW\n",
                                                    encoding="utf-8")
        r = _sudo_run(stubs, [str(appdir)], "install-rule", cwd=str(tmp_path),
                      script=str(script))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "освежаю" in r.stdout
        assert "NEW-FROM-KIT" in (etc / "adguard-vpn-telegram.service").read_text(
            encoding="utf-8")
        assert "# NEW" in (sbin / "tg-health.sh").read_text(encoding="utf-8")

    @pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
    @pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
    def test_no_refresh_on_fresh_install(self, stubs, tmp_path):
        """Юнита ещё нет → ничего не раскладываем и не перезапускаем."""
        kit = tmp_path / "kit2"
        etc = tmp_path / "etc2"
        sbin = tmp_path / "sbin2"
        appdir = tmp_path / "app2"
        for d in (kit, etc, sbin, appdir / "deploy" / "adguard-vpn"):
            d.mkdir(parents=True)
        src = _read(HELPER_SH)
        src = src.replace("/usr/local/share/ymaster-adguard", str(kit))
        src = src.replace("/etc/systemd/system", str(etc))
        src = src.replace("/usr/local/sbin", str(sbin))
        src = src.replace("/etc/sudoers.d/ymaster-adguard", str(tmp_path / "sudoers2"))
        script = appdir / "deploy" / "adguard-vpn" / "ymaster-adguard-setup.sh"
        script.write_text(src, encoding="utf-8")
        for n in ("adguard-vpn-telegram.service", "tg-health.service",
                  "tg-health.timer", "tg-health.sh"):
            (script.parent / n).write_text("[Unit]\n", encoding="utf-8")
        r = _sudo_run(stubs, [str(appdir)], "install-rule", cwd=str(tmp_path),
                      script=str(script))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "освежаю" not in r.stdout
        assert not (etc / "adguard-vpn-telegram.service").exists()


# ==============================================================================
#  7) Набор фаз не изменился: установленное правило продолжает работать
# ==============================================================================
class TestPhaseSetUnchanged:
    def test_allowed_phases_same_as_17025(self):
        sh = _read(HELPER_SH)
        m = re.search(r'ALLOWED_PHASES="([^"]+)"', sh)
        assert m, "ALLOWED_PHASES не найден"
        assert m.group(1) == PHASES_17025

    def test_helper_phases_same_as_17025(self):
        assert tuple(ag.HELPER_PHASES) == tuple(PHASES_17025.split())

    def test_no_new_phase_added(self):
        """Иначе sudoers-правило на сервере пришлось бы переустанавливать."""
        sh = _read(HELPER_SH)
        allowed = set(re.search(r'ALLOWED_PHASES="([^"]+)"', sh).group(1).split())
        dispatched = set(re.findall(r'^\s{4}([a-z-]+)\)\s+phase_', sh, re.M))
        # install-rule/remove-rule — ручные, их в правиле нет намеренно
        assert dispatched - {"install-rule", "remove-rule"} == allowed

    def test_step_order_unchanged(self):
        assert ag.STEP_ORDER == ["precheck", "cli", "login", "tun", "tunnel",
                                 "autopilot", "verify", "done"]


# ==============================================================================
#  8) Документация и чистка
# ==============================================================================
class TestDocsAndCleanup:
    def test_readme_lists_real_phases(self):
        md = _read(README)
        line = [l for l in md.splitlines() if "ymaster-adguard-setup preflight" in l]
        assert line, "строка с перечнем фаз не найдена"
        assert "socks" not in line[0]
        for ph in ("tun", "off", "units", "autopilot"):
            assert ph in line[0], line[0]

    def test_readme_has_no_stale_socks_phase(self):
        """Фазы `socks` больше нет — в инструкции её быть не должно."""
        md = _read(README)
        assert "ymaster-adguard-setup socks" not in md
        assert not re.search(r'\|\s*socks\s*\|', md)

    def test_helper_has_no_stale_synonym_claim(self):
        """Комментарий «программа по-прежнему вызывает фазу socks» — неправда."""
        sh = _read(HELPER_SH)
        assert "по-прежнему вызывает фазу" not in sh
        assert "принято как синоним" not in sh

    def test_readme_documents_self_checks(self):
        md = _read(README)
        assert "Что мастер проверяет сам" in md
        assert "selective" in md
        assert "try-restart" in md

    def test_autopilot_no_redundant_enable(self):
        """Лишнее убрано: `enable` перед `enable --now` был повтором."""
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

    def test_constants_used(self):
        src = _read("app/services/adguard_setup.py")
        assert "TUNNEL_UNIT" in src and "TIMER_UNIT" in src


# ==============================================================================
#  9) Версия и синхронизация
# ==============================================================================
class TestVersions17026:
    def test_app_version(self):
        assert settings.APP_VERSION == VER

    def test_sync_points_have_no_previous_version(self):
        for p in SYNC_POINTS:
            assert PREV not in _read(p), p

    def test_sync_points_have_new_version(self):
        for p in SYNC_POINTS:
            assert VER in _read(p), p

    def test_changelog_entry_once(self):
        md = _read(CHANGELOG)
        assert md.count(f"## [{VER}]") == 1

    def test_changelog_new_entry_is_first(self):
        md = _read(CHANGELOG)
        assert md.index(f"## [{VER}]") < md.index(f"## [{PREV}]")

    def test_changelog_mentions_key_fixes(self):
        md = _read(CHANGELOG)
        sec = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        for needle in ("selective", "1080", "try-restart", "install-rule",
                       "/dev/net/tun", "socks"):
            assert needle in sec, needle

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        assert f"'{VER}': [" in js

    def test_whats_new_is_user_facing(self):
        js = _read(APP_JS)
        i = js.index(f"'{VER}': [")
        block = js[i:i + 3000]
        assert "Telegram" in block
        assert "selective" in block or "только Telegram" in block
