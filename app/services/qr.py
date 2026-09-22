# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Разбор и валидация QR-кода кассового чека по стандарту 54-ФЗ.

Формат строки QR (типовой, ФФД 1.05/1.1/1.2):
    t=20250101T1200&s=1500.00&fn=9999078902001234&i=12345&fp=1234567890&n=1
    t — дата и время расчёта (ГГГГММДДТЧЧММ)
    s — сумма расчёта
    fn — заводской номер фискального накопителя (ФН)
    i  — порядковый номер фискального документа (ФД)
    fp — фискальный признак документа (ФП/ФПД), встречается также «fpd»
    n  — признак расчёта (1 приход, 2 возврат прихода, 3 расход, 4 возврат расхода)

Также поддерживаются расширенные варианты (ОФД добавляют свои теги):
    i=..., ifdg=..., nm=..., и URL-варианты (https://...?fn=..&i=..&fp=..).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from urllib.parse import urlparse, parse_qs


class QRParseError(ValueError):
    """Строка не является QR-кодом кассового чека."""


class MultipleReceiptsError(Exception):
    """На изображении найдено несколько разных чеков."""

    def __init__(self, parsed_list: list["ParsedQR"]):
        self.parsed = parsed_list
        super().__init__(f"Найдено несколько различных чеков: {len(parsed_list)}")


@dataclass
class ParsedQR:
    """Результат разбора QR-кода чека."""
    qr_data: str
    date_time: datetime | None = None
    total_sum: float = 0.0
    fn: str = ""
    fd: str = ""
    fp: str = ""
    operation: int = 1
    extra: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """Ключ дедупликации: ФН + ФД + ФП."""
        return f"{self.fn}|{self.fd}|{self.fp}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date_time"] = self.date_time.isoformat() if self.date_time else None
        d["dedup_key"] = self.dedup_key
        return d


# Теги QR-кода чека: канонические и встречающиеся алиасы
_TAG_ALIASES = {
    "t": "t", "dt": "t",
    "s": "s", "sum": "s",
    "fn": "fn", "f": "fn", "fnnumber": "fn",
    "i": "i", "fd": "i", "fdnumber": "i",
    "fp": "fp", "fpd": "fp", "fpdnumber": "fp",
    "n": "n", "op": "n", "operation": "n",
}

# Маски валидации
_FN_RE = re.compile(r"^\d{8,20}$")       # ФН — 8..20 цифр (типично 16)
_FD_RE = re.compile(r"^\d{1,10}$")       # ФД
_FP_RE = re.compile(r"^\d{1,10}$")       # ФП


def parse_qr(raw: str) -> ParsedQR:
    """
    Разбор строки QR-кода чека.

    Поддерживаются форматы:
      * key=value, разделённые & или ;  (стандарт 54-ФЗ)
      * URL с query-параметрами (некоторые ОФД печатают в QR ссылку)
    """
    raw = (raw or "").strip()
    if len(raw) < 10:
        raise QRParseError("Строка слишком короткая для QR-кода чека")

    tags: dict[str, str] = {}

    # Вариант URL: извлекаем query-параметры
    if raw.lower().startswith(("http://", "https://")):
        try:
            qs = parse_qs(urlparse(raw).query, keep_blank_values=True)
            for k, v in qs.items():
                if v:
                    tags[k.strip().lower()] = v[0].strip()
        except Exception:
            raise QRParseError("Не удалось разобрать URL из QR-кода")
    else:
        # Разделим по & и ; (некоторые кассы используют ;)
        parts = re.split(r"[&;\n]", raw)
        for part in parts:
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            canon = _TAG_ALIASES.get(key.strip().lower())
            if canon and canon not in tags:
                tags[canon] = value.strip()

    # Обязательные теги: fn, i, fp (s и t — опциональны, но желательны)
    if "fn" not in tags or "i" not in tags or "fp" not in tags:
        raise QRParseError(
            "В QR-коде нет обязательных реквизитов чека (fn, i/фд, fp/фпд)"
        )
    if not _FN_RE.match(tags["fn"]):
        raise QRParseError(f"Некорректный номер ФН: «{tags['fn']}»")
    if not _FD_RE.match(tags["i"]):
        raise QRParseError(f"Некорректный номер ФД: «{tags['i']}»")
    if not _FP_RE.match(tags["fp"]):
        raise QRParseError(f"Некорректный фискальный признак: «{tags['fp']}»")

    parsed = ParsedQR(qr_data=raw)
    parsed.fn = tags["fn"]
    parsed.fd = tags["i"]
    parsed.fp = tags["fp"]

    # Дата и время: t=ГГГГММДДTЧЧММ
    if "t" in tags:
        parsed.date_time = _parse_fns_datetime(tags["t"])

    # Сумма: допускаем запятую как десятичный разделитель
    if "s" in tags:
        try:
            parsed.total_sum = float(tags["s"].replace(",", ".").replace(" ", ""))
        except ValueError:
            raise QRParseError(f"Некорректная сумма: «{tags['s']}»")

    # Признак расчёта
    if "n" in tags:
        try:
            n = int(tags["n"])
            parsed.operation = n if n in (1, 2, 3, 4) else 1
        except ValueError:
            parsed.operation = 1

    # Прочие теги — сохраняем как есть (для маппинга и диагностики)
    known = {"t", "s", "fn", "i", "fp", "n"}
    parsed.extra = {k: v for k, v in tags.items() if k not in known}
    return parsed


def _parse_fns_datetime(value: str) -> datetime | None:
    """Парсинг даты из QR: ГГГГММДДTЧЧММ (ФНС), ДДММГГ ЧЧ:ММ (старый формат)."""
    value = value.strip()
    fmts = (
        ("%Y%m%dT%H%M%S", 17), ("%Y%m%dT%H%M", 15),
        ("%Y%m%dT%H%M", 13),   ("%Y%m%dT%H", 11),
    )
    for fmt, ln in fmts:
        if len(value) >= ln:
            try:
                return datetime.strptime(value[:ln], fmt)
            except ValueError:
                continue
    # Старый мобильный формат ДД.ММ.ГГГГ ЧЧ:ММ
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d%m%y %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def build_qr_string(date_time: datetime, total_sum: float, fn: str,
                    fd: str, fp: str, operation: int = 1) -> str:
    """Сборка канонической строки QR из реквизитов (для ручного ввода)."""
    t = date_time.strftime("%Y%m%dT%H%M")
    s = f"{total_sum:.2f}".rstrip("0").rstrip(".")
    return f"t={t}&s={s}&fn={fn}&i={fd}&fp={fp}&n={operation}"


def looks_like_receipt_qr(raw: str) -> bool:
    """Быстрая проверка «похоже на QR чека» (без исключений)."""
    try:
        parse_qr(raw)
        return True
    except QRParseError:
        return False


def receipts_json(parsed: ParsedQR, extra_payload: dict | None = None) -> str:
    """Сериализация разбора в JSON для поля raw_data."""
    payload = parsed.to_dict()
    if extra_payload:
        payload["fns"] = extra_payload
    return json.dumps(payload, ensure_ascii=False)
