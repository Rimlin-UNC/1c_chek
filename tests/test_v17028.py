# -*- coding: utf-8 -*-
"""v1.70.28 — AdGuard VPN: ссылка регистрации приходит; собственный блок.

Главный контракт выпуска, который здесь заблокирован: **владелец получает
ссылку для регистрации сервера прямо в блоке AdGuard VPN**.

Причина поломки была не в правах и не в сети. По документации AdGuardVPNCLI
команда `login` — ИНТЕРАКТИВНАЯ: она показывает меню

    b - Open link in browser
    s - Speed up check
    x - Cancel

и ждёт выбора. Мастер запускал CLI с `stdin=/dev/null`: меню получало конец
файла, и **ссылка не выдавалась вовсе**. В SSH-терминале (как в v1.70.13) всё
работало, потому что `b` нажимал владелец.

Что здесь доказано:
  1) ЖИВОЙ тест: CLI-двойник, который выдаёт ссылку только после ответа на
     меню, реально отдаёт её мастеру — `job.login_url` заполняется. Тот же
     двойник с закрытым stdin ссылки не даёт (негативный контроль: тест не
     вакуумный, он ловит именно прежнюю поломку);
  2) вывод псевдотерминала очищается от ANSI-эскейпов и `\\r` до разбора;
  3) мастер отвечает документированными клавишами, начиная с `b`;
  4) `Failed to get auth link` распознаётся как дефект самого CLI (#142/#169,
     корневые сертификаты) и сопровождается командой лечения;
  5) мастер не рапортует «вход уже выполнен» без документированного признака;
  6) помощник предупреждает об отсутствии CA-сертификатов ДО установки CLI и
     не падает на этом; набор фаз не менялся (правило sudoers живо);
  7) UI: собственный блок в «Устройство и служебное», трёхпозиционный
     переключатель, из карточки Telegram-бота AdGuard удалён, ссылка не
     привязана к `running`;
  8) маршруты API не изменились — развёрнутый бэкенд совместим;
  9) версия, CHANGELOG, «Что нового» и синхронизация версий.

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

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
README = "deploy/adguard-vpn/README.md"
APP_JS = "app/static/js/app.js"
APP_CSS = "app/static/css/app.css"
CHANGELOG = "CHANGELOG.md"
ROUTES = "app/routers/settings_routes.py"
VER = "1.70.28"
PREV = "1.70.27"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Двойник `adguardvpn-cli login`: выдаёт ссылку ТОЛЬКО если у него есть
# терминал и из меню выбран `b`. Без терминала честно отвечает так же, как
# настоящий CLI в обращениях AdguardTeam/AdGuardVPNCLI#134/#142/#169.
LOGIN_STUB = r"""#!/usr/bin/env bash
if [ ! -t 0 ]; then
  printf 'Failed to get auth link\r\n'
  exit 1
fi
printf 'Choose an option:\r\n'
printf '  b - Open link in browser\r\n'
printf '  s - Speed up check\r\n'
printf '  x - Cancel\r\n'
read -r ans
ans="${ans%$'\r'}"
case "$ans" in
  b|B)
    printf '\033[32mOpen this link:\033[0m\r\n'
    printf '\033[36mhttps://auth.adguard.io/device_code?user_code=YMSN-2810\033[0m\r\n'
    printf 'Waiting for confirmation...\r\n'
    printf 'Successfully logged in\r\n'
    exit 0
    ;;
  *)
    printf 'Cancelled\r\n'
    exit 1
    ;;
