# -*- coding: utf-8 -*-
"""v1.70.30 — ссылка со сменяемого домена AdGuard принимается; отказ туннеля
объяснён в панели.

Выпуск сделан по второму живому прогону на сервере владельца. Вход прошёл, но
CLI выдал ссылку такого вида:

    You need to authorize in your browser. The following link will be
    available for 1799 seconds:
    https://guidedeficit.blog/device_code?user_code=NKHD-WXSR

Домен не похож на AdGuard, и `extract_login_url` такую ссылку МОЛЧА
ОТБРАСЫВАЛ: `job.login_url` оставался пустым, панель ничего не показывала, а
мастер ждал подтверждения до таймаута. Это второе, независимое от v1.70.28
объяснение «ссылка не пришла».

Белый список доменов задумывался как защита от подмены
(AdguardTeam/AdGuardVPNCLI#188), но разработчик AdGuard (sfionov) ответил в том
обращении:

    «AdGuard VPN can not use main domains in countries which may block VPNs
     (they are either blocked, or will be blocked if used by AG VPN), so some
     rotating ones are used. If you are in one of such countries, this is
     expected behaviour.»

То есть сменяемые домены — штатный механизм, а защита ломала ровно тех, кому
VPN нужнее всего.

Вторая находка того же прогона: вход успешен, а туннель не поднимается —
«Unable to fetch locations. Backend error: Timed out» и ошибки `UDP_SOCKET`.
Это недоступность бэкенда AdGuard, а не поломка программы; мастер обязан
показать журнал службы и документированные причины.

Что здесь доказано:
  1) живая ссылка с guidedeficit.blog принимается (в том числе ЖИВЫМ прогоном
     через псевдотерминал), как и auctionassociation.space из #188;
  2) случайные URL без формы device-code по-прежнему отвергаются — фильтр стал
     строже по сути, а не слабее;
  3) владелец видит пояснение про сменяемый домен и просьбу сверить код;
  4) `phase_units` при отказе печатает журнал службы и четыре документированные
     причины с конкретными командами;
  5) протокол и семейство адресов программа сама НЕ меняет;
  6) версия, CHANGELOG, «Что нового» и синхронизация версий.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import stat
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
UNIT_FILE = "deploy/adguard-vpn/adguard-vpn-telegram.service"
VER = "1.70.30"
PREV = "1.70.29"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Дословно с живого сервера владельца (19.10.2026).
LIVE_LINE = ("You need to authorize in your browser. The following link will "
             "be available for 1799 seconds: "
             "https://guidedeficit.blog/device_code?user_code=NKHD-WXSR")
LIVE_URL = "https://guidedeficit.blog/device_code?user_code=NKHD-WXSR"
# Дословно из обращения AdguardTeam/AdGuardVPNCLI#188.
ISSUE188_URL = "https://auctionassociation.space/device_code?user_code=X"
# Официальный вид из документации.
OFFICIAL_URL = "https://auth.adguard.io/device_code?user_code=ABCD-1234"

# Двойник CLI: печатает ссылку со СМЕНЯЕМОГО домена в ANSI-эскейпах, как
# настоящий, и ждёт ответа на меню.
LOGIN_STUB = r"""#!/usr/bin/env bash
if [ ! -t 0 ]; then
  printf 'Failed to get auth link\r\n'
  exit 1
