"""v1.70.101: понимание чеков (T-69, ADR-31) и картинки у всех разделов инструкции (T-70).

Поведение проверяют tests/test_goods_understanding.py (словарь с исключениями, читаемые названия, модель, каскад,
память исправлений, профили, политика, API) и tests/js/goods.test.js (позиции, выбор категории, память, профиль).
Сценарий — в DOM-стенде tests/ui_harness.js. Здесь — синхронизация выпуска, схема, проводка, документация и база
знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.101"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.101 — исторические проверки (точные пины — в tests/test_v170102.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 2
        assert read("app/static/index.html").count(f"?v={current}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_order(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.70.100") is True
        assert updater._is_newer("1.70.100", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Понимание чеков", "резервная копия", "поверх 1.70.95–1.70.100 не меняются — шаг pip пропускается",
                       "откатится само", "новая таблица goods_memory", "4 колонки в receipt_items", "48 из 48",
                       "1.70.82–1.70.100", "«Цемент М500 Д0, 50 кг»", "37 видов товаров внутри прежних 19 групп",
                       "«Джинсы» и «Виноград» больше не алкоголь", "«🏷 Память товаров»", "«💡 Статья по товарам»",
                       "«Подставить»", "«Стройка и ремонт»", "ниже 90 %", "«Разобрать данные чеков»",
                       "у всех 33 разделов", "T-63", "T-64", "T-71"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Чеки читаются по-человечески", "«Цемент М500 Д0, 50 кг»", "Статья по товарам и профиль компании",
                       "«Подставить»", "ниже 90 %"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — понимание чеков: читаемые позиции, категории, память товаров и профиль компании"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«В чеке: [М] 4607001234567 ЦЕМЕНТ М500 Д0 50КГ»", "«запомнить» (включена)",
                       "«💡 Статья по товарам: Материалы»", "«Профиль\n  компании»", "«Вернуть по\n  профилю»",
                       "«🏷 Память товаров»", "«Забыть»", "USER_GUIDE §7.9", "у всех 33 разделов", "две\n  карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Инструкция", "Настройки", "Понимание чеков"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != "Понимание чеков"}
        assert {"name": "Понимание чеков", "from": "", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_memory_table_and_item_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 62                                   # v1.70.104: таблиц только прибавляется
        from app.goods.models import GoodsMemory
        table = GoodsMemory.__table__
        assert table.name == "goods_memory" and not table.foreign_keys                           # ADR-09
        assert {c.name for c in table.columns} == {"id", "company_id", "name_key", "category", "sample_name", "hits",
                                                   "created_by", "created_at", "updated_at"}
        assert any(getattr(c, "name", "") == "uq_goods_memory_company_key" for c in table.constraints)
        from app.models import ReceiptItem
        cols = ReceiptItem.__table__.columns
        for name in ("goods_category", "group_source", "group_note"):
            assert cols[name].server_default is not None, name                                  # старая версия пишет без них
        assert cols["group_score"].nullable
        database = read("app/database.py")
        assert "from .goods import models as _goods_models" in database
        for ddl in ("ALTER TABLE receipt_items ADD COLUMN goods_category VARCHAR(40) DEFAULT ''",
                    "ALTER TABLE receipt_items ADD COLUMN group_source VARCHAR(20) DEFAULT ''",
                    "ALTER TABLE receipt_items ADD COLUMN group_score FLOAT NULL",
                    "ALTER TABLE receipt_items ADD COLUMN group_note VARCHAR(160) DEFAULT ''"):
            assert ddl in database, ddl
        assert "from app.goods import models as _goods" in read("tests/test_migration_compat_postgres.py")
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"goods_tables": sorted(t for t in inspect(engine).get_table_names() if t == "goods_memory"),' in rehearsal
        assert 'and up.get("goods_columns") == ["goods_category", "group_note", "group_score", "group_source"],' \
            in rehearsal
        db = read("docs/DATABASE.md")
        assert "| Понимание чеков (v1.70.101) |" in db and "с 1.70.101 — `goods_memory`" in db
        m = re.search(r"(\d+) объявлени[яй] `__tablename__`", db)           # v1.70.104: число — не меньше
        assert m and int(m.group(1)) >= 62


class TestWiring:
    def test_routes_cascade_and_integration_points(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/goods/catalog", "/api/v1/goods/items/{item_id}", "/api/v1/goods/memory",
                     "/api/v1/goods/memory/{memory_id}"):
            assert path in paths, path
        assert "app.include_router(goods_router.router)" in read("app/main.py")
        from app.goods import service, view
        assert (service.MODEL_MIN, service.CONTEXT_MIN, service.TYPE_SHARE, service.REFINE_MIN) == (0.5, 0.45, 0.5, 0.9)
        assert (service.MEMORY_DAYS, service.MEMORY_SCAN, view.POLICY_MIN) == (120, 5000, 0.9)
        assert view.CERTAIN == frozenset({"manual", "memory", "fns", "dictionary", "legacy"})
        card = read("app/services/receipt_card.py")
        assert "goods_service.classify_receipt(db, receipt)" in card and "**item_view(row)," in card
        assert "return goods_service.classify_receipt(object_session(receipt), receipt)[\"items\"]" in card
        receipts = read("app/routers/receipts.py")
        assert receipts.count("goods_service.classify_receipt(db, receipt)") == 2
        assert 'd["goods"] = goods_service.summary(db, receipt)' in receipts
        assert "goods_service.classify_receipt(db, receipt)" in read("app/pool/transfer.py")
        policy = read("app/services/policy.py")
        assert "def default_goods(db: Session, company_id: str | None) -> list[str]:" in policy
        assert "if group and not is_certain(source or \"\", score):" in policy
        assert "**_goods_view(self)," in read("app/models.py")

    def test_model_asset_and_training_sources(self):
        from app.goods import model
        assert model.MODEL_PATH == ROOT / "app/goods/assets/goods_model.json.gz"
        assert model.MODEL_PATH.stat().st_size <= 5 * 1024 * 1024
        info = model.info()
        assert info["loaded"] and info["classes"] == 37 and info["sources"]["temperature"] == 0.5
        clean = info["metrics"]["eval_clean"]
        assert clean["cascade_precision"] >= 97 and clean["dictionary_category"] >= 90
        train = read("qa/goods_train.py")
        assert "SEED = 20261105" in train and "import numpy as np" in train
        assert (ROOT / "qa/goods_corpus/okpd2_goods.tsv.gz").exists() and (ROOT / "qa/goods_corpus/goods_examples.tsv").exists()
        assert (ROOT / "tests/fixtures/goods_eval.tsv").exists()
        assert "ГК РФ, ст. 1259, п. 6" in read("qa/goods_okpd2.py")
        for rel in ("app/goods/model.py", "app/goods/names.py", "app/goods/view.py", "app/goods/profiles.py"):
            assert "import numpy" not in read(rel), rel                                     # вывод — на чистом Python

    def test_settings_and_audit(self):
        settings = read("app/routers/settings_routes.py")
        assert '"goods_profile": goods_profiles.get(db, comp.id) if comp else goods_profiles.DEFAULT,' in settings
        assert "if body.reset_goods:" in settings
        from app.services.audit_human import humanize_audit as describe
        assert describe("goods_category_set", {"before": "build_base", "after": "build_finish", "remembered": True,
                                               "applied": 2})[0] == "изменена категория позиции чека"
        assert describe("goods_memory_forget", {"name_key": "цемент", "category": "build_base"})[0] == \
            "забыто исправление категории товара"
        assert "| receipts: категория позиции и «Память товаров»" in read("docs/rbac_matrix.md")


class TestScreen:
    def test_goods_module_and_app_wiring(self):
        screen = read("app/static/js/goods.js")
        for needle in ("async function goodsCatalog()", "function goodsChipHTML(it, editable)",
                       "function goodsItemsHTML(items, { editable = false, goods = null } = {})",
                       "function goodsBindItems(root, items, { onSaved } = {})", "function goodsMemoryHTML(rows, canEdit)",
                       "function goodsProfileHTML(pol, canEdit)", "'/api/v1/goods/catalog'", "`/api/v1/goods/items/",
                       "`/api/v1/goods/memory/", "remember }"):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith("goods") for t in tops), tops                               # общая область сборки
        app_js = read("app/static/js/app.js")
        assert "from './goods.js';   // v1.70.101: понимание чеков (T-69)" in app_js
        assert "${r.items.length ? `<div class=\"gd-wrap\">${goodsItemsHTML(r.items, { editable: acc, goods: r.goods })}</div>`" \
            in app_js
        assert "if (acc) goodsBindItems(drawer.querySelector('.gd-wrap'), r.items, { onSaved: async () => {" in app_js
        assert "<div class=\"glass card\" id=\"st-goods\" data-sec=\"checks\" style=\"order:25\">" in app_js
        assert "${goodsProfileHTML(pol, isAdmin())}" in app_js and "reset_goods: true" in app_js
        css = read("app/static/css/app.css")
        for cls in ("table.data.gd-table { min-width: 0; }", ".gd-chip {", ".gd-unsure", ".gd-hint {", ".gd-summary {",
                    ".gd-table .gd-price { display: none; }"):
            assert cls in css, cls
        assert '"goods.js"' in read("tests/test_v1121.py")
        card = read("app/static/js/card.js")
        assert "<div class=\"form-hint\">в чеке: ${esc(it.name)}</div>" in card

    def test_category_icons_render_everywhere(self):
        from app.services import goods
        for cat in goods.categories():
            assert all(ord(ch) < 0x1F900 for ch in cat.icon), (cat.code, cat.icon)        # не новее Unicode 9


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_goods(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_goods101.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("позиции чека — читаемые названия и чипы категорий", "бухгалтер меняет категорию — запрос с «запомнить»",
                     "в настройках — профиль компании и «Память товаров»"):
            assert f"PASS v1.70.101: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_adr_guide_manual(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-31](#adr-31) |" in arch and "## ADR-31" in arch and "`goods_memory`" in arch
        assert "**с версии / даты:** 1.70.101, 2026-11-05" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.9. Понимание чеков: читаемые позиции и категории товаров (с v1.70.101)" in guide
        design = read("docs/receipt_understanding.md")
        assert "**не данные ФНС**" in design and "ADR-31" in design and "prog815/okpd2" in design
        from app.services.manual_content import SECTIONS, build_manual
        by_id = {s["id"]: s for s in SECTIONS}
        assert "С версии 1.70.101" in by_id["goods"]["html"] and "«🏷 Память товаров»" in by_id["goods"]["html"]
        assert len(SECTIONS) >= 33 and all(s.get("image") for s in SECTIONS)                 # T-70: у всех разделов; v1.70.112: +«Согласования»
        for sid in ("goods", "claims", "orgcount"):
            image = by_id[sid]["image"]
            assert image == f"/img/manual/{sid}.jpg", sid
            assert (ROOT / "app/static" / image.lstrip("/")).read_bytes()[:2] == b"\xff\xd8", sid    # JPEG
        assert build_manual()["updated"] >= "2026-11-05"                  # v1.70.108: не раньше

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        for tid, issue in (("T-69", 54), ("T-70", 55)):
            row = tasks[tid]
            assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}"), tid
            assert row["Issue/PR"] == f"https://github.com/Rimlin-UNC/1c_chek/issues/{issue}", tid
        assert "ADR-31" in tasks["T-69"]["Связанные ADR"]
        assert tasks["T-69"]["Источник"].endswith("docs/receipt_understanding.md")
        for tid, issue in (("T-63", 48), ("T-64", 49), ("T-71", 56)):
            assert tasks[tid]["Статус"] in ("todo", "done", "blocked") and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.102: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        for tid in ("T-41", "T-42", "T-43", "T-44", "T-45", "T-46", "T-47", "T-48", "T-49"):
            assert tasks[tid]["Источник"].endswith("docs/kb/10_research.md"), tid
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.101):** [#54](https://github.com/Rimlin-UNC/1c_chek/issues/54) T-69" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.102: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.100")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
