"""v1.71.2: удаление клиента без «сирот» (T-85, ADR-50).

ВНИМАНИЕ: файл стал историческим после выпуска 1.71.3. Требования «своя версия — первая»
(CHANGELOG, WHATS_NEW, журнал, README, блоки, реестр) проверяет tests/test_v1713_release.py;
здесь остаются записи выпуска 1.71.2 в истории и политика удаления.

Поведение — tests/test_company_delete.py (SQLite и PostgreSQL), окно удаления —
tests/e2e/app.spec.js. Здесь — точки выпуска, политика очистки, документы и база знаний.
Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.71.2"
PREV = "1.71.1"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        """Текущая версия не отстала от 1.71.2, а прежней нет в cache-buster (браузер не возьмёт кэш)."""
        from app.config import settings
        assert self._ver(settings.APP_VERSION) >= self._ver(VERSION)
        for rel in ("app/static/index.html", "app/static/sw.js", "app/static/manifest.webmanifest",
                    "app/static/maxapp/index.html", "app/static/b2b/index.html"):
            assert f"?v={VERSION}" not in read(rel), rel

    def test_updater_order(self):
        from app.services import updater
        for older in (PREV, "1.71.0", "1.70.114"):
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-08"          # «первая запись» — у текущего выпуска (test_v1713_release)
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "Схема базы не меняется", "T-85, ADR-50",
                       "accountant_clients", "доступ бухгалтера", "перенос", "ИНН",
                       "500", "История решений", "SQLite и PostgreSQL", "только синтетика",
                       "1.70.82–1.71.1"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js, "запись выпуска пропала из «Что нового»"
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Удалённый клиент не оставляет следа", "Перенос без дублей", "доступы бухгалтеров", "ИНН"):
            assert needle in block, needle
        assert f"'{PREV}': [" in js

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Компании»"
        assert head in journal
        entry = head + journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("Удаление компании", "пояснение", "Две карточки", "deleted", "moved"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Рабочее место аутсорсера"]) >= self._ver(VERSION)
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", readme)
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)
        section = readme.split("## 🆕 Последний выпуск", 1)[1].split("\n## ", 1)[0]
        assert "1.71.2" in section, "выпуск 1.71.2 пропал из раздела последнего выпуска"

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-08"
        by_id = {s["id"]: s for s in SECTIONS}
        html = by_id["clients"]["html"]
        assert "<b>С версии 1.71.2 — удаление без «сирот».</b>" in html
        assert "«🗑 Удалить компанию»" in html and "не раздваивается" in html
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_policy_lists(self):
        from app.accounts import cleanup
        from app.database import Base

        declared = {t.name for t in Base.metadata.tables.values() if "company_id" in t.c}
        covered = ({t.name for t in cleanup.TABLES_WIPE}
                   | {t.name for t in cleanup.TABLES_REASSIGN}
                   | {t.name for t in cleanup.TABLES_KEEP}
                   | set(cleanup.TABLES_KEEP_AUDIT)
                   | set(cleanup.TABLES_KEEP_CLOSED)
                   | set(cleanup.TABLES_BY_EXISTING_LOGIC))
        assert not (declared - covered), sorted(declared - covered)
        # ключевые решения политики закреплены: их нельзя «почистить» молча
        wipe = {t.name for t in cleanup.TABLES_WIPE}
        for name in ("bank_connections", "primary_documents", "doc_requests", "advance_approvals"):
            assert name in wipe, name
        reassign = {t.name for t in cleanup.TABLES_REASSIGN}
        for name in ("counterparties", "goods_memory", "approval_settings", "doc_request_settings"):
            assert name in reassign, name
        keep = {t.name for t in cleanup.TABLES_KEEP}
        assert {"webhook_logs", "b2b_applications", "b2b_events"} <= keep
        assert cleanup.TABLES_KEEP_AUDIT == ("audit_log",)
        assert cleanup.TABLES_KEEP_CLOSED == ("unified_company_links",)

    def test_delete_route_uses_cleanup(self):
        source = read("app/routers/companies.py")
        block = source.split("def delete_company(", 1)[1].split("\ndef ", 1)[0]
        assert block.count("cleanup.detach_company(") == 2, "оба режима должны чистить данные"
        assert "target_company_id=target.id" in block
        # сотрудники открепляются, если не переезжают (иначе FK не даёт удалить компанию)
        assert ".update({User.company_id: None}, synchronize_session=False)" in block

    def test_cleanup_module_guards(self):
        src = read("app/accounts/cleanup.py")
        assert "from ..database import Base" in src, "перечень таблиц — из метаданных, а не руками"
        assert "AuditLog" not in src, "журнал действий не удаляется"
        assert "links.close_company" in src or "unified_company_links" in src

    def test_database_doc_unchanged(self):
        """Схема не менялась: 73 таблицы, миграций нет."""
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 75
        assert "75 объявлений `__tablename__`" in read("docs/DATABASE.md")

    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-50](#adr-50) |" in arch and "## ADR-50" in arch
        adr = arch.split("## ADR-50", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.71.2, 2026-11-08", "TABLES_WIPE", "TABLES_REASSIGN",
                       "TABLES_KEEP", "метаданных", "Схема базы не меняется",
                       "[ADR-14](#adr-14)", "T-85"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.7.1. Удаление компании-клиента (с v1.71.2)" in guide
        assert "«🗑 Удалить компанию»" in guide and "не раздваивается" in guide

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t85 = tasks["T-85"]
        assert t85["Статус"] == "in-progress" and t85["Связанные ADR"] == "ADR-50"
        assert "1.71.2" in t85["Задача"] and t85["Обновлено"] == "2026-11-08"
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert tasks["T-86"]["Статус"] in ("todo", "in-progress", "done")          # этап 3 своего выпуска — в test_v1713_release.py
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        # после публикации пишется короткий хэш коммита (40 знаков — до слияния), «после публикации» — до неё
        assert row["Коммит"] in ("после публикации", "809d16d") or re.fullmatch(r"[0-9a-f]{40}", row["Коммит"])
        assert row["Откат на неё"] == "можно", "1.71.2 уступила место текущему выпуску"
        kb = read("docs/kb/README.md")
        m = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb)
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
