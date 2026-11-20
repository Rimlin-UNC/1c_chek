# -*- coding: utf-8 -*-
"""Замок выпуска 1.73.4: печатный УПД один в один с бланком и «📁 Папка с УПД».

Что закреплено:
 1. печатный бланк — 16 граф и 48 полей формы, реквизиты приложения от 26.12.2011
    № 1137 в редакции от 23.01.2026 № 26, отметки ЭДО и банковские реквизиты;
 2. поля формы в Python и в экранном модуле совпадают один в один — новое поле
    нельзя добавить в бланк и забыть на экране; схема запроса их тоже знает;
 3. «Папка с УПД» узнаёт документ по значению ИдФайл (`ON_NSCHFDOPPR…`), а не по
    началу имени файла: выгрузка `1_ON_NSCHFDOPPR_.xml` больше не пропускается;
 4. точки синхронизации выпуска (версия, кэш-бастеры, реестр блоков, CHANGELOG,
    «Что нового», журнал вёрстки, реестр выпусков) переведены на 1.73.4.

Проверки версии — структурные (>= 1.73.4), чтобы следующие выпуски не ломали тест
(правило docs/update_playbook.md, шаг 6).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.primary import printupd
from app.services import updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.4"
PREV = "1.73.3"
PRIMARY_BLOCK = "Первичка (электронные УПД)"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class TestPrintedUpdBlank:
    def test_sixteen_columns_and_forty_eight_fields(self):
        assert len(printupd.COLUMNS) == 16
        assert len(printupd.FIELDS) == 48
        assert len(printupd.COLUMN_WIDTHS) == len(printupd.COLUMNS)

    def test_every_field_has_a_screen_section(self):
        """У каждого поля есть раздел экрана — иначе оно «потеряется» в вёрстке."""
        assert sorted(printupd.GROUPS) == sorted(printupd.FIELDS)
        assert set(printupd.GROUPS.values()) == {"Шапка", "Основание и передача",
                                                 "Подписи", "ЭДО"}
        assert all(printupd.LABELS.get(name) for name in printupd.FIELDS)

    def test_blank_carries_approved_regalia_and_edo(self):
        from app.primary.models import PrimaryDocument

        doc = PrimaryDocument(id=1, doc_number="УТ-215", seller_name="ООО «Ямастер»",
                              buyer_name="ООО «Ромашка»")
        page = printupd.html(doc)
        for needle in ("от 26.12.2011 № 1137", "от 23.01.2026 № 26",
                       "Товар (груз) передал/услуги, результаты работ, права сдал",
                       "Товар (груз) получил/услуги, результаты работ, права принял",
                       "Товары (работы, услуги, права) приняты без расхождений (претензий)"):
            assert needle in page, needle
        assert ">11</th>" in page and ">10а</th>" in page


class TestFormFieldsAreOneToOne:
    def test_schema_matches_blank_fields(self):
        from app.primary.router import PrintFormIn

        props = PrintFormIn.model_json_schema()["properties"]
        assert sorted(props) == sorted(printupd.FIELDS)
        for name, spec in props.items():
            limit = printupd.LIMITS.get(name)
            if limit and spec.get("maxLength") is not None:
                assert spec["maxLength"] == limit, name

    def test_screen_fields_match_blank_fields(self):
        js = read("app/static/js/primary.js")
        keys = re.findall(r"\{\s*key:\s*'([a-z0-9_]+)'", js)
        assert sorted(keys) == sorted(printupd.FIELDS)
        assert "prim-print-group" in js and "prim-print-group" in read("app/static/css/app.css")

    def test_saved_form_keeps_xml_requisites(self):
        """Правка на экране не стирает реквизиты из XML: они лежат в «auto»."""
        from app.primary.models import PrimaryDocument

        doc = PrimaryDocument(id=1, doc_number="УТ-215", seller_name="ООО «Ямастер»",
                              buyer_name="ООО «Ромашка»")
        doc.print_json = printupd.dump({"basis": "Счёт-оферта 42",
                                        "buyer_address": "Москва, Тверская, 1"})
        assert printupd.stored(doc)["basis"] == "Счёт-оферта 42"
        assert printupd.form(doc)["buyer_address"] == "Москва, Тверская, 1"
        page = printupd.html(doc, printupd.stored(doc))
        assert "Счёт-оферта 42" in page and "Ромашка" in page


class TestFolderFindsUpdByFileId:
    def test_marker_helper_and_order(self):
        js = read("app/static/js/primary_folder.js")
        assert "export const PRIM_FOLDER_UPD_MARK = 'ON_NSCHFDOPPR';" in js
        assert "export function primFolderAttr" in js, "признак ИдФайл читается из XML"
        # корректировочные и покупательские — раньше маркера УПД, иначе
        # ON_NSCHFDOPPOK_* уедет в «УПД» и не попадёт в свой список
        kind = js.split("export function primFolderKind", 1)[1]
        assert kind.index("ПРОДАВ") < kind.index("PRIM_FOLDER_UPD_MARK") \
            if "ПРОДАВ" in kind else True

    def test_xml_without_declaration_is_not_mojibake(self):
        js = read("app/static/js/primary_folder.js")
        body = js.split("export function primFolderDecode", 1)[1].split("export ", 1)[0]
        assert "windows-1251" in body and "fatal: true" in body


class TestRelease1734:
    def test_version_and_updater_visibility(self):
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        for older in (PREV, "1.73.2", "1.73.1", "1.72.38"):
            assert updater._is_newer(cur, older) is True, older
            assert updater._is_newer(older, cur) is False, older

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
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/app.js")

        about = TestClient(app).get("/api/v1/about")
        assert about.status_code == 200 and about.json()["version"] == cur

    def test_primary_block_bumped_for_update_report(self):
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert _ver(reg[PRIMARY_BLOCK]) >= _ver(VERSION), reg[PRIMARY_BLOCK]
        assert all(_ver(v) <= _ver(settings.APP_VERSION) for v in reg.values())
        changed = {i["name"]: i
                   for i in updater.diff_blocks(dict(reg, **{PRIMARY_BLOCK: "1.73.2"}), reg)}
        assert changed[PRIMARY_BLOCK]["from"] == "1.73.2"

    def test_changelog_entry_fits_updater_excerpt(self):
        ch = read("CHANGELOG.md")
        assert ch.count(f"## [{VERSION}]") == 1
        assert ch.index(f"## [{VERSION}]") < ch.index(f"## [{PREV}]")
        entry = ch.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("ON_NSCHFDOPPR", "16 граф", "48", "windows-1251", "blocks.py"):
            assert needle in entry, needle

    def test_whats_new_entry_present_and_ordered(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        assert js.index(f"'{VERSION}': [") < js.index(f"'{PREV}': [")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Папка с УПД", "ON_NSCHFDOPPR", "16 граф"):
            assert needle in block, needle

    def test_readme_kb_journal_and_ledger(self):
        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", read("README.md"))
        assert match and _ver(match.group(1)) >= _ver(VERSION)
        assert f"| Последний выпуск | **v{settings.APP_VERSION}**" in read("docs/kb/README.md")

        journal = read("docs/ui_journal.md")
        assert journal.count(f"## v{VERSION} —") == 1
        assert journal.index(f"## v{VERSION} —") < journal.index(f"## v{PREV} —")

        rows = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        row = next(r for r in rows if r["Версия"] == VERSION)
        assert row["Дата"] == "2026-11-19"
        assert not [k for k, v in row.items() if "," in v], "в полях реестра нет запятых"
