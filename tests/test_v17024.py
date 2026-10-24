# -*- coding: utf-8 -*-
"""v1.70.24 — AdGuard VPN: SOCKS5 → TUN + selective-исключения.

Через VPN идёт только Telegram; прочий трафик сервера остаётся прямым.

Что здесь доказано:
  1) помощник realmente выполняет TUN-последовательность (живой прогон на
     stub-CLI под sudo): set-mode TUN → set-tun-routing-mode AUTO →
     site-exclusions mode selective → add <домены>;
  2) wildcards передаются ЛИТЕРАЛЬНОМ (`*.telegram.org`), а не раскрытыми
     шеллом — AdGuard не наследует поддомены сам (issue #148), и раскрытый
     glob добавил бы в исключения имена файлов из cwd;
  3) фаза `socks` сохранена как синоним `tun`: уже установленные на серверах
     sudoers-правила перечисляют фазы явно, а обновление из приложения
     install-rule не перезапускает;
  4) режим SCRIPT (самодельные маршруты по CIDR) не используется — он не
     работает (AdguardTeam/AdGuardVPNCLI#69, #190);
  5) прокси в программе очищается, а не подставляется: в TUN нет слушателя
     127.0.0.1:1080, и старое значение отправило бы бота в мёртвый порт;
  6) автопилот tg-health.sh определяет режим сам и не слепнет в TUN;
  7) панель не подставляет socks5://127.0.0.1:1080 после установки.

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

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
README = "deploy/adguard-vpn/README.md"
TUNNEL_UNIT = "deploy/adguard-vpn/adguard-vpn-telegram.service"
TG_HEALTH = "deploy/adguard-vpn/tg-health.sh"
APP_JS = "app/static/js/app.js"
MANUAL = "app/services/manual_content.py"
VER = "1.70.24"
PREV = "1.70.23"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Stub adguardvpn-cli: пишет полученные аргументы в лог и хранит добавленные
# исключения, чтобы `site-exclusions show` отвечал правдоподобно.
CLI_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$AG_STUB_LOG"
if [ "$1" = "site-exclusions" ] && [ "$2" = "add" ]; then
  shift 2
  for d in "$@"; do printf '%s\n' "$d" >> "$AG_STUB_STATE"; done
fi
if [ "$1" = "site-exclusions" ] && [ "$2" = "show" ]; then
  [ -f "$AG_STUB_STATE" ] && cat "$AG_STUB_STATE"
fi
# v1.70.26: `site-exclusions mode` БЕЗ аргументов печатает текущий режим —
# так ведёт себя настоящий CLI (README AdGuardVPNCLI). Помощник перечитывает
# режим, чтобы не принять general за selective: смысл списка при этом
# противоположный (в VPN всё, КРОМЕ перечисленного). Стартовое значение —
# general, как у CLI по умолчанию, поэтому тест небезразличен к проверке.
if [ "$1" = "site-exclusions" ] && [ "$2" = "mode" ]; then
  if [ "${3:-}" = "selective" ] || [ "${3:-}" = "general" ]; then
    printf '%s\n' "$3" > "$AG_STUB_MODE"
  else
    m=$(cat "$AG_STUB_MODE" 2>/dev/null)
    printf '%s\n' "${m:-general}"
  fi
fi
if [ "$1" = "--version" ]; then echo "adguardvpn-cli 1.7.12"; fi
exit 0
"""


def _read(path):
    return open(path, encoding="utf-8").read()


def _sudo_ok():
    """Есть ли passwordless sudo (нужен для живого прогона фазы)."""
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
    return tmp_path


