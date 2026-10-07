# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.50.0: последовательное обновление (фазы, время,
# килобайты), одно финальное уведомление, версии блоков, состояние на
# диске, кэш проверки, защита от двойного запуска.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import threading
import time


class TestVersion1500:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.50.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.49.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.50.0':" in js
        assert js.index("'1.50.0':") < js.index("'1.49.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.50.0]") == 1
        assert ch.index("## [1.50.0]") < ch.index("## [1.49.0]")


class TestBlocksRegistry:
    def test_registry_valid(self):
        from app.services import blocks, updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg, "реестр блоков пуст"
        assert reg == blocks.BLOCKS
        for name, ver in reg.items():
            assert re.match(r"^\d+\.\d+\.\d+$", ver), (name, ver)
        assert reg["Обновления"] == "1.50.0"
        assert reg["Резервные копии"] == "1.49.0"

    def test_diff_blocks(self):
        from app.services.updater import diff_blocks
        old = {"Копии": "1.1.0", "Почта": "1.2.0"}
        new = {"Копии": "1.5.0", "Почта": "1.2.0", "Пул": "1.4.0"}
        d = diff_blocks(old, new)
        assert {"name": "Копии", "from": "1.1.0", "to": "1.5.0"} in d
        assert {"name": "Пул", "from": "", "to": "1.4.0"} in d
        assert all(x["name"] != "Почта" for x in d)


class TestUpdateState:
    def test_parse_fetch_progress(self):
        from app.services.updater import _parse_fetch_progress
        pct, nbytes = _parse_fetch_progress(
            "Receiving objects:  45% (123/273), 55.00 KiB | 210.00 KiB/s")
        assert pct == 45 and nbytes == 55 * 1024
        pct2, nb2 = _parse_fetch_progress(
            "Receiving objects: 100% (300/300), 2.50 MiB | 5.00 MiB/s")
        assert pct2 == 100 and nb2 == int(2.5 * 1024 * 1024)
        assert _parse_fetch_progress("remote: Enumerating objects: 12") == (None, None)

    def test_job_phases_and_failure(self, tmp_path, monkeypatch):
        from app.services import updater
        monkeypatch.setattr(updater, "APP_DIR", str(tmp_path))  # не писать в data/
        updater.job.reset()
        updater.job.say("backup", 10, "копия…")
        time.sleep(0.01)
        updater.job.say("fetch", 25, "связь…")
        pub = updater.job.public()
        assert pub["phases"]["backup"]["status"] == "done"
        assert pub["phases"]["fetch"]["status"] == "active"
        assert pub["phases"]["backup"]["finished_at"] >= pub["phases"]["backup"]["started_at"]
        updater.job.note_failure()
        assert updater.job.public()["phases"]["fetch"]["status"] == "failed"
        updater.job.reset()

    def test_state_file_roundtrip(self, tmp_path, monkeypatch):
        from app.services import updater
        monkeypatch.setattr(updater, "APP_DIR", str(tmp_path))
        updater.job.reset()
        updater.job.running = False
        updater.job.finished = True
        updater.job.success = True
        updater.job.to_version = "9.9.9"
        updater.job.elapsed_s = 42
        updater.job.downloaded_bytes = 2048
        updater.job.blocks_changed = [{"name": "Обновления", "from": "1.49.0",
                                       "to": "1.50.0"}]
        updater._state_write()
        st = updater.load_update_state()
        assert st and st["success"] is True and st["to_version"] == "9.9.9"
        assert st["elapsed_s"] == 42 and st["downloaded_bytes"] == 2048
        assert st["blocks_changed"][0]["name"] == "Обновления"
        # job_status: память имеет приоритет у живого задания
        updater.job.running = True
        assert updater.job_status()["running"] is True
        updater.job.running = False
        assert updater.job_status()["to_version"] == "9.9.9"
        updater.job.reset()

    def test_single_flight(self, monkeypatch):
        from app.services import updater
        release = threading.Event()
        started = threading.Event()

        def fake_apply(*a, **kw):
            started.set()
            release.wait(5)
            updater.job.running = False
            updater.job.finished = True
            updater.job.success = True

        monkeypatch.setattr(updater, "_do_apply", fake_apply)
        updater.job.reset()
        updater.start_apply("9.9.9", "o/r", "main")
        assert started.wait(2)
        try:
            updater.start_apply("9.9.9", "o/r", "main")
            raise AssertionError("второй запуск должен быть отклонён")
        except RuntimeError as e:
            assert "уже выполняется" in str(e)
        finally:
            release.set()
            for _ in range(100):
                if not updater.job.running:
                    break
                time.sleep(0.05)
            updater.job.reset()


class TestCheckCache:
    def test_check_update_cached(self, monkeypatch):
        from app.services import updater
        calls = {"n": 0}

        def fake_remote(repo, branch):
            calls["n"] += 1
            return {"version": "99.0.0", "checked_at": "2026-10-07T00:00:00Z",
                    "source": "test"}

        monkeypatch.setattr(updater, "_remote_version", fake_remote)
        monkeypatch.setattr(updater, "_remote_changelog", lambda r, b: "")
        updater._check_cache.clear()
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            r1 = updater.check_update(db)
            r2 = updater.check_update(db)                 # из кэша
            assert calls["n"] == 1
            assert r1 == r2
            r3 = updater.check_update(db, force=True)     # мимо кэша
            assert calls["n"] == 2 and r3["update_available"] is True
        finally:
            db.close()
            updater._check_cache.clear()


class TestStatusEndpointAndUI:
    def test_status_endpoint(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/status", headers=adm)
        assert r.status_code == 200
        body = r.json()
        assert body["current_version"] == "1.50.0"
        assert "phases" in body["job"]
        assert isinstance(body["history"], list)

    def test_check_endpoint_force_param(self, client, monkeypatch):
        from tests.conftest import login
        from app.services import updater
        monkeypatch.setattr(updater, "_remote_version",
                            lambda r, b: {"version": "1.50.0",
                                          "checked_at": "2026-10-07T00:00:00Z",
                                          "source": "test"})
        monkeypatch.setattr(updater, "_remote_changelog", lambda r, b: "")
        adm = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/check", headers=adm)
        assert r.status_code == 200
        r2 = client.get("/api/v1/admin/update/check?force=1", headers=adm)
        assert r2.status_code == 200

    def test_ui_single_toast_and_progress(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # фазы и метрики
        assert "UPD_PHASES" in js and "upd-phases" in js
        assert "updFmtKB" in js and "загружено" in js
        assert "upd-elapsed" in js and "upd-summary" in js
        # один финальный тост: флаг finalShown, успех подтверждается версией
        assert "finalShown" in js
        assert "st.current_version === st.to_version" in js
        assert "ymaster-updated" in js        # SW-тост не дублируется
        assert "state.updModalOpen" in js     # баннер не дублируется в окне
        # опрос раз в 2 секунды, не чаще
        assert "}, 2000);" in js
        # кнопка ручной проверки — мимо кэша
        assert "/api/v1/admin/update/check?force=1" in js

    def test_rollback_uses_prepare(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        assert 'job.prepare("rollback", to_version=target_version)' in src
        assert "_apply_lock" in src
        # _do_apply больше не сбрасывает задание сам (иначе гонка)
        assert "job.reset()" not in src.split("def _do_apply")[1].split("def ")[0]
