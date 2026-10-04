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
    except Exception:                  # v1.15.0: даже текст ошибки не должен
        # содержать URL с ключом — отдаём только понятную формулировку
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
    card = _build_card(kind, data)
    if not card["name_full"]:
        raise CheckoError("Checko: компания найдена, но сервис не вернул наименование")
    return {"card": card, "raw": data}


def _build_card(kind: str, data: dict) -> dict:
    """Чистая функция: ответ Checko → сводка карточки (все ключевые реквизиты
    ЕГРЮЛ/ЕГРИП). Поля читаются защитно — структура ответа может расширяться."""
    if kind == "legal":
        comp = _first_dict(data, "Company")
        card = {
            "kind": "legal",
            "name_full": str(_first(comp, "name_full", "name", default="")),
            "name_short": str(_first(comp, "name_short",
                                     default=_first(comp, "name", default=""))),
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
        # --- v1.15.0: расширенные реквизиты ЕГРЮЛ ---
        card["opf"] = str(_deep(comp, "opf", "full",
                                default=_deep(comp, "opf", "name", default="")))
        card["reg_date"] = str(_first(comp, "ogrn_date", "registration_date",
                                      "reg_date", default="") or "")
        card["capital"] = _capital_text(comp)
        card["tax_office"] = str(_deep(comp, "tax_office", "name", default=""))
        card["tax_office_code"] = str(_deep(comp, "tax_office", "code", default=""))
        card["management_post"] = str(_deep(comp, "management", "post", default=""))
        card["okved_extra"] = _okved_extra(comp)
        card["email"] = str(_deep(comp, "email", "email",
                                  default=_first_list_item(comp, "Emails", "email")))
        card["phone"] = str(_first_list_item(comp, "Phones", "phone"))
    else:
        ip = _first_dict(data, "IndividualEntrepreneur", "Entrepreneur")
        card = {
            "kind": "individual",
            "name_full": str(_first(ip, "fio", "full_name", "name", default="")),
            "name_short": "",
            "inn": str(_first(ip, "inn", default="")),
            "kpp": "",
            "ogrn": str(_first(ip, "ogrn", "ogrnip", default="")),
            "address": str(_first(ip, "address", default="")),
            "director": "",
            "status": _status_text(_first(ip, "status", default="")),
            "okved": _okved_text(ip),
        }
        card["opf"] = "Индивидуальный предприниматель"
        card["reg_date"] = str(_first(ip, "ogrn_date", "registration_date",
                                      "reg_date", default="") or "")
        card["capital"] = ""
        card["tax_office"] = str(_deep(ip, "tax_office", "name", default=""))
        card["tax_office_code"] = str(_deep(ip, "tax_office", "code", default=""))
        card["management_post"] = ""
        card["okved_extra"] = _okved_extra(ip)
        card["email"] = ""
        card["phone"] = ""
    return card


def _capital_text(comp: dict) -> str:
    cap = _first_dict(comp, "capital", "Capital")
    s = _first(cap, "sum", "value", default=None)
    if s in (None, ""):
        return ""
    try:
        return f"{float(s):,.0f}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return str(s)


def _first_list_item(d: dict, list_key: str, field: str) -> str:
    lst = d.get(list_key)
    if isinstance(lst, list) and lst and isinstance(lst[0], dict):
        return str(lst[0].get(field) or "")
    return ""


def _okved_extra(d: dict) -> list[str]:
    ov = _first_dict(d, "Okveds")
    out = []
    for item in (ov.get("additional") or [])[:5]:
        if isinstance(item, dict) and item.get("code"):
            out.append(f"{item.get('code')} — {item.get('name', '')}".strip(" —"))
    return out


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