@pytest.fixture()
def stub_cli(tmp_path):
    """Каталог с подставным adguardvpn-cli + файлы лога/состояния."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "adguardvpn-cli"
    cli.write_text(CLI_STUB, encoding="utf-8")
    cli.chmod(0o755)
    log = tmp_path / "cli.log"
    state = tmp_path / "exclusions.txt"
    mode = tmp_path / "mode.txt"
    log.write_text("", encoding="utf-8")
    state.write_text("", encoding="utf-8")
    mode.write_text("", encoding="utf-8")
    return {"bindir": str(bindir), "log": str(log), "state": str(state),
            "mode": str(mode)}


def _run_phase(stub, phase, cwd):
    """Живой прогон фазы помощника от root (sudo) со stub-CLI в PATH.

    cwd намеренно заполнен файлами, похожими на маску glob: если wildcards
    в TG_DOMAINS не экранированы, шелл раскрыл бы `*.telegram.org` в эти имена
    и они уехали бы в исключения вместо домена.
    """
    for junk in ("x.telegram.org", "y.telegram.org"):
        open(os.path.join(cwd, junk), "w").close()
    env_path = stub["bindir"] + ":" + os.environ.get("PATH", "")
    cmd = ["sudo", "-n", "env",
           "PATH=" + env_path,
           "AG_STUB_LOG=" + stub["log"],
           "AG_STUB_STATE=" + stub["state"],
           "AG_STUB_MODE=" + stub["mode"],
           "bash", os.path.abspath(HELPER_SH), phase]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=cwd, stdin=subprocess.DEVNULL)


# ==============================================================================
#  1) Живой прогон: помощник действительно настраивает TUN + selective
# ==============================================================================
@pytest.mark.skipif(not _sudo_ok(), reason="нет passwordless sudo")
@pytest.mark.skipif(not os.path.exists("/dev/net/tun"),
                    reason="нет /dev/net/tun — TUN-режим недоступен")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
class TestPhaseTunLive:
    def test_tun_sequence_for_phase_tun(self, stub_cli, tmp_path):
        """Фаза `tun` (её зовёт программа) настраивает TUN + selective."""
        cwd = str(tmp_path / "cwd")
        os.makedirs(cwd)
        r = _run_phase(stub_cli, "tun", cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        lines = _read(stub_cli["log"]).splitlines()
        assert "config set-mode TUN" in lines
        assert "config set-tun-routing-mode AUTO" in lines
        assert "site-exclusions mode selective" in lines
        assert "site-exclusions add api.telegram.org" in lines

    def test_wildcards_passed_literally(self, stub_cli, tmp_path):
        """`*.telegram.org` доходит до CLI literally, а не glob-ом.

        В cwd лежат x.telegram.org и y.telegram.org: раскрытый шеллом glob
        добавил бы в исключения ИМЯ ФАЙЛА (или оба), и реальный домен
        оказался бы не покрыт.
        """
        cwd = str(tmp_path / "cwd")
        os.makedirs(cwd)
        r = _run_phase(stub_cli, "tun", cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        log = _read(stub_cli["log"])
        assert "site-exclusions add *.telegram.org" in log
        assert "x.telegram.org" not in log
        assert "y.telegram.org" not in log

    def test_bot_api_domain_is_in_exclusions(self, stub_cli, tmp_path):
        """Без api.telegram.org туннель бессмыслен — фаза обязана упасть."""
        cwd = str(tmp_path / "cwd")
        os.makedirs(cwd)
        _run_phase(stub_cli, "tun", cwd)
        assert "api.telegram.org" in _read(stub_cli["state"]).split()

    def test_idempotent_second_run(self, stub_cli, tmp_path):
        """Повторный запуск не ломает настройку (идемпотентность)."""
        cwd = str(tmp_path / "cwd")
        os.makedirs(cwd)
        first = _run_phase(stub_cli, "tun", cwd)
        second = _run_phase(stub_cli, "tun", cwd)
        assert first.returncode == 0 and second.returncode == 0, \
            second.stdout + second.stderr
        # домены не задублировались в списке исключений
        doms = _read(stub_cli["state"]).split()
        assert doms.count("api.telegram.org") == 1, doms

    def test_script_routing_mode_not_used(self, stub_cli, tmp_path):
        """Режим SCRIPT не вызывается: он не работает (#69, #190)."""
        cwd = str(tmp_path / "cwd")
        os.makedirs(cwd)
        r = _run_phase(stub_cli, "tun", cwd)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SCRIPT" not in _read(stub_cli["log"])
        assert "create-route-script" not in _read(stub_cli["log"])


# ==============================================================================
#  2) Статика помощника: список доменов, фазы, guard
# ==============================================================================
class TestHelperStatic:
    def test_tg_domains_declared(self):
        sh = _read(HELPER_SH)
        assert re.search(r"^TG_DOMAINS=\($", sh, re.M), \
            "список доменов Telegram должен быть одним массивом"
        for d in ("api.telegram.org", "telegram.org", "t.me", "telegram.me",
                  "telegram.dog", "core.telegram.org"):
            assert f'"{d}"' in sh, d

    def test_wildcards_quoted_in_source(self):
        """В массиве wildcards в кавычках — иначе их раскроет шелл."""
        sh = _read(HELPER_SH)
        assert '"*.telegram.org"' in sh and '"*.t.me"' in sh

    def test_bot_api_marked_mandatory(self):
        sh = _read(HELPER_SH)
        assert re.search(r'site-exclusions show.*\n.*api\.telegram\.org', sh) \
            or 'grep -q "api.telegram.org"' in sh, \
            "должна быть проверка, что Bot API попал в исключения"

    def test_phase_dispatched(self):
        sh = _read(HELPER_SH)
        assert re.search(r"^\s+tun\)\s+phase_tun ;;", sh, re.M)

    def test_allowed_phases_match_service(self):
        """v1.70.25: «socks» вычищен — два имени одного действия не нужны."""
        sh = _read(HELPER_SH)
        m = re.search(r'^ALLOWED_PHASES="([^"]+)"', sh, re.M)
        assert m, "не найден список разрешённых фаз"
        allowed = m.group(1).split()
        assert "socks" not in allowed
        assert "tun" in allowed and "off" in allowed
        assert "install-rule" not in allowed and "remove-rule" not in allowed
        assert set(allowed) == set(ag.HELPER_PHASES), allowed

    def test_tun_requires_dev_net_tun(self):
        sh = _read(HELPER_SH)
        assert "/dev/net/tun" in sh, \
            "без проверки /dev/net/tun отказ в контейнере будет непонятным"

    def test_routing_mode_is_auto(self):
        sh = _read(HELPER_SH)
        assert "set-tun-routing-mode AUTO" in sh
        assert "set-tun-routing-mode SCRIPT" not in sh
        assert "set-tun-routing-mode NONE" not in sh

    def test_socks_mode_no_longer_configured(self):
        """Прежняя команда set-mode SOCKS из фазы режима исчезла."""
        sh = _read(HELPER_SH)
        assert "config set-mode SOCKS" not in sh
        assert "config set-mode TUN" in sh

    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  3) Сервис: шаг режима унифицирован, прокси очищается
