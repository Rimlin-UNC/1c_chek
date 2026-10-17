# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — PDF-чеки (v1.65.0, Фаза 1 концепции, п.5).
# Электронные чеки приходят письмом/из банка как ТЕКСТОВЫЙ PDF. Отсюда
# достаём фискальную QR-строку (t=…&s=…&fn=…&i=…&fp=…) из текстового
# слоя — без OCR (анти-рекомендация концепции: OCR на этом сервере не
# делаем). Найденные строки идут в ТОТ ЖЕ конвейер приёма, что скан и
# фото: дедуп по ФН+ФД+ФП, авто-проверки, правила данных v1.61.
# Лимиты: PDF_MAX_MB (загрузка, в роуте) и MAX_PDF_PAGES (страниц).
# Пайплайн ПЕЧАТИ чеков (printpack, v1.24+) эта логика не трогает.
# ======================================================================
from __future__ import annotations

import re
import io

# Лимит страниц PDF (атака «десятитысячностраничный файл» → CPU/RAM)
MAX_PDF_PAGES = 50

# Фискальная строка: t=…&s=…&fn=…&i=…&fp=… (обязательны fn, i, fp —
# как в клиентском разборе QR; scan-page/sanner.js не трогаем)
_QR_RE = re.compile(r"t=\d{8}T\d{4}&s=[0-9]+(?:\.[0-9]{1,2})?"
                    r"&fn=\d{4,20}&i=\d{1,10}&fp=\d{1,10}(?:&n=[12])?",
                    re.IGNORECASE)


def extract_qr_texts(raw: bytes) -> list[str]:
    """QR-строки из текстового слоя PDF (уникальные, в порядке появления).

    Ошибки — ЧЕСТНЫЕ ValueError с человеческой причиной:
      • нет текстового слоя (скан/фото в PDF) → «пришлите фото или QR»;
      • текст есть, QR-строки не найдены → показать реквизиты вручную;
      • файл не читается как PDF.
    """
    if not raw.startswith(b"%PDF"):
        raise ValueError("Это не PDF-файл — нужен PDF-чек из письма или банка")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
    except Exception as e:                              # noqa: BLE001
        raise ValueError(f"PDF не читается: файл повреждён?") from e
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"В PDF больше {MAX_PDF_PAGES} страниц — "
                         f"разбейте файл на части")
    chunks: list[str] = []
    try:
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:                           # noqa: BLE001
                text = ""                               # страница-картинка
            chunks.append(text)
    except Exception as e:                              # noqa: BLE001
        raise ValueError("PDF не читается: файл повреждён?") from e
    if not any(c.strip() for c in chunks):
        raise ValueError("В PDF нет текстового слоя (это скан/фото) — "
                         "пришлите фото чека или отсканируйте QR камерой")
    found: list[str] = []
    for text in chunks:
        for m in _QR_RE.finditer(text or ""):
            s = m.group(0)
            if s not in found:
                found.append(s)
    if not found:
        raise ValueError("В PDF есть текст, но фискальный QR-код (t=…&s=…&fn=…) "
                         "не найден — введите реквизиты вручную или "
                         "пришлите фото чека")
    return found