esac
"""

STUB_URL = "https://auth.adguard.io/device_code?user_code=YMSN-2810"


def _read(path):
    return open(path, encoding="utf-8").read()


def _js():
    return _read(APP_JS)


def _helper():
    return _read(HELPER_SH)


def _func_src(fn):
    """Текст функции из исходника модуля (для регресс-гардов по коду)."""
    src = _read(ag.__file__.replace(".pyc", ".py"))
    start = src.index("def " + fn.__name__ + "(")
    tail = src[start:]
    m = re.search(r"\n(?=\S)", tail[1:])
    return tail[:m.start() + 1] if m else tail


@pytest.fixture
def fresh_job(tmp_path, monkeypatch):
    """Чистое задание и изолированный файл состояния (см. v1.70.20)."""
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield ag.job
    ag.job.reset()


@pytest.fixture
def login_stub(tmp_path):
    p = tmp_path / "adguardvpn-cli-login-stub.sh"
    p.write_text(LOGIN_STUB, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# ==============================================================================
#  1) ЖИВОЕ доказательство: ссылка приходит
# ==============================================================================


class TestLoginLinkReallyArrives:
    def test_worker_gets_url_from_interactive_cli(self, fresh_job, login_stub,
                                                  monkeypatch):
        """Ключевой тест выпуска.

        Двойник ведёт себя как настоящий CLI: без терминала ссылки нет вовсе,
        с терминалом — она появляется только после ответа `b` на меню. Мастер
        обязан её получить и положить в `job.login_url`, иначе владелец снова
        увидит «идёт установка» без ссылки.
        """
        monkeypatch.setattr(ag, "_login_argv", lambda cli: [login_stub])
        rc, out, url = ag._login_worker(login_stub)
        assert rc == 0, "двойник должен завершиться успешно после ответа «b»"
        assert url == STUB_URL
        assert fresh_job.login_url == STUB_URL, \
            "ссылка обязана попасть в состояние задания — её читает панель"
        assert "Successfully logged in" in out

    def test_negative_control_without_terminal_no_url(self, login_stub):
        """Негативный контроль: прежний способ (stdin закрыт) ссылки не даёт.

        Без этого теста предыдущий мог бы «проходить» на двойнике, который
        выдаёт ссылку всегда, — и поломку в проде он бы не ловил.
        """
        r = subprocess.run([login_stub], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60)
        assert r.returncode != 0
        assert "Failed to get auth link" in r.stdout
        assert "https://" not in r.stdout

    def test_url_reaches_job_log_as_registration_hint(self, fresh_job,
                                                      login_stub, monkeypatch):
        """Ссылка должна быть видна и текстом в живом журнале, не только в
        состоянии: журнал — то, что владелец читает при проблеме."""
        monkeypatch.setattr(ag, "_login_argv", lambda cli: [login_stub])
        ag._login_worker(login_stub)
        text = "\n".join(fresh_job.log)
        assert STUB_URL in text
        assert "Ссылка для регистрации сервера" in text

    def test_login_worker_never_closes_stdin(self):
        """Регресс-гард: `DEVNULL` в `_login_worker` — это и есть поломка."""
        src = _func_src(ag._login_worker)
        assert "DEVNULL" not in src, \
            "меню CLI получит EOF и ссылка входа не появится"
        assert "pty.openpty()" in src

    def test_pty_gets_window_size(self):
        """Без размера окна интерактивное меню может не отрисоваться."""
        src = _func_src(ag._login_worker)
        assert "TIOCSWINSZ" in src
        assert "ECHO" in src, "эхо ввода надо гасить, иначе оно шумит в журнале"


# ==============================================================================
#  2) Разбор вывода псевдотерминала
# ==============================================================================


class TestTtyOutputParsing:
    def test_clean_tty_strips_ansi_and_cr(self):
        raw = "\x1b[36mhttps://auth.adguard.io/x\x1b[0m\r\n"
        clean = ag._clean_tty(raw)
        assert "\x1b" not in clean and "\r" not in clean
        assert clean.strip() == "https://auth.adguard.io/x"

    def test_url_is_not_polluted_by_escape_suffix(self):
        """Если эскейп не снять, регулярка заберёт его в состав ссылки — и
        владелец получит нерабочий URL."""
        polluted = "https://auth.adguard.io/device_code?user_code=X\x1b[0m"
        assert ag.extract_login_url(polluted) != \
            "https://auth.adguard.io/device_code?user_code=X"
        assert ag.extract_login_url(ag._clean_tty(polluted)) == \
            "https://auth.adguard.io/device_code?user_code=X"

    def test_answers_are_documented_keys(self):
        """Отвечаем на меню тем, что описано в документации AdGuard."""
        assert ag.LOGIN_ANSWERS[0][1] == "b", "первым делом — открыть ссылку"
        assert all(k in ("b", "s") for _d, k in ag.LOGIN_ANSWERS), \
            "«x» нажимать нельзя: это отмена входа"
        assert all(isinstance(d, float) or isinstance(d, int)
                   for d, _k in ag.LOGIN_ANSWERS)

    def test_non_adguard_domain_still_rejected(self):
        """Защита от подменённого домена (#188) не ослаблена pty-выводом."""
        assert ag.extract_login_url(
            ag._clean_tty("open https://evil.example.com/x\x1b[0m")) == ""
        assert ag.extract_login_url(
            ag._clean_tty("open https://auth.adguard.io/x\x1b[0m")) == \
            "https://auth.adguard.io/x"


# ==============================================================================
#  3) Диагностика шага входа
# ==============================================================================


class TestStepLoginDiagnostics:
    def test_auth_link_error_is_explained(self, fresh_job, monkeypatch):
        """«Failed to get auth link» — дефект самого CLI, и владелец должен
        получить команду лечения, а не «вход не завершён»."""
        monkeypatch.setattr(ag, "_find_cli", lambda: "/usr/local/bin/ag")
        monkeypatch.setattr(
            ag, "_login_worker",
            lambda cli: (1, "Failed to get auth link", ""))
        with pytest.raises(RuntimeError) as ei:
            ag._step_login()
        msg = str(ei.value)
        assert "ca-certificates" in msg
        assert "#142" in msg and "#169" in msg
        assert "Failed to get auth link" in msg

    def test_silent_success_is_not_claimed(self, fresh_job, monkeypatch):
        """Без документированного признака входа мастер не рапортует успех:
        врать владельцу, что вход выполнен, — значит оставить его без VPN."""
        monkeypatch.setattr(ag, "_find_cli", lambda: "/usr/local/bin/ag")
        monkeypatch.setattr(ag, "_login_worker", lambda cli: (0, "Choose an "
                                                                "option:", ""))
        ag._step_login()
        assert "Вход уже выполнен" not in "\n".join(fresh_job.log)
        assert ag.LOGIN_ANSWER_HINT in "\n".join(fresh_job.log)

    def test_documented_success_marker_is_recognised(self, fresh_job,
                                                     monkeypatch):
        monkeypatch.setattr(ag, "_find_cli", lambda: "/usr/local/bin/ag")
        monkeypatch.setattr(
            ag, "_login_worker",
            lambda cli: (0, "Successfully logged in", ""))
        ag._step_login()
        assert "Вход уже выполнен" in "\n".join(fresh_job.log)

    def test_confirmed_login_reports_done(self, fresh_job, monkeypatch):
        monkeypatch.setattr(ag, "_find_cli", lambda: "/usr/local/bin/ag")
        monkeypatch.setattr(
            ag, "_login_worker",
            lambda cli: (0, "ok", "https://auth.adguard.io/x"))
        ag._step_login()
        assert "Вход подтверждён" in "\n".join(fresh_job.log)


# ==============================================================================
#  4) Помощник: CA-сертификаты и неизменность набора фаз
# ==============================================================================


class TestHelper:
    def _phase(self, name, nxt):
        src = _helper()
        a = src.index("phase_" + name + "()")
        b = src.index("phase_" + nxt + "()", a)
        return src[a:b]

    def test_preflight_warns_about_ca_certificates(self):
        pf = self._phase("preflight", "install")
        assert "ca-certificates.crt" in pf
        assert "Failed to get auth link" in pf
        assert "update-ca-certificates" in pf

    def test_ca_warning_is_not_fatal(self):
        """Предупреждение не имеет права прерывать установку (принцип
        v1.70.27): сертификат может лежать и в другом месте, а решать за CLI,
        получилось у него или нет, — не нам."""
        pf = self._phase("preflight", "install")
        i = pf.index("# v1.70.28: корневые сертификаты")
        j = pf.index("\n  fi\n", i) + len("\n  fi\n")
        block = pf[i:j]
        assert "ca-certificates.crt" in block
        assert " die " not in block, block
        assert "внимание:" in block, "это должно быть предупреждение, а не отказ"

    def test_ca_check_runs_before_cli_install(self):
        """Смысл предупреждения — сэкономить ссылку входа: оно должно
        появляться ДО установки CLI и ДО шага login."""
        src = _helper()
        assert src.index("ca-certificates.crt") < src.index("phase_install()")

    def test_allowed_phases_unchanged(self):
        """Набор фаз зашит в установленное правило sudoers; менять его из
        приложения нельзя — правило перестанет совпадать, и мастер останется
        без прав. Проверяем фактический список дословно."""
        m = re.search(r'^ALLOWED_PHASES="([^"]*)"', _helper(), re.M)
        assert m, "ALLOWED_PHASES не найден"
        phases = m.group(1).split()
        assert phases == ["preflight", "rights", "install", "login", "tun",
                          "off", "units", "autopilot", "version", "status"], \
            "набор фаз изменился — установленное правило sudoers не подойдёт"
        assert len(phases) == 10

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  5) Новый блок в UI
# ==============================================================================