# ==============================================================================
class TestServiceStepTun:
    def test_step_order_uses_tun(self):
        assert "tun" in ag.STEP_ORDER and "socks" not in ag.STEP_ORDER
        assert ag.STEP_TITLE["tun"]

    def test_old_step_function_gone(self):
        assert not hasattr(ag, "_step_socks")
        assert hasattr(ag, "_step_tun")

    def test_step_tun_always_through_helper(self, monkeypatch, state_dir):
        """Во всех режимах прав — через помощника (один источник логики).

        Раньше в root/nopass команда set-mode выполнялась напрямую через
        run_cmd, и одна настройка жила в двух местах (Python и bash).
        """
        from app.database import SessionLocal
        calls = []
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=ag.CMD_TIMEOUT, stream=False,
                            mode=None:
                            calls.append(phase) or (0, "ok\nготово"))

        def _boom(*a, **k):
            raise AssertionError("шаг режима не должен ходить через run_cmd")
        monkeypatch.setattr(ag, "run_cmd", _boom)

        db = SessionLocal()
        try:
            for mode in ("root", "nopass", "helper"):
                calls.clear()
                ag.job.reset()
                ag.job.sudo_mode = mode
                ag._step_tun(db)
                assert calls == ["tun"], (mode, calls)
                assert ag.job.steps["tun"]["status"] == "done"
        finally:
            db.close()

    def test_step_tun_failure_reports_reason(self, monkeypatch, state_dir):
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=ag.CMD_TIMEOUT, stream=False:
                            (1, "set-mode TUN не удался"))
        ag.job.sudo_mode = "root"
        with pytest.raises(RuntimeError) as ei:
            ag._step_tun(None)
        msg = str(ei.value)
        assert "TUN" in msg and "set-mode TUN не удался" in msg

    def test_step_tun_empty_output_not_silent(self, monkeypatch, state_dir):
        """Пустой вывод не должен превращаться в сообщение-пустышку."""
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=ag.CMD_TIMEOUT, stream=False:
                            (1, ""))
        ag.job.sudo_mode = "root"
        with pytest.raises(RuntimeError) as ei:
            ag._step_tun(None)
        assert str(ei.value).strip().endswith(":") is False
        assert len(str(ei.value)) > len("Не включить режим TUN")


