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


# --------------------------------------------------------------------------
#  v1.16.0: светофор контрагента — риск-оценка по данным ЕГРЮЛ (Checko)
# --------------------------------------------------------------------------
def _age_days(reg_date: str) -> int | None:
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            d = _dt.datetime.strptime((reg_date or "").strip()[:10], fmt)
            return (_dt.datetime.utcnow() - d).days
        except ValueError:
            continue
    return None


def _capital_value(capital: str) -> float | None:
    """'50 000 ₽' → 50000.0."""
    import re as _re
    m = _re.search(r"([\d\s.,]+)", capital or "")
    if not m:
        return None
    num = m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def risk_assessment(card: dict | None, company_inn: str = "") -> dict:
    """Светофор: green/yellow/red + понятные причины. 'none' — данных нет."""
    if not card or not (card.get("name_full") or card.get("ogrn")):
        level = "yellow" if not (company_inn or "").strip() else "none"
        reasons = ["Данные ЕГРЮЛ не заполнены — нажмите «Обновить из Checko»"] \
            if level == "yellow" else []
        return {"level": level, "reasons": reasons}
    reasons = []
    status = (card.get("status") or "").lower()
    if any(x in status for x in ("ликвидирован", "банкрот", "прекратил")):
        return {"level": "red",
                "reasons": [f"Статус в ЕГРЮЛ: {card.get('status')} — расходы по "
                            "чекам такого контрагента под риском снятия"]}
    if any(x in status for x in ("ликвидир", "реорганиз")):
        reasons.append(f"Статус «{card.get('status')}» — идёт реорганизация/ликвидация")
    age = _age_days(card.get("reg_date") or "")
    if age is not None and age < 180:
        reasons.append(f"Молодая компания ({age // 30} мес. с регистрации)")
    cap = _capital_value(card.get("capital") or "")
    if cap is not None and cap < 10000 and card.get("kind") != "individual":
        reasons.append("Уставный капитал меньше 10 000 ₽")
    if not (company_inn or "").strip():
        reasons.append("ИНН не указан — риск-оценка неполная")
    level = "red" if any("Статус" in r for r in reasons) else (
        "yellow" if reasons else "green")
    if level == "green":
        reasons = ["Реквизиты ЕГРЮЛ в норме"]
    return {"level": level, "reasons": reasons}
