"""v1.70.91: правка чека не стирает позиции; «Единая база», шаг 4в (T-39) — данные единой записи в карточке.

Форма правки («✏️») открывалась по строке списка, где нет позиций (только items_count),
и при сохранении отправляла items: [] — сервер стирал позиции чека. Теперь форма
загружает чек целиком, а позиции уходят, только если их меняли. Шаг 4в — решение
владельца Q-28, вариант (в): данные единой записи, которых нет в копии компании, —
отдельным блоком unified_fill с пометкой; копии и выгрузки в 1С не меняются;
«Подставить» переносит их в чек.

Поведение проверяют tests/test_unified_read.py (API, выгрузки, спорные записи,
«Подставить», PostgreSQL), tests/js/unified_fill.test.js, сценарий формы правки в
DOM-стенде tests/ui_harness.js и сквозной тест миграции (32 проверки). Здесь —
синхронизация выпуска, проводка, документация и база знаний. Схема базы не меняется.
"""
from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.91"


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
        entry = md.split("\n## [1.70.90]", 1)[0]
        assert len(entry) <= 4000, len(entry)       # окно обновления — первые 4000 символов
        for needle in (
            "резервная копия", "pip пропускается", "Схема базы не меняется", "Откат", "1.70.80", "48 из 48",
            "Позиции чека (0)", "отправляла пустой список", "загружает чек целиком",
            "только если их меняли", "«Подставить» вернёт их в чек",
            "(T-39)", "Q-28", "«из единой записи»", "В выгрузку в 1С и авансовый отчёт они не попадают",
            "Спорные данные", "без исправления падает", "32 проверки",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("не стирает позиции", "загружает чек целиком", "«из единой записи»", "«Подставить»"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — карточка чека: данные единой записи с пометкой")
        entry = journal.split("\n## v1.70.90", 1)[0]
        for needle in ("«Продавец»", "«Кассир»", "синей пометкой", "Окно руководителя", "«Подставить»",
                       "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Адаптивный интерфейс", "Единый реестр чеков"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 91) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert "Последний выпуск — v1.70.90" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.90.**" in readme


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50


class TestUnifiedRead:
    def test_api_attaches_fill_without_touching_copy(self):
        from app.routers import receipts
        card = inspect.getsource(receipts.get_receipt)
        assert "unified_read.fills_for_receipts(db, [receipt])" in card and 'd["unified_fill"] = fill' in card
        listing = inspect.getsource(receipts.list_receipts)
        assert "unified_read.fills_for_receipts(db, rows, with_items=with_items)" in listing

    def test_reader_hides_disputed_and_labels(self):
        from app.unified import read
        from app.unified.models import REVIEW_BLOCKING, REVIEW_VERIFICATION
        assert read.HIDDEN_FLAGS == REVIEW_BLOCKING | REVIEW_VERIFICATION
        assert read.FILL_FIELDS == ("merchant_name", "merchant_inn", "merchant_address", "cashier")

    def test_ui_module_is_wired(self):
        js = read("app/static/js/app.js")
        assert "from './unified_fill.js'; // v1.70.91" in js
        for call in ("${sellerRows(r)}", "fillItemsTable(r)", "fillCallout(r)", "applyFill(r, {"):
            assert call in js, call
        assert '"unified_fill.js", "unified_review.js", "app.js"' in read("tests/test_v1121.py")   # модуль в бандле DOM-стенда
        for name in ("tests/js/telegram_login.test.js", "tests/js/splash.test.js"):
            assert "fillCallout: () => ''" in read(name), name             # заглушки нового импорта

    def test_edit_form_loads_full_receipt_and_keeps_items(self):
        js = read("app/static/js/app.js")
        form = js.split("async function openEditReceipt(r, onSaved) {", 1)[1].split("\nfunction openNotifyDialog", 1)[0]
        assert "if (!Array.isArray(r.items)) {" in form
        assert "r = await api.get('/api/v1/receipts/' + encodeURIComponent(r.id));" in form
        assert "if (itemsDirty) body.items = collectItems();" in form
        assert "\n      body.items = collectItems();" not in form

    def test_harness_vitest_and_e2e_cover_it(self):
        harness = read("tests/ui_harness.js")
        assert harness.index("v1.70.91: форма правки загрузила полный чек") < harness.rindex("HARNESS_OK checks")
        assert "сохранение без правки позиций не отправляет items" in harness
        assert "applyFill" in read("tests/js/unified_fill.test.js")
        assert "фаза 4в: карточка чека по QR показывает" in read("qa/migration_e2e.py")


class TestDocs:
    def test_design_and_adr(self):
        design = flat("docs/single_base_design.md")
        assert "### 12.9. Шаг 4в: чтение из единой записи (выпуск 1.70.91)" in design
        assert "**Сделано в 1.70.91** по ответу на Q-28 (в)" in design
        assert "Шаг 4в выпущен в 1.70.91 по ответу владельца на Q-28" in flat("docs/ARCHITECTURE.md")

    def test_docs_follow_release(self):
        assert "## Данные единой записи в карточке чека (выпуск 1.70.91, шаг 4в, T-39)" in read("docs/unified_receipts.md")
        db = flat("docs/DATABASE.md")
        assert "32 проверки на каждом" in db and "с 1.70.91 — данные единой записи в карточке" in db
        assert "С 1.70.91 в карточке чека видны продавец, кассир и позиции" in flat("docs/USER_GUIDE.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        assert tasks["T-39"]["Статус"] == "done" and tasks["T-39"]["Задача"].endswith(f"выпущено в {VERSION}")
        assert f"Ставить сразу v{VERSION}" in tasks["T-01"]["Блокер"]
        assert tasks["T-57"]["Статус"] == "todo" and tasks["T-57"]["Приоритет"] == "P1"
        for tid in ("T-50", "T-51", "T-52", "T-53", "T-54", "T-55", "T-56"):
            assert tasks[tid]["Issue/PR"].startswith("https://github.com/Rimlin-UNC/1c_chek/issues/"), tid
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
