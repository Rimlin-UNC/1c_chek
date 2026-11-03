"""v1.70.88: «Единая база», фаза 4а (T-37) — сверка копий и готовность единой записи; выпуск.

Поведение проверяют tests/test_unified_copies.py (тест-инвариант копий на путях
записи, правила сверки, отчёт готовности, мягкая регистрация — и на PostgreSQL),
Vitest (tests/js/unified_review.test.js — вкладка «Готовность»), сквозной тест
миграции (29 проверок), репетиция qa/upgrade_rehearsal.py (48) и этап Б (21).
Здесь — синхронизация выпуска, проводка шага 4а, решения владельца Q-25…Q-27 и
документация. Схема базы в выпуске не меняется.
"""
from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.88"


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
        entry = md.split("\n## [1.70.87]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in (
            # как пройдёт обновление: схема не меняется, откат, репетиция
            "резервная копия", "pip пропускается", "Схема базы не меняется", "Откат",
            "1.70.80", "1.70.82", "1.70.83", "1.70.84", "1.70.85", "1.70.86", "1.70.87", "48 из 48",
            # что нового: сверка, метки, «Разобрать данные чеков», отчёт, мягкая регистрация
            "(T-37)", "Сверка после отката", "«Разбор конфликтов»", "Регион, город и отрасль не сравниваются",
            "«🧭 Разобрать данные чеков»", "вкладка «Готовность»", "GET /api/v1/unified/readiness",
            "python -m app.unified.readiness", "--soft --dry-run", "Сама после обновления команда не запускается",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1
        for needle in ("«Готовность»", "только числа", "после отката", "ничего не меняется"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — ")
        entry = journal.split("\n## v1.70.87", 1)[0]
        for needle in ("«Готовность»", "четвёртая вкладка", "четыре карточки KPI", "семи строк",
                       "Только числа", "«Мягкая регистрация архива»", "одна карточка"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == ["Единый реестр чеков"]
        # версия блока не может опережать версию программы
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 88) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert "Последний выпуск — v1.70.87" not in readme
        assert "фаза 4а (T-37)" in readme


class TestSchema:
    def test_no_new_tables_or_columns(self):
        """Шаг 4а не меняет схему: ни таблиц, ни колонок (отпечаток проекции отклонён)."""
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50
        from app.unified.models import UnifiedDocument, UnifiedOrigin
        assert "projected_hash" not in UnifiedOrigin.__table__.c
        assert "projected_hash" not in UnifiedDocument.__table__.c
        for rel in ("app/unified/copies.py", "app/unified/readiness.py", "app/unified/backfill.py"):
            assert "ALTER TABLE" not in read(rel), rel

    def test_no_supabase_migration(self):
        for path in (ROOT / "supabase" / "migrations").glob("*.sql"):
            text = path.read_text(encoding="utf-8").lower()
            assert "projected_hash" not in text and "readiness" not in text, path.name


class TestPhase4a:
    def test_startup_order(self):
        """Сверка копий идёт после сверки владельцев: удалённые старой версией копии уже сняты."""
        from app import database
        src = inspect.getsource(database.init_db)
        assert src.index("reconcile_on_startup()") < src.index("copies_on_startup()")
        assert src.index("copies_on_startup()") < src.index("links_on_startup()")

    def test_compared_facts_exclude_labels(self):
        from app.unified import copies
        assert copies._LABELS == ("region_code", "region_city", "industry")
        assert not set(copies._LABELS) & set(copies._TEXT)
        assert set(copies._TEXT) == {"fn", "fd", "fp", "merchant_name", "merchant_inn",
                                     "merchant_address", "cashier"}

    def test_readiness_is_admin_only_and_read_only(self):
        from app.unified import readiness, router
        src = inspect.getsource(router.readiness)
        assert "require_admin" in src
        rsrc = inspect.getsource(readiness)
        assert "commit(" not in rsrc and ".add(" not in rsrc and "delete(" not in rsrc

    def test_enrich_syncs_unified_record(self):
        from app.routers import receipts
        src = inspect.getsource(receipts.enrich_receipts)
        assert "sync_registry_safely(db, r)" in src

    def test_soft_cli_and_event_title(self):
        src = read("app/unified/backfill.py")
        assert '"--soft"' in src and 'event_type="backfill_soft"' in src
        assert "backfill_soft: 'Мягкая регистрация архива'" in read("app/static/js/unified_review.js")


class TestOwnerDecisions:
    """Q-25: вариант В по шагам; Q-26: мягкая регистрация только командой; Q-27: сжатие отложено."""

    def test_soft_registration_never_runs_by_itself(self):
        for rel in ("app/database.py", "app/main.py", "app/unified/copies.py",
                    "app/unified/reconcile.py", "app/services/updater.py"):
            assert "soft_backfill" not in read(rel), rel

    def test_compression_deferred(self):
        design = read("docs/single_base_design.md")
        assert "сжатие отложено" in design
        from app.models import Receipt
        from app.pool.models import PoolReceipt
        for col in ("fn", "fd", "fp", "total_sum", "merchant_name", "cashier"):
            assert col in Receipt.__table__.c and col in PoolReceipt.__table__.c, col

    def test_design_describes_implemented_rules(self):
        design = read("docs/single_base_design.md")
        section = design.split("### 12.4. Правила сверки копий (шаг 4а)", 1)[1].split("### 12.5.", 1)[0]
        for needle in ("Реализовано в 1.70.88", "отклонён при реализации", "не сравниваются",
                       "`legacy_edit`", "`copy_restored`", "`copies_diverged`", "`fns_evidence`",
                       "sync_registry_safely"):
            assert needle in section, needle
        row = next(line for line in design.splitlines() if line.startswith("| 4а |"))
        assert "**Сделано в 1.70.88**" in row and "новая колонка" not in row

    def test_kb_task_done(self):
        rows = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        assert rows["T-37"]["Статус"] == "done"
        assert "колонк" not in rows["T-37"]["Задача"]
        assert rows["T-37"]["Задача"].endswith(f"выпущено в {VERSION}")
        assert rows["T-38"]["Статус"] == "todo"


class TestDocs:
    def test_docs_follow_release(self):
        assert "## Копии и готовность единой записи (выпуск 1.70.88" in read("docs/unified_receipts.md")
        assert "### Готовность единой записи (админ, v1.70.88)" in read("docs/USER_GUIDE.md")
        assert "`GET /unified/readiness`" in read("docs/rbac_matrix.md")
        runbook = read("docs/RUNBOOK.md")
        assert "python -m app.unified.backfill --soft --dry-run" in runbook
        assert "Всего 21 проверка" in runbook
        db = read("docs/DATABASE.md")
        assert "сверка копий `app/unified/copies.py`" in db and "29 проверок на каждом" in db
        assert "Шаг 4а выпущен в 1.70.88" in read("docs/ARCHITECTURE.md")
