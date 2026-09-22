# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Модуль обработки изображений чеков: множественное чтение QR через OpenCV.

Пайплайн (по ТЗ):
  1. Предобработка: grayscale, адаптивная бинаризация, масштабирование.
  2. Множественное чтение QR (detectAndDecodeMulti) на нескольких вариантах.
  3. Валидация формата 54-ФЗ, отсев «мусорных» кодов.
  4. Если найдено несколько РАЗНЫХ чеков — MultipleReceiptsError
     (клиент предложит пользователю выбрать нужный).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .qr import MultipleReceiptsError, ParsedQR, QRParseError, parse_qr


@dataclass
class ImageScanResult:
    """Результат сканирования изображения."""
    parsed: ParsedQR | None
    all_valid: list[ParsedQR]
    total_codes: int          # сколько QR-кодов найдено вообще
    debug: list[str]          # служебные сообщения


def _variants(image: np.ndarray) -> list[np.ndarray]:
    """Генерация вариантов изображения для устойчивого распознавания.

    Стратегия: сначала быстрые варианты, затем «тяжёлые» (повороты,
    бинаризация с морфологией). Это даёт высокую устойчивость к наклону
    телефона, бликам и шуму камеры.
    """
    out: list[np.ndarray] = []

    # 0. Оригинал
    out.append(image)

    # 1. Оттенки серого
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    out.append(gray)

    # 2. Апскейл слабых по разрешению снимков
    h, w = gray.shape[:2]
    if h < 1000 or w < 1000:
        k = min(3.0, 1400.0 / max(h, w, 1))
        big = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
        out.append(big)

    # 3. Адаптивная бинаризация (блики, неравномерный свет)
    try:
        block = max(11, (min(h, w) // 14) | 1)
        binz = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, block, 11)
        out.append(binz)
    except cv2.error:
        pass

    # 4. Небольшие повороты (съёмка «от руки»)
    try:
        for angle in (-9, 9, -18, 18):
            M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            canvas = cv2.warpAffine(gray, M, (w, h),
                                    flags=cv2.INTER_CUBIC,
                                    borderMode=cv2.BORDER_CONSTANT,
                                    borderValue=255)
            out.append(canvas)
    except cv2.error:
        pass

    # 5. Медианный фильтр (шум) + резкость
    try:
        blur = cv2.medianBlur(gray, 3)
        out.append(blur)
        sharp = cv2.addWeighted(gray, 1.6, cv2.GaussianBlur(gray, (0, 0), 3), -0.6, 0)
        out.append(sharp)
    except cv2.error:
        pass

    # 6. «Тяжёлая артиллерия»: деноиз → Otsu → морфология (шумная камера)
    try:
        scale = min(1.0, 900.0 / max(h, w, 1))
        work = cv2.resize(gray, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA) if scale < 1 else gray
        den = cv2.fastNlMeansDenoising(work, None, 21, 7, 21)
        _, otsu = cv2.threshold(den, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(otsu, cv2.MORPH_OPEN, kernel)
        out.append(cleaned)
    except cv2.error:
        pass

    return out


def decode_qr_image(image_bytes: bytes) -> ImageScanResult:
    """
    Поиск QR-кодов чека на изображении (JPEG/PNG/WebP).

    Возвращает ImageScanResult. Если найдено несколько различных чеков —
    выбрасывает MultipleReceiptsError со списком разобранных чеков.
    """
    buf = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Не удалось прочитать изображение (поддерживаются JPEG/PNG/WebP)")

    detector = cv2.QRCodeDetector()
    decoded_raw: list[str] = []
    debug: list[str] = []

    def _try_multi(arr: np.ndarray) -> list[str]:
        found: list[str] = []
        try:
            ok, texts, _, _ = detector.detectAndDecodeMulti(arr)
            if ok:
                found = [t for t in texts if t]
        except cv2.error:
            pass
        if not found:
            try:
                single, _, _ = detector.detectAndDecode(arr)
                if single:
                    found = [single]
            except cv2.error:
                pass
        return found

    # Идём по вариантам, пока не найдём хотя бы один валидный QR чека
    parsed_list: list[ParsedQR] = []
    for idx, variant in enumerate(_variants(image)):
        found = _try_multi(variant)
        if found:
            debug.append(f"variant {idx}: {len(found)} code(s)")
            decoded_raw.extend(found)
            for raw in dict.fromkeys(found):
                try:
                    parsed_list.append(parse_qr(raw))
                except QRParseError:
                    debug.append(f"rejected: {raw[:40]}…")
            if parsed_list:
                break  # валидный чек найден — дальше не утяжеляем

    # Дедупликация строк
    unique_raw = list(dict.fromkeys(decoded_raw))

    if not parsed_list:
        if unique_raw:
            raise QRParseError(
                "QR-код найден, но не является чеком (нет реквизитов ФН/ФД/ФП)"
            )
        raise QRParseError(
            "QR-код чека на изображении не найден. "
            "Попробуйте лучше качество съёмки или введите реквизиты вручную."
        )

    # Разные чеки на одном изображении — просим пользователя выбрать
    unique_keys = {p.dedup_key for p in parsed_list}
    if len(unique_keys) > 1:
        # оставляем по одному представителю на каждый чек
        seen: set[str] = set()
        distinct: list[ParsedQR] = []
        for p in parsed_list:
            if p.dedup_key not in seen:
                seen.add(p.dedup_key)
                distinct.append(p)
        raise MultipleReceiptsError(distinct)

    return ImageScanResult(
        parsed=parsed_list[0],
        all_valid=parsed_list,
        total_codes=len(unique_raw),
        debug=debug,
    )
