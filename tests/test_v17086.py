"""v1.70.86: «Единая база», фаза 3 (#24, T-35) и решение Q-24 — выпуск.

Поведение показа числа организаций проверяет tests/test_unified_phase3.py
(SQLite и PostgreSQL), Q-24 — tests/test_unified_phase0.py, экран —
tests/js/unified_review.test.js и сценарий Playwright «число организаций»;
обновление и откат — репетиция qa/upgrade_rehearsal.py и сквозной тест
миграции. Здесь — синхронизация выпуска, схема (только добавление) и тексты
для пользователя.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.86"


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
        entry = md.split("\n## [1.70.85]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in (
            # как пройдёт обновление: одна колонка, откат, репетиция
            "резервная копия", "pip пропускается", "show_companies_count", "Новых таблиц нет",
            "Откат", "1.70.80", "1.70.82", "1.70.83", "1.70.84", "1.70.85", "47 из 47",
            # показ числа компаниям: по умолчанию выключен, только число
            "по умолчанию выключены", "«Показ компаниям»", "«Выключено»", "«Всем компаниям»",
            "«Выбранным компаниям»", "🏢 N", "«Организаций»", "не получают никогда",
            "«Организаций 🏢» → «две и больше»",
            # Q-24: пачка атомарна, остальные — отдельным шагом
            "не перемещается ни один", "«Переместить остальные (N)»", "skip_blocked",
            "{message, blocked, movable}",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("«Показ компаниям»", "🏢 N", "«Организаций»", "По умолчанию выключено",
                       "«Переместить остальные»", "Последних рабочих версиях"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — ")
        entry = journal.split("\n## v1.70.85", 1)[0]
        for needle in ("«Показ компаниям»", "«Выключено»", "🏢 N", "«Организаций»",
                       "«Настройки показа", "«Переместить остальные (N)»", "две и больше"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == [
            "Единый реестр чеков", "Роли и доступ"]
        # версия блока не может опережать версию программы
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 86) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert "Последний выпуск — v1.70.85" not in readme


class TestSchema:
    def test_only_one_column_is_added(self):
        """Схема меняется только добавлением (ADR-03): одна колонка, таблиц не прибавилось."""
        from app.models import Company
        col = Company.__table__.columns["show_companies_count"]
        assert col.nullable is False and col.server_default is not None
        # существующие базы получают колонку через _ensure_schema — без перевода
        # диалекта, поэтому FALSE, а не 0 (на PostgreSQL DEFAULT 0 не проходит)
        assert ("ALTER TABLE companies ADD COLUMN show_companies_count BOOLEAN NOT NULL DEFAULT FALSE"
                in read("app/database.py"))
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50                      # как в 1.70.85
        assert "50 объявлений `__tablename__`" in read("docs/DATABASE.md")

    def test_settings_keys_default_to_off(self):
        """Два ключа app_settings; без записи показ выключен — компании видят реестр как раньше."""
        from app.unified import visibility
        assert (visibility.MODE_KEY, visibility.ROLES_KEY) == ("companies_count_mode",
                                                              "companies_count_roles")
        assert visibility.MODES[0] == visibility.MODE_OFF == "off"
        assert visibility.NO_ROLES == "none"       # пустой список ролей хранится явно

    def test_no_supabase_migration(self):
        """Колонка живёт в базе приложения; миграций Supabase выпуск не добавляет."""
        for path in (ROOT / "supabase" / "migrations").glob("*.sql"):
            text = path.read_text(encoding="utf-8")
            assert "show_companies_count" not in text and "companies_count" not in text, path.name


class TestRehearsal:
    def test_phase3_is_wired(self):
        from qa import upgrade_rehearsal as reh
        assert "/api/v1/unified/visibility" in reh.CLAIMS and '"multi_company": "true"' in reh.CLAIMS
        assert "/api/v1/unified/visibility" in reh.CLAIMS_AGAIN
        assert "Компания после отката" in reh.CLAIMS_AGAIN
        assert "Компания после отката" in read("qa/upgrade_rehearsal.py").split("CLAIMS_AGAIN = ", 1)[1]

    def test_migration_e2e_checks_phase3(self):
        from qa import migration_e2e as e2e
        assert e2e.AFTER_ROLLBACK_COMPANY == "Компания после отката"
        src = read("qa/migration_e2e.py")
        assert "def phase3_facts" in src
        for key in ('res["phase3_before"]', 'res["phase3_after"]', 'res["phase3"]'):
            assert key in src, key

    def test_live_update_checks_phase3(self):
        src = read("qa/live_update.py")
        block = src.split("v1.70.86 (T-35, Q-24)", 1)[1].split("# 4. откат", 1)[0]
        for needle in ('"/api/v1/unified/visibility"', '"multi_company": "true"', '"skip_blocked": True',
                       '"companies" not in row', "fingerprint(db) == base_fp"):
            assert needle in block, needle
        after = src.split("# 5. повторное обновление", 1)[1]
        assert 'kept.get("mode") == "all"' in after and 'org_count("acc_a") == 2' in after


class TestUserFacingTexts:
    def test_manual_describes_the_release(self):
        from app.services.manual_content import SECTIONS
        ids = [s["id"] for s in SECTIONS]
        assert len(ids) == len(set(ids)), "id разделов инструкции не уникальны"
        by_id = {s["id"]: s for s in SECTIONS}
        section = by_id["orgcount"]
        assert section["audience"] == "admin"
        for needle in ("1.70.86", "«Показ компаниям»", "«Выключено»", "«Всем компаниям»",
                       "«Выбранным компаниям»", "🏢 N", "«Организаций»", "две и больше",
                       "Настройки показа сохранены", "никогда"):
            assert needle in section["html"], needle
        assert ids.index("claims") < ids.index("orgcount") < ids.index("viewas")
        assert "Переместить остальные (N)" in by_id["move"]["html"]
        # бухгалтер и руководитель узнают, что значит значок, — без названий других
        for sid, audience in (("companies", "accountant"), ("boss", "manager")):
            assert by_id[sid]["audience"] == audience
            assert "🏢 N" in by_id[sid]["html"] and "Названия других" in by_id[sid]["html"], sid

    def test_texts_match_the_program(self):
        review = read("app/static/js/unified_review.js")
        assert ">Показ компаниям</button>" in review
        for title in ("'Выключено'", "'Всем компаниям'", "'Выбранным компаниям'", "'Бухгалтер'",
                      "'Руководитель'", "'Настройки показа сохранены'",
                      "'Не отмечена ни одна роль — число не увидит никто.'"):
            assert title in review, title
        app_js = read("app/static/js/app.js")
        assert ">Переместить остальные</button>" in app_js
        assert "`Переместить остальные (${blocked.movable})`" in app_js
        assert "Организаций 🏢" in app_js
        assert "Ни один чек не перемещён: отмеченные ждут разбора" in read("app/routers/receipts.py")

    def test_docs_describe_the_release(self):
        assert "### Число организаций у чека: показ компаниям (админ, v1.70.86)" in read("docs/USER_GUIDE.md")
        design = read("docs/single_base_design.md")
        assert "## 11. Фаза 3: сделано (T-35) и решение Q-24" in design and "Выпущено в 1.70.86" in design
        assert "сделано-в-ветке" not in design
        assert "`GET`/`PUT /unified/visibility`" in read("docs/rbac_matrix.md")
        assert "## Число организаций для компаний и Q-24 (выпуск 1.70.86" in read("docs/unified_receipts.md")
        assert "в 1.70.86 (03.11.2026)" in read("docs/ARCHITECTURE.md")
        assert "show_companies_count" in read("docs/DATABASE.md")
        assert "19 проверок" in read("docs/RUNBOOK.md")
