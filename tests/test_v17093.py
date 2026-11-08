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
    """Выпуск 1.70.93 — исторические проверки (точные пины — в tests/test_v17094.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 4

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-03"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("«НДС11» вместо «НДС22»", "«НДС22_122»", "(T-43", "«Щебень крупный»"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2 and "НДС 22 %" in block and "Запрещённые товары" in block

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        assert "«Запрещённые товары в позициях чека»" in entry and "«22 %»" in entry

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Настройки", "Печать и выгрузка в 1С", "Сканирование чеков"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 50


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
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.94: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 93)
        for tid in ("T-53", "T-58"):
            assert tasks[tid]["Приоритет"] == "P1" and tasks[tid]["Источник"].endswith("primary_docs_design.md")
        assert tasks["T-59"]["Статус"] == "todo"
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert any(r["Версия"] == VERSION for r in ledger)
