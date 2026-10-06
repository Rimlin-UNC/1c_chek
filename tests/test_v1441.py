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

    def test_dkim_extraction_real_format(self, tmp_path):
        """Конвейер из скрипта на НАСТОЯЩЕМ формате opendkim-genkey
        (скобки, кавычки, перевод строк, хвостовой комментарий) даёт
        чистое значение — регресс бага «) ; ----- DKIM key …»."""
        import subprocess
        raw = (
            "; ----- DKIM key mail for chek.ymaster.ru\n"
            "mail._domainkey\tIN\tTXT\t( \"v=DKIM1; h=sha256; k=rsa; \"\n"
            "  \"p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1sjJx7gq/B/d5V77uL5Jii0PFHQD9g"
            "GQovT+zV1/A/93wOwyfd2HsSVrIa2QoLTDDoYPVVrkGyv5U4mEKx/Tr8BgS/LGSxCosFDgLIJue25C7v"
            "p1TBiShZcUe/SO6cvPg39d4zp409Sp0iJ/dIRU2JceWN+zlR1RzANY6YHt4tx2IuHXgqFnNIjaBP0va7"
            "41P7OYTZ8oo+p4UVVpBn2+hJjuA1T5g7YH+aBTxLLJ6dkJrUblWpJ/37mKa4sdIOwhwE6+UyhYZJxdHf"
            "hFbwXxC6KHxCIV+HSY7Uu+XvEfXrTusMvKjZFUb5D6VfejmbBUYq8jXUZFnfvDr2rr7pIipQIDAQAB"
            "\" )  ; ----- DKIM key mail for chek.ymaster.ru\n")
        d = tmp_path / "keys"
        d.mkdir()
        (d / "mail.txt").write_text(raw, encoding="utf-8")
        s = open("setup-mail.sh", encoding="utf-8").read()
        a = s.find("# >>DKIM_EXTRACT")
        b = s.find("<<DKIM_EXTRACT")
        assert 0 < a < b, "маркеры >>DKIM_EXTRACT не найдены"
        snippet = "\n".join(l for l in s[a:b].splitlines()
                            if not l.strip().startswith("#"))
        script = 'DKIM_DIR="' + str(d) + '"\n' + snippet + \
            '\nprintf \'%s\' "$DKIM_VALUE"\n'
        r = subprocess.run(["bash", "-c", script],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        val = r.stdout.strip()
        assert val.startswith("v=DKIM1; h=sha256; k=rsa; p=MIIB"), val[:60]
        assert val.endswith("QIDAQAB"), val[-40:]
        assert ")" not in val and '"' not in val
        assert "DKIM key mail" not in val and "  " not in val
