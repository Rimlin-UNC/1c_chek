"""Замер движков распознавания на синтетических документах (T-77, issue #61).

Зачем. Владелец прислал разбор кандидатов: Occular-OCR, Indra OCR, PaddleOCR и Tesseract. Цифры из
чужих статей и описаний проверяем своим замером. Условия одинаковые для всех: те же картинки и два
ядра, как на сервере 2 ГБ. Итог считается по полям черновика УПД, которые увидит бухгалтер (разбор
app/primary/print_form.py), а не только по доле верных символов.

Что сравнивается:
  tesseract  — рабочий путь программы: app/primary/ocr_worker.py, отдельный процесс на документ;
  worker_second — второе прочтение в программе (v1.70.109, ADR-39): тот же ocr_worker.py с аргументом
               second:, модель скачана кодом программы (ocr.download_models — ревизия и хеши);
  tess_det   — Tesseract и детектор строк PP-OCRv6 (приём из статьи на Хабре): читает Tesseract,
               а детектор только раскладывает слова по строкам и ячейкам;
  rapid_v6t  — RapidOCR и PP-OCRv6 Tiny Cyrillic (metrika-labs, Apache-2.0, 2,5 МБ);
  rapid_v6m  — RapidOCR и PP-OCRv6 Medium Cyrillic (metrika-labs, Apache-2.0, 63 МБ);
  *_fast     — те же модели без рецепта из карточки (вход строки не уже 640 точек и запас 50 %):
               проверяем, сколько стоит рецепт по времени и что он даёт по точности;
  indra      — Indra OCR (код Apache-2.0, веса — изменённая OpenRAIL-M);
  occular    — Occular-OCR (код Apache-2.0, веса — OpenRAIL-M: платно для организаций с выручкой
               от 20 млн ₽ или от 8 сотрудников);
  occular_lite — Occular без определения поворота и без порядка чтения по разметке: проверяем,
               помещается ли он в память сервера 2 ГБ хотя бы так.

Кроме движков по отдельности score считает «второе прочтение»: Tesseract и ещё один движок, поля
сводит тот же parse_variants, что и в программе (каждое поле — из прочтения, где оно найдено без
сомнений). Так видно, что даст каскад «сначала Tesseract, при сомнениях — второй движок».

Данные — только синтетика (ADR-14). Это печатная форма УПД из tests/fixtures/primary в шести видах:
скан 300 и 200 точек, фото, плохое качество, дефекты печати, лист боком. Плюс искусственный
кассовый чек. Настоящих документов и персональных данных здесь нет.

Где запускать: GitHub Actions (.github/workflows/ocr-bench.yml). У раннера есть доступ к Hugging
Face, откуда берутся веса кандидатов; программа на сервере их не скачивает. Каждый движок работает
в своём виртуальном окружении и своём процессе: пик памяти честный, а onnxruntime и
onnxruntime-gpu (его ставит Occular) не мешают друг другу.

Команды:
  corpus DIR                         картинки и описание manifest.json (окружение программы)
  fetch DIR                          веса PP-OCRv6 Cyrillic с Hugging Face (окружение RapidOCR)
  warmup NAME DIR                    первый запуск движка: веса скачиваются вне замера
  engine NAME DIR OUT [--threads N]  прогон движка по корпусу → JSON
  sizes OUT NAME=VENV...             размеры окружений и скачанных весов
  score DIR RESULTS [--summary F]    оценка → Markdown и аннотации GitHub (окружение программы)

Аннотаций одного вида GitHub показывает не больше 10 на шаг, поэтому score раскладывает их по файлам
notes_engines.txt (по движку) и notes_variants.txt (по виду документа), а workflow выводит их
отдельными шагами. Так итог читается и в сводке задания, и через API проверок без доступа к журналам.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import resource
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "primary" / "upd_print_text.pdf"
WORKER = ROOT / "app" / "primary" / "ocr_worker.py"
TESSDATA = os.environ.get("YM_BENCH_TESSDATA", "/tmp/tessdata")
MODELS = Path(os.environ.get("YM_BENCH_MODELS", "/tmp/models"))
# Детектор строк: длинная сторона входа. По умолчанию RapidOCR берёт до 2000 точек, и пик процесса
# доходит до 900 МБ; на 1280 точках — около 570 МБ при тех же найденных строках (проба 06.11.2026).
DET_SIDE = int(os.environ.get("YM_BENCH_DET_SIDE", "1280"))
LANG = "rus+eng"
SEED = 77
ENGINES = ("tesseract", "worker_second", "tess_det", "rapid_v6t", "rapid_v6t_fast", "rapid_v6m", "rapid_v6m_fast",
           "indra", "occular", "occular_lite")
HF_REC = {"rapid_v6t": "metrika-labs/PP-OCRv6_tiny_rec_cyrillic_onnx",
          "rapid_v6m": "metrika-labs/PP-OCRv6_medium_rec_cyrillic_onnx"}
RAPID = ("rapidocr", "onnxruntime")
PACKAGES = {"tesseract": ("tesserocr",), "worker_second": RAPID, "tess_det": ("tesserocr", *RAPID), "rapid_v6t": RAPID, "rapid_v6t_fast": RAPID,
            "rapid_v6m": RAPID, "rapid_v6m_fast": RAPID, "indra": ("indra-ocr", "onnxruntime"),
            "occular": ("occular-ocr", "onnxruntime-gpu", "pymupdf"), "occular_lite": ("occular-ocr", "onnxruntime-gpu")}

# Поля черновика: ключ разбора print_form → подпись в отчёте.
FIELDS = {"number": "номер", "date": "дата", "seller_name": "продавец", "seller_inn": "ИНН продавца",
          "seller_kpp": "КПП продавца", "buyer_name": "покупатель", "buyer_inn": "ИНН покупателя",
          "buyer_kpp": "КПП покупателя", "totals": "итоги", "lines": "суммы строк"}


def _lr(left: str, right: str, width: int = 32) -> str:
    """Строка чека: слева текст, справа сумма (моноширинный шрифт)."""
    return left + " " * max(1, width - len(left) - len(right)) + right


RECEIPT_LINES = [
    "ООО «СтройБаза»".center(32), "г. Подольск, ул. Складская, 7".center(32),
    "КАССОВЫЙ ЧЕК".center(32), "ПРИХОД".center(32),
    "Цемент М500 50кг", _lr("  2 x 450.00", "=900.00"),
    "Саморезы 4,2х76 (200 шт)", _lr("  1 x 349.90", "=349.90"),
    "Пакет-майка", _lr("  1 x 9.00", "=9.00"),
    _lr("ИТОГ", "=1258.90"), _lr("БЕЗНАЛИЧНЫМИ", "=1258.90"), _lr("В т.ч. НДС 22%", "=227.02"),
    "СНО: ОСН", "ИНН 5001007329", "ФН № 7380440801234567", "ФД № 48213", "ФП 3826104475",
    "14.10.26 12:34", "Сайт ФНС: www.nalog.gov.ru",
]
RECEIPT_FIELDS = {"ФН": "7380440801234567", "ФД": "48213", "ФП": "3826104475", "ИНН": "5001007329",
                  "ИТОГ": "1258.90", "дата и время": "14.10.26 12:34"}


def _worker():
    """ocr_worker.py без пакета app: там только стандартная библиотека, Pillow подгружается внутри."""
    spec = importlib.util.spec_from_file_location("ym_ocr_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ======================================================================= корпус
def _font(name: str, size: int):
    from PIL import ImageFont
    for folder in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"):
        path = Path(folder) / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise SystemExit(f"Нет шрифта {name}: поставьте пакет fonts-dejavu-core")


def _perspective(src: list, dst: list) -> list:
    """Коэффициенты PIL: точка результата dst[i] берётся из точки исходника src[i]."""
    import numpy as np
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        rhs += [u, v]
    return np.linalg.solve(np.array(rows, float), np.array(rhs, float)).tolist()


def _noise(img, rng, sigma: float):
    import numpy as np
    from PIL import Image
    arr = np.asarray(img, dtype=np.float32)
    arr = arr + rng.normal(0, sigma, arr.shape).astype(np.float32)
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def _photo(scan, rng):
    """Фото телефоном: бежевая бумага на столе, наклон 1,6°, трапеция, тень, размытие, шум."""
    import numpy as np
    from PIL import Image, ImageFilter
    paper = Image.merge("RGB", (scan, scan.point(lambda v: int(v * 0.97)), scan.point(lambda v: int(v * 0.9))))
    table = (96, 88, 80)
    img = paper.rotate(1.6, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=table)
    w, h = img.size
    coeffs = _perspective([(0, 0), (w, 0), (w, h), (0, h)],
                          [(w * 0.025, h * 0.012), (w * 0.985, 0), (w, h), (0, h * 0.99)])
    img = img.transform((w, h), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC, fillcolor=table)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    shade = 0.6 + 0.4 * (0.55 * xx / w + 0.45 * yy / h)
    arr = np.asarray(img, dtype=np.float32) * shade[..., None]
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.1))
    return _noise(img, rng, 7)


def _low_quality(small, rng):
    """Плохое качество: 110 точек, растянуто и пережато, как картинка из мессенджера."""
    from PIL import Image
    img = small.resize((int(small.width * 1.8), int(small.height * 1.8)), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=25)
    return _noise(Image.open(io.BytesIO(buf.getvalue())).convert("L"), rng, 6)


def _print_defects(scan, rng):
    """Дефекты печати: бледный тонер (часть точек букв пропала), светлые полосы, крошки."""
    import numpy as np
    from PIL import Image, ImageFilter
    arr = np.asarray(scan, dtype=np.int16).copy()
    h, w = arr.shape
    arr[(arr < 140) & (rng.random((h, w)) < 0.12)] = 205
    for _ in range(3):
        x, width = int(rng.integers(0, w - 40)), int(rng.integers(14, 32))
        arr[:, x:x + width] = np.minimum(255, arr[:, x:x + width] + 95)
    arr[rng.random((h, w)) < 0.0015] = 30
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5))


def _receipt(rng):
    """Кассовый чек на термобумаге: бледная печать, волна от скручивания, тёмный стол, наклон."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    font = _font("DejaVuSansMono.ttf", 24)
    line_h, margin, width = 32, 24, 576
    paper = Image.new("L", (width, margin * 2 + line_h * len(RECEIPT_LINES)), 255)
    draw = ImageDraw.Draw(paper)
    for i, text in enumerate(RECEIPT_LINES):
        draw.text((margin, margin + i * line_h), text, font=font, fill=40)
    paper = paper.resize((width * 2, paper.height * 2), Image.Resampling.BICUBIC)
    arr = 250 - (250 - np.asarray(paper, dtype=np.float32)) * 0.55
    shift = (7 * np.sin(np.arange(arr.shape[0]) / 140.0)).astype(int)
    arr = np.stack([np.roll(row, s) for row, s in zip(arr, shift)])
    receipt = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    receipt = receipt.rotate(-2.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=70)
    canvas = Image.new("L", (receipt.width + 240, receipt.height + 200), 70)
    canvas.paste(receipt, (120, 100))
    return _noise(canvas.filter(ImageFilter.GaussianBlur(0.9)), rng, 6).convert("RGB")


