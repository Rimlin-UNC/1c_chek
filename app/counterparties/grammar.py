"""Родительный падеж для реквизитов (T-80, ADR-48).

Правила детерминированные, без нейросетей: знакомые организационные формы
раскрываются по-русски, типичные ФИО склоняются простыми окончаниями.
Незнакомое название остаётся как есть — его правит бухгалтер вручную.
"""
from __future__ import annotations

import re

_FORMS = (
    (r"^ООО\b", "Общества с ограниченной ответственностью"),
    (r"^ПАО\b", "Публичного акционерного общества"),
    (r"^АО\b", "Акционерного общества"),
    (r"^ОАО\b", "Открытого акционерного общества"),
    (r"^ЗАО\b", "Закрытого акционерного общества"),
    (r"^ИП\b", "Индивидуального предпринимателя"),
)


def _genitive_word(word: str) -> str:
    low = word.lower()
    if low.endswith("ский") or low.endswith("цкий"):
        return word[:-2] + "ого"
    if low.endswith(("ов", "ев", "ин")):
        return word + "а"
    if low.endswith("ый"):
        return word[:-2] + "ого"
    if low.endswith("ий"):
        return word[:-2] + "ия"
    if low.endswith("й"):
        return word[:-1] + "я"
    if low.endswith("а") or low.endswith("я"):
        return word[:-1] + "ы"
    if low.endswith("ь"):
        return word[:-1] + "я"
    return word + "а"


def genitive_person(name: str) -> str:
    parts = (name or "").split()
    if len(parts) < 2:
        return name or ""
    return " ".join(_genitive_word(p) for p in parts)


def genitive_org(name: str) -> str:
    value = (name or "").strip()
    for pattern, replacement in _FORMS:
        m = re.match(pattern, value, re.IGNORECASE)
        if not m:
            continue
        rest = value[m.end():].strip()
        if rest.startswith(("«", "\"", "'")) and rest.endswith(("»", "\"", "'")):
            return f"{replacement} {rest}"
        if pattern == r"^ИП\b":
            return f"{replacement} {genitive_person(rest)}"
        if rest:
            return f"{replacement} {rest}"
        return replacement
    return value
