# -*- coding: utf-8 -*-
"""v1.70.31 — отказ туннеля откатывается: сервер не остаётся без сети.

Третий живой прогон на сервере владельца исключил три причины подряд:

    sudo adguardvpn-cli license
      You are using the PREMIUM version
      Your subscription is valid until 2028-10-14      ← лицензия в порядке

    sudo adguardvpn-cli config set-protocol http2
      Protocol: http2                                   ← применено
    sudo adguardvpn-cli connect -4
      ERROR [549944] UDP_SOCKET udp_so…                 ← UDP-ошибки остались
      Failed to start the VPN service in the background: Disconnected

Ошибки UDP_SOCKET сохранились даже на http2, а принудительный IPv4 не помог —
значит дело не в протоколе и не в IPv6. Итоговая строка отказа совпала
дословно с открытым обращением AdguardTeam/AdGuardVPNCLI#161: CLI v1.7.12 в
режиме TUN с маршрутизацией auto поднимает tun0, но не исключает из него
собственный трафик и отбрасывает свои же соединения к конечным точкам.

Отсюда второе, более важное следствие. К моменту, когда мастер понимал, что
туннель не поднялся, служба УЖЕ успевала выполнить `connect --no-fork`: в
TUN-режиме это создаёт tun0, правит маршруты и — при `Change system DNS: on`, а
оно у владельца включено — уводит DNS сервера в неподнятый туннель. При
`Restart=always` и только что включённом автозапуске сервер остался бы с чёрной
дырой вместо сети и получил бы её же после перезагрузки. Вместе с сетью легли бы
приём чеков и телеграм-бот — то есть программа, ради которой всё затевалось.
Владелец этот приоритет сформулировал прямо: «главное не сломай нашу программу
это важнее».

Что здесь доказано:
  1) ЖИВЫМ прогоном фазы `units` с заглушками: при неподнявшемся туннеле мастер
     печатает журнал службы, затем ОТКАТЫВАЕТСЯ — гасит службу и выполняет
     `disconnect` — и только потом сообщает об отказе;
  2) порядок отката правильный: сначала `disable --now`, потом `disconnect`
     (иначе `Restart=always` перезапустит `connect` и снова поднимет tun0 уже
     после уборки);
  3) ни одна команда отката не может уронить фазу раньше содержательного
     сообщения;
  4) пятая причина (#161) и её признак в журнале названы;
  5) канал обновлений и режим маршрутизации программа сама НЕ меняет;
  6) README, CHANGELOG, «Что нового» и синхронизация версий.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

APP_JS = "app/static/js/app.js"
CHANGELOG = "CHANGELOG.md"
README = "deploy/adguard-vpn/README.md"
HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
VER = "1.70.31"
PREV = "1.70.30"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Признак из обращения #161 — ровно та строка, что пришла с живого сервера.
DISCONNECTED = ("Failed to start the VPN service in the background: "
                "Disconnected")

KIT_FILES = ("adguard-vpn-telegram.service", "tg-health.service",
             "tg-health.timer", "tg-health.sh")


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


CLI_STUB = r"""#!/bin/sh
printf '%s\n' "cli $*" >> "$AG_STUB_LOG"
exit 0
"""

# Туннель НЕ поднимается: is-active всегда «inactive» — ровно та ситуация,
# которую мастер обязан корректно откатать.
SYSTEMCTL_STUB = r"""#!/bin/sh
printf '%s\n' "systemctl $*" >> "$STUB_LOG"
if [ "$1" = "is-active" ]; then
  echo inactive
elif [ "$1" = "is-enabled" ]; then
  echo enabled