class TestNewBlock:
    def test_card_exists_in_device_section(self):
        js = _js()
        assert 'id="st-adguard"' in js
        assert 'data-sec="device"' in js
        m = re.search(r'id="st-adguard" data-sec="device" style="order:(\d+)"',
                      js)
        assert m, "блок обязан быть в разделе «Устройство и служебное»"
        assert m.group(1) == "49"

    def test_card_is_admin_only(self):
        js = _js()
        i = js.index('id="st-adguard"')
        assert "isAdmin()" in js[max(0, i - 200):i]

    def test_removed_from_telegram_card(self):
        """Пользователь прямо сказал: в карточке Telegram ему не место."""
        js = _js()
        assert 'id="tg-adguard"' not in js
        assert 'id="tg-adguard-hint"' not in js
        assert "$('#tg-adguard')" not in js

    def test_telegram_card_keeps_ping(self):
        """Замер пинга относится к пути Telegram и остаётся в его карточке."""
        js = _js()
        assert 'id="tg-ping"' in js
        assert "settings/telegram/ping" in js

    def test_three_position_switch_markup(self):
        js = _js()
        for needle in ('id="ag-switch"', 'id="ag-sw-off"', 'id="ag-sw-on"',
                       'id="ag-sw-knob"', 'data-pos="mid"', "ag-sw-left",
                       "ag-sw-right"):
            assert needle in js, needle

    def test_switch_semantics_left_off_right_on(self):
        js = _js()
        assert "sw.setAttribute('data-pos'," in js
        assert "busy ? 'mid' : (known ? (agVpnOn ? 'right' : 'left') : 'mid')" \
            in js
        assert "const agOn = $('#ag-sw-on'), agOff = $('#ag-sw-off');" in js
        assert "agOn.onclick" in js and "agOff.onclick" in js

    def test_sides_disabled_by_real_state(self):
        """Выключать несуществующий туннель нечего, включать работающий — тоже."""
        js = _js()
        assert "if (off) off.disabled = !!busy || !agVpnOn;" in js
        assert "if (on) on.disabled = !!busy || agVpnOn;" in js

    def test_block_mentions_only_telegram_through_vpn(self):
        js = _js()
        card = js[js.index('id="st-adguard"'):js.index('id="st-about"')]
        assert "только Telegram" in card
        assert "Вводить пароль не нужно" in card

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  6) Ссылка приходит В БЛОК
# ==============================================================================


