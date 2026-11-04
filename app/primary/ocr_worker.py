"""Распознавание скана или фото в отдельном процессе (T-61, v1.70.95).

Запускается из app/primary/ocr.py так: python -I ocr_worker.py <файл> <tessdata> <языки> <страниц>
<1|0 превью>. Приложение не импортирует: в памяти процесса только Pillow, pypdfium2 и Tesseract.
После ответа процесс завершается, и вся его память возвращается системе. Падение библиотеки
не роняет сервер, а зависание обрывается тайм-аутом родителя.

Ответ — одна строка JSON в stdout:
  {"ok": true, "engine": "...", "pages": [{"text", "layout", "confidence", "angle"}],
   "preview": "<JPEG base64>" | null, "seconds": 1.6, "peak_mb": 89}
  {"ok": false, "error": "понятная причина"}

Строки собираются по координатам слов. Tesseract в режиме автоматической разметки разносит
колонку подписей («Продавец:») и колонку значений в разные блоки. Здесь слова снова собираются
в строки по высоте с поправкой на наклон листа. Широкий промежуток между словами становится
двумя пробелами — та же раскладка колонок, что у текста PDF (print_form это понимает).
"""
from __future__ import annotations

import base64
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import warnings

MAX_SIDE = 3508            # длинная сторона A4 при 300 точках на дюйм: больше Tesseract не нужно
MIN_SIDE = 1000            # совсем мелкую картинку увеличиваем: Tesseract нужно 20–30 точек на букву
RENDER_DPI = 200
MAX_PIXELS = 60_000_000    # защита от «бомбы распаковки»: картинка 60 Мп и больше — отказ
# Опорные слова печатной формы. На правильно повёрнутом листе их больше десяти, на перевёрнутом —
# ноль-одно. Уверенность Tesseract тут обманчива: на повёрнутом листе он уверенно читает «зеркальные» буквы.
ANCHORS = ("фактур", "продав", "покупат", "инн", "кпп", "всего", "оплат", "статус", "документ",
           "передаточн", "адрес", "валют", "наименован", "количеств", "стоимост", "налог", "ставк")
MIN_ANCHORS = 4


class WorkerError(Exception):
    """Причина отказа для пользователя."""


# ------------------------------------------------------------------ вход → картинки
def _pdf_pages(path: str, max_pages: int) -> list:
    from PIL import Image
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        try:
            pdf = pdfium.PdfDocument(path)
        except Exception as exc:                        # noqa: BLE001 — pdfium бросает свои ошибки
            raise WorkerError("PDF не открывается: файл повреждён или защищён паролем") from exc
        try:
            pages = []
            for index in range(min(len(pdf), max_pages)):
                page = pdf[index]
                pages.append(page.render(scale=RENDER_DPI / 72, grayscale=True).to_pil())
                page.close()
            return pages
        finally:
            pdf.close()
    # запасной путь без pypdfium2: берём картинки страниц скана
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = []
    for page in list(reader.pages)[:max_pages]:
        images = sorted(page.images, key=lambda im: len(im.data), reverse=True)
        if images:
            pages.append(Image.open(io.BytesIO(images[0].data)))
    if not pages:
        raise WorkerError("В PDF не нашлось изображений страниц")
    return pages


def _image_pages(path: str, max_pages: int) -> list:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            img = Image.open(path)
            frames = []
            for index in range(min(getattr(img, "n_frames", 1), max_pages)):
                img.seek(index)
                frames.append(ImageOps.exif_transpose(img.copy()))
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise WorkerError("Картинка слишком большая — уменьшите её до 60 мегапикселей") from exc
        except Exception as exc:                        # noqa: BLE001 — Pillow бросает разное
            raise WorkerError("Картинка не открывается: поддерживаются JPG, PNG и TIFF") from exc
    return frames


