# -*- coding: utf-8 -*-
"""Замок выпуска 1.73.3: обновление блога, главной и трёх кластерных SEO-страниц (PR #111).

Проверки версии — структурные (>= 1.73.3), чтобы следующие выпуски не ломали этот тест
(правило docs/update_playbook.md, шаг 6). Поведение блога и кластерных страниц покрыто в
tests/test_blog_pages.py и tests/test_seo_pages.py; здесь — точки синхронизации выпуска,
механизм «🔄 Обновления» (updater), сброс кэша ?v= / Service Worker, WHATS_NEW, CHANGELOG,
README, журнал вёрстки и реестр выпусков docs/kb/09_releases.csv.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import seo, updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.3"
PREV = "1.73.2"
SEO_BLOCK = "Стартовая страница и поисковики (SEO)"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class TestRelease1733:
    def test_version_and_updater_visibility(self):
        """Рабочий сервер на 1.73.2 и старше должен видеть обновление 1.73.3+."""
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        for older in (PREV, "1.73.1", "1.73.0", "1.72.38"):
            assert updater._is_newer(cur, older) is True, older
            assert updater._is_newer(older, cur) is False, older

    def test_all_static_and_ssr_cache_busters_synced(self):
        cur = settings.APP_VERSION
        sw = read("app/static/sw.js")
        idx = read("app/static/index.html")
        b2b = read("app/static/b2b/index.html")
        mx = read("app/static/maxapp/index.html")
        mf = read("app/static/manifest.webmanifest")
        assert f'APP_VERSION: str = "{cur}"' in read("app/config.py")
        assert f"docver-v{cur}" in sw and sw.count(f"?v={cur}") == 5
        assert idx.count(f"?v={cur}") == 3
        assert b2b.count(f"?v={cur}") == 2
        assert f"/maxapp/maxapp.js?v={cur}" in mx
        assert f'"version": "{cur}"' in mf
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/app.js")
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/osm.js")

        client = TestClient(app)
        about = client.get("/api/v1/about")
        assert about.status_code == 200 and about.json()["version"] == cur

        blog_html = client.get("/blog", headers={"host": seo.PRODUCTION_HOST}).text
        assert f'/css/pages.css?v={cur}' in blog_html
        assert f'/js/blog.js?v={cur}' in blog_html

        cluster_html = client.get("/kak-proverit-chek", headers={"host": seo.PRODUCTION_HOST}).text
        assert f'/css/pages.css?v={cur}' in cluster_html

    def test_seo_block_bumped_for_update_report(self):
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert _ver(reg[SEO_BLOCK]) >= _ver(VERSION), reg[SEO_BLOCK]
        assert all(_ver(v) <= _ver(settings.APP_VERSION) for v in reg.values())
        old_reg = dict(reg, **{SEO_BLOCK: "1.72.7"})
        changed = {item["name"]: item for item in updater.diff_blocks(old_reg, reg)}
        assert SEO_BLOCK in changed and changed[SEO_BLOCK]["from"] == "1.72.7"

    def test_changelog_entry_fits_updater_excerpt(self):
        ch = read("CHANGELOG.md")
        assert ch.count(f"## [{VERSION}]") == 1
        assert ch.index(f"## [{VERSION}]") < ch.index(f"## [{PREV}]")
        entry = ch.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("WebP", "Image Sitemap 1.1", "17", "Schema.org", "карусель", "blocks.py"):
            assert needle in entry, needle

    def test_whats_new_entry_present_and_ordered(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        assert js.index(f"'{VERSION}': [") < js.index(f"'{PREV}': [")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Блог и главная", "Image Sitemap 1.1", "WebP", "карусель"):
            assert needle in block, needle

    def test_readme_kb_journal_and_ledger(self):
        readme_match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", read("README.md"))
        assert readme_match and readme_match.group(1) == settings.APP_VERSION
        assert _ver(readme_match.group(1)) >= _ver(VERSION)

        kb = read("docs/kb/README.md")
        assert f"| Последний выпуск | **v{settings.APP_VERSION}**" in kb

        journal = read("docs/ui_journal.md")
        assert journal.count(f"## v{VERSION} —") == 1
        assert journal.index(f"## v{VERSION} —") < journal.index(f"## v{PREV} —")

        rows = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        row = next(r for r in rows if r["Версия"] == VERSION)
        assert row["Дата"] == "2026-11-18"
        assert _ver(rows[-1]["Версия"]) >= _ver(VERSION)
        assert not [k for k, v in row.items() if "," in v], "в полях реестра нет запятых"
