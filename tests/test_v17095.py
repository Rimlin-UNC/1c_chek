"""v1.70.95: «комбайн первички» — XML 5.03 по схеме ФНС, PDF, сканы и фото на своём сервере, ручной ввод (T-61).

Поведение проверяют tests/test_primary_recognize.py (схема, печатная форма, OCR с поддельным движком,
API черновика и подтверждения, миграция колонки) и tests/js/primary_recognize.test.js (окно сверки,
ручной ввод, кнопка моделей). Сценарии — в DOM-стенде tests/ui_harness.js и в Playwright
(tests/e2e/app.spec.js). Здесь — синхронизация выпуска, зависимости, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.95"


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
        assert f"ymaster-check-v{VERSION}" in sw and sw.count(f"?v={VERSION}") == 2
        assert read("app/static/index.html").count(f"?v={VERSION}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-04"
        entry = md.split("\n## [1.70.94]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("(T-61)", "резервная копия", "Зависимости изменились", "lxml, pypdfium2 и tesserocr",
                       "откатится само", "колонка source", "48 из 48", "1.70.82–1.70.94", "официальной XSD-схемой ФНС",
                       "Tesseract 5", "«Сохранить в реестр»", "«Ввести вручную»", "«Скачать модели (8 МБ)»",
                       "«Наим»", "СвФЛУч", "16,67 %", "T-62", "T-63", "T-64", "T-60"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("PDF, скан или фото", "без облаков", "«Сохранить»", "вручную", "схемой ФНС",
                       "иностранных поставщиков"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Первичка»: PDF, сканы и фото со сверкой, ручной ввод, схема ФНС")
        entry = journal.split("\n## v1.70.94", 1)[0]
        for needle in ("«✍️ Ввести вручную»", "«Скачать модели (8 МБ)»", "«Крупнее»", "⚠", "«Подставить»",
                       "«Пропустить»", "«Подробно (N)»", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Первичка (электронные УПД)"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 95) for v in reg.values())
        before = dict(reg, **{"Первичка (электронные УПД)": "1.70.94"})
        assert {"name": "Первичка (электронные УПД)", "from": "1.70.94", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (04.11.2026)" in readme
        assert "Последний выпуск — v1.70.94" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.94.**" in readme


class TestDependencies:
    def test_requirements_are_local_open_source(self):
        req = read("requirements.txt")
        assert "\nlxml>=5.2\n" in req and "\npypdfium2>=4.30\n" in req
        # колесо tesserocr есть только для Linux x86_64/aarch64: на прочих платформах pip его пропустит
        assert ('tesserocr>=2.12; sys_platform == "linux" and (platform_machine == "x86_64" or '
                'platform_machine == "aarch64")') in req
        for banned in ("pymupdf", "paddle", "torch", "easyocr", "openai", "google-cloud-vision", "ollama"):
            assert banned not in req.lower(), banned

    def test_heavy_libraries_stay_out_of_the_app_process(self):
        """Tesseract и pypdfium2 грузятся только в процессе-обработчике, а в приложении — нет."""
        for rel in ("app/primary/ocr.py", "app/primary/service.py", "app/primary/router.py", "app/primary/print_form.py"):
            code = read(rel)
            assert "import tesserocr" not in code and "import pypdfium2" not in code, rel
        worker = read("app/primary/ocr_worker.py")
        assert "import tesserocr" in worker and "import pypdfium2 as pdfium" in worker
        assert "from app" not in worker and "from .." not in worker      # обработчик не тянет приложение
        ocr = read("app/primary/ocr.py")
        assert 'find_spec("tesserocr")' in ocr and '"-I"' in ocr and '"OMP_THREAD_LIMIT": "1"' in ocr
        assert "preexec_fn=" not in ocr                                   # небезопасно в многопоточном сервере


class TestSchema:
    def test_no_new_tables_one_new_column(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 52
        from app.primary.models import PrimaryDocument
        col = PrimaryDocument.__table__.columns["source"]
        assert col.server_default is not None and col.server_default.arg == "xml" and col.type.length == 10
        cols = set(PrimaryDocument.__table__.columns.keys())
        assert not {"xml", "content", "raw", "body", "image", "pdf"} & cols      # файлы по-прежнему не хранятся
        assert ('("source", "ALTER TABLE primary_documents ADD COLUMN source VARCHAR(10) DEFAULT \'xml\'")'
                in read("app/database.py"))
        db = read("docs/DATABASE.md")
        assert "52 объявления `__tablename__`" in db and "колонка `source` (xml, pdf, scan, manual" in db


class TestWiring:
    def test_routes_roles_and_order(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/primary/recognize", "/api/v1/primary/confirm", "/api/v1/primary/ocr",
                     "/api/v1/primary/ocr/models"):
            assert path in paths, path
        # «/ocr» объявлен раньше «/{doc_id}», иначе слово ocr приняли бы за номер документа
        assert paths.index("/api/v1/primary/ocr") < paths.index("/api/v1/primary/{doc_id}")
        router = read("app/primary/router.py")
        assert '@router.post("/ocr/models", dependencies=[Depends(require_admin)])' in router
        assert '@router.get("/ocr", dependencies=[Depends(require_accountant)])' in router
        assert "| primary: «Скачать модели» распознавания (с v1.70.95) | ✓ | — | — | — | — |" in read("docs/rbac_matrix.md")

    def test_no_503_and_busy_is_429(self):
        service = read("app/primary/service.py")
        assert "RecognizeError(str(exc), 429)" in service and "RecognizeError(str(exc), 409)" in service
        assert "HTTPException(424, str(exc))" in read("app/primary/router.py")

    def test_screen_wired(self):
        screen = read("app/static/js/primary.js")
        for needle in ("accept=\".xml,.zip,.pdf,.jpg,.jpeg,.png,.tif,.tiff\"", "'/api/v1/primary/recognize'",
                       "'/api/v1/primary/confirm'", "'/api/v1/primary/ocr'", "'/api/v1/primary/ocr/models'",
                       "{ retries: 1 }", "export function primReviewHtml(rec, opts = {})", "id=\"prim-manual\""):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("prim", "PRIM", "viewPrimary")) for t in tops), tops   # общая область сборки
        css = read("app/static/css/app.css")
        assert ".prim-review" in css and ".prim-uncertain input" in css and ".prim-review.prim-zoom" in css
        spec = read("tests/e2e/app.spec.js")
        assert "test('первичка: PDF печатной формы распознан, сверен и сохранён'" in spec
        assert "toContainText('Файл соответствует схеме ФНС 5.03')" in spec
        assert "tests/fixtures/primary/upd_print_text.pdf" in spec

    def test_manual_and_guide(self):
        from app.services.manual_content import SECTIONS
        section = next(s for s in SECTIONS if s["id"] == "primary")
        for needle in ("С версии 1.70.95 — бумажные документы", "«Ввести вручную»", "«Скачать модели»",
                       "официальной схемой ФНС"):
            assert needle in section["html"], needle
        guide = read("docs/USER_GUIDE.md")
        assert "#### PDF, сканы и фото печатной формы (с v1.70.95)" in guide and "«Скачать модели (8 МБ)»" in guide


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_manual_entry(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_primary95.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("запрошено состояние распознавания сканов", "открылось окно ручного ввода",
                     "без номера и продавца сохранить нельзя", "ручной ввод ушёл на сервер с реквизитами"):
            assert f"PASS v1.70.95: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_design_research_and_adr(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("**Сделано в 1.70.95:**", "| 6б |", "| T-62 |", "| T-63 |", "| T-64 |",
                       "## Комбайн первички (1.70.95): что взяли и почему", "### Совет по серверу",
                       "**1 ГБ и 2 ядер хватает**", "`<Файл ВерсФорм=\"5.03\">`", "upd-builder (daoman)",
                       "| Книжный скан альбомного УПД |"):
            assert needle in design, needle
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-25](#adr-25) |" in arch and "## ADR-25" in arch
        assert "заменено ADR-25 в части распознавания" in arch
        assert "Пересмотрено замером 04.11.2026 (v1.70.95, ADR-25)" in read("docs/concept_roadmap_v160.md")
        assert "Распознавание первички без облаков (срез 04.11.2026, T-61)" in read("docs/kb/10_research.md")
        xsd_readme = read("app/primary/xsd/README.md")
        assert "alexPisar/honest_mark_system" in xsd_readme and "soldatovde1979-ai/DIADOC-1C" in xsd_readme

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t61 = tasks["T-61"]
        assert t61["Статус"] == "done" and t61["Задача"].endswith(f"выпущено в {VERSION}")
        assert t61["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/46"
        assert t61["Связанные ADR"] == "ADR-23, ADR-25"
        for tid, issue in (("T-62", 47), ("T-63", 48), ("T-64", 49)):
            assert tasks[tid]["Статус"] == "todo" and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        assert tasks["T-54"]["Статус"] == "cancelled" and "T-61" in tasks["T-54"]["Блокер"]
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.94" in tasks["T-01"]["Блокер"]
        roadmap = read("docs/kb/02_roadmap.md")
        assert "[#46](https://github.com/Rimlin-UNC/1c_chek/issues/46) T-61" in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert ledger[-2]["Версия"] == "1.70.94" and ledger[-2]["Откат на неё"] == "можно"
