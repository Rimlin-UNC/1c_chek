"""v1.70.90: исправлена потеря данных Чек-Пула при сохранении формы правки чека.

Форма правки («✏️», openEditReceipt) шлёт все поля сразу; у чека, отсканированного по
QR, поля продавца, ИНН, адреса и кассира уходят пустыми. В 1.70.89 PATCH считал любое
пустое поле явной очисткой и стирал эти данные в единой записи и в Чек-Пуле. Теперь
очистка — «было значение, стало пусто».

Поведение проверяют tests/test_unified_merge.py (тело формы правки, явная очистка,
страж полей очистки) и сквозной тест миграции (31 проверка, стадия «фаза 4б» с
сохранением формы). Здесь — синхронизация выпуска, проводка правила в PATCH,
документация и база знаний. Схема базы не меняется. Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.90"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def flat(rel: str) -> str:
    """Текст документа без переносов строк — фразы не зависят от вёрстки абзаца."""
    return " ".join(read(rel).split())


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
        entry = md.split("\n## [1.70.89]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in (
            # как пройдёт обновление
            f"Ставьте {VERSION} вместо 1.70.89", "резервная копия", "pip пропускается",
            "Схема базы не меняется", "Откат", "1.70.80", "1.70.82–1.70.89", "48 из 48",
            # что исправлено и почему
            "формы правки", "по QR", "явной очисткой", "даже ради комментария или сотрудника",
            "было значение и его стёрли", "уже стёртое программа сама не восстанавливает",
            "требует уточнения, #4",
            # проверки
            "то же тело, что и форма правки", "31 проверка",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1
        for needle in ("по QR", "очисткой", "значение было и вы его стёрли", "ничего не меняется"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — вид экранов не менялся")
        entry = journal.split("\n## v1.70.89", 1)[0]
        for needle in ("(«✏️»)", "по QR", "не менялись", "одна карточка"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == ["Единый реестр чеков"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 90) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert "Последний выпуск — v1.70.89" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.89.**" in readme


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50


class TestExplicitClear:
    def test_patch_clears_only_what_was_known(self):
        from app.routers import receipts
        src = inspect.getsource(receipts.patch_receipt)
        # значения копии снимаются до того, как тело правки их перезапишет
        assert src.index("before = {f: (getattr(receipt, f) or \"\").strip() for f in _CLEARABLE_FIELDS}") \
            < src.index("for f in (\"merchant_name\", \"merchant_inn\", \"merchant_address\", \"cashier\"):")
        assert "if before[f] and getattr(body, f) is not None" in src
        assert "cleared=cleared" in src

    def test_clearable_fields(self):
        from app.routers import receipts
        from app.unified import service
        assert receipts._CLEARABLE_FIELDS == ("merchant_name", "merchant_inn", "merchant_address",
                                              "cashier", "industry", "region_code")
        assert set(service.MERGE_TEXT) <= set(receipts._CLEARABLE_FIELDS)

    def test_form_body_test_and_e2e_stage_exist(self):
        merge = read("tests/test_unified_merge.py")
        assert "def test_card_form_save_keeps_what_the_copy_does_not_know" in merge
        assert "def _form_body(" in merge
        e2e = read("qa/migration_e2e.py")
        assert 'res["form"]' in e2e and "сохранение формы правки у чека по QR" in e2e


class TestDocs:
    def test_design_and_adr(self):
        design = flat("docs/single_base_design.md")
        assert "С 1.70.90 очистка — «было значение, стало пусто»" in design
        assert "**Ответ владельца 03.11.2026:** Q-28 — (в): в карточке чека — данные единой записи" in design
        assert "(T-30, #22) — отложить до переноса архива (#10)" in design
        assert "Исправление 1.70.90: явная очистка — «было значение, стало пусто»" in flat("docs/ARCHITECTURE.md")

    def test_docs_follow_release(self):
        assert "С 1.70.90 очистка — «было значение, стало пусто» (`before` в `PATCH`)" in flat(
            "docs/unified_receipts.md")
        db = flat("docs/DATABASE.md")
        assert "31 проверка на каждом" in db and "с 1.70.90 — сохранение формы правки у чека по QR" in db
        assert "С 1.70.90 то же при сохранении формы правки («✏️»)" in flat("docs/USER_GUIDE.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        assert f"Ставить сразу v{VERSION}" in tasks["T-01"]["Блокер"]
        assert "(в)" in tasks["T-39"]["Блокер"] and tasks["T-39"]["Статус"] == "todo"
        assert "#10" in tasks["T-30"]["Блокер"] and "отложено" in tasks["T-30"]["Блокер"].lower()
        assert tasks["T-30"]["Статус"] == "blocked"
        questions = {r["ID"]: r for r in csv.DictReader(read("docs/kb/06_open_questions.csv").splitlines())}
        assert questions["Q-28"]["Статус"] == "answered"
        assert "(в)" in questions["Q-28"]["Вопрос или блокер"]
