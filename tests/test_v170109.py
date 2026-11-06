"""v1.70.109: второе прочтение сканов и фото — RapidOCR + PP-OCRv6 Tiny Cyrillic после Tesseract (T-77, ADR-39).

Всё — на синтетике и подставных движках (ADR-14). Настоящих моделей в CI нет: их скачивает и прогоняет
задание «OCR bench» (qa/ocr_bench.py, движок worker_second). Здесь проверяются:
- правило «когда нужно второе прочтение»;
- связка в service.recognize_file (форма, почта и боты идут через неё);
- режим администратора и строка журнала;
- скачивание модели с проверкой хеша и запасным источником;
- рабочий процесс со вторым движком на подставном RapidOCR (поворот листа, рецепт карточки модели);
- пометка номера из латинских букв-двойников;
- точки выпуска.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.primary import ocr, ocr_worker, print_form, service
from tests.test_primary_recognize import OCR_SCAN, _png, _recognize
from tests.test_primary_upd import firms  # noqa: F401 — фикстура компаний и бухгалтеров

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.109"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/61"
HEAD = "\n".join(OCR_SCAN.splitlines()[:7])            # шапка без таблицы: строк и итогов нет — есть сомнения


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
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

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.108", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-06"
        entry = md.split("\n## [1.70.108]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Второе прочтение сканов и фото", "резервная копия", "pip поставит onnxruntime и rapidocr",
                       "Python 3.10 и новее", "откатится само", "Схема базы не меняется", "48 из 48",
                       "1.70.82–1.70.108", "ADR-39", "57 из 60 вместо 51", "7 из 18 вместо 1", "по сомнению",
                       "Apache-2.0", "Occular-OCR и Indra OCR не взяты", "«YT-215» вместо «УТ-215»", "T-41", "T-46"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Второе прочтение сканов и фото", "57 из 60 вместо", "Номер с латинскими буквами",
                       "«Скачать модель» (2,5 МБ)"):
            assert needle in block, needle
        assert "'1.70.108': [" in js                         # история выпусков на месте

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Первичка»: второе прочтение сканов и фото, режим у администратора")
        entry = journal.split("\n## v1.70.108", 1)[0]
        for needle in ("«🔁 Второе прочтение (PP-OCRv6,", "«по сомнению» (по умолчанию), «всегда», «выключено»",
                       "«Скачать модель (2,5 МБ)»", "«Скачать модели (11 МБ)»", "поля сведены из двух прочтений",
                       "USER_GUIDE §7.15", "ADR-39", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Первичка (электронные УПД)"]
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 109) for v in reg.values())
        before = {**reg, "Первичка (электронные УПД)": "1.70.108"}
        assert updater.diff_blocks(before, reg) == [{"name": "Первичка (электронные УПД)", "from": "1.70.108",
                                                     "to": VERSION}]

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (06.11.2026)" in readme
        assert "Последний выпуск — v1.70.108" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.108.**" in readme
        section = readme.split(f"## 🆕 Последний выпуск — v{VERSION}", 1)[1].split("\n## ", 1)[0]
        assert "Схема базы не меняется" in section and "**Второе прочтение сканов и фото:**" in section

    def test_requirements_markers(self):
        req = read("requirements.txt")
        marker = ('sys_platform == "linux" and (platform_machine == "x86_64" or platform_machine == "aarch64") '
                  'and python_version >= "3.10"')
        assert f"onnxruntime>=1.18; {marker}" in req
        assert f"rapidocr>=3.10,<3.11; {marker}" in req
        assert 'tesserocr>=2.12; sys_platform == "linux"' in req          # Tesseract — как прежде

    def test_manual(self):
        from app.services.manual_content import build_manual
        manual = build_manual()
        assert manual["updated"] == "2026-11-06"
        html = "".join(s.get("html", "") for s in manual["sections"])
        assert "<b>С версии 1.70.109 — второе прочтение.</b>" in html and "«по сомнению» (по умолчанию)" in html


# ====================================================================== когда нужно второе прочтение
class TestRule:
    def test_needs_second_reading(self):
        assert service.needs_second_reading(None) is True
        full = print_form.parse_text(OCR_SCAN)
        assert full["uncertain"] == [] and service.needs_second_reading(full) is False
        head = print_form.parse_text(HEAD)
        assert "lines" not in head["found"] and service.needs_second_reading(head) is True
        assert service.needs_second_reading({**full, "uncertain": ["buyer_kpp"]}) is True
        assert service.needs_second_reading({**full, "found": [f for f in full["found"] if f != "seller_inn"]}) is True
        # КПП и «функция документа» не в списке основных полей: их отсутствие само по себе не повод
        assert service.needs_second_reading({**full, "found": [f for f in full["found"] if f != "buyer_kpp"]}) is False

    @pytest.mark.parametrize("number, twins", [("YT-215", True), ("AB-1", True), ("УТ-215", False), ("INV-12", False),
                                               ("215", False), ("ТО-5/1", False), ("", False)])
    def test_latin_twins(self, number, twins):
        assert print_form.latin_twins(number) is twins

    def test_latin_number_is_flagged(self):
        latin = print_form.parse_text(OCR_SCAN.replace("№ УТ-215", "№ YT-215", 1))
        assert latin["number"] == "YT-215" and "number" in latin["uncertain"] and "number" in latin["found"]
        assert "number" not in print_form.parse_text(OCR_SCAN)["uncertain"]
        merged = print_form.parse_variants([(OCR_SCAN.replace("№ УТ-215", "№ YT-215", 1), ""), (OCR_SCAN, "")])
        assert merged["number"] == "УТ-215" and "number" not in merged["uncertain"]   # второе прочтение подставит


# ====================================================================== связка в service.recognize_file
def _fake_two(monkeypatch, first: str, second: str, calls: list, *, ready: bool = True, error=None):
    def fake(_data, _suffix, *, second=False, **_options):            # preview и wait — в _options
        calls.append("second" if second else "first")
        if second and error is not None:
            raise error
        text = second_text if second else first
        return {"ok": True, "engine": "PP-OCRv6 test" if second else "Tesseract test",
                "pages": [{"text": text, "layout": "", "confidence": 80, "angle": 0}], "preview": None,
                "stats": {"seconds": 2.5 if second else 1.5, "peak_mb": 500 if second else 90}}
    second_text = second
    monkeypatch.setattr(service.ocr, "recognize", fake)
    monkeypatch.setattr(service.ocr, "second_status", lambda: {"ready": ready})


@pytest.fixture
def mode(client, firms):  # noqa: F811
    admin = firms[0]

    def set_mode(value: str):
        assert client.put("/api/v1/primary/ocr/second", headers=admin, json={"mode": value}).status_code == 200
    yield set_mode
    client.put("/api/v1/primary/ocr/second", headers=admin, json={"mode": "auto"})   # другим тестам — по умолчанию


class TestCascade:
    def test_doubtful_draft_gets_second_reading(self, client, firms, monkeypatch):  # noqa: F811
        calls: list = []
        _fake_two(monkeypatch, HEAD, OCR_SCAN, calls)
        body = _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50)).json()
        assert calls == ["first", "second"]
        assert [ln["sum_with_vat"] for ln in body["draft"]["lines"]] == [6100.0, 97600.0, 1830.0]
        assert body["draft"]["totals"]["with_vat"] == 105530.0 and "lines" in body["found"]
        assert body["ocr"]["engine"] == "Tesseract test" and body["ocr"]["second"]["engine"] == "PP-OCRv6 test"
        assert body["ocr"]["second"]["seconds"] == 2.5

    def test_clean_draft_skips_it(self, client, firms, monkeypatch):  # noqa: F811
        calls: list = []
        _fake_two(monkeypatch, OCR_SCAN, OCR_SCAN, calls)
        body = _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50)).json()
        assert calls == ["first"] and "second" not in body["ocr"]

    def test_modes_always_and_off(self, client, firms, monkeypatch, mode):  # noqa: F811
        calls: list = []
        _fake_two(monkeypatch, OCR_SCAN, OCR_SCAN, calls)
        mode("always")
        _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50))
        assert calls == ["first", "second"]
        calls.clear()
        _fake_two(monkeypatch, HEAD, OCR_SCAN, calls)
        mode("off")
        body = _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50)).json()
        assert calls == ["first"] and body["draft"]["lines"] == []          # сомнения остались — решает человек

    def test_failure_or_not_ready_keeps_tesseract_draft(self, client, firms, monkeypatch):  # noqa: F811
        calls: list = []
        _fake_two(monkeypatch, HEAD, OCR_SCAN, calls, error=ocr.OcrBusy("Сервер распознаёт другой документ"))
        body = _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50)).json()
        assert calls == ["first", "second"] and body["draft"]["lines"] == []
        assert body["ocr"]["second"] == {"error": "Сервер распознаёт другой документ"}
        calls.clear()
        _fake_two(monkeypatch, HEAD, OCR_SCAN, calls, ready=False)
        assert _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50)).status_code == 200
        assert calls == ["first"]

    def test_unreadable_first_reading_is_saved_by_second(self, client, firms, monkeypatch):  # noqa: F811
        calls: list = []
        _fake_two(monkeypatch, "что-то нечитаемое", OCR_SCAN, calls)
        body = _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50)).json()
        assert calls == ["first", "second"] and body["draft"]["number"] == "УТ-215"
        _fake_two(monkeypatch, "что-то нечитаемое", "и тут ничего", calls)
        res = _recognize(client, firms[1]["hdr"], "scan.png", _png(50, 50))
        assert res.status_code == 422                                      # как раньше: текст не разобран


# ====================================================================== режим и журнал
class TestModeAndAudit:
    def test_status_and_mode_endpoint(self, client, firms):  # noqa: F811
        admin, a, _ = firms
        st = client.get("/api/v1/primary/ocr", headers=a["hdr"]).json()
        assert st["second_mode"] == "auto" and st["second"]["model"] == "PP-OCRv6 Tiny Cyrillic"
        assert st["second"]["license"] == "Apache-2.0" and isinstance(st["second"]["ready"], bool)
        assert client.put("/api/v1/primary/ocr/second", headers=a["hdr"], json={"mode": "off"}).status_code == 403
        assert client.put("/api/v1/primary/ocr/second", headers=admin, json={"mode": "иногда"}).status_code == 422
        try:
            res = client.put("/api/v1/primary/ocr/second", headers=admin, json={"mode": "always"})
            assert res.status_code == 200 and res.json()["second_mode"] == "always" and "second" in res.json()
            assert client.get("/api/v1/primary/ocr", headers=a["hdr"]).json()["second_mode"] == "always"
        finally:
            client.put("/api/v1/primary/ocr/second", headers=admin, json={"mode": "auto"})

    def test_unknown_stored_mode_falls_back_to_auto(self, monkeypatch):
        monkeypatch.setattr(service.appsettings, "get_setting", lambda _db, _key, _default="": "иногда")
        assert service.second_mode(object()) == "auto"
        with pytest.raises(service.RecognizeError, match="по сомнению, всегда или выключено"):
            service.set_second_mode(object(), "иногда")

    def test_audit_text_and_icon(self):
        from app.services.audit_human import humanize_audit
        assert humanize_audit("primary_ocr_second", {"mode": "off"}) == (
            "изменён режим второго прочтения сканов", "режим: выключено")
        assert humanize_audit("primary_ocr_second", {"mode": "auto"})[1] == "режим: по сомнению"
        assert "primary_ocr_second: ['info', '🔁']" in read("app/static/js/app.js")


# ====================================================================== модуль ocr: состояние, скачивание, запуск
class TestOcrModule:
    def test_second_status_explains(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ocr, "SECOND_DIR", tmp_path)
        monkeypatch.setattr(ocr, "SECOND_LAST_FILE", tmp_path / "last.json")
        monkeypatch.setattr(ocr, "_second_modules", lambda: False)
        st = ocr.second_status()
        assert st["ready"] is False and "не установлены модули rapidocr и onnxruntime" in st["reason"]
        monkeypatch.setattr(ocr, "_second_modules", lambda: True)
        st = ocr.second_status()
        assert st["missing"] == ["inference.onnx", "rec_keys.txt"] and "кнопка «Скачать модели»" in st["reason"]
        for name, meta in ocr.SECOND_FILES.items():
            (tmp_path / name).write_bytes(b"\0" * meta["size"])
        assert ocr.second_status()["ready"] is True and ocr.second_status()["reason"] == ""
        assert "second" in ocr.status()

    def test_pinned_sources(self):
        assert ocr.SECOND_REPO == "metrika-labs/PP-OCRv6_tiny_rec_cyrillic_onnx"
        assert re.fullmatch(r"[0-9a-f]{40}", ocr.SECOND_REVISION)
        assert ocr.SECOND_FILES["inference.onnx"]["size"] == 2497057
        assert re.fullmatch(r"[0-9a-f]{64}", ocr.SECOND_FILES["inference.onnx"]["sha256"])
        assert [label for label, _ in ocr.SECOND_SOURCES] == ["huggingface.co", "hf-mirror.com"]

    def test_download_with_fallback_and_hash(self, monkeypatch, tmp_path):
        model, keys = b"onnx-model-test", b"keys"
        monkeypatch.setattr(ocr, "MODELS", {})
        monkeypatch.setattr(ocr, "TESSDATA_DIR", tmp_path / "tess")
        monkeypatch.setattr(ocr, "SYSTEM_TESSDATA", ())
        monkeypatch.setattr(ocr, "SECOND_DIR", tmp_path / "second")
        monkeypatch.setattr(ocr, "SECOND_LAST_FILE", tmp_path / "last.json")
        monkeypatch.setattr(ocr, "_second_modules", lambda: True)
        monkeypatch.setattr(ocr, "SECOND_FILES", {
            "inference.onnx": {"sha256": hashlib.sha256(model).hexdigest(), "size": len(model)},
            "rec_keys.txt": {"sha1": ocr.git_blob_sha1(keys), "size": len(keys)}})
        calls = []

        def fetch(url, _headers):
            calls.append(url)
            if "huggingface.co" in url:
                raise OSError("нет сети")
            return model if url.endswith("inference.onnx") else keys

        result = ocr.download_models(fetch=fetch)
        assert calls[0] == f"https://huggingface.co/{ocr.SECOND_REPO}/resolve/{ocr.SECOND_REVISION}/inference.onnx"
        assert "PP-OCRv6 inference.onnx: скачано с hf-mirror.com" in result["log"]
        assert (tmp_path / "second" / "inference.onnx").read_bytes() == model and result["second"]["ready"] is True
        assert "PP-OCRv6 rec_keys.txt: уже есть" in ocr.download_models(fetch=fetch)["log"]
        for name in ("inference.onnx", "rec_keys.txt"):
            (tmp_path / "second" / name).unlink()
        poisoned = ocr.download_models(fetch=lambda url, headers: b"poisoned-model!")   # Tesseract не страдает
        assert any(line.startswith("второе прочтение: модель PP-OCRv6 не скачана") for line in poisoned["log"])
        assert not list((tmp_path / "second").iterdir())
        monkeypatch.setattr(ocr, "_second_modules", lambda: False)
        assert ocr.download_models(fetch=fetch)["log"][-1].startswith("второе прочтение: модули rapidocr и onnxruntime")

    def test_recognize_second_runs_worker_with_argument(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ocr, "LOCK_FILE", tmp_path / "ocr.lock")
        monkeypatch.setattr(ocr, "SECOND_LAST_FILE", tmp_path / "last_second.json")
        monkeypatch.setattr(ocr, "LAST_FILE", tmp_path / "last.json")
        monkeypatch.setattr(ocr, "second_status", lambda: {"ready": False, "reason": "нет модели"})
        with pytest.raises(ocr.OcrUnavailable, match="Второе прочтение не настроено: нет модели"):
            ocr.recognize(b"x", ".png", second=True)
        monkeypatch.setattr(ocr, "second_status", lambda: {"ready": True, "reason": ""})
        seen = {}

        def run(cmd, **_kwargs):
            seen["cmd"] = cmd
            answer = {"ok": True, "engine": "PP-OCRv6 Tiny Cyrillic (RapidOCR 3.10.0)", "seconds": 2.5, "peak_mb": 510,
                      "pages": [{"text": "т", "layout": "т", "confidence": 90, "angle": 0}]}
            return types.SimpleNamespace(stdout=json.dumps(answer).encode(), returncode=0)

        monkeypatch.setattr(ocr.subprocess, "run", run)
        out = ocr.recognize(b"x", ".png", second=True)
        assert seen["cmd"][-1] == f"second:{ocr.SECOND_DIR}" and out["stats"]["peak_mb"] == 510
        assert json.loads((tmp_path / "last_second.json").read_text())["engine"].startswith("PP-OCRv6")
        assert not (tmp_path / "last.json").exists()                     # замер Tesseract не затёрт


# ====================================================================== рабочий процесс: подставной RapidOCR
LINES = ("Счет-фактура № УТ-215 от 14 октября 2026 г.", "Продавец: ООО «СтройБаза»",
         "ИНН/КПП продавца: 5001007329/500101001", "Покупатель: ООО «Ромашка-Строй»",
         "ИНН/КПП покупателя: 7704123450/770401001", "Валюта: наименование, код Российский рубль, 643",
         "Всего к оплате 86 500,00 19 030,00 105 530,00")


class FakeRapid:
    """Как RapidOCR: ровный лист — широкие строки; лист боком — строки столбиком (их RapidOCR читает сам);
    вверх ногами без классификатора — мусор. Ориентир — чёрный квадрат в левом верхнем углу исходника."""

    def __init__(self):
        self.calls = []

    def __call__(self, img, use_cls=True):
        self.calls.append((img.size, use_cls))
        upright = img.getpixel((10, 10))[0] < 128
        if img.width < img.height:
            boxes = [[[100 + 60 * i, 100], [140 + 60 * i, 100], [140 + 60 * i, 1500], [100 + 60 * i, 1500]]
                     for i in range(len(LINES))]
            texts = LINES if use_cls else tuple("ʎɹǝʌ" for _ in LINES)
        else:
            boxes = [[[100, 100 + 60 * i], [1500, 100 + 60 * i], [1500, 140 + 60 * i], [100, 140 + 60 * i]]
                     for i in range(len(LINES))]
            texts = LINES if (upright or use_cls) else tuple("ɐɓɔ" for _ in LINES)
        return types.SimpleNamespace(boxes=np.array(boxes, dtype=float), txts=tuple(texts),
                                     scores=tuple(0.9 for _ in texts))


def _sheet() -> Image.Image:
    img = Image.new("L", (1600, 1000), 255)
    ImageDraw.Draw(img).rectangle((0, 0, 50, 50), fill=0)
    return img


class TestWorkerSecond:
    def test_vertical(self):
        tall = {"t": "x", "x0": 0, "y0": 0, "x1": 40, "y1": 900}
        wide = {"t": "x", "x0": 0, "y0": 0, "x1": 900, "y1": 40}
        assert ocr_worker.vertical([tall, tall, wide]) is True
        assert ocr_worker.vertical([tall, wide, wide]) is False and ocr_worker.vertical([]) is False

    def test_upright_sheet(self):
        page = ocr_worker.recognize_page_second(_sheet(), FakeRapid())
        assert page["angle"] == 0 and page["confidence"] == 90
        assert "Продавец: ООО «СтройБаза»" in page["layout"] and ocr_worker.anchors(page["text"]) >= 4

    def test_sideways_sheet_is_turned_back(self):
        engine = FakeRapid()
        sideways = _sheet().rotate(90, expand=True)                       # квадрат ушёл в левый нижний угол
        page = ocr_worker.recognize_page_second(sideways, engine)
        assert page["angle"] == 270 and "ИНН/КПП покупателя" in page["layout"]
        assert [cls for _, cls in engine.calls] == [True, False, False, True]   # пробы — без классификатора

    def test_engine_recipe(self, monkeypatch):
        class Rec:
            def __init__(self):
                self.rec_image_shape, self.ratios = [3, 48, 640], []

            def resize_norm_img(self, img, ratio):
                self.ratios.append(ratio)
                return img

        class Rapid:
            def __init__(self, params):
                self.params, self.rec = params, Rec()

            def _load_rec_model(self):
                return self.rec

        monkeypatch.setitem(sys.modules, "rapidocr", types.SimpleNamespace(RapidOCR=Rapid))
        engine = ocr_worker.second_engine("/models")
        p = engine.params
        assert (p["Det.limit_type"], p["Det.limit_side_len"]) == ("max", 1280)
        assert p["Rec.model_path"] == "/models/inference.onnx" and p["Rec.rec_keys_path"] == "/models/rec_keys.txt"
        assert p["EngineConfig.onnxruntime.intra_op_num_threads"] == 1 and p["Rec.rec_img_shape"] == [3, 48, 640]
        assert engine.rec.rec_image_shape == [3, 48, 48]
        engine.rec.resize_norm_img("img", 2.0)
        engine.rec.resize_norm_img("img", 20.0)
        assert engine.rec.ratios == [640 / 48, 30.0]

    def test_main_uses_second_engine(self, monkeypatch, tmp_path):
        path = tmp_path / "sheet.png"
        _sheet().convert("RGB").save(path)
        monkeypatch.setattr(ocr_worker, "second_engine", lambda models: FakeRapid())
        answer = ocr_worker.main([str(path), "/нет", "rus+eng", "5", "0", "second:/models"])
        assert answer["ok"] is True and answer["engine"].startswith("PP-OCRv6 Tiny Cyrillic")
        assert answer["pages"][0]["angle"] == 0

        def missing(_models):
            raise ImportError("rapidocr")
        monkeypatch.setattr(ocr_worker, "second_engine", missing)
        with pytest.raises(ocr_worker.WorkerError, match="Не установлены модули второго прочтения"):
            ocr_worker.main([str(path), "/нет", "rus+eng", "5", "0", "second:/models"])


# ====================================================================== документы и база знаний
class TestDocsAndKb:
    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-39](#adr-39) |" in arch and "## ADR-39" in arch
        adr = arch.split("## ADR-39", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.70.109, 2026-11-06", "дополняет [ADR-25](#adr-25)", "Q-30",
                       "42147246976", "`ocr_second_reading`", "hf-mirror.com", "SHA-256", "[ADR-14](#adr-14)", "T-77"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        head = "### 7.15. Второе прочтение сканов и фото (с v1.70.109) — бухгалтер/админ, режим — администратор"
        assert head in guide and guide.index(head) < guide.index("## 8. Маппинг реквизитов")
        assert "### 7.14." not in guide and "data/ocr_models/ppocrv6_tiny_cyrillic" in guide
        design = read("docs/primary_docs_design.md")
        assert "### Как сделано в 1.70.109" in design and "## Распознавание русскоязычных документов (T-77" in design

    def test_bench_checks_the_program_path(self):
        bench = read("qa/ocr_bench.py")
        assert '"worker_second"' in bench and 'f"second:{second}"' in bench
        flow = read(".github/workflows/ocr-bench.yml")
        assert "run worker_second /tmp/v-app 1" in flow and "result = ocr.download_models()" in flow
        assert 'assert result["second"]["ready"]' in flow

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t77 = tasks["T-77"]
        assert t77["Статус"] == "done" and t77["Задача"].endswith(f"выпущено в {VERSION}")
        assert t77["Issue/PR"] == ISSUE and t77["Связанные ADR"] == "ADR-25, ADR-38, ADR-39"
        assert t77["Связанные вопросы"] == "Q-30"
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.108 без замороженного «Моего дела»" \
            in tasks["T-01"]["Блокер"]
        questions = {r["ID"]: r for r in csv.DictReader(read("docs/kb/06_open_questions.csv").splitlines())}
        assert questions["Q-30"]["Статус"] == "answered" and "«Да, делать в 1.70.109»" in questions["Q-30"]["Вопрос или блокер"]
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 06.11.2026 (выпуск v{VERSION}):** [#61]({ISSUE}) T-77" in roadmap
        assert f"T-01 — установить на рабочий сервер v{VERSION} (включает 1.70.82–1.70.108" in roadmap
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert "[#26](https://github.com/Rimlin-UNC/1c_chek/issues/26) T-41" in agent
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
        assert ledger[-1]["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", ledger[-1]["Коммит"])
        assert ledger[-2]["Версия"] == "1.70.108" and ledger[-2]["Откат на неё"] == "можно"
        seo = read("docs/kb/12_seo.md")
        assert "**Сделано владельцем 06.11.2026:**" in seo and "всё вставил в блок всё работает" in seo
