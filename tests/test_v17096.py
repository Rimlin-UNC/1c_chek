"""v1.70.96: первичка — выгрузка в 1С (T-60).

Поведение проверяют tests/test_primary_export.py (файл УПД 5.03 по официальной схеме ФНС, причины пропуска,
выгрузка из программы, EnterpriseData, API для 1С с токеном компании, путь «PDF → файл для 1С») и
tests/js/primary_export.test.js (отметки, окно выгрузки, карточка). Сценарии — в DOM-стенде tests/ui_harness.js
и в Playwright (tests/e2e/app.spec.js). Здесь — синхронизация выпуска, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.96"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.96 — исторические проверки (точные пины — в tests/test_v17097.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 4
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-04"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("(T-60)", "«Покупки → Поступление (акты, накладные, УПД) → Загрузить → Из файла»",
                       "официальной схеме ФНС 5.03", "подписант помечен «не указан»", "/onec/v1/primary/pull"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2 and "«В 1С»" in block and "Загрузить → Из файла" in block

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        assert "«⬇ В 1С»" in entry and "«⬇ Файл для 1С»" in entry

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Первичка (электронные УПД)", "Печать и выгрузка в 1С"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchemaAndDependencies:
    def test_no_new_tables_columns_or_dependencies(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 52
        from app.primary.models import PrimaryDocument
        # отметка «выгружен» — колонка exported_at из 1.70.94: новых колонок выпуск не добавляет
        assert set(PrimaryDocument.__table__.columns.keys()) >= {"exported_at", "source", "status", "company_id"}
        assert "с v1.70.96 колонку `exported_at` заполняет выгрузка в 1С" in read("docs/DATABASE.md")
        # зависимости — те же, что в 1.70.95 (новых пакетов выпуск не тянет)
        req = read("requirements.txt")
        assert "\nlxml>=5.2\n" in req and "\npypdfium2>=4.30\n" in req

    def test_export_module_uses_official_schema_and_honest_signer(self):
        code = read("app/primary/export1c.py")
        for needle in ('KND = "1115131"', 'FILE_PREFIX = "ON_NSCHFDOPPR"', "schema.validate(data, \"5.03\")",
                       '"ДопСведПодп": SIGNER_NOTE', '{"Фамилия": "Не указана", "Имя": "Не указано"}',
                       'encoding="windows-1251"', "class ExportSkip(ValueError)"):
            assert needle in code, needle
        assert "Адрес" not in code.split('"""', 2)[2]         # адресов в реестре нет — в файл не пишутся
        assert "БезАкциз" not in code                         # акциз в схеме 5.03 необязателен


class TestWiring:
    def test_routes_roles_and_order(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/primary/export/check", "/api/v1/primary/export", "/onec/v1/primary/pull",
                     "/onec/v1/primary/ack", "/onec/v1/primary/enterprise-data", "/onec/v1/primary/{doc_id}/upd"):
            assert path in paths, path
        router = read("app/primary/router.py")
        assert "def export_check(user: Accountant, db: Db, body: ExportIn)" in router
        assert "def export_documents(user: Accountant, db: Db, body: ExportIn)" in router
        assert 'log_action(user, "primary_exported"' in router
        onec = read("app/routers/onec.py")
        assert '"pending_primary": _primary_query(db, scope).count()' in onec
        assert 'raise HTTPException(403, "Токен компании не даёт доступа к документам другой компании")' in onec
        assert "| primary: выгрузка в 1С — файлы УПД и EnterpriseData" in read("docs/rbac_matrix.md")

    def test_screen_wired(self):
        screen = read("app/static/js/primary.js")
        for needle in ("'/api/v1/primary/export/check'", "'/api/v1/primary/export'", "id=\"prim-export\"",
                       "id=\"prim-exported\"", "id=\"prim-pick-all\"", "class=\"prim-pick\"", "id=\"prim-card-1c\"",
                       "export function primExportBody(", "export function primExportSummaryHtml(check)",
                       "export function primExportedLabel(iso)", "colspan=\"9\""):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("prim", "PRIM", "viewPrimary")) for t in tops), tops   # общая область сборки
        assert ".prim-export-format" in read("app/static/css/app.css")
        assert "primary_exported: ['ok', '📤']" in read("app/static/js/app.js")
        spec = read("tests/e2e/app.spec.js")
        assert "test('первичка: отмеченный документ выгружается файлом УПД для 1С'" in spec
        assert "new TextDecoder('windows-1251')" in spec and "page.waitForEvent('download')" in spec

    def test_manual_guide_and_integration(self):
        from app.services.manual_content import SECTIONS
        section = next(s for s in SECTIONS if s["id"] == "primary")
        for needle in ("С версии 1.70.96 — выгрузка в 1С", "«⬇ В 1С»", "Загрузить → Из файла"):
            assert needle in section["html"], needle
        guide = read("docs/USER_GUIDE.md")
        assert "#### Выгрузка в 1С (с v1.70.96)" in guide and "**«Загрузить → Из файла»**" in guide
        integration = read("docs/INTEGRATION_1C.md")
        assert "## 3а. Первичка: УПД и счета-фактуры (с 1.70.96)" in integration
        assert "`GET /onec/v1/primary/pull?limit=100&include_errors=false`" in integration


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_export(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_primary96.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("окно выгрузки спросило у сервера, что уйдёт в 1С (новые, без ошибок, файлы УПД)",
                     "бухгалтер видит, сколько уйдёт и сколько документов с ошибками останется",
                     "есть что выгружать — «Скачать» доступна", "«Скачать» запросила файл для 1С"):
            assert f"PASS v1.70.96: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_design_research_and_adr(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("**Сделано в 1.70.96:**", "## Выгрузка в 1С (1.70.96): что выбрали и почему",
                       "| Подписант |", "| Что нельзя собрать честно |", "**требует уточнения**",
                       "https://infoblog.mywebguide.ru/articles/page_015330.html"):
            assert needle in design, needle
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-26](#adr-26) |" in arch and "## ADR-26" in arch and "дополняет [ADR-22](#adr-22)" in arch
        assert "Как первичка попадает в 1С без программиста (срез 04.11.2026, T-60)" in read("docs/kb/10_research.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t60 = tasks["T-60"]
        assert t60["Статус"] == "done" and t60["Задача"].endswith(f"выпущено в {VERSION}")
        assert t60["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/45"
        assert t60["Связанные ADR"] == "ADR-22, ADR-23, ADR-26"
        assert "/onec/v1/primary/pull" in tasks["T-52"]["Задача"] and tasks["T-52"]["Статус"] == "todo"
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.97: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 96)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 04.11.2026 (выпуск v1.70.96):** [#45](https://github.com/Rimlin-UNC/1c_chek/issues/45) T-60" \
            in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        rows = {r["Версия"]: r for r in ledger}
        assert rows[VERSION]["Опора репетиции"] == "да" and rows["1.70.95"]["Откат на неё"] == "можно"