fi
printf 'You need to authorize in your browser.\r\n'
printf '\033[36mhttps://guidedeficit.blog/device_code?user_code=NKHD-WXSR\033[0m\r\n'
printf '  b - Open link in browser\r\n'
printf '  s - Speed up check\r\n'
printf '  x - Cancel\r\n'
read -r ans
printf 'Successfully Logged in\r\n'
exit 0
"""


def _read(path):
    return open(path, encoding="utf-8").read()


def _helper():
    return _read(HELPER_SH)


def _units_phase():
    sh = _helper()
    return sh[sh.index("phase_units()"):sh.index("phase_autopilot()")]


def _func_src(fn):
    """Текст функции из исходника модуля (для регресс-гардов по коду)."""
    src = _read(ag.__file__.replace(".pyc", ".py"))
    start = src.index("def " + fn.__name__ + "(")
    tail = src[start:]
    m = re.search(r"\n(?=\S)", tail[1:])
    return tail[:m.start() + 1] if m else tail


@pytest.fixture
def fresh_job(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield ag.job
    ag.job.reset()


@pytest.fixture
def login_stub(tmp_path):
    p = tmp_path / "cli-rotating-domain.sh"
    p.write_text(LOGIN_STUB, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# ==============================================================================
#  1) Сменяемый домен принимается
# ==============================================================================


class TestRotatingDomainAccepted:
    def test_live_server_link_accepted(self):
        """Ссылка, снятая с живого сервера, обязана извлекаться."""
        assert ag.extract_login_url(LIVE_LINE) == LIVE_URL

    def test_issue188_link_accepted(self):
        """Домен из #188 — тот же штатный механизм, а не подмена."""
        line = ("You need to authorize in your browser. The following link "
                "will be available for 1799 seconds: " + ISSUE188_URL)
        assert ag.extract_login_url(line) == ISSUE188_URL

    def test_official_link_still_accepted(self):
        assert ag.extract_login_url(
            "The following link: " + OFFICIAL_URL) == OFFICIAL_URL

    def test_live_link_reaches_job_state(self, fresh_job, login_stub,
                                        monkeypatch):
        """ЖИВОЙ прогон: двойник печатает ссылку со сменяемого домена в
        эскейпах и ждёт ответа на меню — мастер обязан её получить и показать.

        Без этой проверки «принимается» осталось бы утверждением про функцию
        разбора, а не про то, что владелец увидит ссылку в панели.
        """
        monkeypatch.setattr(ag, "_login_argv", lambda cli: [login_stub])
        rc, out, url = ag._login_worker(login_stub)
        assert rc == 0
        assert url == LIVE_URL
        assert fresh_job.login_url == LIVE_URL

    def test_owner_is_told_domain_is_rotating(self, fresh_job, login_stub,
                                              monkeypatch):
        """Сменяемый домен выглядит как подделка — владельца надо предупредить,
        иначе ссылку не откроют и вход сорвётся по таймауту."""
        monkeypatch.setattr(ag, "_login_argv", lambda cli: [login_stub])
        ag._login_worker(login_stub)
        text = "\n".join(fresh_job.log)
        assert LIVE_URL in text
        assert "сменяемые домены" in text
        assert "#188" in text
        assert "сверите" in text or "Сверьте" in text

    def test_answers_are_not_skipped_after_url_seen(self):
        """Регресс-гард на баг, найденный этим же файлом.

        Настоящий CLI печатает ссылку РАНЬШЕ меню, поэтому прежний выход из
        цикла ответов по условию «url уже есть» оставлял меню без ответа: CLI
        висел в ожидании выбора, а мастер ждал его до LOGIN_TIMEOUT (15 минут)
        и сообщал об отказе входа на полностью рабочем сервере.
        """
        src = _func_src(ag._login_worker)
        i = src.index("def _answer()")
        answer = src[i:]
        assert "or url" not in answer, \
            "ответы на меню снова прерываются по найденной ссылке"
        assert "if stop.is_set() or proc.poll() is not None:" in answer

    def test_speed_up_check_is_repeated_while_waiting(self):
        """«s - Speed up check» документирован как ускорение проверки — пока
        владелец подтверждает вход в браузере, его уместно повторять."""
        keys = [k for _d, k in ag.LOGIN_ANSWERS]
        assert keys[0] == "b", "первым делом отвечаем на меню"
        assert keys.count("s") >= 2
        assert "x" not in keys, "«x» — это отмена входа"
        delays = [d for d, _k in ag.LOGIN_ANSWERS]
        assert delays == sorted(delays), "задержки должны расти"

    def test_known_adguard_domain_gets_no_warning(self, fresh_job, monkeypatch):
        """На официальном домене предупреждение не нужно — оно только пугает."""
        monkeypatch.setattr(ag, "_find_cli", lambda: "/usr/local/bin/ag")
        monkeypatch.setattr(ag.subprocess, "Popen",
                            _fake_popen("link: " + OFFICIAL_URL))
        monkeypatch.setattr(ag, "pty", None, raising=False)
        ag._login_worker("/usr/local/bin/ag")
        text = "\n".join(fresh_job.log)
        if OFFICIAL_URL in text:
            assert "сменяемые домены" not in text


