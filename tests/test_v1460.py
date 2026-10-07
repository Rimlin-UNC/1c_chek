# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.46.0:
#   1) восстановление базы из резервных копий (UI-кнопка → API);
#   2) «последние рабочие версии»: реестр known-good, откат из
#      приложения и терминальный rollback.sh.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import os
import re
import shutil
import sqlite3
import subprocess

import pytest

from tests.conftest import login


class TestVersion1460:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.46.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.44" not in idx and "?v=1.45" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.46.0':" in js
        assert js.index("'1.46.0':") < js.index("'1.44.2':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.46.0]") == 1
        assert ch.index("## [1.46.0]") < ch.index("## [1.44.2]")


class TestBackupRestore:
    def test_restore_roundtrip(self, client):
        """Копия → изменение данных → восстановление → данные из копии."""
        from app.database import SessionLocal
        from app.services import appsettings, backups
        adm = login(client, "admin", "admin123")
        # 1) создаём копию
        r = client.post("/api/v1/admin/backups", headers=adm)
        assert r.status_code == 200
        items = r.json()["items"]
        assert items, "копия не создана"
        name = items[0]["name"]
        # 2) меняем состояние базы после копии
        db = SessionLocal()
        appsettings.set_setting(db, "restore_probe", "ПОСЛЕ-копии")
        db.close()
        # 3) восстанавливаем через API
        r = client.post("/api/v1/admin/backups/restore", headers=adm,
                        json={"name": name})
        assert r.status_code == 200, r.text
        assert "Страховая копия" in r.json()["message"]
        # 4) ключа «после копии» в восстановленной базе нет
        db = SessionLocal()
        assert appsettings.get_setting(db, "restore_probe", "") == ""
        db.close()
        # 5) страховая копия появилась в списке
        names = {b["name"] for b in backups.list_backups()}
        assert any("preupdate" in n for n in names), names
        # уборка зонда
        db = SessionLocal()
        from app.services.appsettings import AppSetting
        row = db.get(AppSetting, "restore_probe")
        if row:
            db.delete(row)
            db.commit()
        db.close()

    def test_restore_rejects_bad_names(self, client):
        adm = login(client, "admin", "admin123")
        for name in ("../etc/passwd", "db-fake.db", ""):
            r = client.post("/api/v1/admin/backups/restore", headers=adm,
                            json={"name": name})
            assert r.status_code in (404, 422), (name, r.status_code)

    def test_restore_detects_corrupted_copy(self, client):
        from app.services import backups
        d = backups.backups_dir()
        bad = os.path.join(d, "db-manual-broken-test.db")
        with open(bad, "wb") as f:
            f.write(b"not-a-sqlite-file" * 8)   # мусор вместо базы
        try:
            ok, msg = backups.restore_backup("db-manual-broken-test.db")
            assert ok is False and "повреждена" in msg
        finally:
            os.remove(bad)

    def test_restore_audit(self, client):
        from app.database import SessionLocal
        from app.models import AuditLog
        adm = login(client, "admin", "admin123")
        items = client.post("/api/v1/admin/backups", headers=adm).json()["items"]
        name = items[0]["name"]
        client.post("/api/v1/admin/backups/restore", headers=adm,
                    json={"name": name})
        db = SessionLocal()
        row = (db.query(AuditLog).filter(AuditLog.action == "backup_restore")
               .order_by(AuditLog.id.desc()).first())
        db.close()
        assert row is not None and name in (row.details or "")


