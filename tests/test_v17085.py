"""v1.70.85: «Единая база», фазы 1–2 (#24, T-34) — выпуск.

Поведение заявок компаний проверяет tests/test_unified_claims.py (SQLite и
PostgreSQL), экран — tests/js/unified_review.test.js; обновление и откат —
репетиция qa/upgrade_rehearsal.py (фазы CLAIMS и CLAIMS_AGAIN) и сквозной тест
миграции. Здесь — синхронизация выпуска, схема (только добавление) и тексты
для пользователя.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.85"


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
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-02"
        entry = md.split("\n## [1.70.84]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in (
            # как пройдёт обновление: одна новая таблица, откат, репетиция
            "резервная копия", "pip пропускается", "unified_company_links", "Откат",
            "1.70.80", "1.70.82", "1.70.83", "1.70.84", "45 из 45",
            # заявки и решения администратора
            "Заявка передана администратору.", "«Заявки»", "Передать чек", "Отклонить",
            "Не учитывать", "Причина обязательна", "«Организации»",
            # закрытость для сотрудников и наполнение задним числом
            "Сотрудники компаний ничего из этого не видят", "--claims-only", "--dry-run",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Заявка передана администратору", "«Заявки»", "«Организации»",
                       "Последних рабочих версиях"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — ")
        entry = journal.split("\n## v1.70.84", 1)[0]
        for needle in ("«Споры»", "«Заявки»", "«Организации»", "Заявка передана администратору."):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == [
            "Единый реестр чеков", "Роли и доступ", "Сканирование чеков", "Чек-Пул"]
        # версия блока не может опережать версию программы
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 85) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (02.11.2026)" in readme
        assert "Последний выпуск — v1.70.84" not in readme


class TestSchema:
    def test_only_one_table_is_added(self):
        """Схема меняется только добавлением (ADR-03): одна новая таблица."""
        from app.database import Base
        from app.unified.models import UnifiedCompanyLink
        table = UnifiedCompanyLink.__table__
        assert table.name == "unified_company_links" and table.name in Base.metadata.tables
        assert {c.name for c in table.columns} == {
            "id", "document_id", "company_id", "kind", "status", "source", "attempts",
            "first_at", "last_at", "first_user_id", "last_user_id", "decided_by", "decided_at"}
        fks = [(fk.parent.name, fk.column.table.name, fk.ondelete) for fk in table.foreign_keys]
        # компания — без внешнего ключа: старая версия после отката удаляет компании
        assert fks == [("document_id", "unified_documents", "CASCADE")]
        uniques = [sorted(c.name for c in con.columns) for con in table.constraints
                   if con.__class__.__name__ == "UniqueConstraint"]
        assert uniques == [["company_id", "document_id"]]

    def test_no_supabase_migration(self):
        """Таблица живёт в базе приложения; миграций Supabase выпуск не добавляет."""
        migrations = sorted(p.name for p in (ROOT / "supabase" / "migrations").glob("*.sql"))
        assert not any("company_links" in name or "claims" in name for name in migrations)


class TestRehearsal:
    def test_claim_phases_are_wired(self):
        from qa import upgrade_rehearsal as reh
        assert '"receipt_duplicate"' in reh.SEED                      # отказы /scan старой версии
        assert "/api/v1/unified/claims" in reh.UPGRADE
        assert "/api/v1/unified/links/" in reh.CLAIMS and "/companies" in reh.CLAIMS
        assert "REH_CLAIMS" in reh.CLAIMS_AGAIN and "REH_GIVEN" in reh.OLD_AFTER_BULK
        src = read("qa/upgrade_rehearsal.py")
        assert 'run_phase("заявки компаний", CLAIMS' in src
        assert 'run_phase("повторное обновление после заявок", CLAIMS_AGAIN' in src
        assert 'len(up["unified_tables"]) == 6' in src

    def test_migration_e2e_checks_claims(self):
        from qa import migration_e2e as e2e
        assert "unified_company_links" in e2e.UNIFIED_TABLES and len(e2e.UNIFIED_TABLES) == 6
        assert "def claims_facts" in e2e.FACTS
        assert 'final["claims"] == after["claims"]' in read("qa/migration_e2e.py")

    def test_live_update_checks_a_claim(self):
        src = read("qa/live_update.py")
        block = src.split("v1.70.85 (T-34)", 1)[1].split("# 4. откат", 1)[0]
        for needle in ('"/api/v1/unified/claims"', "/void", "Заявка передана администратору",
                       "fingerprint(db) == base_fp"):
            assert needle in block, needle


class TestUserFacingTexts:
    def test_manual_has_claims_section(self):
        from app.services.manual_content import SECTIONS
        ids = [s["id"] for s in SECTIONS]
        assert len(ids) == len(set(ids)), "id разделов инструкции не уникальны"
        section = next(s for s in SECTIONS if s["id"] == "claims")
        assert section["audience"] == "admin"
        for needle in ("1.70.85", "Заявка передана администратору.", "«Заявки»", "Передать чек",
                       "Отклонить", "Не учитывать", "«Организации»", "--claims-only"):
            assert needle in section["html"], needle
        assert ids.index("conflicts") < ids.index("claims")
        scan = next(s for s in SECTIONS if s["id"] == "scan")
        assert "Заявка" in scan["html"] and "передана администратору" in scan["html"]

    def test_texts_match_the_program(self):
        from app.unified import links
        assert links.CLAIM_NOTE == "Заявка передана администратору."
        js = read("app/static/js/unified_review.js")
        for title in ("Передать чек", "Отклонить", "Не учитывать", "Ждёт решения", "Отклонена",
                      "Не учитывается", "Чек передан"):
            assert f"'{title}'" in js, title
        assert 'id="ur-tab-claims"' in js and "Учитывают чек:" in js
        # значок меню считает споры и заявки (app.js и экран — одна функция)
        assert "setConflictsBadge(badgeCount(u))" in read("app/static/js/app.js")

    def test_docs_describe_the_release(self):
        assert "### Заявки компаний на чужие чеки (админ, v1.70.85)" in read("docs/USER_GUIDE.md")
        design = read("docs/single_base_design.md")
        assert "## 10. Фазы 1–2: сделано (T-34)" in design and "Выпущено в 1.70.85" in design
        assert "50 объявлений `__tablename__`" in read("docs/DATABASE.md")
        assert "`GET /unified/claims`" in read("docs/rbac_matrix.md")
        assert "## Заявки компаний (выпуск 1.70.85" in read("docs/unified_receipts.md")
        assert "фазы 1–2 (T-34" in read("docs/ARCHITECTURE.md")