def make_corpus(out: Path) -> None:
    import numpy as np
    import pypdfium2 as pdfium
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)
    page = pdfium.PdfDocument(str(FIXTURE))[0]

    def render(dpi: int):
        return page.render(scale=dpi / 72, grayscale=True).to_pil().convert("L")

    files = []

    def save(img, name: str, kind: str, title: str, **options) -> None:
        img.save(out / name, **options)
        files.append({"file": name, "kind": kind, "title": title, "size": list(img.size)})

    scan300 = render(300)
    save(scan300, "upd_scan300.png", "upd", "скан 300 точек")
    save(render(200), "upd_scan200.png", "upd", "скан 200 точек")
    save(_photo(scan300, rng), "upd_photo.jpg", "upd", "фото: наклон, тень, размытие, шум", quality=55)
    save(_low_quality(render(110), rng), "upd_lowq.jpg", "upd", "плохое качество: 110 точек, сжатие", quality=35)
    save(_print_defects(scan300, rng), "upd_defects.png", "upd", "дефекты печати: тонер, полосы")
    save(scan300.rotate(90, expand=True), "upd_rot90.png", "upd", "лист боком (90°)")
    receipt = _receipt(rng)
    save(receipt, "receipt_photo.jpg", "receipt", "фото кассового чека", quality=60)
    receipt.crop((0, 0, receipt.width, receipt.height // 4)).save(out / "warmup.png")
    manifest = {"seed": SEED, "fixture": str(FIXTURE.relative_to(ROOT)), "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"корпус: {len(files)} файлов в {out}")


def fetch(out: Path) -> None:
    """Веса PP-OCRv6 Cyrillic (Apache-2.0) — только на раннере GitHub, не в программе."""
    from huggingface_hub import hf_hub_download
    for name, repo in HF_REC.items():
        for filename in ("inference.onnx", "rec_keys.txt"):
            path = hf_hub_download(repo_id=repo, filename=filename, local_dir=str(out / name))
            print(name, filename, Path(path).stat().st_size)


# ======================================================================= движки
def _image(path: Path):
    from PIL import Image
    with Image.open(path) as img:
        return img.convert("RGB")


def _item(text, quad, conf) -> dict:
    """Строка движка: текст, рамка, уверенность 0–1 и верхняя кромка (по ней считается наклон)."""
    xs, ys = [float(p[0]) for p in quad], [float(p[1]) for p in quad]
    return {"t": str(text), "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "c": round(float(conf), 3), "top": [xs[0], ys[0], xs[1], ys[1]]}


def _rect(x0, y0, x1, y1) -> list:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _vertical(lines: list) -> bool:
    """Больше половины строк выше своей ширины — лист лежит боком."""
    tall = sum(1 for it in lines if it["y1"] - it["y0"] > 1.5 * (it["x1"] - it["x0"]))
    return len(lines) > 0 and tall * 2 > len(lines)


def _rotating(read, worker, probe=None):
    """Поворот листа как в рабочем ocr_worker.py: мало опорных слов формы — пробуем 90°, 270° и 180°.
    Нужен движкам без своего определения поворота (RapidOCR и Tesseract с детектором).

    RapidOCR читает и лист боком: высокие строки он поворачивает сам. Опорные слова тогда находятся,
    а строки таблицы стоят столбиком. Поэтому второй признак — вертикальные строки. Сторону поворота
    (90° или 270°) решает пробное чтение probe без классификатора переворота строк: на неверной стороне
    строки читаются вверх ногами, и опорных слов почти нет."""
    def run(path: Path, kind: str) -> dict:
        img = _image(path)
        result, angle = read(img), 0
        if kind != "upd":
            return {**result, "angle": 0}
        if _vertical(result["lines"]):
            look = probe or read
            angle = max((90, 270), key=lambda t: worker.anchors(look(img.rotate(t, expand=True))["text"]))
            result = read(img.rotate(angle, expand=True))
        elif worker.anchors(result["text"]) < worker.MIN_ANCHORS:
            for turn in (90, 270, 180):
                other = read(img.rotate(turn, expand=True))
                if worker.anchors(other["text"]) > worker.anchors(result["text"]):
                    result, angle = other, turn
                if worker.anchors(result["text"]) >= 2 * worker.MIN_ANCHORS:
                    break
        return {**result, "angle": angle}
    return run


def _rapid_engine(threads: int, rec_dir: Path | None = None, recipe: bool = True):
    from rapidocr import RapidOCR
    params = {"Global.log_level": "error", "Det.limit_type": "max", "Det.limit_side_len": DET_SIDE,
              "EngineConfig.onnxruntime.intra_op_num_threads": threads,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1}
    if rec_dir is not None:
        params.update({"Rec.model_path": str(rec_dir / "inference.onnx"),
                       "Rec.rec_keys_path": str(rec_dir / "rec_keys.txt"),
                       "Rec.rec_img_shape": [3, 48, 640 if recipe else 320]})
    engine = RapidOCR(params=params)
    if rec_dir is not None and recipe:
        # Как в карточке модели metrika-labs: вход распознавателя не уже 640 точек, иначе модель,
        # обученная на ширине 640, теряет буквы на коротких строках.
        rec = engine._load_rec_model()
        rec.rec_image_shape = [3, 48, 48]
        resize = rec.resize_norm_img
        rec.resize_norm_img = lambda img, ratio: resize(img, max(640 / 48, 1.5 * ratio))
    return engine


def _make_rapid(model: str, recipe: bool):
    def factory(threads: int):
        engine = _rapid_engine(threads, MODELS / model, recipe)

        def read(img, cls: bool = True) -> dict:
            out = engine(img, use_cls=cls)
            if out.boxes is None:
                return {"text": "", "lines": []}
            lines = [_item(t, q, s) for q, t, s in zip(out.boxes.tolist(), out.txts, out.scores)]
            return {"text": "\n".join(out.txts), "lines": lines}
        return _rotating(read, _worker(), probe=lambda img: read(img, cls=False))
    return factory


def _make_tess_det(threads: int):
    os.environ["OMP_THREAD_LIMIT"] = str(threads)
    import tesserocr
    from tesserocr import PSM, RIL, iterate_level
    worker = _worker()
    det = _rapid_engine(threads)
    api = tesserocr.PyTessBaseAPI(path=TESSDATA, lang=LANG, psm=PSM.AUTO)

    def read(img) -> dict:
        gray = worker.prepare(img)
        scale = gray.width / img.width
        out = det(gray.convert("RGB"), use_det=True, use_cls=False, use_rec=False)
        quads = [] if out.boxes is None else out.boxes.tolist()
        rects = [(min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q))
                 for q in quads]
        api.SetImage(gray)
        api.Recognize()
        groups: dict[int, list] = {i: [] for i in range(len(quads))}
        loose = []
        for word in iterate_level(api.GetIterator(), RIL.WORD):
            text, box = word.GetUTF8Text(RIL.WORD), word.BoundingBox(RIL.WORD)
            if not text or not box:
                continue
            x0, y0, x1, y1 = box
            area, best, best_i = max(1, (x1 - x0) * (y1 - y0)), 0.0, -1
            for i, (a0, b0, a1, b1) in enumerate(rects):
                share = max(0, min(x1, a1) - max(x0, a0)) * max(0, min(y1, b1) - max(y0, b0)) / area
                if share > best:
                    best, best_i = share, i
            entry = (text.strip(), box, word.Confidence(RIL.WORD) / 100)
            (groups[best_i] if best >= 0.5 else loose).append(entry)
        lines = []
        for i, words in groups.items():
            if words:
                words.sort(key=lambda w: w[1][0])
                conf = sum(w[2] for w in words) / len(words)
                lines.append(_item(" ".join(w[0] for w in words), [(x / scale, y / scale) for x, y in quads[i]], conf))
        lines += [_item(t, [(x / scale, y / scale) for x, y in _rect(*b)], c) for t, b, c in loose]
        lines.sort(key=lambda it: ((it["y0"] + it["y1"]) / 2, it["x0"]))
        return {"text": "\n".join(it["t"] for it in lines), "lines": lines}
    return _rotating(read, worker)


def _make_indra(threads: int):
    import numpy as np
    import indra_ocr
    conv = indra_ocr.load(threads=threads, mode="ocr")

    def run(path: Path, _kind: str) -> dict:
        page = conv.ocr(np.asarray(_image(path).convert("L")))
        lines = []
        for line in page.lines:
            box, text, conf = (line.box, line.text, line.conf) if hasattr(line, "box") else tuple(line)
            lines.append(_item(text, _rect(*[float(v) for v in box]), conf))
        return {"text": page.text, "lines": lines, "angle": int(getattr(page, "rotation", 0) or 0)}
    return run


def _make_occular(threads: int, lite: bool = False):
    from occular import OCRPipeline, Settings
    options = {"num_threads": threads, "orientation": not lite}
    if lite:
        options["reading_order"] = False
    pipe = OCRPipeline(settings=Settings(**options))

    def run(path: Path, _kind: str) -> dict:
        lines = [_item(ln["text"], ln["quad"], ln.get("confidence", 0)) for ln in pipe.process_image(str(path))]
        return {"text": "\n".join(it["t"] for it in lines), "lines": lines}
    return run


FACTORIES = {"tess_det": _make_tess_det, "rapid_v6t": _make_rapid("rapid_v6t", True),
             "rapid_v6t_fast": _make_rapid("rapid_v6t", False), "rapid_v6m": _make_rapid("rapid_v6m", True),
             "rapid_v6m_fast": _make_rapid("rapid_v6m", False), "indra": _make_indra, "occular": _make_occular,
             "occular_lite": lambda threads: _make_occular(threads, lite=True)}


def _versions(name: str) -> dict:
    from importlib import metadata
    found = {}
    for package in PACKAGES[name]:
        try:
            found[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return found


def _run_worker(corpus: Path, files: list, threads: int, second: str = "") -> dict:
    """Рабочий путь: на каждый документ — отдельный процесс ocr_worker.py, как в app/primary/ocr.py.
    second — папка модели второго прочтения (тогда вместо Tesseract читает RapidOCR)."""
    env = dict(os.environ, OMP_THREAD_LIMIT=str(threads))
    extra = [f"second:{second}"] if second else []
    results, peak = [], 0
    for item in files:
        before, started = resource.getrusage(resource.RUSAGE_CHILDREN), time.perf_counter()
        run = subprocess.run([sys.executable, "-I", str(WORKER), str(corpus / item["file"]), TESSDATA, LANG, "5", "0",
                              *extra], capture_output=True, timeout=600, env=env, check=False)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            answer = json.loads(run.stdout.decode("utf-8") or "{}")
        except ValueError:
            answer = {"ok": False, "error": run.stderr.decode("utf-8", "replace")[-300:]}
        page = (answer.get("pages") or [{}])[0]
        results.append({"file": item["file"], "seconds": round(time.perf_counter() - started, 2),
                        "cpu_seconds": round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 2),
                        "text": page.get("text", ""), "layout": page.get("layout", ""), "angle": page.get("angle", 0),
                        "error": None if answer.get("ok") else answer.get("error") or "нет ответа"})
        peak = max(peak, int(answer.get("peak_mb") or 0))
    return {"load_seconds": 0.0, "peak_mb": peak, "files": results}


def warmup(name: str, corpus: Path) -> None:
    """Первый запуск: движок скачивает веса в кэш Hugging Face, замер потом их не ждёт."""
    started = time.perf_counter()
    FACTORIES[name](1)(corpus / "warmup.png", "receipt")
    print(f"{name}: готов за {time.perf_counter() - started:.1f} с")


def run_engine(name: str, corpus: Path, out: Path, threads: int) -> None:
    files = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))["files"]
    if name == "tesseract":
        result = _run_worker(corpus, files, threads)
    elif name == "worker_second":
        sys.path.insert(0, str(ROOT))
        from app.primary import ocr
        if not ocr.second_status()["ready"]:
            raise SystemExit(f"второе прочтение не готово: {ocr.second_status()['reason']}")
        result = _run_worker(corpus, files, threads, str(ocr.SECOND_DIR))
    else:
        started = time.perf_counter()
        run = FACTORIES[name](threads)
        run(corpus / "warmup.png", "receipt")          # ленивые модели загружаются здесь, вне замера
        load = time.perf_counter() - started
        results = []
        for item in files:
            started, cpu = time.perf_counter(), time.process_time()
            try:
                page, error = run(corpus / item["file"], item["kind"]), None
            except Exception as exc:                    # noqa: BLE001 — сбой движка → строка отчёта
                page, error = {"text": "", "lines": []}, f"{type(exc).__name__}: {exc}"[:400]
            results.append({"file": item["file"], "seconds": round(time.perf_counter() - started, 2),
                            "cpu_seconds": round(time.process_time() - cpu, 2), "error": error, **page})
        result = {"load_seconds": round(load, 2), "files": results,
                  "peak_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}
    result.update({"engine": name, "threads": threads, "versions": _versions(name),
                   "python": sys.version.split()[0], "det_side": DET_SIDE if name.startswith(("rapid", "tess_det")) else None})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"{name} t{threads}: загрузка {result['load_seconds']} с, пик {result['peak_mb']} МБ, "
          + ", ".join(f"{f['file']} {f['seconds']} с" for f in result["files"]))


def _du(path: Path) -> int:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    return round(total / 2 ** 20)


def sizes(out: Path, venvs: list[str]) -> None:
    data: dict = {"venvs": {}, "models": {}}
    for spec in venvs:
        name, folder = spec.split("=", 1)
        site = next(Path(folder).glob("lib/python*/site-packages"), None)
        if site is None:
            continue
        top = sorted(((_du(p), p.name) for p in site.iterdir() if p.is_dir()), reverse=True)[:8]
        data["venvs"][name] = {"total_mb": _du(site), "top": top}
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    for folder in sorted(hub.glob("models--*")):
        data["models"][folder.name.removeprefix("models--").replace("--", "/")] = _du(folder)
    for name in HF_REC:
        if (MODELS / name).is_dir():
            data["models"][name] = _du(MODELS / name)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))


