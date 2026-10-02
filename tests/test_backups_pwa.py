# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.5.0: резервные копии, PWA, вёрстка маппинга.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import os

from tests.conftest import login


class TestBackups:
    def test_create_and_list(self, client):
        from app.services.backups import create_backup, list_backups
        p = create_backup("manual")
        assert p and os.path.exists(p)
        assert any(i["kind"] == "manual" for i in list_backups())

    def test_archive_once_per_month(self, client):
        from app.services.backups import create_backup
        a = create_backup("archive")
        b = create_backup("archive")
        assert a == b

    def test_retention(self, client):
        from app.services.backups import RETENTION, _cleanup, backups_dir
        kind = "daily"
        keep = RETENTION[kind]
        for i in range(keep + 2):
            open(os.path.join(backups_dir(), f"db-{kind}-000000000{i:04d}-120000.db"), "w").write("x")
        _cleanup(kind)
        left = [f for f in os.listdir(backups_dir()) if f.startswith(f"db-{kind}-")]
        assert len(left) <= keep

    def test_path_traversal_protected(self, client):
        from app.services.backups import backup_path
        assert backup_path("../../.env") is None
        assert backup_path("random.txt") is None

    def test_api_list_create_download(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/backups", headers=hdr)
        assert r.status_code == 200
        items = r.json()["items"]
        assert items
        manual = next(i for i in items if i["kind"] == "manual")
        d = client.get(f"/api/v1/admin/backups/{manual['name']}/download", headers=hdr)
        assert d.status_code == 200
        assert len(d.content) > 100
        assert client.get("/api/v1/admin/backups/..%2F.env/download", headers=hdr).status_code == 404

    def test_backups_require_admin(self, client):
        client.cookies.clear()
        assert client.get("/api/v1/admin/backups").status_code in (401, 403)


class TestPwaInfrastructure:
    def test_manifest_installable(self):
        import json
        mf = json.load(open("app/static/manifest.webmanifest", encoding="utf-8"))
        assert mf["display"] in ("standalone", "fullscreen")
        icons = mf.get("icons", [])
        assert any("192" in i.get("sizes", "") for i in icons)
        assert any("512" in i.get("sizes", "") for i in icons)
        assert mf.get("start_url") == "/"

    def test_icons_exist(self):
        assert os.path.exists("app/static/img/icon-192.png")
        assert os.path.exists("app/static/img/icon-512.png")

    def test_sw_registered_and_install_prompt_used(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "serviceWorker" in js
        assert "beforeinstallprompt" in js

    def test_install_button_in_sidebar(self):
        assert 'id="pwa-install-btn"' in open("app/static/index.html", encoding="utf-8").read()


class TestMappingMarkup:
    def test_map_row_structure(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "map-row" in js and "map-cell" in js
        assert "div.className = 'mapping-grid mapping-row'" not in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".map-row {" in css