class TestProxyCleared:
    """v1.70.25: очистка прокси переехала из отдельного шага `proxy` в шаг
    `tun` — в TUN-режиме прокси как понятие не применяется, и отдельный шаг
    «Настройка программы (прокси не нужен)» только путал владельца."""

    def test_proxy_value_is_empty(self):
        assert ag.PROXY_VALUE == "", \
            "в TUN-режиме нет SOCKS-слушателя: прокси должен быть пустым"

    def test_no_separate_proxy_step(self):
        assert "proxy" not in ag.STEP_ORDER
        assert "proxy" not in ag.STEP_TITLE
        assert not hasattr(ag, "_step_proxy")

    def test_step_tun_clears_setting(self, state_dir, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=ag.CMD_TIMEOUT, stream=False,
                            mode=None: (0, "ok\nготово"))
        db = SessionLocal()
        try:
            appsettings.set_setting(db, ag.SETT_PROXY,
                                    "socks5://127.0.0.1:1080")
            ag.job.sudo_mode = "root"
            ag._step_tun(db)
            # get_setting подставляет default при ПУСТОМ значении, поэтому
            # возврат сенсинела и есть доказательство, что прокси очищен.
            assert appsettings.get_setting(db, ag.SETT_PROXY, "SENSEL") == \
                "SENSEL"
            assert ag.job.steps["tun"]["status"] == "done"
            assert any("убран" in line for line in ag.job.log), ag.job.log
        finally:
            db.close()

    def test_step_tun_warns_about_env_override(self, state_dir, monkeypatch):
        """TELEGRAM_PROXY переопределяет пустую настройку — шаг об этом пишет."""
        from app.database import SessionLocal
        monkeypatch.setattr(ag, "run_helper",
                            lambda phase, timeout=ag.CMD_TIMEOUT, stream=False,
                            mode=None: (0, "ok\nготово"))
        monkeypatch.setattr(settings, "TELEGRAM_PROXY",
                            "socks5://env:1080", raising=False)
        db = SessionLocal()
        try:
            ag.job.sudo_mode = "root"
            ag._step_tun(db)
            assert any("TELEGRAM_PROXY" in line for line in ag.job.log), \
                ag.job.log
        finally:
            db.close()

    def test_no_socks_proxy_left_in_service(self):
        src = _read("app/services/adguard_setup.py")
        assert 'PROXY_VALUE = "socks5://127.0.0.1:1080"' not in src


# ==============================================================================
#  4) Автопилот: замер не слепнет в TUN-режиме
# ==============================================================================
class TestAutopilotModeAware:
    def test_proxy_not_hardcoded(self):
        """По умолчанию прокси пуст; socks5h назначается ТОЛЬКО в ветке
        с проверкой слушателя — иначе в TUN замер всегда давал бы -1."""
        s = _read(TG_HEALTH)
        assert 'PROXY="${TG_PROXY:-}"' in s, \
            "значение по умолчанию должно быть пустым"
        i_default = s.index('PROXY="${TG_PROXY:-}"')
        i_probe = s.index("/dev/tcp/127.0.0.1/1080")
        i_socks = s.index('PROXY="socks5h://127.0.0.1:1080"')
        # порядок: пустое значение → проверка порта → назначение прокси
        assert i_default < i_probe < i_socks, (i_default, i_probe, i_socks)
        # назначение живёт внутри условной ветки, а не на верхнем уровне
        branch = s[i_probe:i_socks]
        assert "if [ -z \"$PROXY\" ]" in s[max(0, i_probe - 60):i_probe + 40]
        assert "\nfi" in s[i_socks:i_socks + 120]
        assert branch.count("PROXY=") == 0

    def test_detects_socks_listener(self):
        s = _read(TG_HEALTH)
        assert "/dev/tcp/127.0.0.1/1080" in s, \
            "режим должен определяться по наличию слушателя 1080"

    def test_can_measure_directly(self):
        """Есть ветка замера без -x (это и есть путь бота в TUN)."""
        s = _read(TG_HEALTH)
        assert re.search(r'if \[ -n "\$PROXY" \]', s)
        # обе ветки вызывают curl с тем же CHECK_URL
        assert s.count("$CHECK_URL") >= 2

    def test_overridable_by_env(self):
        assert 'PROXY="${TG_PROXY:-}"' in _read(TG_HEALTH)

    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", TG_HEALTH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr

    def test_autopilot_logic_preserved(self):
        """Проверка пинга и подбор локации не пострадали от смены режима."""
        s = _read(TG_HEALTH)
        assert "list-locations" in s
        assert "germany|герман" in s
        assert "TARGET_MS:-30" in s
        assert "connect -l" in s
        assert "time_total" in s