# ======================================================================= оценка
TOKEN = re.compile(r"[0-9a-zа-я]+(?:[.,][0-9]+)*")


def _tokens(text: str) -> Counter:
    return Counter(t for t in TOKEN.findall(text.lower().replace("ё", "е")) if len(t) >= 2)


def _recall(truth: Counter, text: str) -> float:
    got, total = _tokens(text), sum(truth.values())
    return sum(min(n, got[t]) for t, n in truth.items()) / total if total else 0.0


def _digits(text: str) -> str:
    """Числа без пробелов внутри и с точкой: «1 258,90» → «1258.90»."""
    return re.sub(r"(?<=\d)\s+(?=\d)", "", text.replace(",", "."))


def _value(doc: dict, key: str):
    if key in ("number", "date", "totals", "lines"):
        return doc.get(key)
    role, field = key.split("_", 1)
    return (doc.get(role) or {}).get(field)


def _line_view(line: dict) -> tuple:
    return tuple(line.get(k) for k in ("position", "item_code", "name", "unit_code", "unit_name", "quantity",
                                         "price", "sum_without_vat", "vat_rate", "vat_sum", "sum_with_vat"))


def _upd_score(doc: dict | None, truth: dict) -> dict:
    """Поля из 10; тихая ошибка — неверное значение без пометки «проверьте» (бухгалтер её не увидит)."""
    if doc is None:
        return {"fields": 0, "lines_exact": 0, "names": 0, "silent": [], "missed": list(FIELDS)}
    good, silent, missed = 0, [], []
    flagged = set(doc.get("uncertain") or [])
    for key in FIELDS:
        got, want = _value(doc, key), _value(truth, key)
        if key == "lines":
            got, want = [ln.get("sum_with_vat") for ln in got or []], [ln.get("sum_with_vat") for ln in want]
        if got == want:
            good += 1
        elif not got or (key == "totals" and not any(got.values())):
            missed.append(key)
        elif key not in flagged:
            silent.append(key)
    pairs = list(zip(doc.get("lines") or [], truth["lines"]))
    return {"fields": good, "lines_exact": sum(_line_view(a) == _line_view(b) for a, b in pairs),
            "names": sum(a.get("name") == b.get("name") for a, b in pairs), "silent": silent, "missed": missed}


