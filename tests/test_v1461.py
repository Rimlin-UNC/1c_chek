# -*- coding: utf-8 -*-
# Тесты v1.46.1: блок отката рабочий и информативный, реестр без дублей.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
import json
import re


class TestVersion1461:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        # точный пин 1.46.1 перенесён в tests/test_v1470.py
        assert 'APP_VERSION: str = "' in cfg
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "app.css?v=" in idx and "app.js?v=" in idx
        assert "?v=1.46.0" not in idx and "?v=1.45" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert '"version":' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.46.1':" in js
        assert js.index("'1.46.1':") < js.index("'1.46.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.46.1]") == 1
        assert ch.index("## [1.46.1]") < ch.index("## [1.46.0]")


class TestRegistryDedupe:
    def test_mark_dedupes_by_version(self, tmp_path, monkeypatch):
        from app.services import releases
        monkeypatch.setattr(releases, "releases_dir", lambda: str(tmp_path))
        assert releases.mark_healthy("1.46.0", "a" * 40) is True
        assert releases.mark_healthy("1.46.0", "b" * 40) is False
        items = releases.list_releases()
        assert len(items) == 1 and items[0]["commit"] == "b" * 40
        bad = [{"version": "1.46.0", "commit": "c" * 40, "at": "z1"},
               {"version": "1.46.0", "commit": "d" * 40, "at": "z2"},
               {"version": "1.45.0", "commit": "e" * 40, "at": "z0"}]
        (tmp_path / "known_good.json").write_text(json.dumps(bad), encoding="utf-8")
        versions = [i["version"] for i in releases.list_releases()]
        assert versions.count("1.46.0") == 1
        assert set(versions) == {"1.46.0", "1.45.0"}

    def test_keep_three_distinct_versions(self, tmp_path, monkeypatch):
        from app.services import releases
        monkeypatch.setattr(releases, "releases_dir", lambda: str(tmp_path))
        for i in range(5):
            releases.mark_healthy(f"1.{i}.0", f"{i}" * 40)
        assert len(releases.list_releases()) == 3


class TestReleasesBlockUi:
    def _js(self):
        return open("app/static/js/app.js", encoding="utf-8").read()

    def test_rollback_buttons_and_refresh(self):
        js = self._js()
        assert 'data-rb="${esc(v.version)}|${esc(v.commit)}"' in js
        assert "rel-refresh" in js and "Обновить" in js
        assert "Реестр пока пуст" in js
        assert "Предыдущие рабочие" in js
        assert "из 3" in js

    def test_manual_commit_rollback(self):
        js = self._js()
        assert "rb-commit" in js and "rb-commit-go" in js
        assert "/^[0-9a-f]{7,40}$/.test(c)" in js
        adm = open("app/routers/admin.py", encoding="utf-8").read()
        assert 're.fullmatch(r"[0-9a-f]{7,40}", commit or "")' in adm

    def test_works_with_api_shapes(self):
        js = self._js()
        i = js.index("const relRender = (items) => {")
        body = js[i:js.index("const relLoad = async () => {", i)]
        assert "v.current" in body and "работает сейчас" in body
        assert "others()" in body
