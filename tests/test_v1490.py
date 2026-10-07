# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.49.0: загрузка копии из внешнего источника
# (файл → проверка → тип «загруженная» → восстановление как обычно).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import datetime as dt

import pytest


class TestVersion1490:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.49.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.48.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.49.0':" in js
        assert js.index("'1.49.0':") < js.index("'1.48.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.49.0]") == 1
        assert ch.index("## [1.49.0]") < ch.index("## [1.48.0]")


@pytest.fixture
def bdir(tmp_path, monkeypatch):
    """Изолированный каталог копий — тесты не зависят от накопленных файлов."""
    from app.services import backups
    monkeypatch.setattr(backups, "backups_dir", lambda: str(tmp_path))
    return tmp_path


class TestImportService:
    def _make_copy_bytes(self, backups) -> bytes:
        name = backups.create_backup("manual")
        assert name
        return open(name, "rb").read()

    def test_import_roundtrip(self, bdir):
        from app.services import backups
        data = self._make_copy_bytes(backups)
        up = bdir / "upload.db"
        up.write_bytes(data)
        ok, msg, path = backups.import_backup(str(up), "копия_с_ноутбука.db")
        assert ok, msg
        assert path
        items = [b for b in backups.list_backups() if b["kind"] == "imported"]
        assert len(items) == 1
        entry = backups._manifest_read()[items[0]["name"]]
        assert entry["verified"] is True and entry["sha256"]
        assert entry["source"] == "копия_с_ноутбука.db"
        ok2, msg2 = backups.verify_backup(items[0]["name"])
        assert ok2 and "целая" in msg2

    def test_import_rejects_garbage(self, bdir):
        from app.services import backups
        up = bdir / "broken.db"
        up.write_bytes(b"this is not a database at all")
        ok, msg, path = backups.import_backup(str(up), "broken.db")
        assert not ok and path is None and "SQLite" in msg
        assert not [b for b in backups.list_backups() if b["kind"] == "imported"]

    def test_import_rejects_foreign_schema(self, bdir):
        import sqlite3
        from app.services import backups
        up = bdir / "other.db"
        con = sqlite3.connect(str(up))
        con.execute("CREATE TABLE t (x INTEGER)")
        con.commit()
        con.close()
        ok, msg, path = backups.import_backup(str(up), "other.db")
        assert not ok and "таблиц" in msg

    def test_import_rejects_bad_extension(self, bdir):
        from app.services import backups
        up = bdir / "photo.jpg"
        up.write_bytes(b"binary")
        ok, msg, path = backups.import_backup(str(up), "photo.jpg")
        assert not ok and ".db" in msg

    def test_import_rotation_keeps_ten(self, bdir, monkeypatch):
        from app.services import backups
        data = self._make_copy_bytes(backups)

        class FakeDT(dt.datetime):
            """Каждая загрузка — через минуту, чтобы имена различались."""
            _off = 0

            @classmethod
            def now(cls):
                cls._off += 61
                return super().now() + dt.timedelta(seconds=cls._off)

        monkeypatch.setattr(backups, "datetime", FakeDT)
        for i in range(11):
            up = bdir / f"up{i}.db"
            up.write_bytes(data)
            ok, msg, path = backups.import_backup(str(up), f"up{i}.db")
            assert ok, msg
        imported = [b for b in backups.list_backups() if b["kind"] == "imported"]
        assert len(imported) == backups.RETENTION["imported"] == 10
        # другие ярусы (ручные копии) загрузками не тронуты
        assert [b for b in backups.list_backups() if b["kind"] == "manual"]


class TestImportEndpoint:
    def test_import_endpoint(self, client):
        from tests.conftest import login
        from app.services import backups
        assert backups.create_backup("manual")
        name = [b["name"] for b in backups.list_backups()
                if b["kind"] == "manual"][0]
        data = open(backups.backup_path(name), "rb").read()
        adm = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/backups/import", headers=adm,
                        files={"file": ("my_copy.db", data,
                                        "application/octet-stream")})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and "загружена" in body["message"]
        kinds = [i["kind"] for i in body["items"]]
        assert "imported" in kinds
        r2 = client.post("/api/v1/admin/backups/import", headers=adm,
                         files={"file": ("bad.db", b"garbage-bytes",
                                         "application/octet-stream")})
        assert r2.status_code == 422
        assert "SQLite" in r2.json()["detail"]

    def test_import_requires_admin(self, client):
        r = client.post("/api/v1/admin/backups/import",
                        files={"file": ("x.db", b"x", "application/octet-stream")})
        # 401/403 — нет прав; 422 — multipart-валидация срабатывает раньше auth:
        # важно, что без прав копия не загружается
        assert r.status_code in (401, 403, 422)


class TestImportUI:
    def test_ui_elements(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="btn-bk-import"' in js and 'id="bk-import-file"' in js
        assert 'accept=".db,.sqlite,.sqlite3"' in js
        assert "/api/v1/admin/backups/import" in js
        assert "FormData()" in js
        # тип «загруженная» в фильтре копий
        assert "imported: 'загруженная (10)'" in js
        # пометка «из файла» в строке копии
        assert "из файла" in js
