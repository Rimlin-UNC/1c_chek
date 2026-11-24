"""Замок выпуска 1.73.9: AO-1 и полные копии чеков в серверном PDF.

Проверяем не только синхронизацию версии во всех точках кэша, но и сам
контракт выпуска: форма АО-1 в предпросмотре, все itemized копии чеков,
снятый лимит 40 QR и настоящий PDF endpoint в браузерном E2E.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.9"
PREV = "1.73.8"
BLOCKS = ("Печать и выгрузка в 1С", "Кабинет и отчёты")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class TestRelease1739:
    def test_version_and_updater_visibility(self):
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        assert updater._is_newer(cur, PREV) is True
        assert updater._is_newer(PREV, cur) is False
        about = TestClient(app).get("/api/v1/about")
        assert about.status_code == 200 and about.json()["version"] == cur

    def test_all_static_cache_busters_synced(self):
        cur = settings.APP_VERSION
        sw = read("app/static/sw.js")
        assert f'APP_VERSION: str = "{cur}"' in read("app/config.py")
        assert f"ymaster-check-v{cur}" in sw and sw.count(f"?v={cur}") == 5
        assert read("app/static/index.html").count(f"?v={cur}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={cur}") == 2
        assert f"/maxapp/maxapp.js?v={cur}" in read("app/static/maxapp/index.html")
        assert f'"version": "{cur}"' in read("app/static/manifest.webmanifest")
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/osm.js")
        assert f"server_monitor.js?v={cur}" in read("app/static/js/app.js")
        assert "/js/ao_folder.js" in sw

    def test_release_blocks_are_bumped_from_previous(self):
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for block in BLOCKS:
            assert _ver(reg[block]) >= _ver(VERSION), (block, reg[block])
            changed = {
                item["name"]: item
                for item in updater.diff_blocks(dict(reg, **{block: PREV}), reg)
            }
            assert changed[block]["from"] == PREV, block

    def test_changelog_whats_new_and_registry(self):
        changelog = read("CHANGELOG.md")
        assert changelog.startswith(f"## [{VERSION}]")
        entry = changelog.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 5000, len(entry)
        for needle in ("АО-1", "полные itemized копии", "PDF_QR_LIMIT",
                       "42 QR", "1.73.9"):
            assert needle in entry, needle
        assert changelog.index(f"## [{VERSION}]") < changelog.index(f"## [{PREV}]")

        js = read("app/static/js/app.js")
        new_key, old_key = f"'{VERSION}': [", f"'{PREV}': ["
        assert new_key in js and js.index(new_key) < js.index(old_key)
        block = js.split(new_key, 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 3
        for needle in ("АО-1", "все чеки целиком", "настоящем PDF"):
            assert needle in block, needle

        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)",
                          read("README.md"))
        assert match and _ver(match.group(1)) >= _ver(VERSION)
        assert (f"| Последний выпуск | **v{settings.APP_VERSION}**"
                in read("docs/kb/README.md"))
        journal = read("docs/ui_journal.md")
        assert journal.count(f"## v{VERSION} —") == 1
        assert journal.index(f"## v{VERSION} —") < journal.index(f"## v{PREV} —")

        with (ROOT / "docs/kb/09_releases.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        row = next(r for r in rows if r["Версия"] == VERSION)
        assert row["Дата"] == "2026-11-24"
        assert "pull/115" in row["Источник"]
        assert not [key for key, value in row.items() if "," in value]
        assert rows[-1]["Версия"] == settings.APP_VERSION

    def test_stage2_pdf_contract_is_locked(self):
        service = read("app/services/ao_pdf.py")
        assert "PDF_QR_LIMIT" not in service
        assert "capacity=32 * 1024 * 1024" in service
        assert "max_render_seconds=240" in service
        assert "_pack_ticket_pages" in service
        assert "_receipt_section" in service
        assert "_render_image_limit" in service
        ocr = read("app/primary/ocr_worker.py")
        assert "_image_pixel_limit" in ocr
        assert "Image.MAX_IMAGE_PIXELS = MAX_PIXELS" not in ocr
        router = read("app/routers/receipts.py")
        assert "_card.build_card(rec)" in router
        assert "cards_by_id" in router
        e2e = read("tests/e2e/ao_folder.spec.js")
        assert "Buffer.from('%PDF-1.4" not in e2e
        assert "route.fetch()" in e2e
        assert "realPdf.includes(Buffer.from('/FontFile'))" in e2e