fi
exit 0
"""

# Журнал службы с настоящим признаком из #161 — проверяем, что он доезжает до
# панели, а не остаётся в SSH.
JOURNALCTL_STUB = r"""#!/bin/sh
printf '%s\n' "journalctl $*" >> "$STUB_LOG"
echo 'Oct 19 01:08:54 server ERROR UDP_SOCKET udp_socket'
echo 'Oct 19 01:09:14 server Failed to start the VPN service in the background: Disconnected'
exit 0
"""


def _units_phase():
    sh = _read(HELPER_SH)
    return sh[sh.index("phase_units()"):sh.index("phase_autopilot()")]


def _failure_branch():
    """Ветка отказа: от проверки is-active до die включительно."""
    u = _units_phase()
    return u[u.index('!= "active" ]; then'):]


@pytest.fixture()
def stubs(tmp_path):
    """Каталог заглушек + файлы журнала. Root-файлы чистятся через sudo."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("adguardvpn-cli", CLI_STUB),
                       ("systemctl", SYSTEMCTL_STUB),
                       ("journalctl", JOURNALCTL_STUB)):
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    yield {"bindir": str(bindir), "log": str(log)}
    if HAS_SUDO:
        subprocess.run(["sudo", "-n", "chown", "-R",
                        "%d:%d" % (os.getuid(), os.getgid()), str(tmp_path)],
                       capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL)


@pytest.fixture()
def redirected(stubs, tmp_path):
    """Копия помощника с перенаправленными системными каталогами.

    Фаза `units` раскладывает файлы по /etc/systemd/system и /usr/local/sbin —
    в тесте их трогать нельзя. Подменяются ТОЛЬКО пути и ТОЛЬКО длительность
    ожидания (30×2 с → 2×0 с), чтобы прогон не шёл минуту; проверяемая логика
    остаётся прежней.
    """
    kit, etc, sbin = (tmp_path / n for n in ("kit", "etc", "sbin"))
    for d in (kit, etc, sbin):
        d.mkdir()
    for n in KIT_FILES:
        (kit / n).write_text("[Unit]\nDescription=kit\n", encoding="utf-8")

    src = _read(HELPER_SH)
    for old, new in (("/usr/local/share/ymaster-adguard", str(kit)),
                     ("/etc/systemd/system", str(etc)),
                     ("/usr/local/sbin", str(sbin)),
                     ("/etc/sudoers.d/ymaster-adguard", str(tmp_path / "sudoers")),
                     # CLI_REAL несуществующий ⇒ ensure_link ничего не делает,
                     # а cli_path() падает на command -v и берёт заглушку из PATH
                     ("/usr/local/bin/adguardvpn-cli", str(tmp_path / "no-link")),
                     ("/opt/adguardvpn_cli/adguardvpn-cli", str(tmp_path / "no-real"))):
        src = src.replace(old, new)

    i, j = src.index("phase_units()"), src.index("phase_autopilot()")
    fast = src[i:j].replace("$(seq 1 30)", "$(seq 1 2)").replace("sleep 2", "sleep 0")
    src = src[:i] + fast + src[j:]

    script = tmp_path / "helper.sh"
    script.write_text(src, encoding="utf-8")
    return {"script": str(script), "stubs": stubs, "log": stubs["log"]}


def _sudo_run(redirected, phase):
    st = redirected["stubs"]
    cmd = ["sudo", "-n", "env",
           "PATH=" + st["bindir"] + ":" + os.environ.get("PATH", ""),
           "AG_STUB_LOG=" + st["log"],
           "STUB_LOG=" + st["log"],
           "bash", redirected["script"], phase]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          cwd=os.getcwd(), stdin=subprocess.DEVNULL)


def _calls(redirected):
    return _read(redirected["log"]).split("\n")