def prepare(img):
    """Серый и разумного размера. Контраст и фильтры не трогаем: Tesseract сам выбирает порог
    (метод Оцу), а растяжение контраста на шумном фото теряло строки — проверено замером."""
    img = img.convert("L")
    long_side = max(img.size)
    if long_side > MAX_SIDE:
        ratio = MAX_SIDE / long_side
        img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
    elif long_side < MIN_SIDE:
        ratio = min(2.0, MIN_SIDE / max(1, long_side))
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    return img


# ------------------------------------------------------------------ слова → строки
def build_layout(words: list[dict], slope: float = 0.0) -> str:
    """Слова с рамками → строки текста; широкий промежуток → два пробела (граница колонки).

    words: [{"t": текст, "x0", "y0", "x1", "y1"}]; slope — наклон строк (dy/dx) по базовым линиям.
    Высота слова пересчитывается в «выпрямленную»: y − slope·x, тогда наклонённая строка
    остаётся одной строкой и на ширине всего листа."""
    items = [w for w in words if str(w.get("t", "")).strip()]
    if not items:
        return ""
    heights = sorted(max(1, w["y1"] - w["y0"]) for w in items)
    line_h = heights[len(heights) // 2]
    for w in items:
        w["cy"] = (w["y0"] + w["y1"]) / 2 - slope * (w["x0"] + w["x1"]) / 2
    items.sort(key=lambda w: w["cy"])
    rows: list[list[dict]] = []
    for w in items:
        if rows and abs(w["cy"] - sum(x["cy"] for x in rows[-1]) / len(rows[-1])) <= line_h * 0.6:
            rows[-1].append(w)
        else:
            rows.append([w])
    out = []
    char_w = line_h * 0.55
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        text, prev = "", None
        for w in row:
            if prev is not None:
                text += "  " if w["x0"] - prev["x1"] > char_w * 1.6 else " "
            text += str(w["t"]).strip()
            prev = w
        out.append(text)
    return "\n".join(out)


def median_slope(lines: list[tuple[float, float, float, float]]) -> float:
    """Наклон листа: медиана наклонов длинных строк (x1, y1, x2, y2)."""
    slopes = sorted((y2 - y1) / (x2 - x1) for x1, y1, x2, y2 in lines if x2 - x1 > 150)
    return slopes[len(slopes) // 2] if slopes else 0.0


# ------------------------------------------------------------------ движки
def _ocr_tesserocr(img, tessdata: str, lang: str) -> dict:
    import tesserocr
    from tesserocr import PSM, RIL, iterate_level
    with tesserocr.PyTessBaseAPI(path=tessdata, lang=lang, psm=PSM.AUTO) as api:
        api.SetImage(img)
        api.Recognize()
        words, lines = [], []
        for item in iterate_level(api.GetIterator(), RIL.WORD):
            text, box = item.GetUTF8Text(RIL.WORD), item.BoundingBox(RIL.WORD)
            if text and box:
                words.append({"t": text, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]})
        for item in iterate_level(api.GetIterator(), RIL.TEXTLINE):
            base = item.Baseline(RIL.TEXTLINE)
            if base:
                (x1, y1), (x2, y2) = base
                lines.append((x1, y1, x2, y2))
        return {"text": api.GetUTF8Text(), "words": words, "slope": median_slope(lines),
                "confidence": int(api.MeanTextConf())}


def _ocr_cli(img, tessdata: str, lang: str) -> dict:
    """Запасной путь: системный tesseract (apt install tesseract-ocr tesseract-ocr-rus), вывод TSV."""
    exe = shutil.which("tesseract")
    if not exe:
        raise WorkerError("Tesseract не найден")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    cmd = [exe, "stdin", "stdout", "-l", lang, "--psm", "3"]
    if tessdata:
        cmd += ["--tessdata-dir", tessdata]
    run = subprocess.run(cmd + ["tsv"], input=buf.getvalue(), capture_output=True, timeout=120, check=False)
    if run.returncode != 0:
        raise WorkerError("Tesseract завершился с ошибкой: " + run.stderr.decode("utf-8", "replace")[-200:])
    words, by_line, confs = [], {}, []
    for row in run.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        x0, y0, w, h, conf = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), float(cols[10])
        word = {"t": cols[11], "x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h}
        words.append(word)
        by_line.setdefault(tuple(cols[2:5]), []).append(word)
        if conf >= 0:
            confs.append(conf)
    lines = [(ws[0]["x0"], (ws[0]["y0"] + ws[0]["y1"]) / 2, ws[-1]["x1"], (ws[-1]["y0"] + ws[-1]["y1"]) / 2)
             for ws in by_line.values() if len(ws) >= 3]
    text = "\n".join(" ".join(w["t"] for w in ws) for ws in by_line.values())
    return {"text": text, "words": words, "slope": median_slope(lines),
            "confidence": int(sum(confs) / len(confs)) if confs else 0}


def engine_name() -> tuple[str, object]:
    try:
        import tesserocr
        version = (tesserocr.tesseract_version().split() or ["", "?"])[1]
        return f"Tesseract {version} (tesserocr {tesserocr.__version__})", _ocr_tesserocr
    except ImportError:
        if shutil.which("tesseract"):
            return "Tesseract (системный)", _ocr_cli
    raise WorkerError("Не установлен модуль распознавания Tesseract")


def anchors(text: str) -> int:
    low = (text or "").lower()
    return sum(1 for word in ANCHORS if word in low)


def recognize_page(img, run, tessdata: str, lang: str) -> dict:
    """Страница → текст. Мало опорных слов — лист повёрнут: пробуем 90°, 270° и 180°."""
    result, angle = run(img, tessdata, lang), 0
    if anchors(result["text"]) < MIN_ANCHORS:
        for turn in (90, 270, 180):                    # PIL поворачивает против часовой стрелки
            other = run(img.rotate(turn, expand=True), tessdata, lang)
            if anchors(other["text"]) > anchors(result["text"]):
                result, angle = other, turn
            if anchors(result["text"]) >= 2 * MIN_ANCHORS:
                break
    return {"text": result["text"], "layout": build_layout(result["words"], result["slope"]),
            "confidence": result["confidence"], "angle": angle}


def _preview(img) -> str:
    small = img.copy()
    small.thumbnail((1400, 1400))
    buf = io.BytesIO()
    small.convert("L").save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main(argv: list[str]) -> dict:
    path, tessdata, lang, max_pages, want_preview = argv[0], argv[1], argv[2], int(argv[3]), argv[4] == "1"
    started = time.time()
    with open(path, "rb") as fh:
        head = fh.read(5)
    pages = _pdf_pages(path, max_pages) if head.startswith(b"%PDF") else _image_pages(path, max_pages)
    if not pages:
        raise WorkerError("В файле нет страниц")
    engine, run = engine_name()
    prepared = [prepare(p) for p in pages]
    result_pages = [recognize_page(img, run, tessdata, lang) for img in prepared]
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    return {"ok": True, "engine": engine, "pages": result_pages,
            "preview": _preview(prepared[0]) if want_preview else None,
            "seconds": round(time.time() - started, 2), "peak_mb": int(peak)}


if __name__ == "__main__":
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")       # один поток: второе ядро остаётся веб-серверу
    if hasattr(os, "nice"):
        try:
            os.nice(10)                                  # уступаем процессор приложению
        except OSError:
            sys.stderr.write("приоритет не понижен\n")
    try:
        answer = main(sys.argv[1:])
    except WorkerError as exc:
        answer = {"ok": False, "error": str(exc)}
    except Exception as exc:                            # noqa: BLE001 — любой сбой → понятный ответ
        answer = {"ok": False, "error": f"Сбой распознавания: {type(exc).__name__}"}
    sys.stdout.write(json.dumps(answer, ensure_ascii=False))
