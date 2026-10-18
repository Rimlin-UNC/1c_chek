# -*- coding: utf-8 -*-
"""v1.70.27 — AdGuard VPN: только документированная последовательность команд.

Главный контракт выпуска, который здесь заблокирован: **ни одна проверка,
выходящая за рамки документации AdGuard, не имеет права прерывать установку**.

В v1.70.26 расхождение показаний CLI считалось фатальным. Это ошибка:
документированный контракт — код возврата команды (`config set-mode TUN`,
`site-exclusions mode selective`, `connect`), а формат текстового вывода CLI
нам не подконтролен. Падать из-за нераспознанной строки — значит мешать VPN
запуститься на сервере.

Что здесь доказано:
  1) документированная последовательность команд присутствует ЦЕЛИКОМ и
     именно в этом порядке: login -> set-mode TUN -> set-tun-routing-mode AUTO
     -> site-exclusions mode selective -> add -> служба с connect --no-fork;
  2) фатальны только отказы самих документированных команд, /dev/net/tun в
     фазе tun и реально не поднявшийся туннель;
  3) перечитывание режима, слушатель на 1080, ранний /dev/net/tun и состояние
     автопилота при выключении — предупреждения (в их блоках нет `die`/`raise`);
  4) ожидание туннеля 60 с вместо 20 — первое подключение в TUN медленнее;
  5) настоящая проверка пути — шаг verify, а не разбор вывода CLI;
  6) версия, CHANGELOG, «Что нового» и синхронизация версий.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
HEALTH_SH = "deploy/adguard-vpn/tg-health.sh"
README = "deploy/adguard-vpn/README.md"
APP_JS = "app/static/js/app.js"
CHANGELOG = "CHANGELOG.md"
UNIT_FILE = "deploy/adguard-vpn/adguard-vpn-telegram.service"
VER = "1.70.27"
PREV = "1.70.26"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")


def _read(path):
    return open(path, encoding="utf-8").read()


def _helper():
    return _read(HELPER_SH)


def _phase(src, name, nxt):
    """Тело фазы помощника от её объявления до следующей."""
    return src[src.index(f"phase_{name}()"):src.index(f"phase_{nxt}()")]


# ==============================================================================
#  1) Документированная последовательность команд — целиком и по порядку
# ==============================================================================
class TestDocumentedSequence:
    def test_login_is_documented_command(self):
        """Ссылка подтверждения — штатный `adguardvpn-cli login`."""
        tun = _phase(_helper(), "login", "tun")
        assert re.search(r'exec "\$cli" login', tun)

    def test_mode_tun(self):
        assert 'config set-mode TUN' in _phase(_helper(), "tun", "units")

    def test_routing_auto(self):
        """AUTO, а не SCRIPT: режим SCRIPT не работает (#69, #190)."""
        body = _phase(_helper(), "tun", "units")
        assert 'config set-tun-routing-mode AUTO' in body
        assert 'SCRIPT' not in body
        assert 'create-route-script' not in body

    def test_selective_mode(self):
        assert 'site-exclusions mode selective' in _phase(_helper(), "tun", "units")

    def test_telegram_domains_added(self):
        sh = _helper()
        for d in ("api.telegram.org", "core.telegram.org", "telegram.org",
                  "*.telegram.org", "t.me", "*.t.me"):
            assert f'"{d}"' in sh, d

    def test_wildcards_are_quoted(self):
        """Неэкранированный `*.t.me` раскрыл бы шелл (issue #148)."""
        sh = _helper()
        assert '"*.telegram.org"' in sh and '"*.t.me"' in sh
        assert not re.search(r'^\s+\*\.', sh, re.M)

    def test_tunnel_service_uses_connect_no_fork(self):
        """Подключение — штатным `connect --no-fork` под systemd."""
        u = _read(UNIT_FILE)
        assert "connect --no-fork" in u
        assert "ExecStart=/usr/local/bin/adguardvpn-cli" in u

    def test_order_in_helper_source(self):
        """Порядок команд в фазе tun — как в документации: режим -> маршрутизация
        -> selective -> домены."""
        body = _phase(_helper(), "tun", "units")
        idx = [body.index(c) for c in ("config set-mode TUN",
                                       "config set-tun-routing-mode AUTO",
                                       "site-exclusions mode selective",
                                       "site-exclusions add")]
        assert idx == sorted(idx), idx

    def test_steps_order_in_program(self):
        assert ag.STEP_ORDER == ["precheck", "cli", "login", "tun", "tunnel",
                                 "autopilot", "verify", "done"]

    def test_readme_documents_the_sequence(self):
        md = _read(README)
        for c in ("adguardvpn-cli login", "config set-mode TUN",
                  "config set-tun-routing-mode AUTO",
                  "site-exclusions mode selective", "site-exclusions add"):
            assert c in md, c


# ==============================================================================
#  2) Фатально — только то, что описано в документации
# ==============================================================================
class TestFatalOnlyForDocumentedFailures:
    def test_phase_tun_dies(self):
        """Отказы фазы tun перечислены ПОЛНОСТЬЮ: /dev/net/tun (факт, а не
        догадка), отсутствие CLI, три документированные команды и обязательный
        Bot API в списке. Ничего сверх этого — никаких отказов по текстовому
        выводу CLI."""
        body = _phase(_helper(), "tun", "units")
        assert body.count("die ") == 6, body.count("die ")
        for needle in ('die "нет /dev/net/tun',
                       'die "CLI не найден"',
                       'die "set-mode TUN не удался"',
                       'die "set-tun-routing-mode AUTO не удался"',
                       'die "site-exclusions mode selective не удался"',
                       'die "api.telegram.org не попал'):
            assert needle in body, needle

    def test_readback_block_is_not_fatal(self):
        """Перечитывание режима: между `mode selective` и циклом доменов нет die."""
        body = _phase(_helper(), "tun", "units")
        # от комментария перечитывания, а не от самой команды (у неё свой
        # документированный отказ на код возврата)
        start = body.index('перечитываем режим')
        end = body.index('for d in "${TG_DOMAINS[@]}"')
        block = body[start:end]
        assert "die " not in block, block
        assert "ВНИМАНИЕ" in block          # но владелец предупреждён

    def test_port_1080_block_is_not_fatal(self):
        body = _phase(_helper(), "units", "autopilot")
        start = body.index("/dev/tcp/127.0.0.1/1080")
        block = body[start:start + 700]
        assert "die " not in block, block
        assert "внимание" in block

    def test_phase_off_dies_only_if_tunnel_survives(self):
        """Единственный фатальный исход выключения — туннель не остановился."""
        sh = _helper()
        body = sh[sh.index("phase_off()"):sh.index("\nmain()")]
        assert body.count("die ") == 1, body.count("die ")
        assert 'die "туннель не остановился' in body

    def test_off_timer_block_is_not_fatal(self):
        sh = _helper()
        body = sh[sh.index("phase_off()"):]
        body = body[:body.index("# ------")] if "# ------" in body else body
        start = body.index('systemctl is-active "$TIMER_UNIT"')
        block = body[start:start + 800]
        assert "die " not in block, block
        assert "внимание" in block

    def test_python_precheck_does_not_raise_on_tun(self):
        src = _read("app/services/adguard_setup.py")
        fn = src[src.index("def _step_precheck"):]
        fn = fn[:fn.index("\ndef ")]
        i = fn.index("/dev/net/tun")
        block = fn[i - 400:i + 500]
        assert "raise RuntimeError" not in block.split("/dev/net/tun")[1][:300]
        assert "job.say" in block


# ==============================================================================
#  3) Ожидание туннеля: 60 с вместо 20
# ==============================================================================
class TestTunnelWaitExtended:
    def test_helper_waits_60s(self):
        body = _phase(_helper(), "units", "autopilot")
        assert "seq 1 30" in body
        assert "sleep 2" in body
        assert "за 60 с" in body
        assert "за 20 с" not in body

    def test_program_waits_60s(self):
        src = _read("app/services/adguard_setup.py")
        fn = src[src.index("def _step_tunnel"):]
        fn = fn[:fn.index("\ndef ")]
        assert "range(30)" in fn
        assert "за 60 с" in fn
        assert "range(10)" not in fn

    def test_helper_timeout_allows_60s(self):
        """Таймаут вызова фазы units в программе больше нового ожидания."""
        src = _read("app/services/adguard_setup.py")
        m = re.search(r'run_helper\("units", timeout=(\d+)\)', src)
        assert m and int(m.group(1)) >= 90, m


# ==============================================================================
#  4) Настоящая проверка пути — шаг verify
# ==============================================================================
class TestVerifyIsTheRealCheck:
    def test_verify_measures_real_path(self):
        src = _read("app/services/adguard_setup.py")
        fn = src[src.index("def _step_verify"):]
        fn = fn[:fn.index("\ndef ")]
        assert "ping_proxy" in fn
        assert "samples=3" in fn

    def test_verify_message_lists_documented_commands(self):
        src = _read("app/services/adguard_setup.py")
        fn = src[src.index("def _step_verify"):]
        fn = fn[:fn.index("\ndef ")]
        for needle in ("license", "site-exclusions mode", "site-exclusions show"):
            assert needle in fn, needle

    def test_autopilot_still_protects_off_switch(self):
        """Главная защита от воскрешения VPN — сам автопилот, а не отказ off."""
        sh = _read(HEALTH_SH)
        assert 'systemctl is-enabled adguard-vpn-telegram' in sh
        assert '"$EN" = "disabled"' in sh
        assert '"$EN" = "masked"' in sh

    def test_readme_explains_why_warnings(self):
        md = _read(README)
        assert "код возврата" in md or "кодам возврата" in md
        assert "не прерывают установку" in md


# ==============================================================================
#  5) Версия, CHANGELOG, «Что нового»
# ==============================================================================
class TestVersions17027:
    def test_app_version(self):
        assert settings.APP_VERSION == VER

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_has_new_version(self, path):
        assert VER in _read(path), path

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_has_no_previous_version(self, path):
        assert PREV not in _read(path), path

    def test_changelog_entry_once_and_first(self):
        md = _read(CHANGELOG)
        assert md.count(f"## [{VER}]") == 1
        assert md.index(f"## [{VER}]") < md.index(f"## [{PREV}]")

    def test_changelog_explains_non_blocking(self):
        md = _read(CHANGELOG)
        sec = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        for needle in ("код возврата", "предупреждения", "60 с", "selective",
                       "1080", "/dev/net/tun", "verify"):
            assert needle in sec, needle

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        assert f"'{VER}': [" in js

    def test_whats_new_explains_to_owner(self):
        js = _read(APP_JS)
        block = js[js.index(f"'{VER}': ["):]
        block = block[:block.index("'1.70.26':")]
        for needle in ("документаци", "TUN", "selective", "60 секунд",
                       "предупреждени"):
            assert needle in block, needle

    def test_phase_set_still_unchanged(self):
        """Правило на сервере переустанавливать не нужно."""
        m = re.search(r'ALLOWED_PHASES="([^"]+)"', _helper())
        assert m.group(1) == ("preflight rights install login tun off units "
                              "autopilot version status")
        assert tuple(ag.HELPER_PHASES) == tuple(m.group(1).split())
