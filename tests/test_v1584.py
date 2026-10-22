# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.4: Почтовый центр предзаполнен реквизитами
# mail_timeweb.md (smtp.timeweb.ru:465 SSL, chek@ymaster.ru) — админ
# вводит только пароль.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import pytest
import re


def _login(client):
    from tests.conftest import login
    return login(client, "admin", "admin123")


class TestVersion1584:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.59.0: структурная проверка — строгий замок версии в test_v1590
        assert tuple(int(x) for x in ver.split(".")) >= (1, 58, 4)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.58.3" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.58.4':") < js.index("'1.58.3':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.58.4]") == 1
        assert ch.index("## [1.58.4]") < ch.index("## [1.58.3]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Почтовый центр"] == "1.58.4"
        assert reg["Адаптивный интерфейс"] == "1.58.3"   # не задет
        assert tuple(int(x) for x in reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 58, 0)
        assert reg["Чек-Пул"] == "1.56.1"


class TestMailDefaults:
    """GET-эндпоинты возвращают реквизиты mail_timeweb.md по умолчанию."""

    @pytest.fixture(autouse=True)
    def _fresh_mail_defaults(self):
        """v1.58.5: перед тестом — пустые SMTP-настройки (только тогда
        эндпоинты обязаны вернуть значения по умолчанию); при параллельном
        прогоне соседние тест-файлы могут оставлять свои настройки."""
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            for _k in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                       "smtp_pass", "proverkacheka_token"):
                appsettings.set_setting(db, _k, "")
            db.commit()
        finally:
            db.close()
        yield

    def test_pool_smtp_defaults(self, client):
        hdr = _login(client)
        r = client.get("/api/v1/pool-admin/smtp", headers=hdr)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["host"] == "smtp.timeweb.ru"
        assert str(j["port"]) == "465"
        assert j["user"] == "chek@ymaster.ru"
        assert j["sender"] == "chek@ymaster.ru"
        assert j["base_url"] == "https://chek.ymaster.ru"
        assert j["configured"] is False        # пароля ещё нет — не настроено

    def test_mail_admin_config_defaults(self, client):
        hdr = _login(client)
        r = client.get("/api/v1/mail-admin/config", headers=hdr)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["host"] == "smtp.timeweb.ru"
        assert str(j["port"]) == "465"
        assert j["user"] == "chek@ymaster.ru"
        assert j["sender"] == "chek@ymaster.ru"

    def test_save_with_password_only_flow(self, client):
        """Реальный сценарий: форма с предзаполненными полями + пароль →
        сохранено, configured=True; пароль не возвращается."""
        hdr = _login(client)
        r = client.put("/api/v1/pool-admin/smtp", headers=hdr, json={
            "host": "smtp.timeweb.ru", "port": 465,
            "user": "chek@ymaster.ru", "password": "СекретЯщика123",
            "sender": "chek@ymaster.ru", "tls": True,
            "base_url": "https://chek.ymaster.ru"})
        assert r.status_code == 200, r.text
        j = client.get("/api/v1/pool-admin/smtp", headers=hdr).json()
        assert j["configured"] is True and j["has_password"] is True
        assert "password" not in j             # пароль наружу не отдаётся


class TestFormHints:
    def test_mc_form_updated(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="mc-port" type="number" value="465"' in js
        assert 'placeholder="smtp.timeweb.ru"' in js
        assert js.count('placeholder="chek@ymaster.ru"') >= 2
        assert "для порта 465 не важен" in js
        assert "cfg.port || 465" in js
        # в ФОРМЕ почтового центра старых подсказок нет
        # (в исторических записях WHATS_NEW старый адрес — легитимен)
        form = js.split('id="mail-card"')[1].split('id="mc-save"')[0]
        assert "mail.chek.ymaster.ru" not in form
        assert "chek@chek.ymaster.ru" not in form

    def test_pool_form_hint_updated(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'placeholder="smtp.yandex.ru"' not in js

    def test_doc_updated(self):
        d = open("docs/knowledge/mail_timeweb.md", encoding="utf-8").read()
        assert "с v1.58.4 сервер, порт" in d
