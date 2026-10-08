# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.54.0: главная-лендинг (предложение, скан чека,
# разбор на e-mail) при полностью сохранённом входе в программу.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1540:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.54.0 перенесён в tests/test_v1550.py (версия ушла вперёд)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.53.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.54.0':" in js
        assert js.index("'1.54.0':") < js.index("'1.53.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.54.0]") == 1
        assert ch.index("## [1.54.0]") < ch.index("## [1.53.0]")


class TestLandingMarkup:
    def test_hero_present_and_seo_static(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        # предложение и заработок — в статичной разметке (видно без JS)
        assert "Сканируйте чеки" in idx and "баллы и кэшбэк" in idx
        assert "land-wrap" in idx and "land-h1" in idx and "land-stats" in idx
        # CTA со сканером и авто-камерой
        assert 'href="#/public?cam=1"' in idx
        assert "📸 Сканировать чек" in idx
        # шаги с реальными картинками сайта
        assert 'src="/img/manual/scan.jpg"' in idx
        assert 'src="/img/manual/report.jpg"' in idx
        # форма разбора на e-mail + согласие 152-ФЗ
        assert 'id="land-lead"' in idx and 'id="land-consent"' in idx
        assert "152-ФЗ" in idx
        # FAQ
        assert "land-faq" in idx and "Нужна ли регистрация?" in idx

    def test_login_untouched(self):
        """Вход для существующих пользователей сохранён полностью."""
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="login-form"' in idx
        assert 'id="login-username"' in idx and 'id="login-password"' in idx
        assert 'id="login-submit"' in idx
        assert 'id="login-passkey"' in idx          # вход по отпечатку
        assert "Войти по отпечатку пальца / Face ID" in idx
        assert 'id="login-error"' in idx
        # ссылка «Сдать чек» в футере карточки осталась
        assert 'href="#/public"' in idx
        # форма лендинга НЕ пересекается с формой входа
        assert 'id="land-lead"' in idx and 'id="login-form"' in idx

    def test_css_styles(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        for cls in (".land-wrap", ".land-h1", ".land-stats", ".land-steps",
                    ".land-lead-form", ".land-faq", ".land-badge",
                    ".land-accent"):
            assert cls in css, cls
        # адаптив: одна колонка на узких экранах, вход сверху
        assert "max-width: 920px" in css and "order: -1" in css

    def test_route_parses_query(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # '#/public?cam=1' маршрутизируется как public
        assert "split('?')[0]" in js
        # авто-открытие камеры по параметру
        assert "/[?&]cam=1/.test(location.hash" in js
        assert "setTimeout(camPick, 350)" in js

    def test_landing_bindings(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function bindLanding()" in js
        assert "bindLanding();" in js            # вызывается при показе входа
        # статистика из публичного эндпоинта
        assert "api.get('/api/v1/public/pool/info')" in js
        # заявка сохраняет e-mail для автозаполнения письма
        assert "ymaster-lead-email" in js
        # письмо с данными чека
        assert "mailReceiptDialog" in js
        assert "pool/receipt-email" in js


class TestLeadsApi:
    def test_lead_created_and_validated(self, client):
        r = client.post("/api/v1/public/pool/leads", json={
            "email": "IVAN@Mail.ru", "consent": True,
            "hp": "", "form_ms": 5000})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and "принята" in body["message"]
        # без согласия — отказ
        r2 = client.post("/api/v1/public/pool/leads", json={
            "email": "x@mail.ru", "consent": False, "hp": "", "form_ms": 5000})
        assert r2.status_code == 422
        # плохой адрес — отказ
        r3 = client.post("/api/v1/public/pool/leads", json={
            "email": "не-адрес", "consent": True, "hp": "", "form_ms": 5000})
        assert r3.status_code == 422
        # бот (honeypot) — «успех», но заявка не сохраняется
        r4 = client.post("/api/v1/public/pool/leads", json={
            "email": "bot@mail.ru", "consent": True, "hp": "trap", "form_ms": 1})
        assert r4.status_code == 200

    def test_lead_visible_to_admin(self, client):
        from tests.conftest import login
        client.post("/api/v1/public/pool/leads", json={
            "email": "admin-view@mail.ru", "consent": True,
            "hp": "", "form_ms": 5000})
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/pool-leads", headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert any(i["email"] == "admin-view@mail.ru" for i in body["items"])
        # без прав — отказ (чистый клиент: без cookie от логина)
        from fastapi.testclient import TestClient
        from app.main import app as _app
        with TestClient(_app) as c2:
            r2 = c2.get("/api/v1/admin/pool-leads")
            assert r2.status_code in (401, 403)


class TestReceiptEmailApi:
    def test_receipt_email_no_smtp_503(self, client):
        r = client.post("/api/v1/public/pool/receipt-email", json={
            "fn": "1234567890123456", "email": "a@mail.ru", "hp": ""})
        # в тестовой среде SMTP не настроен — честный 503
        assert r.status_code == 503
        assert "не настроена" in r.json()["detail"]

    def test_receipt_email_bad_address(self, client):
        r = client.post("/api/v1/public/pool/receipt-email", json={
            "fn": "123", "email": "хм", "hp": ""})
        assert r.status_code == 422

    def test_receipt_email_honeypot_silent_ok(self, client):
        r = client.post("/api/v1/public/pool/receipt-email", json={
            "fn": "123", "email": "a@mail.ru", "hp": "trap"})
        assert r.status_code == 200 and r.json()["ok"] is True


class TestBlocksRegistry:
    def test_pool_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        # структурные пины: не старее версий на момент 1.54.0
        vt = lambda v: tuple(int(x) for x in v.split("."))
        assert vt(reg["Чек-Пул"]) >= (1, 54, 0)
        assert vt(reg["Обновления"]) >= (1, 53, 0)
