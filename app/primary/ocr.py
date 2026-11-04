"""Распознавание сканов и фото первички на своём сервере (T-61, v1.70.95): Tesseract 5, без облаков.

Почему так (замер на синтетическом УПД, 2 ядра; docs/primary_docs_design.md, ADR-25):
  • Tesseract 5 через колесо tesserocr — 1,5–2 с на страницу, пик памяти процесса 70–150 МБ.
    Помещается рядом с приложением на сервере 1 ГБ, если распознавать по одному документу.
  • Распознавание идёт в отдельном процессе (ocr_worker.py): память возвращается системе сразу,
    сбой библиотеки не роняет сервер, зависание обрывается тайм-аутом.
  • Одновременно — один документ (файловая блокировка data/ocr.lock), процесс с пониженным
    приоритетом и одним потоком OpenMP: веб-интерфейс не тормозит даже на двух ядрах.

Языковые модели (rus и eng из tesseract-ocr/tessdata_fast, Apache-2.0, около 8 МБ) скачивает
администратор кнопкой в «Первичке» или сервер находит системные (пакет tesseract-ocr-rus).
Каждый файл сверяется по SHA-1 git-объекта: подменённую модель сервер не примет.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..config import DATA_DIR

WORKER = Path(__file__).with_name("ocr_worker.py")
TESSDATA_DIR = DATA_DIR / "tessdata"
LOCK_FILE = DATA_DIR / "ocr.lock"
LAST_FILE = DATA_DIR / "ocr_last.json"
LANG = "rus+eng"
MAX_PAGES = 5
TIMEOUT = 150                 # секунд на весь файл: 5 страниц с поворотами укладываются с запасом
WAIT = 45                     # сколько ждать, пока распознаётся чужой документ
SYSTEM_TESSDATA = ("/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
                   "/usr/share/tessdata", "/usr/local/share/tessdata")

# tessdata_fast: SHA-1 git-объекта и размер (сверено при выборе 04.11.2026; docs/primary_docs_design.md)
MODELS = {
    "rus": {"sha1": "b146cb2263acbc6383f8e92ea0ce759537687bb8", "size": 3861738},
    "eng": {"sha1": "bbef4675053b5b468cdb477053e28b1c698ba08e", "size": 4113088},
}
SOURCES = (
    ("api.github.com", "https://api.github.com/repos/tesseract-ocr/tessdata_fast/git/blobs/{sha1}",
     {"Accept": "application/vnd.github.raw"}),
    ("raw.githubusercontent.com", "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{name}.traineddata",
     {}),
    ("github.com", "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{name}.traineddata", {}),
)

_thread_lock = threading.Lock()


class OcrError(Exception):
    """Распознавание не выполнено — сообщение для пользователя."""


class OcrBusy(OcrError):
    """Сервер распознаёт другой документ."""


class OcrUnavailable(OcrError):
    """Распознавание сканов не настроено на этом сервере."""


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


# ------------------------------------------------------------------ состояние
def _engine() -> str:
    """Чем распознавать, не загружая библиотеку в процесс приложения."""
    if importlib.util.find_spec("tesserocr") is not None:
        return "tesserocr"
    if shutil.which("tesseract"):
        return "cli"
    return ""


def _tessdata() -> tuple[str, list[str]]:
    """Каталог с моделями и недостающие языки. Сначала свои (data/tessdata), затем системные."""
    need = LANG.split("+")
    candidates = [str(TESSDATA_DIR)]
    if os.environ.get("TESSDATA_PREFIX"):
        candidates.append(os.environ["TESSDATA_PREFIX"])
    candidates += list(SYSTEM_TESSDATA)
    best, best_missing = str(TESSDATA_DIR), need
    for folder in candidates:
        missing = [lang for lang in need if not (Path(folder) / f"{lang}.traineddata").is_file()]
        if not missing:
            return folder, []
        if len(missing) < len(best_missing):
            best, best_missing = folder, missing
    return best, best_missing


def last_run() -> dict:
    try:
        with open(LAST_FILE, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def status() -> dict:
    engine = _engine()
    folder, missing = _tessdata()
    if not engine:
        reason = ("не установлен модуль tesserocr: он ставится вместе с обновлением программы "
                  "(или установите пакет tesseract-ocr)")
    elif missing:
        reason = f"нет языковых моделей: {', '.join(missing)} — администратору: кнопка «Скачать модели» (8 МБ)"
    else:
        reason = ""
    return {"ready": bool(engine) and not missing, "engine": engine, "languages": LANG.split("+"),
            "missing": missing, "tessdata": folder, "reason": reason, "last": last_run(),
            "max_pages": MAX_PAGES}


# ------------------------------------------------------------------ языковые модели
def _fetch(url: str, headers: dict) -> bytes:
    import httpx
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url, headers={"User-Agent": "ymaster-check", **headers})
        response.raise_for_status()
        return response.content


def download_models(fetch=None) -> dict:
    """Скачать недостающие модели в data/tessdata с проверкой хеша. Ответ — status() и журнал."""
    fetch = fetch or _fetch
    TESSDATA_DIR.mkdir(parents=True, exist_ok=True)
    log = []
    for name, meta in MODELS.items():
        target = TESSDATA_DIR / f"{name}.traineddata"
        if target.is_file() and target.stat().st_size == meta["size"]:
            log.append(f"{name}: уже есть")
            continue
        data, errors = None, []
        for label, template, headers in SOURCES:
            try:
                body = fetch(template.format(sha1=meta["sha1"], name=name), headers)
            except Exception as exc:                    # noqa: BLE001 — сеть: пробуем следующий источник
                errors.append(f"{label}: {type(exc).__name__}")
                continue
            if len(body) == meta["size"] and git_blob_sha1(body) == meta["sha1"]:
                data = body
                log.append(f"{name}: скачано с {label}")
                break
            errors.append(f"{label}: файл не совпал по хешу")
        if data is None:
            raise OcrError(f"Модель {name} не скачана ({'; '.join(errors)}). Проверьте доступ сервера к GitHub")
        fd, tmp = tempfile.mkstemp(dir=TESSDATA_DIR, prefix=f".{name}.")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    return {**status(), "log": log}


# ------------------------------------------------------------------ распознавание
@contextlib.contextmanager
def _one_at_a_time(wait: float):
    """Один документ за раз на весь сервер: файловая блокировка (и потоковая — где нет fcntl)."""
    try:
        import fcntl
    except ImportError:                                 # Windows: только внутри процесса
        if not _thread_lock.acquire(timeout=wait):
            raise OcrBusy("Сервер распознаёт другой документ — повторите через минуту") from None
        try:
            yield
        finally:
            _thread_lock.release()
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+") as fh:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OcrBusy("Сервер распознаёт другой документ — повторите через минуту") from None
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def recognize(data: bytes, suffix: str, *, preview: bool = False, wait: float = WAIT) -> dict:
    """Файл (скан PDF или картинка) → {engine, pages[{text, layout, confidence, angle}], preview, ...}."""
    state = status()
    if not state["ready"]:
        raise OcrUnavailable(f"Распознавание сканов не настроено: {state['reason']}")
    with _one_at_a_time(wait), tempfile.TemporaryDirectory(prefix="ymaster-ocr-") as tmp:
        path = Path(tmp) / f"input{suffix}"
        path.write_bytes(data)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OMP_THREAD_LIMIT": "1",
               "LANG": "C.UTF-8", "HOME": tmp}
        cmd = [sys.executable, "-I", str(WORKER), str(path), state["tessdata"], LANG, str(MAX_PAGES),
               "1" if preview else "0"]
        started = time.monotonic()
        try:
            # приоритет понижает сам обработчик: preexec_fn небезопасен в многопоточном сервере
            run = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, env=env, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OcrError(f"Распознавание не уложилось в {TIMEOUT} секунд — разбейте файл на части") from exc
    try:
        answer = json.loads(run.stdout.decode("utf-8") or "{}")
    except ValueError:
        answer = {}
    if not answer.get("ok"):
        raise OcrError(answer.get("error") or "Распознавание завершилось сбоем — попробуйте другой скан")
    stats = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": answer.get("seconds"),
             "wall_seconds": round(time.monotonic() - started, 2), "peak_mb": answer.get("peak_mb"),
             "pages": len(answer.get("pages") or []), "engine": answer.get("engine", "")}
    with contextlib.suppress(OSError):
        LAST_FILE.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    return {**answer, "stats": stats}
