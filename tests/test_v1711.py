"""v1.71.1: клиента заводит сам бухгалтер — «＋ Новый клиент» на экране «Клиенты» (T-84, ADR-49).

Поведение — tests/test_clients_create.py (SQLite и PostgreSQL), окно — tests/js/clients.test.js,
путь пользователя — сценарий в tests/e2e/app.spec.js. Здесь — точки выпуска, маршруты, права,
документы и база знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.71.1"
PREV = "1.71.0"
REPORT = "docs/clients_status_report.md"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        """Точки синхронизации не отстали от своей версии (их точное равенство — у текущего выпуска)."""
        from app.config import settings
        assert self._ver(settings.APP_VERSION) >= self._ver(VERSION)
        # старая версия не должна остаться в cache-buster: иначе браузер отдаст кэш
        for rel in ("app/static/index.html", "app/static/sw.js", "app/static/manifest.webmanifest",
                    "app/static/maxapp/index.html", "app/static/b2b/index.html"):
            assert f"?v={VERSION}" not in read(rel), rel

    def test_updater_order(self):
        from app.services import updater
        for older in (PREV, "1.70.114", "1.70.106"):
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        """Своя запись 1.71.1 в истории; «первая запись = текущий выпуск» — в tests/test_v1712.py."""
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-08"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "Схема базы не меняется", "«＋ Новый клиент»",
                       "ИНН", "🔍 ЕГРЮЛ", "переключателе клиента", "той же транзакции",
                       "предел клиентов", "из одних нулей", "SQLite и PostgreSQL",
                       "только синтетика", "T-84", "1.70.82–1.71.0"):
            assert needle in entry, needle

    def test_whats_new_entry_kept(self):
        """Запись 1.71.1 остаётся в истории «Что нового»; «первая» — в тесте своего выпуска."""
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Клиента заводит сам бухгалтер", "«＋ Новый клиент»", "ИНН",
                       "🔍 ЕГРЮЛ", "дубль"):
            assert needle in block, needle
        assert f"'{PREV}': [" in js                          # история выпусков на месте

    def test_journal_entry_kept(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Клиенты»"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«＋ Новый клиент»", "`#cl-new`", "переключателе", "двойного нажатия",
                       "дубль", "Две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Рабочее место аутсорсера"]) >= self._ver(VERSION)
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-08"
        by_id = {s["id"]: s for s in SECTIONS}
        html = by_id["clients"]["html"]
        assert "<b>С версии 1.71.1 — «＋ Новый клиент».</b>" in html
        assert "«Создать клиента»" in html and "переключателе" in html
        assert "дубль" in html
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_routes_and_permissions(self):
        from app.main import app
        paths = app.openapi()["paths"]
        for path in ("/api/v1/companies", "/api/v1/companies/similar",
                     "/api/v1/companies/lookup-checko"):
            assert path in paths, path
        source = read("app/routers/companies.py")
        # создание компании — администратор и бухгалтер, остальное у компаний — только администратор
        create = source.split('def create_company(', 1)[1].split("\n@router", 1)[0]
        assert "require_accountant" in create and "adopt_client" in create
        similar = source.split('def similar_companies(', 1)[1].split("\n@router", 1)[0]
        assert "require_accountant" in similar and "allowed_company_ids" in similar
        for name in ("def list_companies(", "def patch_company(", "def company_users("):
            block = source.split(name, 1)[1].split("\n@router", 1)[0]
            assert "require_admin" in block, name

    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-49](#adr-49) |" in arch and "## ADR-49" in arch
        adr = arch.split("## ADR-49", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.71.1, 2026-11-08", "`accountant_clients`",
                       "`clients.service.adopt_client`", "`check_client_limit`", "той же транзакции",
                       "[ADR-30](#adr-30)", "[ADR-09](#adr-09)", "[ADR-14](#adr-14)",
                       "Схема базы **не меняется**", "T-84"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        assert "**Новый клиент (с v1.71.1).**" in guide
        assert "«＋ Новый клиент»" in guide and "«Создать клиента»" in guide

    def test_database_doc_unchanged(self):
        """Схема не менялась: 73 таблицы, новая колонка не появилась."""
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 72
        db = read("docs/DATABASE.md")
        assert "75 объявлений `__tablename__`" in db

    def test_report_and_plan_kept(self):
        report = read(REPORT)
        for needle in ("## A.", "## B.", "## C.", "T-84", "### Этап 1", "### Этап 9"):
            assert needle in report, needle
        assert "Кнопка «＋ Новый клиент» на экране «Клиенты»" in report

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t84 = tasks["T-84"]
        assert t84["Статус"] == "in-progress" and t84["Приоритет"] == "P1"
        assert "1.71.1" in t84["Задача"] and t84["Связанные ADR"] == "ADR-49"
        assert t84["Обновлено"] == "2026-11-08"
        # этапы 3…9 идут по плану: задача может быть в очереди, в работе или завершена
        for tid in ("T-86", "T-87", "T-88", "T-89", "T-90", "T-91"):
            assert tasks[tid]["Статус"] in ("todo", "in-progress", "done"), tid
            assert tasks[tid]["Источник"].endswith("clients_status_report.md"), tid
        assert tasks["T-85"]["Статус"] in ("todo", "in-progress", "done")
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])
        assert m and self.ver(m.group(1)) >= self.ver(VERSION)
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", row["Коммит"])
        assert ledger[PREV]["Откат на неё"] in ("можно", "текущий выпуск")
        kb = read("docs/kb/README.md")
        m = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb)
        assert m and self.ver(m.group(1)) >= self.ver(VERSION)
        assert "ADR-49" in kb

    @staticmethod
    def ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))
