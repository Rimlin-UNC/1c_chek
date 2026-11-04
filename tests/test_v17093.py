"""v1.70.93: НДС 22 % и ставка НДС в выгрузке в 1С; запрещённые товары (T-43); классификатор под стройку.

Поведение проверяют tests/test_vat_2026.py (коды тега 1199, выгрузка EnterpriseData JSON
и XML, push-режим, правка позиций) и tests/test_policy_goods.py (корпус стройматериалов и
запрещённых товаров, отметка политики, личная сумма, настройка компании, пересчёт после
данных ФНС). Здесь — синхронизация выпуска, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.93"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    def test_sync_points(self):
        from app.config import settings
        assert settings.APP_VERSION == VERSION
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert VERSION in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{VERSION}" in sw and sw.count(f"?v={VERSION}") == 2
        assert read("app/static/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-03"
        entry = md.split("\n## [1.70.92]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "48 из 48", "не переписываются", "«НДС1» вместо «НДС20»",
                       "«НДС11» вместо «НДС22»", "05.01.2026", "«НДС22_122»", "не сбрасывает их ставку НДС",
                       "(T-43", "общепит", "личные, нарушением не считаются", "«Щебень крупный»"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("НДС 22 %", "«НДС22»", "Запрещённые товары", "Личные покупки"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — политика: запрещённые товары; ставки НДС 22 %")
        entry = journal.split("\n## v1.70.92", 1)[0]
        for needle in ("«Запрещённые товары в позициях чека»", "«в чеке запрещённые товары»", "«22 %»", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == \
            ["Настройки", "Печать и выгрузка в 1С", "Сканирование чеков"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 93) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.92.**" in readme


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50


class TestVat:
    def test_code_table_and_1c_enum(self):
        from app.services import exporter, fns_refs
        assert fns_refs.VAT_BY_CODE["11"] == "22" and fns_refs.VAT_BY_CODE["12"] == "22/122"
        assert exporter.VAT_1C["22"] == "НДС22" and exporter.VAT_1C["22/122"] == "НДС22_122"
        src = inspect.getsource(exporter)
        assert '"СтавкаНДС": vat_1c(item, receipt)' in src and '("СтавкаНДС", vat_1c(it, r))' in src
        assert 'f"НДС{item.vat_rate}"' not in src

    def test_form_keeps_item_vat(self):
        js = read("app/static/js/app.js")
        assert "row.dataset.vatRate = it.vat_rate || 'none';" in js
        assert "vat_rate: row.dataset.vatRate || 'none'," in js


class TestPolicyGoods:
    def test_defaults_and_wiring(self):
        from app.services import goods, policy
        assert policy.DEFAULT_FORBIDDEN_GOODS == ("Алкогольные напитки", "Табачные изделия", "Продукты питания", "Общепит")
        assert goods.RULES[0][1] == "Строительные материалы и инструмент"     # стройка — первой
        assert "goods_violation(r, goods)" in inspect.getsource(policy._flags)
        assert "policy.refresh_receipt(db, receipt)" in inspect.getsource(
            __import__("app.services.receipt_card", fromlist=["x"]).apply_to_receipt)
        js = read("app/static/js/app.js")
        assert "forbidden_goods: 'в чеке запрещённые товары" in js and "class=\"pol-good\"" in js


class TestDocsAndKb:
    def test_primary_docs_design(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("«службу сбора первички»", "«часа икс»", "5.03", "T-53", "T-58", "T-59", "klerk.ru"):
            assert needle in design, needle

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        assert tasks["T-43"]["Статус"] == "done" and tasks["T-43"]["Задача"].endswith(f"выпущено в {VERSION}")
        assert f"Ставить сразу v{VERSION}" in tasks["T-01"]["Блокер"]
        for tid in ("T-53", "T-58"):
            assert tasks[tid]["Приоритет"] == "P1" and tasks[tid]["Источник"].endswith("primary_docs_design.md")
        assert tasks["T-59"]["Статус"] == "todo"
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION
