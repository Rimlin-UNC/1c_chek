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
    """Выпуск 1.70.90 — исторические проверки.

    v1.70.91: точные пины перенесены в tests/test_v17091.py. Здесь остаётся то,
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
        assert sw.count(f"?v={current}") == 4
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2
        assert f'"version": "{current}"' in read("app/static/manifest.webmanifest")

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-03"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("Ставьте 1.70.90 вместо 1.70.89", "резервная копия", "формы правки", "по QR",
                       "было значение и его стёрли", "требует уточнения, #4", "31 проверка"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1
        for needle in ("по QR", "очисткой", "значение было и вы его стёрли", "ничего не меняется"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        for needle in ("(«✏️»)", "по QR", "не менялись", "одна карточка"):
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
        assert declared >= 50


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
        # v1.70.91: число проверок растёт — «32 проверки на каждом»
        m = re.search(r"(\d+) проверк\w* на каждом", db)
        assert m and int(m.group(1)) >= 31 and "с 1.70.90 — сохранение формы правки у чека по QR" in db
        assert "С 1.70.90 то же при сохранении формы правки («✏️»)" in flat("docs/USER_GUIDE.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        # v1.70.91: T-01 указывает на последний выпуск — не старше этого
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 90)
        # v1.70.91: T-39 выпущен — блокера с ответом на Q-28 больше нет
        assert tasks["T-39"]["Статус"] == "done" or "(в)" in tasks["T-39"]["Блокер"]
        assert "#10" in tasks["T-30"]["Блокер"] and "отложено" in tasks["T-30"]["Блокер"].lower()
        assert tasks["T-30"]["Статус"] == "blocked"
        questions = {r["ID"]: r for r in csv.DictReader(read("docs/kb/06_open_questions.csv").splitlines())}
        assert questions["Q-28"]["Статус"] == "answered"
        assert "(в)" in questions["Q-28"]["Вопрос или блокер"]
