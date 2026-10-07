# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.44.2: почта Timeweb — порт 465 как SMTP_SSL
# (точечный возврат поддержки из откатанной 1.45.0; остальное — 1.44.0).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import re


class TestVersion1442:
    def test_versions_synced(self):
        # точный пин 1.44.2 перенесён в tests/test_v1460.py (версия ушла вперёд)
        cfg = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "' in cfg
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "?v=1.44.0" not in idx and "?v=1.45" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.44.2':" in js
        assert js.index("'1.44.2':") < js.index("'1.44.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.44.2]") == 1
        assert ch.index("## [1.44.2]") < ch.index("## [1.44.0]")
        assert "SMTP_SSL" in ch


class TestSmtpSsl465:
    def test_mailer_source(self):
        m = open("app/pool/mailer.py", encoding="utf-8").read()
        assert 'cfg["port"] == 465' in m
        assert "smtplib.SMTP_SSL(" in m
        # STARTTLS не запускается внутри уже-защищённого SSL-соединения
        assert "tls_needed = False" in m
        # отката не было «навернул»: обычный путь остался
        assert "smtplib.SMTP(cfg[\"host\"], cfg[\"port\"], timeout=12)" in m

    def test_465_sends_via_ssl(self, client, monkeypatch):
        """Бот рассылок на порту 465 использует SMTP_SSL (перехват)."""
        import smtplib
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
        appsettings.set_setting(db, mail_center.SET_BOT_ON, "1")
        u = PoolUser(email="tw2@test.ru", email_verified=True)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.add(MailRule(name="Timeweb-465", trigger="condition",
                        condition_key="pool_all", hh_mm="00:00",
                        subject="s", body="b"))
        db.commit()
        mail_center.tick(db, now=dt.datetime.utcnow())
        # уборка: тестовая БД общая
        db.query(MailRule).filter(
            MailRule.name == "Timeweb-465").delete(synchronize_session=False)
        db.query(PoolUser).filter(
            PoolUser.email == "tw2@test.ru").delete(synchronize_session=False)
        for k in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass",
                  "smtp_from"):
            appsettings.set_setting(db, k, "")
        db.commit()
        db.close()
        assert any(x[0] == "SSL" and x[2] == 465 and x[1] == "smtp.timeweb.ru"
                   for x in sent), sent
        assert any(x[0] == "MSG" and x[1] == "tw2@test.ru" and
                   "chek@ymaster.ru" in x[2] for x in sent), sent

    def test_non465_path_untouched(self, client, monkeypatch):
        """Порт 587 (не 465) — прежний путь: SMTP + STARTTLS по флагу."""
        import smtplib
        from app.pool import mailer
        from app.database import SessionLocal
        from app.services import appsettings
        calls = []
        db = SessionLocal()
        appsettings.set_setting(db, "smtp_host", "smtp.test.ru")
        appsettings.set_setting(db, "smtp_port", "587")
        appsettings.set_setting(db, "smtp_user", "u@test.ru")
        appsettings.set_setting(db, "smtp_pass", "pw")
        appsettings.set_setting(db, "smtp_from", "u@test.ru")

        class FakeSMTP:
            def __init__(self, host, port, timeout=12):
                calls.append(("SMTP", port))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                calls.append(("STARTTLS",))

            def login(self, u, p):
                pass

            def send_message(self, msg):
                calls.append(("MSG",))

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        ok, err = mailer.send_mail(db, "x@test.ru", "s", "body")
        for k in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass",
                  "smtp_from"):
            appsettings.set_setting(db, k, "")
        db.commit()
        db.close()
        assert ok is True, err
        assert ("SMTP", 587) in calls and ("STARTTLS",) in calls
        assert not any(c[0] == "SSL" for c in calls)
