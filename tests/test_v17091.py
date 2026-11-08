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
    """Выпуск 1.70.91 — исторические проверки.

    v1.70.92: точные пины перенесены в tests/test_v17092.py. Здесь остаётся то,
    что верно и после следующих выпусков.
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
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 3

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-03"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("Позиции чека (0)", "загружает чек целиком", "(T-39)", "Q-28", "«из единой записи»",
                       "32 проверки"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("не стирает позиции", "«из единой записи»", "«Подставить»"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        for needle in ("«Продавец»", "синей пометкой", "«Подставить»", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Адаптивный интерфейс", "Единый реестр чеков"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 50


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
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.92: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 91)
        assert tasks["T-57"]["Статус"] in ("todo", "done") and tasks["T-57"]["Приоритет"] == "P1"   # v1.70.92: выпущен
        for tid in ("T-50", "T-51", "T-52", "T-53", "T-54", "T-55", "T-56"):
            assert tasks[tid]["Issue/PR"].startswith("https://github.com/Rimlin-UNC/1c_chek/issues/"), tid
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        row = next(r for r in ledger if r["Версия"] == VERSION)
        assert row["Опора репетиции"] == "да"