class TestKnownGoodRegistry:
    def test_mark_and_keep_three(self, tmp_path, monkeypatch):
        from app.services import releases
        monkeypatch.setattr(releases, "releases_dir",
                            lambda: str(tmp_path))
        for i in range(5):
            releases.mark_healthy(f"1.{i}.0", f"{'a' * 39}{i}")
        items = releases.list_releases()
        assert len(items) == releases.KEEP == 3
        assert [i["version"] for i in items] == ["1.4.0", "1.3.0", "1.2.0"]
        assert all(len(i["commit"]) == 40 for i in items)

    def test_dedupe_and_invalid(self, tmp_path, monkeypatch):
        from app.services import releases
        monkeypatch.setattr(releases, "releases_dir",
                            lambda: str(tmp_path))
        assert releases.mark_healthy("1.0.0", "a" * 40) is True
        assert releases.mark_healthy("1.0.0", "a" * 40) is False  # дубль
        assert releases.mark_healthy("", "b" * 40) is False       # нет версии
        assert releases.mark_healthy("1.0.1", "unknown") is False  # нет коммита
        assert len(releases.list_releases()) == 1

    def test_find_by_version(self, tmp_path, monkeypatch):
        from app.services import releases
        monkeypatch.setattr(releases, "releases_dir",
                            lambda: str(tmp_path))
        releases.mark_healthy("2.0.0", "c" * 40)
        assert releases.find_by_version("2.0.0")["commit"] == "c" * 40
        assert releases.find_by_version("9.9.9") is None

    def test_marked_on_startup(self, client):
        """lifespan успешно стартовал → текущая версия в реестре рабочих."""
        from app.services import releases
        items = releases.list_releases(limit=5)
        assert any(i["version"] == "1.46.0" and i["current"] for i in items), items


