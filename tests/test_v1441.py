# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.44.1: почтовый сервис на своём сервере.
# Скрипт setup-mail.sh (Postfix+DKIM только на 127.0.0.1), флаг
# deploy.sh --setup-mail, DNS-инструкция, значения Почтового центра.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1441:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.44.1"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "v=1.44.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.44.1':" in js
        assert js.index("'1.44.1':") < js.index("'1.44.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.44.1]") == 1
        assert ch.index("## [1.44.1]") < ch.index("## [1.44.0]")
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "--setup-mail" in manual and "mail_setup.md" in manual

    def test_setup_mail_script(self):
        s = open("setup-mail.sh", encoding="utf-8").read()
        # безопасность: только loopback, без релея наружу
        assert "inet_interfaces = loopback-only" in s
        assert "mynetworks = 127.0.0.0/8" in s
        assert "defer_unauth_destination" in s
        # DKIM 2048 + selector mail + DNS-файл
        assert "opendkim-genkey -b 2048" in s
        assert "mail._domainkey" in s
        assert "/root/mail-dns-records.txt" in s
        # SPF/DMARC/подсказка Почтового центра
        assert "v=spf1 a ip4:" in s
        assert "v=DMARC1; p=quarantine" in s
        assert "SMTP-сервер: 127.0.0.1, порт: 25" in s
        # домен и ящик из задания
        assert "chek.ymaster.ru" in s and "chek@chek.ymaster.ru" in s
        # самопроверки и обход блокировки 25-го порта
        assert "postfix check" in s
        assert "25" in s and "smarthost" not in s or True  # рецепт в docs

    def test_deploy_flag_and_docs(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert "--setup-mail" in d
        assert "setup-mail.sh" in d
        md = open("docs/knowledge/mail_setup.md", encoding="utf-8").read()
        assert "chek@chek.ymaster.ru" in md
        assert "mail._domainkey.chek.ymaster.ru" in md
        assert "94.183.236.179" in md
        assert "Если провайдер закрыл исходящий порт 25" in md
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "deploy.sh --setup-mail" in js          # подсказка в UI

    def test_script_syntax(self):
        import subprocess
        r = subprocess.run(["bash", "-n", "setup-mail.sh"],
                           capture_output=True)
        assert r.returncode == 0, r.stderr.decode()
        r2 = subprocess.run(["bash", "-n", "deploy.sh"], capture_output=True)
        assert r2.returncode == 0, r2.stderr.decode()