# ==============================================================================
#  1) ЖИВОЙ прогон: при неподнявшемся туннеле мастер откатывается
# ==============================================================================
@pytest.mark.skipif(not HAS_SUDO, reason="нет passwordless sudo")
@pytest.mark.skipif(not HAS_BASH, reason="bash недоступен")
class TestRollbackIsPerformedLive:
    def test_journal_reaches_the_panel(self, redirected):
        """Журнал службы доезжает до вывода мастера — вместе с настоящим
        признаком из #161, который владелец иначе искал бы в SSH."""
        r = _sudo_run(redirected, "units")
        assert r.returncode != 0, "отказ туннеля обязан быть отказом"
        assert "журнал службы" in r.stdout
        assert "UDP_SOCKET" in r.stdout
        assert DISCONNECTED in r.stdout
        assert any(c.startswith("journalctl -u adguard-vpn-telegram")
                   for c in _calls(redirected))

    def test_service_is_stopped_and_tunnel_disconnected(self, redirected):
        r = _sudo_run(redirected, "units")
        calls = _calls(redirected)
        assert any(c.startswith("systemctl disable --now adguard-vpn-telegram")
                   for c in calls), calls
        assert any(c == "cli disconnect" for c in calls), calls

    def test_rollback_order_service_first_then_disconnect(self, redirected):
        """Порядок важен: при Restart=always живая служба перезапустит `connect`
        и снова поднимет tun0 уже после уборки. Это тот же класс ошибок, что и
        при выключении, — сначала гасим то, что может воскресить туннель."""
        _sudo_run(redirected, "units")
        calls = _calls(redirected)
        i_stop = next(i for i, c in enumerate(calls)
                      if c.startswith("systemctl disable --now"))
        i_disc = next(i for i, c in enumerate(calls) if c == "cli disconnect")
        assert i_stop < i_disc, calls

    def test_owner_is_told_server_is_intact(self, redirected):
        r = _sudo_run(redirected, "units")
        assert "откатываю туннель" in r.stdout
        assert "в прежнем состоянии" in r.stdout
        assert "откатан" in r.stdout, "в тексте отказа должно быть сказано, что выполнение откатано"

    def test_failure_names_issue_161(self, redirected):
        r = _sudo_run(redirected, "units")
        assert "#161" in r.stdout
        assert "v1.7.12" in r.stdout

    def test_negative_control_success_path_does_not_roll_back(self, redirected):
        """Отрицательный контроль: на поднявшемся туннеле отката нет.

        Без этой проверки «откат выполнен» могло бы означать, что мастер
        выполняет disable/disconnect вообще всегда.
        """
        # заставляем is-active ответить «active» — туннель поднялся
        p = os.path.join(redirected["stubs"]["bindir"], "systemctl")
        open(p, "w", encoding="utf-8").write(
            SYSTEMCTL_STUB.replace("echo inactive", "echo active"))
        os.chmod(p, 0o755)
        r = _sudo_run(redirected, "units")
        calls = _calls(redirected)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "туннель активен" in r.stdout
        assert not any(c == "cli disconnect" for c in calls), calls
        assert not any(c.startswith("systemctl disable") for c in calls), calls


# ==============================================================================
#  2) Статика: откат устроен правильно
# ==============================================================================
class TestRollbackIsBuiltCorrectly:
    def test_cleanup_commands_cannot_fail_the_phase(self):
        """Откат не имеет права превращаться в новый отказ."""
        br = _failure_branch()
        for cmd in ('systemctl disable --now "$TUNNEL_UNIT"',
                    '"$cli" disconnect'):
            line = next(l for l in br.split("\n") if cmd in l)
            assert "|| true" in line, line

    def test_cleanup_happens_before_die(self):
        br = _failure_branch()
        assert br.index('systemctl disable --now') < br.index('die "туннель не поднялся')
        assert br.index('"$cli" disconnect') < br.index('die "туннель не поднялся')

    def test_cli_lookup_is_guarded(self):
        """Если CLI вдруг не найден, откат не должен уронить фазу на `local`."""
        br = _failure_branch()
        assert 'if cli="$(cli_path' in br
        assert "local cli" in br

    def test_journal_dump_is_optional(self):
        assert "command -v journalctl" in _failure_branch()

    def test_die_count_unchanged(self):
        """Откат объясняет отказ, а не добавляет новые фатальные исходы."""
        assert _units_phase().count("die ") == 5

    def test_success_path_untouched(self):
        u = _units_phase()
        assert "seq 1 30" in u, "ожидание 60 с (v1.70.27) должно остаться"
        assert "за 60 с" in u
        assert u.count('systemctl restart "$TUNNEL_UNIT"') == 1

    def test_helper_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr

    def test_phase_set_unchanged(self):
        """Набор фаз не менялся — установленное правило sudoers живо."""
        m = re.search(r'^ALLOWED_PHASES="([^"]*)"', _read(HELPER_SH), re.M)
        assert m
        assert m.group(1).split() == [
            "preflight", "rights", "install", "login", "tun", "off", "units",
            "autopilot", "version", "status"]