class TestLinkInBlock:
    def test_link_elements_live_in_new_block(self):
        js = _js()
        card = js[js.index('id="st-adguard"'):js.index('id="st-about"')]
        for needle in ('id="ag-link-box"', 'id="ag-link"', 'id="ag-link-hint"',
                       'id="ag-link-url"',
                       "Ссылка для регистрации сервера"):
            assert needle in card, needle

    def test_link_no_longer_gated_on_running(self):
        """Прежнее условие `login_url && st.running` прятало ссылку, как
        только задание останавливалось, — а живёт она около 30 минут."""
        js = _js()
        assert "st.login_url && st.running" not in js
        assert "if (st.login_url && !(st.finished && st.success))" in js

    def test_link_shows_url_as_text(self):
        """Кнопку на телефоне можно и не нажать — адрес должен быть виден."""
        js = _js()
        assert "lurl.textContent = st.login_url" in js

    def test_panel_still_renders_steps_and_log(self):
        js = _js()
        card = js[js.index('id="st-adguard"'):js.index('id="st-about"')]
        for needle in ('id="ag-setup"', 'id="ag-steps"', 'id="ag-setup-log"',
                       'id="ag-state"', 'id="ag-cancel"', 'id="ag-close"',
                       'id="ag-rule-row"', 'id="ag-cmd"', 'id="ag-recheck"'):
            assert needle in card, needle

    def test_no_password_field_anywhere(self):
        """Страховка v1.70.16 осталась в силе: пароля сервера нет."""
        js = _js()
        assert "ag-sudo-pw" not in js and "ag-sudo-row" not in js
        card = js[js.index('id="st-adguard"'):js.index('id="st-about"')]
        assert "Пароль сервера" not in card


# ==============================================================================
#  7) Стили переключателя
# ==============================================================================


