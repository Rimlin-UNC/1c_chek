"""v1.70.114: печатная форма УПД — бланк из данных реестра, заполняемые поля и печать в PDF (T-62, #47, ADR-45).

Поведение — tests/test_primary_printupd.py (SQLite и PostgreSQL), окно — tests/js/primary_print.test.js.
Здесь — точки выпуска, маршруты, регистрация колонки, документы и база знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.114"
CURRENT = "1.72.1"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/47"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        # точки синхронизации не отстают от версии выпуска (равенство — у своего выпуска, test_v1713)
        assert settings.APP_VERSION == CURRENT
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert CURRENT in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{CURRENT}" in sw and sw.count(f"?v={CURRENT}") == 4
        assert read("app/static/index.html").count(f"?v={CURRENT}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={CURRENT}") == 2
        assert f'"version": "{CURRENT}"' in read("app/static/manifest.webmanifest")

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.114", "1.70.113", "1.70.106"):
            assert updater._is_newer(CURRENT, older) is True
            assert updater._is_newer(older, CURRENT) is False

    def test_changelog_entry_fits_update_window(self):
        """Своя запись 1.70.114 остаётся в истории и не расползается.

        «Первая запись CHANGELOG = текущий выпуск» проверяет tests/test_v1711.py:
        в v1.71.1 первая запись чужая для этого файла.
        """
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-07"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "`print_json`", "T-62, #47, ADR-45", "постановлению № 1137",
                       "«🖨 Печатная форма»", "графами А–9", "«Сохранить поля»", "«Сохранить как PDF»",
                       "A4", "SQLite и PostgreSQL", "синтетика (ADR-14)", "1.70.114"):
            assert needle in entry, needle

    def test_whats_new_entry_kept(self):
        """Запись 1.70.114 остаётся в истории «Что нового»; первая — у выпуска."""
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("УПД печатается как оригинал", "Печатная форма", "«Печать / PDF»",
                       "постановлению № 1137", "Сохранить как PDF"):
            assert needle in block, needle

    def test_journal_entry_kept(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Первичка»: печатная форма УПД и печать в PDF"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«🖨 Печатная форма»", "(1)–(8)", "А, 1, 1а…9", "ADR-45", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert reg["Контрагенты"] == "1.71.0"          # блок заведён выпуском 1.71.0
        assert reg["Первичка (электронные УПД)"] == VERSION
        assert reg["Банковская выписка"] == "1.70.113"
        assert all(TestRelease._ver(v) <= TestRelease._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(CURRENT)
        section = read("README.md").split("## 🆕 Последний выпуск", 1)[1].split("\n## ", 1)[0]
        assert "**Контрагенты:**" in section or "контрагенты" in section.lower()

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-08"
        by_id = {s["id"]: s for s in SECTIONS}
        html = by_id["counterparties"]["html"]
        assert "<b>С версии 1.71.0 — контрагенты.</b>" in html
        assert "«Контрагенты»" in html and "«Реквизиты стороны»" in html
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_routes_and_audit(self):
        from app.main import app
        from app.services.audit_human import humanize_audit
        paths = app.openapi()["paths"]
        assert "get" in paths["/api/v1/primary/{doc_id}/print"]
        assert {"get", "put"} <= set(paths["/api/v1/primary/{doc_id}/print-form"])
        body = paths["/api/v1/primary/{doc_id}/print-form"]["put"]["requestBody"]["content"]["application/json"]
        schema = body["schema"]
        assert schema.get("$ref", "").endswith("PrintFormIn") or "properties" in schema
        title, details = humanize_audit("primary_print_form", {"filled": ["basis", "signer_chief"]})
        assert title == "заполнены поля печатной формы УПД" and "полей: 2" in details
        assert humanize_audit("primary_printed", {})[0] == "напечатана печатная форма УПД"
        js = read("app/static/js/app.js")
        assert "primary_print_form: ['info', '🖨'], primary_printed: ['info', '🖨']" in js
        router = read("app/primary/router.py")
        assert 'Query(alias="print")' in router and "HTMLResponse" in router

    def test_column_registered_and_guarded(self):
        from app.primary.models import PrimaryDocument
        assert PrimaryDocument.__table__.c.print_json is not None
        db = read("app/database.py")
        assert ('("print_json", "ALTER TABLE primary_documents ADD COLUMN print_json TEXT DEFAULT \'\'")') in db
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert 'if c["name"] in ("source", "channel", "print_json")' in rehearsal
        assert 'up.get("primary_columns") == ["channel", "print_json", "source"]' in rehearsal
        assert '"tests/test_primary_printupd.py"' in read("qa/pg_local.py")
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 73                                    # 1.71.0 добавил три таблицы контрагентов

    def test_form_rules(self):
        from app.primary import printupd
        assert printupd.LIMITS["status"] == 1 and printupd.LIMITS["sheets"] == 3
        assert len(printupd.FIELDS) == 16 and len(printupd.COLUMNS) == 13
        assert printupd.status_title("ДОП") == "2" and printupd.status_title("СЧФДОП") == "1"
        assert printupd.VAT_LABELS["22/122"] == "22/122%"
        source = read("app/primary/printupd.py")
        assert "from html import escape" in source and "escape(" in source
        assert "A4 landscape" in source and "@media print" in source

    def test_screen_modules(self):
        primary = read("app/static/js/primary.js")
        assert "export const PRIM_PRINT_FIELDS = [" in primary
        assert "export function primPrintHtml(data) {" in primary
        assert "export function primPrintBody(get) {" in primary
        for needle in ("id=\"prim-card-print\"", "print-form`", "/print?print=1", "window.open('', '_blank')"):
            assert needle in primary, needle
        page = read("app/static/js/primary_print_page.js")
        assert "window.print()" in page and "dataset.print" in page
        assert "script-src 'self'" in read("app/security.py")            # встроенных скриптов нет
        assert 'src="/js/primary_print_page.js"' in read("app/primary/printupd.py")
        css = read("app/static/css/app.css")
        assert ".prim-print-form" in css and "@media (min-width: 760px) { .prim-print-form" in css


# ====================================================================== документы и база знаний
class TestDocsAndKb:
    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-45](#adr-45) |" in arch and "## ADR-45" in arch
        adr = arch.split("## ADR-45", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.70.114, 2026-11-07", "`primary_documents`", "`print_json`",
                       "[ADR-26](#adr-26)", "[ADR-25](#adr-25)", "[ADR-03](#adr-03)", "[ADR-09](#adr-09)",
                       "[ADR-33](#adr-33)", "«Сохранить как PDF»", "недоверенный", "T-62", f"[#47]({ISSUE})"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.18. Печатная форма УПД (с v1.70.114) — бухгалтер/админ" in guide
        assert "«Сохранить как PDF»" in guide and "юридическую силу имеет подписанный" in guide

    def test_database_doc_and_counts(self):
        db = read("docs/DATABASE.md")
        assert "73 объявлений `__tablename__`" in db and "все 73 таблиц приложения" in db
        assert "с v1.70.114 (T-62, ADR-45) колонка `print_json`" in db

    def test_design_and_counterparties(self):
        design = read("docs/counterparties_design.md")
        for needle in ("T-80", "T-81", "[#65]", "[#66]", "1.71.0", "1.72.0", "ADR-44", "падеж",
                       "«Реквизиты стороны»", "«Преамбула договора»", "«Для платёжного поручения»",
                       "только синтетика (ADR-14)", "Требует уточнения"):
            assert needle in design, needle
        arch = read("docs/ARCHITECTURE.md")
        assert "## ADR-44" in arch and "1.70.N+1" in arch

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t62 = tasks["T-62"]
        assert t62["Статус"] == "done" and t62["Задача"].endswith(f"выпущено в {VERSION}")
        assert t62["Issue/PR"] == ISSUE and t62["Связанные ADR"] == "ADR-25, ADR-45"
        assert t62["Обновлено"] == "2026-11-07"
        for tid, prio, state in (("T-80", "P1", "done"), ("T-81", "P2", "todo"), ("T-82", "P1", "done")):
            assert tasks[tid]["Статус"] == state and tasks[tid]["Приоритет"] == prio, tid
        assert tasks["T-80"]["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/65"
        assert tasks["T-81"]["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/66"
        assert tasks["T-82"]["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/59"
        assert "1.71.0" in tasks["T-80"]["Задача"] and "1.72.0" in tasks["T-81"]["Задача"]
        t01 = tasks["T-01"]["Блокер"]
        m = re.search(r"Ставить сразу v([\d.]+)", t01)          # v1.71.1: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(CURRENT)
        assert "без замороженного «Моего дела»" in t01 and "1.70.82" in t01
        assert "с каждой из 1.70.82–1.70.114 — 48 из 48 проверок" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 07.11.2026 (выпуск v{VERSION}):** [#47]({ISSUE}) T-62" in roadmap
        assert "## Новые запросы владельца 07.11.2026 — разбивка на задачи" in roadmap
        assert "T-80" in roadmap and "T-82" in roadmap and "ADR-44" in roadmap
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", row["Коммит"])
        assert ledger["1.70.113"]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        m = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb)   # v1.71.1: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(CURRENT)
        assert "контрагенты (T-80; ADR-48)" in kb
        # PR #73 и #74 уже слиты: в живой сводке указан фактический main
        assert "PR #74" in kb and "10a84c4" in kb and "issues #4–#66" in kb
        seo = read("docs/kb/12_seo.md")
        assert "## 8. Запрос владельца 07.11.2026: блог, 25 ключей и картинки (T-82)" in seo
        assert "проверка чеков на сайте налоговой" in seo