class TestRollbackApiAndEngine:
    def test_releases_endpoint(self, client):
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/releases", headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert body["current"] == "1.46.0" and "items" in body and "busy" in body

    def test_rollback_validates_input(self, client):
        adm = login(client, "admin", "admin123")
        # мусор вместо версии/коммита
        r = client.post("/api/v1/admin/update/rollback", headers=adm,
                        json={"version": "не версия", "commit": "zzz"})
        assert r.status_code == 422
        # текущая версия — откат в себя же запрещён
        r = client.post("/api/v1/admin/update/rollback", headers=adm,
                        json={"version": "1.46.0"})
        assert r.status_code == 422 and "уже и так работает" in r.json()["detail"]
        # несуществующий коммит
        r = client.post("/api/v1/admin/update/rollback", headers=adm,
                        json={"version": "0.0.1", "commit": "a" * 40})
        assert r.status_code == 404

    def test_rollback_engine_real_git(self, tmp_path, monkeypatch):
        """Полный конвейер отката на реальном git-репозитории (2 коммита):
        файлы возвращаются, база и лишние каталоги не трогаются."""
        import app.services.updater as up
        dst = tmp_path / "appcopy"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks",
                        os.getcwd(), str(dst)], check=True)
        # «старая рабочая версия»: правим APP_VERSION и коммитим
        cfg = dst / "app" / "config.py"
        cur = re.search(r'APP_VERSION: str = "([^"]+)"',
                        cfg.read_text(encoding="utf-8")).group(1)
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       .replace(f'APP_VERSION: str = "{cur}"',
                                'APP_VERSION: str = "0.0.1"'), encoding="utf-8")
        subprocess.run(["git", "-C", str(dst), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(dst), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(dst), "commit", "-qam", "old"],
                       check=True)
        old_commit = subprocess.run(
            ["git", "-C", str(dst), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        # «новая сломанная»: меняем config и requirements, добавляем мусор
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       .replace('APP_VERSION: str = "0.0.1"',
                                'APP_VERSION: str = "0.0.2"'), encoding="utf-8")
        req = dst / "requirements.txt"
        req.write_text(req.read_text(encoding="utf-8") + "\n# broken\n",
                       encoding="utf-8")
        subprocess.run(["git", "-C", str(dst), "commit", "-qam", "broken"],
                       check=True)
        # данные, которые откат трогать не должен
        (dst / "data").mkdir(exist_ok=True)
        (dst / "data" / "ymaster_check.db").write_bytes(b"db-data-here")
        (dst / "мусор.txt").write_text("лишний файл", encoding="utf-8")

        monkeypatch.setattr(up, "APP_DIR", str(dst))
        monkeypatch.setattr(up, "_health_check", lambda: (True, "0.0.1"))
        monkeypatch.setattr(up, "_restart_service",
                            lambda *a, **k: (True, "restarted"))
        monkeypatch.setattr(up, "_append_history", lambda *a, **k: None)
        monkeypatch.setattr(up, "_pip_cmd", lambda: ["true"])  # пип не нужен

        up._do_rollback("0.0.1", old_commit)
        assert up.job.success, up.job.log
        assert up.job.action == "rollback"
        # файлы вернулись к рабочей версии (в «рабочем» коммите 0.0.1)
        assert 'APP_VERSION: str = "0.0.1"' in cfg.read_text(encoding="utf-8")
        assert 'APP_VERSION: str = "0.0.2"' not in cfg.read_text(encoding="utf-8")
        # база не тронута, мусор вычищен
        assert (dst / "data" / "ymaster_check.db").read_bytes() == b"db-data-here"
        assert not (dst / "мусор.txt").exists()
        # копия базы перед откатом создана
        assert any(n.startswith("db-preupdate-")
                   for n in os.listdir(dst / "data" / "backups"))

    def test_rollback_failure_restores_state(self, tmp_path, monkeypatch):
        """Если откат не удался (проверка целостности) — состояние
        возвращается ровно к тому, что было до попытки."""
        import app.services.updater as up
        dst = tmp_path / "appcopy2"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks",
                        os.getcwd(), str(dst)], check=True)
        cfg = dst / "app" / "config.py"
        cur = re.search(r'APP_VERSION: str = "([^"]+)"',
                        cfg.read_text(encoding="utf-8")).group(1)
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       .replace(f'APP_VERSION: str = "{cur}"',
                                'APP_VERSION: str = "0.0.1"'), encoding="utf-8")
        subprocess.run(["git", "-C", str(dst), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(dst), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(dst), "commit", "-qam", "old"],
                       check=True)
        old_commit = subprocess.run(
            ["git", "-C", str(dst), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       .replace('APP_VERSION: str = "0.0.1"',
                                'APP_VERSION: str = "0.0.2"'), encoding="utf-8")
        subprocess.run(["git", "-C", str(dst), "commit", "-qam", "broken"],
                       check=True)
        broken_head = subprocess.run(
            ["git", "-C", str(dst), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()

        monkeypatch.setattr(up, "APP_DIR", str(dst))
        monkeypatch.setattr(up, "_health_check",
                            lambda: (False, "импорт не удался (тест)"))
        monkeypatch.setattr(up, "_append_history", lambda *a, **k: None)

        up._do_rollback("0.0.1", old_commit)
        assert up.job.success is False
        assert up.job.rolled_back is True
        # состояние возвращено к «сломанному», как было до попытки
        head = subprocess.run(["git", "-C", str(dst), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
        assert head == broken_head


class TestTerminalRollback:
    def test_script_safety(self):
        s = open("rollback.sh", encoding="utf-8").read()
        assert subprocess.run(["bash", "-n", "rollback.sh"],
                              capture_output=True).returncode == 0
        # страховка базы до любых действий
        assert "db-prerollback-" in s and 'cp "$DB"' in s
        # файлы — через git reset на рабочую версию
        assert 'git reset --hard "$COMMIT"' in s
        # данные/секреты/venv/база не вычищаются
        assert "git clean -fd -e data -e .env -e venv -e '*.db'" in s
        # проверка здоровья после рестарта + подсказка при сбое
        assert "/api/v1/about" in s and "journalctl" in s

    def test_deploy_passthrough(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert "--rollback" in d and "rollback.sh" in d
        assert subprocess.run(["bash", "-n", "deploy.sh"],
                              capture_output=True).returncode == 0

    def test_ui_block(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Последние рабочие версии" in js
        assert "/api/v1/admin/update/releases" in js
        assert "/api/v1/admin/update/rollback" in js
        # восстановление копий из приложения
        assert "data-restore" in js
        assert "/api/v1/admin/backups/restore" in js