def _pair_slope(lines: list) -> float:
    """Наклон листа по соседним рамкам одной строки (у прямоугольных рамок Indra кромки наклона не знают):
    медиана dy/dx между рамкой и ближайшей соседкой справа той же высоты."""
    boxes = sorted((it["x0"], it["y0"], it["x1"], it["y1"]) for it in lines)
    slopes = []
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        for a0, b0, a1, b1 in boxes[i + 1:]:
            if a0 - x1 > 600:
                break
            h, hh = y1 - y0, b1 - b0
            if a0 < x1 or abs(hh - h) > 0.5 * max(h, hh) or min(y1, b1) - max(y0, b0) < 0.3 * min(h, hh):
                continue
            dx = (a0 + a1 - x0 - x1) / 2
            if dx > 150:
                slopes.append(((b0 + b1) - (y0 + y1)) / 2 / dx)
                break
    return statistics.median(slopes) if len(slopes) >= 5 else 0.0


def _layout(lines: list, worker) -> str:
    tops = [tuple(it["top"]) for it in lines if it.get("top")]
    slope = worker.median_slope(tops) if tops else 0.0
    if abs(slope) < 1e-4:
        slope = _pair_slope(lines)
    return worker.build_layout([{k: it[k] for k in ("t", "x0", "y0", "x1", "y1")} for it in lines], slope)