def _fake_popen(line):
    """Popen-двойник для запасного (канального) пути: печатает строку и
    завершается нулём."""
    class P:
        returncode = 0
        stdin = None

        def __init__(self):
            self.stdout = iter([line + "\n"])

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake(argv, **k):
        return P()

    return fake


# ==============================================================================
#  2) Фильтр стал строже по сути: форма, а не домен
# ==============================================================================


class TestDeviceCodeFormIsTheFilter:
    @pytest.mark.parametrize("line", [
        "no link here",
        "инструкция: https://example.com/README.md",
        "см. https://github.com/AdguardTeam/AdGuardVPNCLI/tree/master",
        "curl -fsSL https://raw.githubusercontent.com/x/y/install.sh",
        "Downloaded from https://github.com/AdguardTeam/x/releases/download/v1",
    ])
    def test_random_urls_rejected(self, line):
        """Случайные адреса из журнала не должны становиться «ссылкой входа»:
        иначе владелец получил бы кнопку, ведущую на установщик."""
        assert ag.extract_login_url(line) == ""

    @pytest.mark.parametrize("url", [
        "https://guidedeficit.blog/device_code?user_code=NKHD-WXSR",
        "https://auctionassociation.space/device_code?user_code=X",
        "https://auth.adguard.io/device_code?user_code=ABCD-1234",
        "https://any-rotating-domain.example/user_code?x=1",
    ])
    def test_device_code_shape_accepted_regardless_of_domain(self, url):
        assert ag.extract_login_url("link: " + url) == url

    def test_markers_are_grounded(self):
        """Маркеры формы взяты из трёх независимых источников, а не выдуманы."""
        for m in ag.DEVICE_CODE_MARKERS:
            assert m in LIVE_URL.lower()
            assert m in ISSUE188_URL.lower()
            assert m in OFFICIAL_URL.lower()

    def test_non_http_scheme_rejected(self):
        assert ag.extract_login_url(
            "ftp://guidedeficit.blog/device_code?user_code=X") == ""

    def test_malformed_url_does_not_raise(self):
        """urlsplit умеет бросать ValueError — шаг не имеет права упасть."""
        for bad in ("", None, "https://", "http://[::1/device_code?user_code=1"):
            try:
                ag.extract_login_url(bad)
            except ValueError as e:          # pragma: no cover
                pytest.fail(f"extract_login_url упал на {bad!r}: {e}")


# ==============================================================================
#  3) Классификация домена — справка, а не фильтр
# ==============================================================================


class TestDomainClassification:
    @pytest.mark.parametrize("host,expected", [
        ("auth.adguard.io", True),
        ("adguard.io", True),
        ("auth.adguardaccount.com", True),
        ("adguard-vpn.com", True),
        ("guidedeficit.blog", False),
        ("auctionassociation.space", False),
        ("", False),
    ])
    def test_is_adguard_domain(self, host, expected):
        assert ag.is_adguard_domain(host) is expected

    def test_login_url_host(self):
        assert ag.login_url_host(LIVE_URL) == "guidedeficit.blog"
        assert ag.login_url_host(OFFICIAL_URL) == "auth.adguard.io"
        assert ag.login_url_host("") == ""

    def test_whitelist_is_kept_but_not_a_filter(self):
        """Список доменов остался (для пояснения), но извлечение ссылки от него
        больше не зависит."""
        assert ag.ADGUARD_DOMAINS
        src = _read(ag.__file__.replace(".pyc", ".py"))
        fn = src[src.index("def extract_login_url("):]
        fn = fn[:fn.index("\ndef ", 10)]
        assert "ADGUARD_DOMAINS" not in fn, \
            "фильтр по домену вернулся — ссылка со сменяемого домена пропадёт"
        assert "DEVICE_CODE_MARKERS" in fn


# ==============================================================================
#  4) Отказ туннеля объяснён в панели
# ==============================================================================


