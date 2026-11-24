# -*- coding: utf-8 -*-
"""Замок выпуска 1.73.8: настоящий PDF авансового отчёта.

Закреплено:
 1. кнопка «Печать / PDF» предлагает сохранить файл или распечатать бланк;
 2. PDF текстовый, кириллический шрифт и лицензия в репозитории;
 3. бинарные байты доезжают до FSA/ZIP, а не перекодируются как UTF-8 строки;
 4. все точки версии и реестр выпусков синхронизированы.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from app.config import settings
from app.services import updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.8"
PREV = "1.73.7"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class TestRelease1738:
    def test_version_and_updater_visibility(self):
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        assert updater._is_newer(cur, PREV) is True
        assert updater._is_newer(PREV, cur) is False

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
        assert "/js/ao_folder.js" in sw      # обновлённый модуль в предзагрузке

    def test_whats_new_blocks_and_registry(self):
        js = read("app/static/js/app.js")
        assert "'1.73.8':" in js and "💾 Сохранить PDF" in js
        assert "/api/v1/receipts/advance-report/pdf" in js
        assert "aoPrintChoice" in js and "aoSavePdf" in js
        assert "id=\"ao-pdf-save\"" in js and "id=\"ao-pdf-print\"" in js
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert _ver(reg["Печать и выгрузка в 1С"]) >= _ver(VERSION)
        assert _ver(reg["Кабинет и отчёты"]) >= _ver(VERSION)
        changelog = read("CHANGELOG.md")
        assert changelog.startswith("## [1.73.8]")
        assert "## [1.73.7]" in changelog
        assert "Последний выпуск — v1.73.8" in read("README.md")
        assert "| Последний выпуск | **v1.73.8**" in read("docs/kb/README.md")
        assert read("docs/ui_journal.md").startswith("## v1.73.8")
        with (ROOT / "docs/kb/09_releases.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[-1]["Версия"] == settings.APP_VERSION
        assert "PDF" in rows[-1]["Суть"]

    def test_text_pdf_assets_and_dependencies(self):
        assert "xhtml2pdf>=0.2.29" in read("requirements.txt")
        font = ROOT / "app/assets/fonts/DejaVuSans.ttf"
        assert font.is_file() and font.stat().st_size > 500_000
        license_ = read("app/assets/fonts/LICENSE-DejaVu.txt")
        assert "Bitstream Vera" in license_ and "Permission is hereby granted" in license_
        service = read("app/services/ao_pdf.py")
        assert "pisa.CreatePDF" in service and "ToUnicode" in service
        assert "DejaVuSans.ttf" in service and "qrcode.make" in service
        assert "ResourceAccessPolicy" in service and "allow_remote=False" in service
        assert "_link_callback" in service
        assert "ReportLab Helvetica" not in service
        schemas = read("app/schemas.py")
        assert "class AdvanceReportPdfBody" in schemas
        router = read("app/routers/receipts.py")
        assert '@router.post("/advance-report/pdf"' in router
        assert "require_accountant" in router

    def test_binary_pdf_save_helpers_exist(self):
        folder = read("app/static/js/ao_folder.js")
        for needle in ("aoBlobBytes", "aoEntryBytes", "aoFileBytes",
                       "aoFileHandleForSave", "aoSaveFile", "aoPdfZipReadme"):
            assert needle in folder, needle
        assert "showSaveFilePicker" in folder
        assert "application/pdf" in read("app/static/js/app.js")
        # Имя файла для a.download остаётся ASCII: Chromium не должен получить «download».
        assert "aoAsciiFilename" in read("app/static/js/app.js")

    def test_no_inline_script_was_added(self):
        # CSP app/security.py разрешает только внешние script-src 'self'.
        idx = read("app/static/index.html")
        assert re.search(r"<script(?![^>]*\bsrc=)", idx) is None
        assert "script-src 'self'" in read("app/security.py")

    def test_vercel_demo_bundle_is_trimmed(self):
        # xhtml2pdf/ReportLab приблизили демо-бандл к потолку 500 МБ.
        # В сборку не кладём корневые тексты и WebP-дубли с JPEG-фолбэком.
        ignore = read(".vercelignore")
        for needle in ("/CHANGELOG.md", "/README.md", "/AGENTS.md", "/LICENSE",
                       "/app/static/img/blog/*.webp",
                       "/app/static/img/pages/*.webp",
                       "/app/static/img/manual/*.webp"):
            assert needle in ignore, needle
