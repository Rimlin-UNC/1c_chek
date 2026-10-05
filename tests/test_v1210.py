# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.21.0: инструкция по ролям с картинками,
# баннер обновления, диалог выхода, сворачиваемые блоки, контраст тем.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from pathlib import Path

from tests.conftest import login

BASE = Path(__file__).resolve().parent.parent


def _mk(client, role, username):
    hdr = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites", json={"role": role}, headers=hdr).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


class TestManual:
    def test_requires_auth(self, client):
        client.cookies.clear()   # логин предыдущих тестов оставляет HttpOnly-cookie
        assert client.get("/api/v1/manual").status_code == 401

    def test_user_manual_scoped(self, client):
        h = _mk(client, "user", "man_usr")
        r = client.get("/api/v1/manual", headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["show_tabs"] is False
        auds = {s["audience"] for s in d["sections"]}
        assert "admin" not in auds and "accountant" not in auds
        assert "all" in auds and "user" in auds
        assert d["version"]

    def test_accountant_manual_scoped(self, client):
        h = _mk(client, "accountant", "man_acc")
        d = client.get("/api/v1/manual", headers=h).json()
        auds = {s["audience"] for s in d["sections"]}
        assert "admin" not in auds and "accountant" in auds

    def test_admin_full_and_tabs(self, client):
        h = login(client, "admin", "admin123")
        d = client.get("/api/v1/manual", headers=h).json()
        assert d["show_tabs"] is True
        auds = {s["audience"] for s in d["sections"]}
        assert {"all", "admin", "accountant", "user"} <= auds

    def test_images_exist_and_referenced(self):
        from app.services.manual_content import SECTIONS
        imgs = [s["image"] for s in SECTIONS if s.get("image")]
        assert len(imgs) >= 3
        for p in imgs:
            f = BASE / "app" / "static" / p.lstrip("/")
            assert f.is_file(), p

    def test_ui_manual_shell(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("openManual", "btn-manual", "manual-shell", "manual-tab",
                       "manual-sec", "/api/v1/manual", "актуально для версии"):
            assert needle in js, needle
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="btn-manual"' in idx and "📖 Инструкция" in idx

    def test_manual_cached_offline(self):
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "/img/manual/hero.jpg" in sw and "/img/manual/scan.jpg" in sw


class TestUpdateBanner:
    def test_no_auto_reload_on_server_update(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "showUpdateBanner" in js
        assert "hardReset(true, false)" in js        # кэш — тихо, без reload
        assert "ub-reload" in js and "ub-later" in js
        # в обработчике server_update больше нет авто-перезагрузки
        i = js.index("case 'server_update'")
        chunk = js[i:i + 400]
        assert "setTimeout(hardReset" not in chunk
        assert "showUpdateBanner" in chunk

    def test_hardreset_autoreload_guard(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "async function hardReset(silent = false, autoReload = true)" in js
        assert "if (autoReload) setTimeout(() => location.reload(), 400);" in js


class TestLogoutDialog:
    def test_dialog_and_default_stay(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function showLogoutDialog" in js
        assert "$('#btn-logout').onclick = showLogoutDialog" in js
        assert 'id="lo-stay"' in js and 'id="lo-exit"' in js
        assert "stay.focus()" in js                  # Enter = «Остаться»
        assert "Выйти из аккаунта?" in js
        assert "Остаться" in js

    def test_danger_button_css(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".btn-danger" in css


class TestCollapsibleSettings:
    def test_ui_fold(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("makeSettingsCollapsible", "fold-chevron", "fold-body",
                       "ymaster-fold", "foldable"):
            assert needle in js, needle
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".foldable.collapsed > .fold-body" in css
        assert ".fold-chevron" in css


class TestContrast:
    def test_light_theme_tokens(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        # v1.25.0: одна светлая тема — токены объявлены один раз в :root
        assert "--text-faint: #6b6b6b" in css
        assert "--info: #237a90" in css

    def test_dark_theme_tokens(self):
        # v1.25.0: тёмная тема удалена
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "--text-faint: #949494" not in css
        assert "--bad: #d4635f" not in css

    def test_accent_button_dark_text(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        i = css.index(".btn-accent {")
        chunk = css[i:i + 200]
        assert "color: #1a1a1a" in chunk                   # был #fff (2.3:1)

    def test_focus_visible(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ":focus-visible" in css


class TestVersion1210:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # актуальный пин — в тесте текущей версии (v1.22.0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert f"'{ver}':" in js