COMBOS = (("tesseract", 1, "worker_second", 1), ("tesseract", 1, "rapid_v6t", 1), ("tesseract", 1, "rapid_v6t_fast", 1),
          ("tesseract", 1, "indra", 1), ("tesseract", 1, "occular", 1), ("tesseract", 1, "rapid_v6m", 2))


def _reading(page: dict, worker) -> list[tuple[str, str]]:
    """Прочтение страницы в виде вариантов для parse_variants — как в app/primary/service.py."""
    text = page.get("text") or ""
    layout = page["layout"] if "layout" in page else _layout(page.get("lines") or [], worker)
    return [(text, layout), (layout, layout), (text, "")]


def _md(rows: list[list]) -> str:
    head, *body = rows
    out = ["| " + " | ".join(map(str, head)) + " |", "|" + "---|" * len(head)]
    return "\n".join(out + ["| " + " | ".join(map(str, r)) + " |" for r in body])


def score(corpus: Path, results_dir: Path, summary: Path | None) -> None:
    sys.path.insert(0, str(ROOT))
    from app.primary import print_form
    worker = _worker()
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    kinds = {f["file"]: f for f in manifest["files"]}
    plain, layout, _ = print_form.pdf_text(FIXTURE.read_bytes())
    truth = print_form.parse_variants([(plain, layout), (plain, "")])
    upd_words, receipt_words = _tokens(plain), _tokens("\n".join(RECEIPT_LINES))
    size_file = results_dir / "sizes.json"
    size_info = json.loads(size_file.read_text(encoding="utf-8")) if size_file.exists() else {}
    runs = []
    for path in sorted(results_dir.glob("*-t*.json")):
        runs.append(json.loads(path.read_text(encoding="utf-8")))
    runs.sort(key=lambda r: (ENGINES.index(r["engine"]) if r["engine"] in ENGINES else 99, r["threads"]))
    table = [["Движок", "Потоки", "Поля УПД (из 60)", "Строки целиком (из 18)", "Названия (из 18)",
              "Тихие ошибки", "Слова УПД", "Чек: поля (из 6)", "Чек: слова", "Загрузка, с", "Страница, с",
              "Процессор на стр., с", "Пик памяти, МБ", "Сбои"]]
    detail = [["Вариант"] + [f"{r['engine']} t{r['threads']}" for r in runs]]
    cells: dict[str, list] = {f["file"]: [f["title"]] for f in manifest["files"]}
    notes: dict[str, list[str]] = {}
    for run in runs:
        total = {"fields": 0, "lines_exact": 0, "names": 0, "silent": 0}
        recalls, receipt_fields, receipt_recall, seconds, cpu, errors = [], 0, 0.0, [], [], 0
        for page in run["files"]:
            item = kinds[page["file"]]
            text = page.get("text") or ""
            variants = _reading(page, worker)
            seconds.append(page["seconds"])
            cpu.append(page.get("cpu_seconds") or 0)
            errors += bool(page.get("error"))
            if item["kind"] == "receipt":
                found = [k for k, v in RECEIPT_FIELDS.items() if _digits(v) in _digits(text)]
                receipt_fields, receipt_recall = len(found), _recall(receipt_words, text)
                cells[page["file"]].append(f"{len(found)}/6 · {receipt_recall:.0%}")
                continue
            try:
                doc = print_form.parse_variants(variants)
            except print_form.PrintFormError:
                doc = None
            got = _upd_score(doc, truth)
            for key in total:
                total[key] += len(got[key]) if key == "silent" else got[key]
            recalls.append(_recall(upd_words, text))
            mark = f" ⚠{len(got['silent'])}" if got["silent"] else ""
            cells[page["file"]].append(f"{got['fields']}/10{mark} · {recalls[-1]:.0%} · {page['seconds']} с")
        row = [run["engine"], run["threads"], total["fields"], total["lines_exact"], total["names"], total["silent"],
               f"{statistics.mean(recalls):.0%}" if recalls else "—", receipt_fields, f"{receipt_recall:.0%}",
               run["load_seconds"], statistics.median(seconds) if seconds else "—",
               statistics.median(cpu) if cpu else "—", run["peak_mb"], errors]
        table.append(row)
        notes.setdefault(run["engine"], []).append(
            f"t{run['threads']}: " + "; ".join(f"{h}={v}" for h, v in zip(table[0][2:], row[2:])))
        notes[run["engine"]][0] += "" if len(notes[run["engine"]]) > 1 else f"; версии={run.get('versions')}"
    for file in cells:
        detail.append(cells[file])
    by_key = {(r["engine"], r["threads"]): r for r in runs}
    combo = [["Связка", "Поля УПД (из 60)", "Строки целиком (из 18)", "Названия (из 18)", "Тихие ошибки",
              "Время на стр., с (оба движка)", "Пик памяти, МБ (больший)"]]
    for first, t1, second, t2 in COMBOS:
        a, b = by_key.get((first, t1)), by_key.get((second, t2))
        if a is None or b is None:
            continue
        pages_b = {f["file"]: f for f in b["files"]}
        total = {"fields": 0, "lines_exact": 0, "names": 0, "silent": 0}
        times = []
        for page in a["files"]:
            other = pages_b.get(page["file"])
            if kinds[page["file"]]["kind"] != "upd" or other is None:
                continue
            try:
                doc = print_form.parse_variants(_reading(page, worker) + _reading(other, worker))
            except print_form.PrintFormError:
                doc = None
            got = _upd_score(doc, truth)
            for key in total:
                total[key] += len(got[key]) if key == "silent" else got[key]
            times.append(page["seconds"] + other["seconds"])
        combo.append([f"{first} t{t1} + {second} t{t2}", total["fields"], total["lines_exact"], total["names"],
                      total["silent"], round(statistics.median(times), 2) if times else "—", max(a["peak_mb"], b["peak_mb"])])
        notes.setdefault("связки", []).append(" · ".join(map(str, combo[-1])))
    parts = ["## Замер распознавания (T-77): синтетика, 2 ядра", "",
             "Поля — черновик УПД после разбора print_form, как его увидит бухгалтер. «Тихая ошибка» — "
             "неверное значение без пометки «проверьте». Слова — доля слов эталона, найденных в тексте.", "",
             _md(table), "", "### По вариантам: поля из 10 · слова · время", "", _md(detail), "",
             "### Второе прочтение: Tesseract и ещё один движок, поля сводит parse_variants", "", _md(combo)]
    if size_info:
        parts += ["", "### Размеры", "", "```json", json.dumps(size_info, ensure_ascii=False, indent=1), "```"]
    text = "\n".join(parts) + "\n"
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write(text)
    (results_dir / "report.md").write_text(text, encoding="utf-8")
    print(text)
    engines = [f"::notice title=OCR {name}::" + " || ".join(lines) for name, lines in notes.items()]
    variants = [f"::notice title=OCR вариант {file}::" + " | ".join(map(str, row)) for file, row in zip(cells, detail[1:])]
    (results_dir / "notes_engines.txt").write_text("\n".join(engines) + "\n", encoding="utf-8")
    header = "::notice title=OCR колонки вариантов::" + " | ".join(detail[0])
    (results_dir / "notes_variants.txt").write_text("\n".join([header, *variants]) + "\n", encoding="utf-8")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("corpus").add_argument("dir", type=Path)
    sub.add_parser("fetch").add_argument("dir", type=Path)
    warm = sub.add_parser("warmup")
    warm.add_argument("name", choices=tuple(FACTORIES))
    warm.add_argument("dir", type=Path)
    eng = sub.add_parser("engine")
    eng.add_argument("name", choices=ENGINES)
    eng.add_argument("dir", type=Path)
    eng.add_argument("out", type=Path)
    eng.add_argument("--threads", type=int, default=1)
    siz = sub.add_parser("sizes")
    siz.add_argument("out", type=Path)
    siz.add_argument("venvs", nargs="*")
    sco = sub.add_parser("score")
    sco.add_argument("dir", type=Path)
    sco.add_argument("results", type=Path)
    sco.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "corpus":
        make_corpus(args.dir)
    elif args.cmd == "fetch":
        fetch(args.dir)
    elif args.cmd == "warmup":
        warmup(args.name, args.dir)
    elif args.cmd == "engine":
        run_engine(args.name, args.dir, args.out, args.threads)
    elif args.cmd == "sizes":
        sizes(args.out, args.venvs)
    else:
        score(args.dir, args.results, args.summary)


if __name__ == "__main__":
    main(sys.argv[1:])
