"""v1.70.89: исправлена потеря данных Чек-Пула; «Единая база», фаза 4б (T-38) — единая точка записи.

Поведение проверяют tests/test_unified_merge.py (правило слияния — правка бухгалтера,
явная очистка, сверка на старте, дополнение из Чек-Пула, метки и позиции — и на
PostgreSQL), tests/test_unified_write_point.py (страж единой точки записи), сквозной
тест миграции (30 проверок, стадия «фаза 4б») и репетиция обновления. Здесь —
синхронизация выпуска, проводка правила слияния и документация. Схема базы не меняется.
"""
from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.89"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.89 — исторические проверки.

    v1.70.90: точные пины перенесены в tests/test_v17090.py. Здесь остаётся то,
    что верно и после следующих выпусков: версия только растёт, запись о
    выпуске на месте и видна целиком, точки синхронизации совпадают с текущей
    версией.
    """

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert current in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw
        assert sw.count(f"?v={current}") == 2
        assert read("app/static/index.html").count(f"?v={current}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2
        assert f'"version": "{current}"' in read("app/static/manifest.webmanifest")

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-03"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in (
            # как пройдёт обновление
            "Ставьте 1.70.89 вместо 1.70.88", "резервная копия", "pip пропускается",
            "Схема базы не меняется", "Откат", "1.70.80", "1.70.87", "1.70.88", "48 из 48",
            # что исправлено и почему
            "по QR", "данными ФНС", "даже комментария", "сверка при каждом запуске", "с 1.70.82",
            "«неизвестно»", "явная очистка в карточке", "Уже стёртое в Чек-Пуле программа сама не восстанавливает",
            "требует уточнения, #4", "Разметка Чек-Пула",
            # фаза 4б
            "(T-38)", "единую запись в той же транзакции", "Новый автотест", "фазы 4в",
        ):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1
        for needle in ("Чек-Пула", "не стираются", "пустое поле ничего не стирает", "ничего не меняется"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        for needle in ("(фаза 4б, T-38)", "не менялись", "«Готовность»", "одна карточка"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Единый реестр чеков"]) >= self._ver(VERSION)
        # версия блока не может опережать версию программы
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50
        from app.unified.models import UnifiedDocument, UnifiedOrigin
        assert "projected_hash" not in UnifiedOrigin.__table__.c
        assert "projected_hash" not in UnifiedDocument.__table__.c


class TestMergeRule:
    def test_merge_constants(self):
        from app.unified import service
        assert service.MERGE_TEXT == ("merchant_name", "merchant_inn", "merchant_address", "cashier")
        assert service.LABELS == ("region_code", "region_city", "industry")
        assert service._unknown("") and service._unknown(None) and not service._unknown("x")

    def test_sync_functions_take_explicit_clears(self):
        from app.unified import service
        for fn in (service.sync_registry, service.sync_pool):
            assert "cleared" in inspect.signature(fn).parameters, fn.__name__
            assert "_merge_core" in inspect.getsource(fn), fn.__name__
        assert "_apply_core(doc, data)" not in inspect.getsource(service.sync_registry)
        assert "_fill_unknown(doc, data)" in inspect.getsource(service.register_source)
        assert "clear" in inspect.signature(service._project_pool).parameters

    def test_patch_passes_explicit_clears(self):
        from app.routers import receipts
        src = inspect.getsource(receipts.patch_receipt)
        # v1.70.90: поля очистки — общая константа; очистка — «было значение, стало пусто»
        assert "cleared=cleared" in src and "_CLEARABLE_FIELDS" in src and "before[f]" in src

    def test_pool_labels_are_only_filled(self):
        from app.unified import service
        src = inspect.getsource(service._project_pool)
        for label in ("region_code", "industry", "city"):
            assert f"if not pool.{label}:" in src, label

    def test_reconcile_treats_registry_empties_as_unknown(self):
        from app.unified import copies
        src = inspect.getsource(copies.candidate_query)
        assert "service.MERGE_TEXT" in src and 'model.receipt_date.isnot(None)' in src
        assert "_fill_unknown" in inspect.getsource(copies._reconcile_document)
        assert "_fill_items" in inspect.getsource(copies._reconcile_document)

    def test_guard_and_e2e_stage_exist(self):
        assert "WRITE_POINTS" in read("tests/test_unified_write_point.py")
        e2e = read("qa/migration_e2e.py")
        assert "MERGE_RULE = PHASE_COMMON" in e2e and "фаза 4б: правило слияния" in e2e
        # копия кода новой версии берёт справочники app/pool/geo/data
        assert 'Path(path) == ROOT and "data" in names' in e2e


class TestDocs:
    def test_design_and_adr(self):
        design = read("docs/single_base_design.md")
        assert "### 12.8. Шаг 4б: правило слияния и единая точка записи (выпуск 1.70.89)" in design
        row = next(line for line in design.splitlines() if line.startswith("| 4б |"))
        assert "**Сделано в 1.70.89**" in row and "Q-28" in row
        # v1.70.90: вопрос закрыт ответом владельца — проверяется, что он на месте
        assert "- **Q-28 (к шагу 4в" in design
        assert "Шаг 4б выпущен в 1.70.89" in read("docs/ARCHITECTURE.md")

    def test_docs_follow_release(self):
        assert "## Правило слияния и единая точка записи (выпуск 1.70.89" in read("docs/unified_receipts.md")
        db = read("docs/DATABASE.md")
        m = re.search(r"(\d+) проверк\w* на каждом", db)
        assert m and int(m.group(1)) >= 30 and "с 1.70.89 — правило слияния" in db
        assert "С 1.70.89 пустое поле в копии чека ничего не стирает" in read("docs/USER_GUIDE.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        assert tasks["T-38"]["Статус"] == "done" and tasks["T-38"]["Задача"].endswith(f"выпущено в {VERSION}")
        # v1.70.91: T-39 выпущен — до выпуска его блокером был ответ на Q-28
        assert tasks["T-39"]["Статус"] == "done" or "Q-28" in tasks["T-39"]["Блокер"]
        questions = {r["ID"]: r for r in csv.DictReader(read("docs/kb/06_open_questions.csv").splitlines())}
        # v1.70.90: открыт в 1.70.89, затем ответ владельца
        assert questions["Q-28"]["Статус"] in ("open", "answered")
        assert questions["Q-28"]["Связанная задача"] == "T-39"
