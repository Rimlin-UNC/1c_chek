# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — справочники «Чек-Пула»: регионы, города, отрасли, сети
# (v1.33.0, Этап 4). Всё ОФЛАЙН (data/*.json) — без внешних запросов.
# Гео: адрес места расчёта → регион/город (geo_accuracy="address");
# fallback ИНН → Checko (юр. адрес) → регион (geo_accuracy="inn");
# ручная разметка модератором (geo_accuracy="manual").
# Отрасль: сеть из словаря → отрасль; иначе ключевые слова позиций,
# доминирующие по сумме. «Не запрет, а разметка»: не определили —
# поле пусто, чек остаётся в пуле и попадает в панель «без гео/отрасли».
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=None)
def _load(name: str):
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def regions() -> list[dict]:
    """[{code, name, district}] — 89 субъектов РФ."""
    return _load("regions")


def industries() -> list[dict]:
    """[{code, name, kw}] — отрасли с ключевыми словами позиций."""
    return _load("industries")


def region_name(code: str) -> str:
    for r in regions():
        if r["code"] == str(code):
            return r["name"]
    return ""


def industry_name(code: str) -> str:
    for i in industries():
        if i["code"] == code:
            return i["name"]
    return ""


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return f" {s} "


# --- гео ---------------------------------------------------------------------
def _region_aliases():
    """[(алиас, код)] — полные и краткие имена, сначала самые длинные."""
    out = []
    for r in regions():
        n = r["name"].lower().replace("ё", "е")
        out.append((n, r["code"]))
        for tail in ("республика ", "автономный округ ", "автономная область ",
                     "край", "область", "город федерального значения"):
            n2 = n.replace(tail, " ").strip()
            if n2 and len(n2) > 4:
                out.append((n2, r["code"]))
    # особые случаи
    extra = {"санкт петербург": "78", "спб": "78", "питер": "78",
             "москва": "77", "г москва": "77",
             "ленинградская": "47", "московская": "50",
             "донецкая народная": "80", "луганская народная": "81",
             "днр": "80", "лнр": "81", "крым": "91", "югра": "86",
             "якутия": "14", "северная осетия": "15", "марий эл": "12"}
    out.extend(extra.items())
    out.sort(key=lambda x: -len(x[0]))
    return out


def _cities_by_region():
    return [(c, str(code)) for c, code in _load("cities").items()]


def _extract_city(address: str) -> str:
    m = re.search(r"\bг(?:ор|ород)?\s+([а-яa-z][а-яa-z\-]{2,30})", address)
    return m.group(1).capitalize() if m else ""


def resolve_region(address: str) -> tuple[str, str, str]:
    """Адрес → (код региона, город, точность). Не нашли — ('', '', '').
    «ул. Тверская» не должна давать Тверскую область: сначала ищем в части
    адреса ДО уличных ключевых слов, затем города по всей строке."""
    a = _norm(address)
    if not a.strip():
        return "", "", ""
    # часть адреса до улицы: регион и город почти всегда там
    prefix = re.split(
        r"\s(?:ул|улица|проспект|пр|пр-кт|пр-т|шоссе|ш|наб|бул|пер|мкр)\s", a)[0]
    for alias, code in _region_aliases():
        if _norm(alias) in prefix:
            return code, _extract_city(a), "address"
    for city, code in sorted(_cities_by_region(), key=lambda x: -len(x[0])):
        if _norm(city) in prefix:
            return code, _extract_city(a) or city.capitalize(), "address"
    # город может стоять после улицы
    for city, code in sorted(_cities_by_region(), key=lambda x: -len(x[0])):
        if _norm(city) in a:
            return code, _extract_city(a) or city.capitalize(), "address"
    # полное название региона (с суффиксом) по всей строке — надёжный случай
    for r in regions():
        if _norm(r["name"]) in a:
            return r["code"], _extract_city(a), "address"
    return "", "", ""


def resolve_region_by_inn(db, inn: str) -> tuple[str, str, str]:
    """Fallback: ИНН → юр. адрес (Checko, ключ из Настроек) → регион."""
    if not inn:
        return "", "", ""
    from ...services import appsettings, checko
    key = appsettings.get_setting(db, checko.SETTING_KEY, "")
    if not key:
        return "", "", ""
    try:
        card = checko.fetch_card(key, inn) or {}
        addr = str((card.get("card") or {}).get("address") or "")
        code, city, _ = resolve_region(addr)
        return (code, city, "inn") if code else ("", "", "")
    except Exception:                                     # noqa: BLE001
        return "", "", ""


# --- отрасли -------------------------------------------------------------------
def _chain_aliases():
    """[(алиас, отрасль, название сети)] — сначала самые длинные."""
    out = []
    for ch in _load("chains"):
        for a in ch["aliases"]:
            out.append((a.lower().replace("ё", "е"), ch["industry"], ch["name"]))
    out.sort(key=lambda x: -len(x[0]))
    return out


def match_industry(merchant_name: str, items) -> tuple[str, str]:
    """Отрасль чека: сеть → отрасль; иначе ключевые слова позиций,
    доминирующие по сумме. Возвращает (код отрасли, название сети)."""
    m = _norm(merchant_name)
    if m.strip():
        for alias, code, chain_name in _chain_aliases():
            if alias in m:
                return code, chain_name
    scores: dict[str, float] = {}
    for it in items or []:
        n = _norm(getattr(it, "name", ""))
        total = float(getattr(it, "total", 0) or 0)
        if not n.strip() or total <= 0:
            continue
        for ind in industries():
            if any(k in n for k in ind["kw"]):
                scores[ind["code"]] = scores.get(ind["code"], 0) + total
    if scores:
        return max(scores, key=scores.get), ""
    return "", ""


def normalize_item_name(name: str) -> str:
    """Нормализация имени позиции: пробелы, «Ё», КАПС-слова → Обычный вид
    (латиницу и артикулы не трогаем)."""
    s = re.sub(r"\s+", " ", (name or "").replace("Ё", "Е").replace("ё", "е")).strip()
    if len(s) > 6 and s.upper() == s:
        words = s.split(" ")
        fixed = []
        for w in words:
            alpha = [c for c in w if c.isalpha()]
            if len(alpha) > 2 and w.isupper() and all(ord(c) < 1200 for c in alpha):
                w = w.capitalize()
            fixed.append(w)
        s = " ".join(fixed)
    return s


# --- обогащение чека ------------------------------------------------------------
def enrich_receipt(db, receipt, items=None) -> None:
    """Заполняет region_code/city/geo_accuracy/industry (что смогли).
    Идемпотентно: существующие значения не перезатирает."""
    try:
        if not receipt.region_code:
            code = city = acc = ""
            if receipt.merchant_address:
                code, city, acc = resolve_region(receipt.merchant_address)
            if not code and receipt.merchant_inn:
                code, city, acc = resolve_region_by_inn(db, receipt.merchant_inn)
            if code:
                receipt.region_code = str(code)
                receipt.city = (city or "")[:128]
                receipt.geo_accuracy = acc
        if not receipt.industry:
            if items is None:
                from ..models import PoolItem
                items = (db.query(PoolItem)
                         .filter(PoolItem.receipt_id == receipt.id).all())
            ind, _net = match_industry(receipt.merchant_name, items)
            if ind:
                receipt.industry = ind
    except Exception:                                     # noqa: BLE001
        pass                                              # разметка не ломает приём