class TestTunnelFailureIsExplained:
    def test_journal_is_dumped_on_failure(self):
        u = _units_phase()
        assert "journalctl -u \"$TUNNEL_UNIT\" -n 25 --no-pager" in u
        assert "журнал службы" in u

    def test_journal_dump_is_optional(self):
        """journalctl может отсутствовать — это не повод ронять фазу раньше
        содержательного сообщения."""
        u = _units_phase()
        assert "command -v journalctl" in u

    def test_four_documented_causes_listed(self):
        u = _units_phase()
        for needle in ("license", "set-protocol http2", "connect -4",
                       "Unable to fetch locations"):
            assert needle in u, needle

    def test_udp_and_ipv6_causes_reference_sources(self):
        """Причины взяты из документации и обращений, а не придуманы."""
        u = _units_phase()
        assert "UDP_SOCKET" in u
        assert "#176" in u

    def test_success_path_untouched(self):
        """Диагностика добавлена только в ветку отказа: успешный подъём
        туннеля не оброс лишними командами."""
        u = _units_phase()
        assert "seq 1 30" in u, "ожидание 60 с (v1.70.27) должно остаться"
        assert "за 60 с" in u
        assert u.count("systemctl restart \"$TUNNEL_UNIT\"") == 1

    def test_die_count_unchanged(self):
        """Количество фатальных исходов фазы не изменилось — диагностика
        объясняет отказ, а не добавляет новые."""
        assert _units_phase().count("die ") == 5

    def test_helper_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  5) Программа не меняет протокол и семейство адресов сама
# ==============================================================================


class TestNoBlindProtocolChanges:
    def test_protocol_is_not_set_automatically(self):
        """`set-protocol` встречается только как СОВЕТ в тексте отказа."""
        sh = _helper()
        assert '"$cli" config set-protocol' not in sh
        assert "config set-protocol http2" in sh      # совет владельцу

    def test_ipv4_is_not_forced_in_unit(self):
        unit = _read(UNIT_FILE)
        line = [l for l in unit.splitlines() if l.startswith("ExecStart=")][0]
        assert "connect" in line and "--no-fork" in line
        assert " -4" not in line and " -6" not in line

    def test_documented_commands_are_still_the_only_config(self):
        """Конфигурацию по-прежнему задаёт документированная
        последовательность v1.70.24, ничего нового не добавлено."""
        tun = _helper()[_helper().index("phase_tun()"):]
        tun = tun[:tun.index("phase_units()")]
        for cmd in ('config set-mode TUN', 'config set-tun-routing-mode AUTO',
                    'site-exclusions mode selective'):
            assert cmd in tun, cmd
        for cmd in ('config set-protocol', 'config set-dns',
                    'config set-change-system-dns', 'config set-socks-port'):
            assert cmd not in tun, cmd

    def test_readme_says_program_does_not_choose(self):
        rd = _read(README)
        assert "намеренно" in rd and "не меняет" in rd


# ==============================================================================
#  6) Документация
# ==============================================================================


class TestDocs:
    def test_readme_documents_rotating_domains(self):
        rd = _read(README)
        assert "guidedeficit.blog" in rd
        assert "сменяемого домена" in rd
        assert "#188" in rd
        assert "expected behaviour" in rd or "rotating" in rd

    def test_readme_documents_backend_failure(self):
        rd = _read(README)
        assert "Unable to fetch locations" in rd
        assert "Failed to get locations list" in rd
        assert "UDP_SOCKET" in rd
        assert "set-protocol http2" in rd
        assert "#176" in rd

    def test_readme_no_longer_claims_domain_whitelist(self):
        rd = _read(README)
        assert "подменённый домен не показывается" not in rd

    def test_changelog_entry(self):
        md = _read(CHANGELOG)
        assert md.count(f"## [{VER}]") == 1
        assert md.index(f"## [{VER}]") < md.index(f"## [{PREV}]")
        entry = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        for needle in ("guidedeficit.blog", "rotating", "#188",
                       "Unable to fetch locations", "device_code"):
            assert needle in entry, needle

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        assert f"'{VER}':" in js
        entry = js[js.index(f"'{VER}':"):js.index(f"'{PREV}':")]
        assert "guidedeficit.blog" in entry
        assert "сменяемые" in entry or "сменяемых" in entry

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  7) Версия
# ==============================================================================


class TestVersion17030:
    def test_app_version(self):
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= \
            (1, 70, 30)

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_matches_current_version(self, path):
        assert settings.APP_VERSION in _read(path), path

    def test_helper_phases_unchanged(self):
        m = re.search(r'^ALLOWED_PHASES="([^"]*)"', _helper(), re.M)
        assert m
        assert m.group(1).split() == [
            "preflight", "rights", "install", "login", "tun", "off", "units",
            "autopilot", "version", "status"]
