# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.45.0: чистка самохостингованной почты,
# почта Timeweb (465 SSL, пресет, сброс пароля ящика),
# SEO-страница «Проверить чек онлайн» (мета, JSON-LD, robots, sitemap).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import json
import re


class TestVersion1450:
    def test_versions_synced(self):
        # точный пин 1.45.0 перенесён в tests/test_v1451.py (версия ушла вперёд)
        cfg = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "' in cfg
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "v=1.44.1" not in idx and "v=1.44.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.45.0':" in js
        assert js.index("'1.45.0':") < js.index("'1.44.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.45.0]") == 1
        assert ch.index("## [1.45.0]") < ch.index("## [1.44.1]")


class TestRemoveMail:
    def test_script_safety(self):
        s = open("remove-mail.sh", encoding="utf-8").read()
        # удаляет только своё; защищает приложение и сервисы
        assert "apt-get purge -y -qq postfix opendkim opendkim-tools mailutils" in s
        # приложение только упоминается (комментарии/проверка) — rm/purge его не касаются
        import re as _re
        assert not _re.search(r"(rm\s|purge)[^\n]*ymaster-check", s)
        # предохранитель на пользователя: только служебный (nologin)
        assert "nologin" in s and "НЕ удаляю автоматически" in s
        # архив-страховка перед удалением
        assert "mail-legacy-backup-" in s
        assert "tar -czf" in s
        # deploy-флаг
        d = open("deploy.sh", encoding="utf-8").read()
        assert "--remove-mail" in d and "remove-mail.sh" in d
        import subprocess
        assert subprocess.run(["bash", "-n", "remove-mail.sh"],
                              capture_output=True).returncode == 0


class TestTimewebMail:
    def test_ssl_465_and_preset(self):
        m = open("app/pool/mailer.py", encoding="utf-8").read()
        assert "SMTP_SSL" in m and 'cfg["port"] == 465' in m
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "mc-timeweb" in js and "smtp.timeweb.ru" in js
        assert "'chek@ymaster.ru'" in js
        # пресет выключает STARTTLS (465 = SSL)
        assert "$m('mc-tls').checked = false;" in js

    def test_password_reset_endpoint(self, client):
        from tests.conftest import login
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        appsettings.set_setting(db, "smtp_pass", "старый-пароль")
        db.close()
        adm = login(client, "admin", "admin123")
        r = client.delete("/api/v1/mail-admin/config/password", headers=adm)
        assert r.status_code == 200 and r.json()["ok"] is True
        db = SessionLocal()
        assert appsettings.get_setting(db, "smtp_pass", "") == ""
        db.close()
        # повторный сброс — «пароля не было»
        r2 = client.delete("/api/v1/mail-admin/config/password", headers=adm)
        assert r2.status_code == 200 and "не было" in r2.json()["message"]
        hum = open("app/services/audit_human.py", encoding="utf-8").read()
        assert "mail_password_reset" in hum

    def test_465_sends_via_ssl(self, client, monkeypatch):
        """Правило бота при порте 465 использует SMTP_SSL (перехват)."""
        import smtplib
        import datetime as dt
        from app.database import SessionLocal
        from app.pool.models import MailRule, PoolUser
        from app.services import appsettings, mail_center
        sent = []

        class FakeSSL:
            def __init__(self, host, port, timeout=12):
                sent.append(("SSL", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                pass

            def send_message(self, msg):
                sent.append(("MSG", msg["To"], msg["From"]))

        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
        db = SessionLocal()
        appsettings.set_setting(db, "smtp_host", "smtp.timeweb.ru")
        appsettings.set_setting(db, "smtp_port", "465")
        appsettings.set_setting(db, "smtp_user", "chek@ymaster.ru")
        appsettings.set_setting(db, "smtp_pass", "pw")
        appsettings.set_setting(db, "smtp_from", "chek@ymaster.ru")
        appsettings.set_setting(db, "mail_bot_enabled", "1")
        u = PoolUser(email="tw@test.ru", email_verified=True)
        db.add(u)
        db.commit()
        db.refresh(u)
        pid = u.id
        db.add(MailRule(name="Timeweb-проверка", trigger="condition",
                        condition_key="pool_all", hh_mm="00:00",
                        subject="s", body="b"))
        db.commit()
        res = mail_center.tick(db, now=dt.datetime(2026, 10, 7, 12, 0))
        # уборка: тестовая БД общая, не оставлять правило/юзера/настройки
        db.query(MailRule).filter(MailRule.name == "Timeweb-проверka"
                                  ).delete()
        db.query(MailRule).filter(MailRule.name == "Timeweb-проверка"
                                  ).delete(synchronize_session=False)
        db.query(PoolUser).filter(PoolUser.email == "tw@test.ru"
                                  ).delete(synchronize_session=False)
        for k in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass",
                  "smtp_from", "mail_bot_enabled"):
            appsettings.set_setting(db, k, "")
        db.commit()
        db.close()
        assert any(x[0] == "SSL" and x[2] == 465 for x in sent), sent
        assert any(x[0] == "MSG" and x[1] == "tw@test.ru" and
                   "chek@ymaster.ru" in x[2] for x in sent), sent


class TestSeoLanding:
    def test_seo_injection_and_files(self, client):
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        assert "seo-injected" in html
        assert "Проверить чек онлайн по QR-коду" in html
        assert '<link rel="canonical" href="https://chek.ymaster.ru/">' in html
        assert 'property="og:title"' in html
        # JSON-LD валиден и содержит FAQPage + HowTo
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                      html, re.S)
        assert m, "JSON-LD не найден"
        ld = json.loads(m.group(1))
        types = {g["@type"] for g in ld["@graph"]}
        assert {"WebApplication", "FAQPage", "HowTo"} <= types
        # пререндер-текст для поисковиков
        assert "seo-landing" in html and "Частые вопросы" in html
        # robots + sitemap
        rb = client.get("/robots.txt")
        assert rb.status_code == 200 and "Sitemap:" in rb.text
        sm = client.get("/sitemap.xml")
        assert sm.status_code == 200 and "chek.ymaster.ru/" in sm.text

    def test_guest_landing_ui(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # лендинг: hero, шаги, форма, похвала, регистрация
        assert "Проверить чек онлайн" in js
        assert "landing-hero.jpg" in js and "landing-bonus.jpg" in js
        assert "pub-praise" in js and "Отлично, чек настоящий!" in js
        assert "Создать кабинет — забрать баллы" in js
        # SPA скрывает SEO-пререндер и гостей ведёт на лендинг
        assert "seo-landing" in js and "seo.remove()" in js
        for img in ("landing-hero.jpg", "landing-bonus.jpg"):
            import pathlib
            assert (pathlib.Path("app/static/img/manual") / img).is_file()
