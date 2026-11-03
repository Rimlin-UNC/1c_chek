"""v1.70.87: исправление #25 — поздний ответ раздела не подменяет открытый раздел; выпуск.

Поведение проверяют tests/test_route_race.py (страж шаблона во всех разделах и
DOM-стенд с настоящим route()), Vitest (tests/js/supabase_demo.test.js) и
сценарий Playwright «дашборд: поздний ответ…»; обновление и откат — репетиция
qa/upgrade_rehearsal.py и этап Б. Здесь — синхронизация выпуска, тексты для
пользователя и запись решения владельца по фазе 4 (ADR-14). Схема базы и данные
в выпуске не меняются.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.87"


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
        assert f"ymaster-check-v{VERSION}" in sw
        assert sw.count(f"?v={VERSION}") == 2
        assert read("app/static/index.html").count(f"?v={VERSION}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-03"
        entry = md.split("\n## [1.70.86]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in (
            # как пройдёт обновление: схема не меняется, откат, репетиция
            "резервная копия", "pip пропускается", "Схема базы и данные не меняются", "Откат",
            "1.70.80", "1.70.82", "1.70.83", "1.70.84", "1.70.85", "1.70.86", "47 из 47",
            # что исправлено и где
            "(#25)", "«Чеки»", "«База чеков»", "1.8.0", "«Настроек»", "«Пользователей»",
            "отчёта руководителя", "Supabase-демо", "не перебрасывает",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1
        for needle in ("«Чеки»", "дашборд", "остаётся на месте"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — ")
        entry = journal.split("\n## v1.70.86", 1)[0]
        for needle in ("(#25)", "«База чеков»", "«Лента событий»", "вид экранов не менялся",
                       "одна карточка"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == ["Адаптивный интерфейс"]
        # версия блока не может опережать версию программы
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 87) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert "Последний выпуск — v1.70.86" not in readme


class TestSchema:
    def test_tables_unchanged(self):
        """Выпуск меняет только интерфейс: таблиц столько же, сколько в 1.70.85–1.70.86."""
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50


class TestOwnerDecision:
    """Фаза 4: рабочая база остаётся на рабочем сервере, здесь — синтетические тесты."""

    def test_adr_14_accepted(self):
        arch = read("docs/ARCHITECTURE.md")
        row = next(line for line in arch.splitlines() if line.startswith("| [ADR-14](#adr-14)"))
        assert "принято (решение владельца 03.11.2026)" in row
        section = arch.split("## ADR-14\n", 1)[1].split("## ADR-15\n", 1)[0]
        for needle in ("Рабочая база остаётся в SQLite на рабочем сервере", "синтетических данных",
                       "отменён", "отдельное решение"):
            assert needle in section, needle

    def test_database_status_follows_decision(self):
        db = read("docs/DATABASE.md")
        assert "**не выполняется** — решение владельца 03.11.2026 (ADR-14, T-08)" in db
        assert "переход на PostgreSQL/Supabase не\n> выполняется, здесь — только синтетические тесты" in db
        design = read("docs/single_base_design.md")
        assert "Переход рабочей базы на PostgreSQL **не выполняется**" in design

    def test_agents_never_moves_working_database(self):
        agents = read("AGENTS.md")
        never = agents.split("## Никогда", 1)[1]
        assert "Перенос рабочей базы, её копий и выгрузок в Supabase, среду разработки или CI" in never
