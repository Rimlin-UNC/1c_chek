# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.13 «AdGuard VPN для Telegram»:
#  • комплект deploy/adguard-vpn (инструкция + systemd-юнит);
#  • пресет-кнопка в UI и подсказки;
#  • прокси-механизм v1.29.0 не тронут (socksio, PROXY_SCHEMES).
# ======================================================================
import re

from app.config import settings


class TestVersions17013:
    def test_version(self):
        # v1.70.14: строгий замок версии переехал в test_v17014
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 70, 13)

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.13]") == 1
        assert ch.index("## [1.70.13]") < ch.index("## [1.70.12]")

    def test_whatsnew(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.13':") < js.index("'1.70.12':")


class TestAdguardKit:
    def test_readme(self):
        rd_txt = open("deploy/adguard-vpn/README.md", encoding="utf-8").read()
        assert "AdGuardVPNCLI" in rd_txt and "install.sh" in rd_txt
        assert "config set-mode SOCKS" in rd_txt
        assert "adguardvpn-cli login" in rd_txt
        assert "socks5h://127.0.0.1:1080" in rd_txt
        assert "api.telegram.org" in rd_txt
        # v1.70.14: README — страница скачивания комплекта с GitHub
        # (файлов нет на сервере, пока их не скачали командами отсюда).
        assert "raw.githubusercontent.com/Rimlin-UNC/1c_chek" in rd_txt
        # v1.70.23: комплект скачивается в /tmp и ставится через `sudo install
        # -m 0755 -o root -g root`, а не записью curl прямо в
        # /usr/local/sbin — без root тот zapis всё равно падал, а с root
        # оставлял файл без нужных владельца/прав.
        assert "curl -fsSL -o /tmp/tg-health.sh" in rd_txt
        assert "install -m 0755 -o root -g root" in rd_txt
        assert "ln -sf /opt/adguardvpn_cli/adguardvpn-cli" in rd_txt

    def test_unit(self):
        unit = open("deploy/adguard-vpn/adguard-vpn-telegram.service",
                    encoding="utf-8").read()
        assert "adguardvpn-cli connect" in unit and "--no-fork" in unit
        assert "Restart=always" in unit
        assert "WantedBy=multi-user.target" in unit

    def test_proxy_stack_intact(self):
        # механизм v1.29.0 не тронут
        req = open("requirements.txt", encoding="utf-8").read()
        assert "socksio" in req
        tg = open("app/services/telegram_bot.py", encoding="utf-8").read()
        assert 'PROXY_SCHEMES = ("socks5://", "socks5h://", "http://", "https://")' in tg
        assert tg.count("set_mode") == 0            # режим CLI задаётся вручную


class TestUiAndHints:
    def test_preset_button(self):
        """v1.70.13 ставил в карточку Telegram кнопку-пресет.

        v1.70.28: у AdGuard VPN собственный блок в разделе «Устройство и
        служебное», из карточки Telegram-бота он удалён — там ему не место.
        Очистка поля прокси после установки осталась (в TUN локального
        слушателя 127.0.0.1:1080 нет, и бот не должен уйти в пустой прокси).
        """
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="st-adguard"' in js and 'data-sec="device"' in js
        assert 'id="ag-switch"' in js
        assert 'id="tg-adguard"' not in js, "в карточке Telegram следов AdGuard быть не должно"
        assert "prx.value = " in js
        assert "prx.value = 'socks5://127.0.0.1:1080'" not in js

    def test_hint_mentions_adguard(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Второй сервер не нужен" in js
        assert "deploy/adguard-vpn" in js

    def test_diagnose_mentions_adguard(self):
        tg = open("app/services/telegram_bot.py", encoding="utf-8").read()
        assert "deploy/adguard-vpn" in tg
        # старый замок v1290: домен в подсказке сохранён
        assert "api.telegram.org" in tg

    def test_manual_li(self):
        src = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "AdGuard VPN на этом же сервере" in src
        assert "deploy/adguard-vpn/README.md" in src


class TestHarness17013:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.13: пресет AdGuard VPN для Telegram" in h
