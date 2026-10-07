# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.47.0: архивная система копий (день/неделя/месяц),
# верификация копий, манифест состава, расписание BackupGuard.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import json
import os
import re


class TestVersion1470:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.47.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.46.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.47.0':" in js
        assert js.index("'1.47.0':") < js.index("'1.46.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.47.0]") == 1
        assert ch.index("## [1.47.0]") < ch.index("## [1.46.1]")
        assert "quick_check" in ch and "WAL" in ch


class TestArchiveTiers:
    def test_retention_includes_weekly(self):
        from app.services import backups
        assert backups.RETENTION["weekly"] == 2
        assert backups.RETENTION["archive"] == 12
        assert backups.RETENTION["daily"] == 7

    def test_weekly_keeps_two(self, client):
        from app.services import backups
        for _ in range(3):
            assert backups.create_backup("weekly")
        weekly = [b for b in backups.list_backups() if b["kind"] == "weekly"]
        assert len(weekly) == backups.RETENTION["weekly"]
        for w in weekly:
            assert w["rows"] is not None and "users" in w["rows"]
            assert w["copy_version"]

    def test_archive_monthly_immutable_and_single(self, client):
        from app.services import backups
        p1 = backups.create_backup("archive")
        p2 = backups.create_backup("archive")
        assert p1 == p2                       # одна месячная копия в месяц
        arch = [b for b in backups.list_backups() if b["kind"] == "archive"]
        assert len(arch) >= 1

    def test_preupdate_never_touches_archive_tiers(self, client):
        """Обновления не переписывают недельные и месячные копии."""
        from app.services import backups
        assert backups.create_backup("weekly")
        assert backups.create_backup("archive")
        before = {b["name"] for b in backups.list_backups()
                  if b["kind"] in ("weekly", "archive")}
        for _ in range(6):
            backups.create_backup("preupdate")
        after = {b["name"] for b in backups.list_backups()
                 if b["kind"] in ("weekly", "archive")}
        assert before <= after, "обновление тронуло архивную историю"

    def test_every_copy_verified_and_in_manifest(self, client):
        from app.services import backups
        items = backups.list_backups()
        with_manifest = [b for b in items if b["rows"] is not None]
        assert with_manifest, "копии без манифеста состава"
        man = backups._manifest_read()
        for b in with_manifest:
            assert b["name"] in man


class TestBackupGuard:
    def test_pick_kinds_schedule(self):
        from app.services.backups import DailyBackupGuard as G
        monday = dt.datetime(2026, 10, 5)      # понедельник, не 1-е число
        assert G.pick_kinds(monday) == ["daily", "weekly"]
        first = dt.datetime(2026, 11, 1)       # 1-е число, воскресенье
        assert G.pick_kinds(first) == ["daily", "archive"]
        both = dt.datetime(2024, 1, 1)         # понедельник И 1-е число
        assert sorted(G.pick_kinds(both)) == ["archive", "daily", "weekly"]
        plain = dt.datetime(2026, 10, 7)       # среда
        assert G.pick_kinds(plain) == ["daily"]

    def test_tick_creates_daily(self, monkeypatch, tmp_path):
        from app.services import backups
        calls = []
        monkeypatch.setattr(backups, "create_backup",
                            lambda k: calls.append(k))
        g = backups.DailyBackupGuard()
        g.tick()
        assert calls == ["daily"]              # обычный день
        g2 = backups.DailyBackupGuard()
        calls.clear()
        real_dt = backups.datetime

        class FakeDT(real_dt):
            @classmethod
            def now(cls):
                return cls(2026, 10, 5, 12, 0)  # понедельник

        monkeypatch.setattr(backups, "datetime", FakeDT)
        g2.tick()
        assert calls == ["daily", "weekly"]


class TestApi1470:
    def test_create_with_kind(self, client):
        from tests.conftest import login
        adm = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/backups", headers=adm,
                        json={"kind": "weekly"})
        assert r.status_code == 200 and r.json()["ok"] is True
        weekly = [b for b in r.json()["items"] if b["kind"] == "weekly"]
        assert weekly and weekly[0]["rows"] is not None
        # несуществующий тип — 422
        r = client.post("/api/v1/admin/backups", headers=adm,
                        json={"kind": "hourly"})
        assert r.status_code == 422

    def test_ui_shows_rows_and_kind_select(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "bk-kind" in js and "Недельная — хранятся 2" in js
        assert "В копии" in js and "rowsText" in js
        assert "чек." in js and "польз." in js and "комп." in js
        assert "каждый понедельник — недельная" in js
        assert "не переписывается" in js