class TestSwitchCss:
    def test_css_classes_present(self):
        css = _read(APP_CSS)
        for sel in (".ag-sw {", ".ag-sw-zone", ".ag-sw-knob", ".ag-sw-left",
                    ".ag-sw-right", ".ag-linkbox", ".ag-panel", ".ag-steps",
                    ".ag-log"):
            assert sel in css, sel

    def test_three_positions_are_styled(self):
        css = _read(APP_CSS)
        for pos in ('.ag-sw[data-pos="left"] .ag-sw-knob',
                    '.ag-sw[data-pos="right"] .ag-sw-knob'):
            assert pos in css, pos

    def test_colors_mean_on_and_off(self):
        """Зелёный = включён, красный = выключен (требование владельца)."""
        css = _read(APP_CSS)
        left = css[css.index('.ag-sw[data-pos="left"]'):]
        assert "var(--bad)" in left[:600]
        right = css[css.index('.ag-sw[data-pos="right"] .ag-sw-right'):]
        assert "var(--ok)" in right[:200]

    def test_respects_reduced_motion(self):
        css = _read(APP_CSS)
        assert "prefers-reduced-motion" in css
        assert "ag-sw-pulse" in css


# ==============================================================================
#  8) Бэкенд не сломан: маршруты те же
# ==============================================================================


class TestBackendCompatible:
    def test_routes_unchanged(self):
        """Переписан UI, а не движок: развёрнутый сервер обязан остаться
        совместимым с панелью."""
        src = _read(ROUTES)
        assert '"/telegram/adguard/setup"' in src
        assert '"/telegram/adguard/setup/preflight"' in src

    def test_ui_calls_same_routes(self):
        js = _js()
        assert js.count("/api/v1/settings/telegram/adguard/setup") >= 4
        assert "/api/v1/settings/telegram/adguard/setup/preflight" in js

    def test_actions_still_supported(self):
        js = _js()
        assert "{ action: 'start' }" in js
        assert "{ action: 'cancel' }" in js
        assert "{ action: 'off' }" in js

    def test_backend_engine_files_untouched_in_shape(self):
        """Помощник, автопилот и юниты остались (выбор владельца: ui_only)."""
        for p in (HELPER_SH, "deploy/adguard-vpn/tg-health.sh",
                  "deploy/adguard-vpn/adguard-vpn-telegram.service",
                  "deploy/adguard-vpn/tg-health.timer"):
            assert os.path.exists(p), p

    def test_helper_phases_used_by_backend_exist(self):
        js = _js()
        assert "ymaster-adguard-setup" in js


# ==============================================================================
#  9) Документация
# ==============================================================================


class TestDocs:
    def test_readme_mentions_switch(self):
        rd = _read(README)
        assert "переключател" in rd.lower() or "переключатель" in rd

    def test_changelog_entry(self):
        cl = _read(CHANGELOG)
        assert cl.count(f"## [{VER}]") == 1
        assert cl.index(f"## [{VER}]") < cl.index(f"## [{PREV}]")
        entry = cl[cl.index(f"## [{VER}]"):cl.index(f"## [{PREV}]")]
        for needle in ("псевдотерминал", "Failed to get auth link",
                       "ca-certificates", "st-adguard",
                       "трёхпозицион", "ссылка"):
            assert needle in entry, needle

    def test_whats_new_entry(self):
        js = _js()
        assert f"'{VER}':" in js
        entry = js[js.index(f"'{VER}':"):js.index(f"'{PREV}':")]
        assert "псевдотерминал" in entry
        assert "собственный блок" in entry

    def test_whats_new_1713_no_longer_promises_socks(self):
        """Запись 1.70.13 обещала SOCKS5 и кнопку в настройках Telegram —
        ни того, ни другого нет с 1.70.24/1.70.28."""
        js = _js()
        entry = js[js.index("'1.70.13':"):js.index("'1.70.12':")]
        assert "режим SOCKS5 поднимает локальный прокси" not in entry
        assert "кнопка-пресет «🛡 AdGuard VPN» в настройках Telegram" not in entry


# ==============================================================================
#  10) Версия
# ==============================================================================


class TestVersion:
    def test_app_version(self):
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= \
            (1, 70, 28)
        assert settings.APP_VERSION == VER

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_has_new_version(self, path):
        assert VER in _read(path), path

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_has_no_previous_version(self, path):
        assert PREV not in _read(path), path