# ==============================================================================
#  5) UI: прокси не подставляется, текст карточки обновлён
# ==============================================================================
class TestUi:
    def test_no_proxy_prefill_after_success(self):
        js = _read(APP_JS)
        assert "prx.value = 'socks5://127.0.0.1:1080'" not in js, \
            "сохранив форму, владелец отправил бы бота в мёртвый порт"
        assert "prx.value = ''" in js

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        assert f"'{VER}':" in js
        assert "selective" in js and "TUN" in js

    def test_button_hint_mentions_tun(self):
        js = _read(APP_JS)
        assert "через VPN только Telegram" in js

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  6) Юнит, инструкция и справка в приложении
# ==============================================================================
class TestDocs:
    def test_unit_describes_tun(self):
        unit = _read(TUNNEL_UNIT)
        assert "TUN" in unit
        # поведение юнита не изменилось — он по-прежнему держит туннель
        assert "adguardvpn-cli connect --no-fork" in unit
        assert "Restart=always" in unit
        assert "User=root" in unit

    def test_unit_check_command_without_proxy(self):
        unit = _read(TUNNEL_UNIT)
        assert "curl https://api.telegram.org/ -m 10" in unit

    def test_readme_documents_tun_scheme(self):
        rd = _read(README)
        assert "config set-mode TUN" in rd
        assert "site-exclusions mode selective" in rd
        assert "site-exclusions add api.telegram.org" in rd
        assert "set-tun-routing-mode AUTO" in rd

    def test_readme_keeps_socks_rollback(self):
        """Откат на прежнюю схему описан — selective не идеален (#166)."""
        rd = _read(README)
        assert "Возврат на SOCKS5" in rd
        assert "config set-mode SOCKS" in rd
        assert "socks5h://127.0.0.1:1080" in rd

    def test_readme_warns_about_selective_bug(self):
        rd = _read(README)
        assert "166" in rd, "нужна ссылка на открытый дефект selective-режима"

    def test_readme_explains_why_not_script(self):
        rd = _read(README)
        assert "SCRIPT" in rd

    def test_readme_says_proxy_must_be_empty(self):
        rd = _read(README)
        assert "пустым" in rd or "пустое" in rd

    def test_manual_updated(self):
        m = _read(MANUAL)
        assert "selective" in m and "TUN" in m
        assert "только Telegram" in m


# ==============================================================================
#  7) Версии
# ==============================================================================
class TestVersions17024:
    def test_version_at_least(self):
        parts = tuple(int(x) for x in settings.APP_VERSION.split("."))
        assert parts >= (1, 70, 24), settings.APP_VERSION

    def test_static_pins_follow_current_version(self):
        for path in SYNC_POINTS:
            assert settings.APP_VERSION in _read(path), path

    def test_no_previous_version_left(self):
        for path in SYNC_POINTS:
            assert PREV not in _read(path), path

    def test_changelog_and_whatsnew(self):
        ch = _read("CHANGELOG.md")
        assert ch.count(f"## [{VER}]") == 1
        assert f"'{VER}':" in _read(APP_JS)

    def test_harness_check_added(self):
        h = _read("tests/ui_harness.js")
        assert f"'{VER}':" in h
        assert "v1.70.24" in h
