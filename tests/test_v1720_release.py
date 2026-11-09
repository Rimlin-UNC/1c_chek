"""v1.72.0: массовые действия по клиентам (T-89, этап 6 плана «Клиенты», ADR-53).

Пины версии здесь исторические: тест закрепляет САМ выпуск 1.72.0, а не «текущая версия».
С 1.72.1 проверки «версия = текущая» переведены в структурные (>= / присутствие), иначе каждый
бамп версии ломал бы чужой релизный тест (правило playbook, шаг 6).

Поведение — tests/test_v1720.py (SQLite и PostgreSQL), окно — tests/js/clients.test.js.
Здесь — точки выпуска, документы, руководство и база знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.72.0"
PREV = "1.71.3"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in value.split("."))


# ====================================================================== выпуск
class TestRelease:
    def test_sync_points(self):
        """Точки синхронности: файлы несут ОДНУ версию, и она не старше 1.72.0."""
        from app.config import settings
        current = settings.APP_VERSION
        assert _ver(current) >= _ver(VERSION), current
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert current in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 4
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2
        assert f'"version": "{current}"' in read("app/static/manifest.webmanifest")

    def test_updater_order(self):
        from app.config import settings
        from app.services import updater
        current = settings.APP_VERSION
        for older in (PREV, "1.71.2", "1.71.1", "1.70.114"):
            assert updater._is_newer(current, older) is True
            assert updater._is_newer(older, current) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        first = re.search(r"## \[([\d.]+)\]", md).group(1)
        assert _ver(first) >= _ver(VERSION), first
        assert f"\n## [{VERSION}] - 2026-11-08" in md
        entry = md.split(f"\n## [{VERSION}] - 2026-11-08", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "Зависимости и схема базы не меняются", "T-89, ADR-53",
                       "📣 Напомнить всем", "📦 Пакет по всем", "не отменяет остальные",
                       "200", "1.70.82–1.71.3", "SQLite и PostgreSQL", "только синтетика", "T-89 закрыт"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        order = re.findall(r"\n  '([\d.]+)': \[", js.split("WHATS_NEW = {", 1)[1])
        assert order and order[0] == max(order, key=_ver), "записи не по убыванию версий"
        assert _ver(order[0]) >= _ver(VERSION)
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Массовые действия по клиентам", "Видно, что получилось", "Отметьте несколько компаний"):
            assert needle in block, needle
        assert f"'{PREV}': [" in js

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert f"## v{VERSION} — «Клиенты»" in journal
        entry = journal.split(f"## v{VERSION} — «Клиенты»", 1)[1].split("\n## v", 1)[0]
        for needle in ("data-pick", "cl-bulk", "clientsBulkRun", "clientsBulkResult", "Две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert _ver(reg["Рабочее место аутсорсера"]) >= _ver(VERSION)
        assert all(_ver(v) <= _ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release(self):
        from app.config import settings
        readme = read("README.md")
        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", readme)
        assert match and match.group(1) == settings.APP_VERSION
        assert _ver(match.group(1)) >= _ver(VERSION)
        assert "Из 1.72.0:" in readme or match.group(1) == VERSION

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-08"
        by_id = {s["id"]: s for s in SECTIONS}
        html = by_id["clients"]["html"]
        assert "<b>С версии 1.72.0 — массовые действия.</b>" in html
        assert "«📣 Напомнить всем»" in html and "«🗄 В архив»" in html
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_bulk_route(self):
        from app.main import app
        paths = app.openapi()["paths"]
        assert "/api/v1/clients/bulk" in paths
        src = read("app/clients/router.py")
        for needle in ("class BulkIn", 'action: Literal["remind", "archive", "restore", "assign"]',
                       "service.MAX_BULK", "service.bulk_targets(", 'log_action(user, "clients_bulk"'):
            assert needle in src, needle
        svc = read("app/clients/service.py")
        assert 'BULK_ACTIONS = ("remind", "archive", "restore", "assign")' in svc
        assert "MAX_BULK = 200" in svc

    def test_refusals_are_explained(self):
        """Отказ по компании возвращается с причиной, а не проглатывается молча."""
        svc = read("app/clients/service.py")
        assert '"компания не найдена"' in svc
        assert "не назначена этому бухгалтеру" in svc
        js = read("app/static/js/clients.js")
        assert "export function clientsBulkResult(" in js and "Первая причина:" in js
        assert "export function clientsBulkHtml(" in js and "data-bulk=" in js

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t89 = tasks["T-89"]
        assert t89["Статус"] == "done" and "ADR-53" in t89["Связанные ADR"]
        assert "1.72.0" in t89["Задача"] and t89["Обновлено"] == "2026-11-08"
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])
        assert m and _ver(m.group(1)) >= _ver(VERSION)
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{7,40}", row["Коммит"])
        assert ledger[PREV]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        latest = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb).group(1)
        assert _ver(latest) >= _ver(VERSION), latest

    def test_journal_rows_are_complete(self):
        """Журнал сессий: каждая запись — семь колонок (найдено при проверке выпуска)."""
        rows = list(csv.reader(read("docs/kb/08_session_journal.csv").splitlines()))
        assert all(len(r) == len(rows[0]) for r in rows), [len(r) for r in rows if len(r) != len(rows[0])]

    def test_schema_unchanged(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 73
        assert "73 объявлений `__tablename__`" in read("docs/DATABASE.md")

    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-53](#adr-53) |" in arch and "## ADR-53" in arch
        adr = arch.split("## ADR-53", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.72.0, 2026-11-08", "/api/v1/clients/bulk", "200",
                       "Схема базы не меняется", "[ADR-30](#adr-30)", "[ADR-14](#adr-14)", "T-89"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.7.4. Массовые действия по клиентам (с v1.72.0)" in guide
        assert "**📣 Напомнить всем**" in guide and "**📦 Пакет по всем**" in guide
