# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — интеграция Checko.ru (v1.13.0): карточка компании по ИНН
# из ЕГРЮЛ/ЕГРИП. API: https://api.checko.ru/v2/company (юрлица, ИНН-10)
# и https://api.checko.ru/v2/entrepreneur (ИП, ИНН-12); ключ — из личного
# кабинета checko.ru (бесплатный тариф: 100 запросов/день).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import httpx

from .companies_util import inn_kind

BASE_URL = "https://api.checko.ru/v2"
TIMEOUT = 20.0

SETTING_KEY = "checko_api_key"


class CheckoError(Exception):
    """Понятная пользователю ошибка интеграции."""


def _deep(d: dict, *path, default=None):
    """Безопасное чтение вложенных полей."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def fetch_card(api_key: str, inn: str) -> dict:
    """Данные компании/ИП по ИНН → {card: сводка для UI, raw: ответ Checko}."""
    if not api_key:
        raise CheckoError("API-ключ Checko не задан — укажите его в карточке компании")
    kind = inn_kind(inn)
    if kind is None:
        raise CheckoError("ИНН должен содержать 10 цифр (ООО) или 12 цифр (ИП)")
    url = f"{BASE_URL}/company" if kind == "legal" else f"{BASE_URL}/entrepreneur"
    try:
        resp = httpx.get(url, params={"key": api_key, "inn": inn}, timeout=TIMEOUT)
    except httpx.HTTPError:
        raise CheckoError("Сервис Checko недоступен — попробуйте позже")
    if resp.status_code in (401, 403):
        raise CheckoError("Checko: ключ недействителен — проверьте API-ключ")
    if resp.status_code == 429:
        raise CheckoError("Checko: превышен лимит запросов (на бесплатном тарифе 100/день)")
    if resp.status_code == 404:
        raise CheckoError("Checko: компания с таким ИНН не найдена в ЕГРЮЛ/ЕГРИП")
    if resp.status_code >= 400:
        raise CheckoError(f"Checko: ошибка сервиса (HTTP {resp.status_code})")
    data = resp.json()

    if kind == "legal":
        comp = _first_dict(data, "Company")
        card = {
            "kind": "legal",
            "name_full": str(_first(comp, "name_full", "name", default="")),
            "inn": str(_first(comp, "inn", default="")),
            "kpp": str(_first(comp, "kpp", default="")),
            "ogrn": str(_first(comp, "ogrn", default="")),
            "address": str(_deep(comp, "address", "full_address",
                                 default=_first(comp, "address", default=""))),
            "director": str(_deep(comp, "management", "name",
                                  default=_first(comp, "director", default=""))),
            "status": _status_text(_first(comp, "status", default="")),
            "okved": _okved_text(comp),
        }
    else:
        ip = _first_dict(data, "IndividualEntrepreneur", "Entrepreneur")
        card = {
            "kind": "individual",
            "name_full": str(_first(ip, "fio", "full_name", "name", default="")),
            "inn": str(_first(ip, "inn", default="")),
            "kpp": "",
            "ogrn": str(_first(ip, "ogrn", "ogrnip", default="")),
            "address": str(_first(ip, "address", default="")),
            "director": "",
            "status": _status_text(_first(ip, "status", default="")),
            "okved": _okved_text(ip),
        }
    if not card["name_full"]:
        raise CheckoError("Checko: компания найдена, но сервис не вернул наименование")
    return {"card": card, "raw": data}


def _first_dict(d: dict, *keys) -> dict:
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, dict):
            return v
    return {}


def _status_text(s) -> str:
    m = {"active": "Действует", "liquidating": "Ликвидируется",
         "liquidated": "Ликвидирована", "reorganization": "Реорганизация",
         "bankruptcy": "Банкротство"}
    if isinstance(s, dict):
        s = _first(s, "code", default=s)
    return m.get(str(s).lower(), str(s) if s else "")


def _okved_text(d: dict) -> str:
    ov = _first_dict(d, "Okveds")
    main = _first_dict(ov, "main")
    if main:
        return f"{main.get('code', '')} — {main.get('name', '')}".strip(" —")
    return ""
