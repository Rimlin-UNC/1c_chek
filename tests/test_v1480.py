# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.48.0: иерархия настроек (важное сверху),
# фильтры копий (тип+дата), проверка копии (quick_check + SHA-256).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1480:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.48.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.47.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.48.0':" in js
        assert js.index("'1.48.0':") < js.index("'1.47.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.48.0]") == 1
        assert ch.index("## [1.48.0]") < ch.index("## [1.47.0]")


class TestSettingsHierarchy:
    def test_sections_and_order(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # заголовки секций в порядке важности
        assert 'settings-sect" style="order:5">🗄 Данные и восстановление' in js
        assert 'settings-sect" style="order:19">🧾 Проверка чеков и почта' in js
        assert 'settings-sect" style="order:29">🧩 Чек-Пул и интеграции' in js
        assert 'settings-sect" style="order:39">👤 Личное и доступ' in js
        assert 'settings-sect" style="order:49">📱 Устройство и служебное' in js
        # критичные карточки получили меньшие order, чем служебные
        i_backup = js.index('💾 Резервные копии <span class="form-hint">(v1.5.0)</span>')
        seg_backup = js[js.rindex('glass card', i_backup - 500, i_backup):i_backup]
        assert 'order:10' in seg_backup
        i_upd = js.index('🔄 Обновления <span class="form-hint">(v1.6.0)</span>')
        seg_upd = js[js.rindex('glass card', i_upd - 500, i_upd):i_upd]
        assert 'order:11' in seg_upd
        i_dev = js.index('🧹 Обслуживание устройства')
        seg_dev = js[js.rindex('glass card', i_dev - 500, i_dev):i_dev]
        assert 'order:52' in seg_dev
        # пустые секции скрываются
        assert "querySelectorAll('.settings-sect')" in js
        assert "h.style.display = 'none'" in js

    def test_section_css(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".settings-sect {" in css and "grid-column: 1 / -1" in css


class TestBackupFiltersAndVerify:
    def test_manifest_has_sha256_and_verified(self, client):
        from app.services import backups
        assert backups.create_backup("manual")
        man = backups._manifest_read()
        entry = list(man.values())[-1]
        assert entry.get("verified") is True
        sha = entry.get("sha256")
        assert isinstance(sha, str) and len(sha) == 64

    def test_verify_ok_and_tamper(self, client):
        from app.services import backups
        name = None
        for b in backups.list_backups():
            if b["kind"] == "manual":
                name = b["name"]
                break
        assert name, "нет ручной копии"
        ok, msg = backups.verify_backup(name)
        assert ok and "хеш совпадает" in msg and "В копии:" in msg
        # подмена байта → хеш не совпадает
        import os
        p = backups.backup_path(name)
        data = open(p, "rb").read()
        open(p, "wb").write(data[:-1] + bytes([data[-1] ^ 0xFF]))
        try:
            ok2, msg2 = backups.verify_backup(name)
            assert not ok2 and "изменился" in msg2
        finally:
            open(p, "wb").write(data)   # вернуть как было

    def test_verify_endpoint(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        items = client.get("/api/v1/admin/backups", headers=adm).json()["items"]
        assert items and items[0].get("verified") is not None
        r = client.post("/api/v1/admin/backups/verify", headers=adm,
                        json={"name": items[0]["name"]})
        assert r.status_code == 200
        r2 = client.post("/api/v1/admin/backups/verify", headers=adm,
                         json={"name": "db-fake.db"})
        assert r2.status_code == 404

    def test_filters_ui(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "bk-filters" in js and "bk-date" in js and "bk-type" in js
        assert "Все даты" in js and "Все типы" in js
        assert "Копий в архиве" in js and "показано" in js
        assert "data-verify" in js and "/backups/verify" in js
        # фильтр действительно фильтрует таблицу
        assert "const visible = items.filter" in js
