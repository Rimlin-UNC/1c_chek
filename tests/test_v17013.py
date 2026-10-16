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
        assert "deploy/adguard-vpn" not in rd_txt   # самоссылки не нужны

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
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="tg-adguard"' in js
        assert "socks5://127.0.0.1:1080" in js
        assert "#tg-adguard" in js and "prx.value = " in js

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
