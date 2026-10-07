# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.45.3: корневая причина «главная не открывается» —
# сообщение о выключенном приёме стирало всю страницу; теперь заменяется
# только карточка проверки. Приём чеков включён по умолчанию.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1453:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.45.3"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.45.2" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.45.3':" in js
        assert js.index("'1.45.3':") < js.index("'1.45.2':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.45.3]") == 1
        assert ch.index("## [1.45.3]") < ch.index("## [1.45.2]")


class TestDisabledKeepsLanding:
    def test_message_scoped_to_check_card(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # замена только карточки #land-check (фолбэк — root, как в приложении)
        assert "const card = root.querySelector('#land-check') || root;" in js
        assert "card.innerHTML = '<div class=\"info-callout\">Приём чеков сейчас выключен" in js
        # прямую замену root убрали из этой ветки
        i = js.index("if (!info.enabled) {")
        body = js[i:js.index("return;", i)]
        assert "root.innerHTML" not in body

    def test_landing_structure_intact_regardless_of_pool(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        i = js.index("function landHeroHTML() {")
        hero = js[i:js.index("function landSimpleHTML() {", i)]
        # hero/шаги/кабинет/FAQ не зависят от pool/info — рендерятся всегда
        assert "land-hero-img" in hero and "land-steps" in hero
        assert "land-auth" in hero and "land-faq" in hero


class TestPoolEnabledByDefault:
    def test_default_is_on(self):
        s = open("app/pool/ingest.py", encoding="utf-8").read()
        m = re.search(r'appsettings\.get_setting\(db, SETT_ENABLED, "(\d)"\)', s)
        assert m and m.group(1) == "1", "дефолт приёма чеков должен быть «1»"

    def test_info_reports_enabled_on_clean_db(self, client):
        # чистая установка (настройки нет в базе) → приём включён
        from app.database import SessionLocal as SL
        from app.services.appsettings import AppSetting
        db = SL()
        row = db.get(AppSetting, "pool_enabled")
        if row is not None:               # явная настройка приоритетна
            db.delete(row)
            db.commit()
        db.close()
        r = client.get("/api/v1/public/pool/info")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

    def test_explicit_off_still_wins(self, client):
        from app.database import SessionLocal as SL
        from app.services import appsettings
        db = SL()
        appsettings.set_setting(db, "pool_enabled", "0")
        db.commit()
        db.close()
        r = client.get("/api/v1/public/pool/info")
        assert r.json()["enabled"] is False
        db = SL()
        appsettings.set_setting(db, "pool_enabled", "")
        db.commit()
        db.close()