# ==============================================================================
#  3) Программа не принимает спорных решений за владельца
# ==============================================================================
class TestNoBlindDecisions:
    def test_update_channel_is_not_switched(self):
        """Переход на beta/nightly на боевом сервере — решение владельца."""
        sh = _read(HELPER_SH)
        assert '"$cli" config set-update-channel' not in sh
        assert "scripts/beta/install.sh" not in sh
        assert "scripts/nightly/install.sh" not in sh

    def test_routing_mode_is_not_switched(self):
        """`none` в #161 не помог, `script` сломан (#69) — менять их вслепую
        значит получать другую поломку вместо диагноза."""
        sh = _read(HELPER_SH)
        assert "config set-tun-routing-mode none" not in sh
        assert "config set-tun-routing-mode SCRIPT" not in sh
        assert "config set-tun-routing-mode script" not in sh
        tun = sh[sh.index("phase_tun()"):sh.index("phase_units()")]
        assert "config set-tun-routing-mode AUTO" in tun

    def test_system_dns_is_not_changed_behind_the_owner(self):
        sh = _read(HELPER_SH)
        assert "set-change-system-dns" not in sh

    def test_readme_leaves_channel_choice_to_owner(self):
        rd = _read(README)
        assert "решение владельца" in rd or "решает владелец" in rd


# ==============================================================================
#  4) Документация
# ==============================================================================
class TestDocs:
    def test_readme_documents_issue_161(self):
        rd = _read(README)
        assert "#161" in rd
        assert DISCONNECTED in rd
        assert "tun0" in rd
        assert "Rejecting connection redirected to endpoint" in rd
        assert "None of the endpoints are available" in rd

    def test_readme_documents_channels_honestly(self):
        """В release-канале новее v1.7.12 сборок нет — это надо сказать прямо,
        иначе владелец будет ждать, что check-update что-то предложит."""
        rd = _read(README)
        assert "release" in rd.lower()
        assert "v1.7.12" in rd
        assert "beta" in rd and "nightly" in rd
        assert "scripts/beta/install.sh" in rd

    def test_readme_documents_rollback(self):
        rd = _read(README)
        assert "Откат при отказе" in rd
        assert "disable --now" in rd
        assert "disconnect" in rd
        assert "Restart=always" in rd
        assert "Change system DNS" in rd

    def test_readme_keeps_toggle_wording(self):
        rd = _read(README)
        assert "трёхпозиционный переключатель" in rd
        assert "влево" in rd and "вправо" in rd

    def test_changelog_entry(self):
        md = _read(CHANGELOG)
        assert md.count(f"## [{VER}]") == 1
        assert md.index(f"## [{VER}]") < md.index(f"## [{PREV}]")
        entry = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        for needle in ("#161", DISCONNECTED, "PREMIUM", "set-protocol http2",
                       "connect -4", "disconnect", "Restart=always"):
            assert needle in entry, needle

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        assert f"'{VER}':" in js
        assert js.index(f"'{VER}':") < js.index(f"'{PREV}':")
        entry = js[js.index(f"'{VER}':"):js.index(f"'{PREV}':")]
        assert "#161" in entry
        assert "чёрной дырой" in entry

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  5) Версия
# ==============================================================================
class TestVersion17031:
    def test_app_version(self):
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= \
            (1, 70, 31)

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_matches_current_version(self, path):
        assert settings.APP_VERSION in _read(path), path

    def test_helper_syntax_py(self):
        import ast
        ast.parse(_read("app/services/adguard_setup.py"))
