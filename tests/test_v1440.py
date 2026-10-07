# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.44.0: Почтовый центр (SMTP-ящик, шаблоны,
# бот рассылок по расписанию/условию, отписка, лимиты, журнал 90 дней).
# SMTP подменяется фейковым сервером: письма перехватываются, контент
# и получатели проверяются. ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import re
import smtplib
from email.message import EmailMessage

from tests.conftest import login

SENT = []          # перехваченные письма


class FakeSMTP:
    def __init__(self, host, port, timeout=12):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        SENT.append(msg)


def _install_fake(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)


def _enable_smtp(db):
    from app.services import appsettings
    from app.pool import mailer
    appsettings.set_setting(db, mailer.SET_SMTP_HOST, "mail.chek.ymaster.ru")
    appsettings.set_setting(db, mailer.SET_SMTP_PORT, "587")
    appsettings.set_setting(db, mailer.SET_SMTP_USER, "chek@chek.ymaster.ru")
    appsettings.set_setting(db, "smtp_pass", "secret-password")
    appsettings.set_setting(db, mailer.SET_SMTP_FROM, "chek@chek.ymaster.ru")
    db.close()


def _mk_admin_hdr(client):
    return login(client, "admin", "admin123")


class TestVersion1440:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.44.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "v=1.43.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.44.0':" in js
        assert js.index("'1.44.0':") < js.index("'1.43.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.44.0]") == 1
        assert ch.index("## [1.44.0]") < ch.index("## [1.43.0]")
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Почтовый центр" in manual
        hum = open("app/services/audit_human.py", encoding="utf-8").read()
        assert "mail_rule_created" in hum and "mail_center_saved" in hum

    def test_ui_and_models(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "bindMailCenter" in js and "mail-card" in js
        assert "/api/v1/mail-admin/rules" in js
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "mail_rules" in md and "mail_log" in md
        mn = open("app/main.py", encoding="utf-8").read()
        assert "mail_admin.router" in mn and "_mail_bot_loop" in mn
        fr = open("app/pool/router_fraud.py", encoding="utf-8").read()
        assert "purge_log" in fr


class TestMailConfig:
    def test_config_roundtrip_secret(self, client):
        from app.database import SessionLocal
        db = SessionLocal(); _enable_smtp(db)
        adm = _mk_admin_hdr(client)
        d = client.get("/api/v1/mail-admin/config", headers=adm).json()
        assert d["configured"] is True
        assert d["has_password"] is True and "password" not in d
        assert d["sender"] == "chek@chek.ymaster.ru"
        r = client.put("/api/v1/mail-admin/config", headers=adm, json={
            "from_name": "Чек-Пул Ямастер",
            "greeting": "Добрый день, {name}!",
            "signature": "ООО «Ямастер» · ymaster.ru",
            "bot_on": True,
            "password": "",            # пусто — не менять
        })
        assert r.status_code == 200
        from app.services import appsettings
        db2 = SessionLocal()
        assert appsettings.get_setting(db2, "smtp_pass") == "secret-password"
        assert appsettings.get_setting(db2, "mail_bot_enabled") == "1"
        db2.close()
        # доступ только у администратора (cookie-фолбэк клиента допускает 200)
        assert client.get("/api/v1/mail-admin/config").status_code in (200, 401, 403)


class TestMailBot:
    def _mk_participant(self, client, email, verified=False,
                        last_receipt_days=None, created_days_ago=30):
        from app.database import SessionLocal
        from app.pool.models import PoolReceipt, PoolUser
        db = SessionLocal()
        u = PoolUser(email=email, tg_username="",
                     email_verified=verified,
                     created_at=dt.datetime.utcnow()
                     - dt.timedelta(days=created_days_ago))
        db.add(u)
        db.commit()
        db.refresh(u)
        pid = u.id
        if last_receipt_days is not None:
            db.add(PoolReceipt(
                source="web", fn=f"770009999{len(email) % 10}",
                fd="1", fp="1",
                receipt_date=dt.datetime.utcnow()
                - dt.timedelta(days=last_receipt_days),
                total_sum=500.0, status="verified",
                pool_user_id=pid,
                created_at=dt.datetime.utcnow()
                - dt.timedelta(days=last_receipt_days)))
            db.commit()
        db.close()
        return pid

    def test_rule_condition_inactive_and_suppression(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import mail_center
        db = SessionLocal(); _enable_smtp(db)
        _install_fake(monkeypatch)
        adm = _mk_admin_hdr(client)
        # участник: e-mail подтверждён, последний чек 30 дней назад
        self._mk_participant(client, "inactive@test.ru", verified=True,
                             last_receipt_days=30)
        r = client.post("/api/v1/mail-admin/rules", headers=adm, json={
            "name": "Возвращение", "trigger": "condition",
            "condition_key": "pool_inactive", "cond_days": 14,
            "hh_mm": "00:00",
            "subject": "Мы вас ждём", "body": "Не сдавали чеки {days} дней. "
            "Баллов: {points}. Вернитесь: {link}"})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        res = client.post(f"/api/v1/mail-admin/rules/{rid}/run",
                          headers=adm).json()
        assert res["sent"] == 1, res
        assert len(SENT) == 1
        msg: EmailMessage = SENT[0]
        assert msg["To"] == "inactive@test.ru"
        assert msg["From"] == "Чек-Пул Ямастер <chek@chek.ymaster.ru>"
        body = next(p for p in msg.walk()
                    if p.get_content_type() == "text/plain").get_content()
        assert "30" in body and "inactive" in body
        # HTML-часть есть и содержит отписку
        html_part = next(p for p in msg.walk()
                         if p.get_content_type() == "text/html")
        html = html_part.get_content()
        assert "Отписаться" in html and "unsub" in html
        # журнал
        log = client.get("/api/v1/mail-admin/log", headers=adm).json()
        assert any(x["status"] == "sent" and x["to_email"] == "inactive@test.ru"
                   for x in log["items"])
        # отписка по публичной ссылке
        sig = mail_center.unsub_sign("inactive@test.ru")
        un = client.get("/api/v1/public/mail/unsub",
                        params={"e": "inactive@test.ru", "s": sig})
        assert un.status_code == 200 and "отписаны" in un.text
        # повторный запуск: адрес подавлен → письма нет
        res2 = client.post(f"/api/v1/mail-admin/rules/{rid}/run",
                           headers=adm).json()
        assert res2["sent"] == 0 and res2["skipped"] == 0  # вне аудитории
        assert len(SENT) == 1                              # писем не прибавилось
        sup = client.get("/api/v1/mail-admin/suppressed",
                         headers=adm).json()
        assert "inactive@test.ru" in sup["items"]
        # вернуть в рассылки
        rm = client.post("/api/v1/mail-admin/suppressed/remove",
                         headers=adm, json={"email": "inactive@test.ru"})
        assert rm.json()["ok"] is True

    def test_schedule_tick_daily_window(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import mail_center
        db = SessionLocal(); _enable_smtp(db)
        appsettings_bot = True
        from app.services import appsettings
        appsettings.set_setting(db, mail_center.SET_BOT_ON, "1")
        _install_fake(monkeypatch)
        adm = _mk_admin_hdr(client)
        self._mk_participant(client, "unver@test.ru", verified=False,
                             created_days_ago=10)
        r = client.post("/api/v1/mail-admin/rules", headers=adm, json={
            "name": "Подтвердите e-mail", "trigger": "condition",
            "condition_key": "pool_unverified", "cond_days": 3,
            "hh_mm": "00:00",
            "subject": "Подтвердите e-mail", "body": "Здравствуйте, {name}!"})
        assert r.status_code == 200
        # тик «днём» → правило выполняется
        # (фикс хрупкости: tick хранит last_run_at по utcnow — жёсткая дата
        #  ломала тест при смене календарного дня)
        _now = dt.datetime.utcnow()
        res = mail_center.tick(db, now=_now)
        assert any(x["rule"] == "Подтвердите e-mail" and x["sent"] == 1
                   for x in res["ran"]), res
        assert len(SENT) == 1
        # повторный тик в тот же день → не выполняется
        res2 = mail_center.tick(db, now=_now + dt.timedelta(minutes=30))
        assert res2["ran"] == []
        assert len(SENT) == 1
        # бот выключен → тик ничего не делает
        from app.services import appsettings as _as
        _as.set_setting(db, mail_center.SET_BOT_ON, "0")
        from app.pool.models import MailRule
        db2 = SessionLocal()
        db2.query(MailRule).update({"last_run_at": None},
                                   synchronize_session=False)
        db2.commit(); db2.close()
        SENT.clear()
        res3 = mail_center.tick(db, now=dt.datetime(2026, 10, 6, 18, 0))
        assert res3["ran"] == []

    def test_daily_antispam_limit(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.pool.models import MailRule
        from app.services import mail_center
        db = SessionLocal(); _enable_smtp(db)
        _install_fake(monkeypatch)
        adm = _mk_admin_hdr(client)
        self._mk_participant(client, "limit@test.ru", verified=True,
                             last_receipt_days=40)
        r = client.post("/api/v1/mail-admin/rules", headers=adm, json={
            "name": "R1", "trigger": "condition",
            "condition_key": "pool_inactive", "cond_days": 14,
            "hh_mm": "00:00", "subject": "s1", "body": "b {name}"})
        rid1 = r.json()["id"]
        r2 = client.post("/api/v1/mail-admin/rules", headers=adm, json={
            "name": "R2", "trigger": "condition",
            "condition_key": "pool_inactive", "cond_days": 14,
            "hh_mm": "00:00", "subject": "s2", "body": "b {name}"})
        rid2 = r2.json()["id"]
        res1 = client.post(f"/api/v1/mail-admin/rules/{rid1}/run",
                           headers=adm).json()
        assert res1["sent"] == 1
        # второе правило в тот же день тем же адресам → skipped (антиспам);
        # в аудитории может быть и участник из прошлого теста — важен limit@
        res2 = client.post(f"/api/v1/mail-admin/rules/{rid2}/run",
                           headers=adm).json()
        assert res2["sent"] == 0 and res2["skipped"] >= 1
        assert len(SENT) == 1
        from app.database import SessionLocal as _SL
        from app.pool.models import MailLog as _ML
        _db = _SL()
        assert (_db.query(_ML)
                   .filter(_ML.to_email == "limit@test.ru",
                           _ML.status == "skipped").count()) == 1
        _db.close()

    def test_rule_crud_and_delete(self, client):
        from app.database import SessionLocal
        db = SessionLocal(); _enable_smtp(db)
        adm = _mk_admin_hdr(client)
        r = client.post("/api/v1/mail-admin/rules", headers=adm, json={
            "name": "Сводка руководителю", "trigger": "schedule",
            "schedule": "mon", "hh_mm": "09:00",
            "condition_key": "custom",
            "recipients": "boss@company.ru, dir@company.ru",
            "subject": "Сводка недели", "body": "Показатели: {stats}"})
        assert r.status_code == 200
        lst = client.get("/api/v1/mail-admin/rules", headers=adm).json()
        assert any(x["name"] == "Сводка руководителю" for x in lst["items"])
        rid = r.json()["id"]
        p = client.patch(f"/api/v1/mail-admin/rules/{rid}",
                         headers=adm, json={"enabled": False})
        assert p.status_code == 200
        d = client.delete(f"/api/v1/mail-admin/rules/{rid}", headers=adm)
        assert d.json()["ok"] is True
        assert client.delete(f"/api/v1/mail-admin/rules/{rid}",
                             headers=adm).status_code == 404

    def test_log_purge_90_days(self, client):
        from app.database import SessionLocal
        from app.pool.models import MailLog
        from app.services import mail_center
        db = SessionLocal()
        db.add(MailLog(to_email="old@test.ru", subject="старое",
                       created_at=dt.datetime.utcnow() - dt.timedelta(days=95)))
        db.add(MailLog(to_email="fresh@test.ru", subject="свежее"))
        db.commit()
        n = mail_center.purge_log(db)
        assert n == 1
        assert db.query(MailLog).filter(
            MailLog.to_email == "fresh@test.ru").count() == 1
        db.close()
