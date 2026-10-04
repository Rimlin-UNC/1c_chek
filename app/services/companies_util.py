# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — утилиты компаний: канонизация названий, похожесть,
# валидация ИНН (v1.13.0 — защита от дублей «ООО Ямастер» / «ООО "ямастер"»
# / «ООО «ЯМАСТЕР»»).
#
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import difflib
import re
import unicodedata

# Кавычки/скобки/знаки, которые не влияют на «суть» названия
_NOISE = re.compile("[^0-9A-ZА-Я ]+")
_SPACES = re.compile(r"\s+")


def canonical_name(name: str) -> str:
    """Канонический ключ названия для сравнения на дубль.

    «ООО «Ямастер»», «ООО "ямастер"», «ООО «ЯМАСТЕР»», « ооо ямастер »
    → один и тот же ключ «ООО ЯМАСТЕР». Юридические формы не режем:
    «ООО Альфа» и «ИП Альфа» — РАЗНЫЕ компании."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).upper().replace("Ё", "Е")
    s = _NOISE.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def similar_ratio(a: str, b: str) -> float:
    """Похожесть двух названий (0..1) по каноническим ключам."""
    ca, cb = canonical_name(a), canonical_name(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


SIMILAR_THRESHOLD = 0.84      # порог «похожих» для подсказки в интерфейсе


def normalize_inn(inn: str) -> str:
    """Только цифры."""
    return re.sub(r"\D", "", str(inn or ""))


_INN10_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _inn_checksum(digits: str, weights: tuple) -> int:
    s = sum(int(d) * w for d, w in zip(digits, weights))
    return s % 11 % 10


def inn_is_valid(inn: str) -> bool:
    """ИНН юрлица (10 цифр) или ИП (12 цифр) с контрольными числами.
    Пустая строка — валидна (ИНН необязателен, но рекомендован)."""
    d = normalize_inn(inn)
    if not d:
        return True
    if not d.isdigit():
        return False
    if len(d) == 10:
        return _inn_checksum(d[:9], _INN10_WEIGHTS) == int(d[9])
    if len(d) == 12:
        c10 = _inn_checksum(d[:10], _INN12_W1) == int(d[10])
        c11 = _inn_checksum(d[:11], _INN12_W2) == int(d[11])
        return c10 and c11
    return False


def inn_kind(inn: str) -> str | None:
    """legal (10) | individual (12) | None."""
    d = normalize_inn(inn)
    if len(d) == 10:
        return "legal"
    if len(d) == 12:
        return "individual"
    return None
