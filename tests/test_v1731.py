# -*- coding: utf-8 -*-
"""Замок выпуска 1.73.1: «📁 Папка с УПД» — пределы архива и отбор файлов без лишнего трафика.

Выпуск исправляет T-79 по итогам проверки на настоящей выгрузке: архив в 50+ документов отдавал HTTP 400
на всю пачку, а браузер читал и отправлял то, что сервер всё равно отклонял (скрытые файлы, пустые файлы,
XML крупнее 5 МБ). Тесты-«мозги» живут в tests/test_primary_folder_limits.py и tests/js/primary_folder.test.js;
здесь — замок на то, что правки вообще на месте и версия не откатилась.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.1"

XML = ('<?xml version="1.0" encoding="windows-1251"?>'
       '<Файл ИдФайл="ON_NSCHFDOPPR_1" Функция="СЧФДОП"></Файл>').encode("cp1251")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in value.split("."))


def _zip(count: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for i in range(count):
            archive.writestr(f"внутри/ON_NSCHFDOPPR_{i}.xml", XML)
    return buf.getvalue()


# ====================================================================== выпуск
class TestRelease:
    def test_config_version(self):
        from app.config import settings
        assert _ver(settings.APP_VERSION) >= _ver(VERSION), settings.APP_VERSION

    def test_static_versions_synced(self):
        ver = read("app/config.py").split('APP_VERSION: str = "', 1)[1].split('"', 1)[0]
        sw = read("app/static/sw.js")
        idx = read("app/static/index.html")
        assert f"ymaster-check-v{ver}" in sw
        assert sw.count(f"?v={ver}") == 5
        assert idx.count(f"?v={ver}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={ver}") == 2
        assert "/maxapp/maxapp.js?v=" in read("app/static/maxapp/index.html")
        assert f'"version": "{ver}"' in read("app/static/manifest.webmanifest")

    def test_changelog_entry(self):
        ch = read("CHANGELOG.md")
        assert ch.count(f"## [{VERSION}]") == 1
        assert ch.index(f"## [{VERSION}]") < ch.index("## [1.73.0]")
        entry = ch.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("HTTP 400", "50", "скрытые", "5 МБ", "DecompressionStream", "SHA-256"):
            assert needle in entry, needle

    def test_whats_new_present(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        assert js.index(f"'{VERSION}': [") < js.index("'1.73.0': [")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Папка с УПД", "архив", "5 МБ", "СБИС"):
            assert needle in block, needle

    def test_readme_latest_release(self):
        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", read("README.md"))
        assert match and _ver(match.group(1)) >= _ver(VERSION)

    def test_ledger_row(self):
        import csv
        rows = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert rows[-1]["Версия"] == VERSION and rows[-1]["Дата"] == "2026-11-17"
        assert not [k for k, v in rows[-1].items() if "," in v], "в полях реестра нет запятых"

    def test_ui_journal_entry(self):
        journal = read("docs/ui_journal.md")
        assert journal.count(f"## v{VERSION} —") == 1
        entry = journal.split(f"## v{VERSION} —", 1)[1].split("\n## ", 1)[0]
        for needle in ("архив", "скрытые", "5 МБ", "замечан"):
            assert needle.lower() in entry.lower(), needle


# ====================================================================== сервер: предел архива
class TestArchiveLimit:
    def test_limit_trims_instead_of_refusing(self):
        """Главная причина выпуска: 51 документ давал 51 элемент и HTTP 400 на всю пачку."""
        from app.primary import service
        for count in (service.MAX_FILES + 1, 80):
            items = service.unpack("архив.zip", _zip(count))
            assert len(items) <= service.MAX_FILES + 1, count
            bodies = [b for _n, b, _r in items if b is not None]
            assert len(bodies) == service.MAX_FILES, count
            notes = [r for _n, b, r in items if b is None]
            assert len(notes) == 1 and "остальные" in notes[0]

    def test_router_does_not_raise_on_trimmed_archive(self):
        """Роутер считает элементы: обрезанный архив не должен попадать в ветку отказа."""
        source = read("app/primary/router.py")
        assert "dropped = len(items) - service.MAX_FILES" in source
        assert "raise HTTPException(400, f\"Не больше {service.MAX_FILES} документов за одну загрузку\")" not in source

    def test_limits_file_present(self):
        assert (ROOT / "tests" / "test_primary_folder_limits.py").is_file()


# ====================================================================== браузер: отбор
class TestClientScan:
    def test_hidden_empty_and_oversize_are_skipped(self):
        js = read("app/static/js/primary_folder.js")
        assert "primFolderServicePath" in js
        assert "PRIM_FOLDER_XML_MAX = 5 * 1024 * 1024" in js
        assert "count('hidden')" in js and "count('empty')" in js and "count('too_big')" in js

    def test_archive_is_split_into_documents(self):
        js = read("app/static/js/primary_folder.js")
        assert "export async function primFolderZipList" in js
        assert "DecompressionStream('deflate-raw')" in js
        assert "PRIM_FOLDER_ZIP_DOCS = 50" in js
        assert "primFolderEntryName" in js, "имя внутри архива читается по флагу UTF-8"

    def test_notices_are_shown(self):
        js = read("app/static/js/primary_folder.js")
        assert "prim-folder-notices" in js
        assert "notices" in js.split("export function primFolderSummaryHtml", 1)[1][:2000]
        assert "prim-folder-notices" in read("app/static/css/app.css")

    def test_no_silent_swallow_in_zip_reading(self):
        """Ошибка распаковки не должна молча давать «в архиве нет документов»."""
        js = read("app/static/js/primary_folder.js")
        block = js.split("async function primFolderZipDoc", 1)[1].split("export function primFolderServicePath", 1)[0]
        assert "return 'распаковка не удалась" in block
        assert "catch (e) {\n      return null;" not in block

    @pytest.mark.parametrize("needle", ["hidden:", "empty:", "too_big:"])
    def test_kind_titles_exist(self, needle):
        """Виды есть в словаре подписей — иначе итог покажет сырое имя вида вместо слов."""
        js = read("app/static/js/primary_folder.js")
        assert needle in js.split("PRIM_FOLDER_KINDS = {", 1)[1].split("};", 1)[0]
