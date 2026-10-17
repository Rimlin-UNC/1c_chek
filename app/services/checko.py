# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — интеграция Checko.ru (v1.16.1: переписано ПО ДОКУМЕНТАЦИИ).
#
# Документация: checko.ru/integration/api/company и /entrepreneur
#   • запрос: GET https://api.checko.ru/v2/company?key=API_KEY&inn=ИНН
#     (для ИП с ИНН-12 — https://api.checko.ru/v2/entrepreneur);
#     лимиты: 100 запросов/сутки бесплатно, техлимит 32 запроса/сек;
#   • ответ: { meta: {status: "ok"|"error", message?, today_request_count,
#     balance}, data: {...} } — КЛЮЧИ ВНУТРИ data — РУССКИЕ:
#     ЮЛ: ОГРН, ИНН, КПП, ОКПО, НаимПолн, НаимСокр, ДатаОГРН, ДатаРег,
#         Статус (строка или {Код,Наим}), Регион{Код,Наим},
#         ЮрАдрес{АдресРФ, Недост, НедостОпис, МассАдрес[]} или строка,
#         ОКВЭД{Код,Наим}, УпрОрг{НаимСокр,...}, Учред{ФЛ[],РосОрг[]},
#         Подразд{Филиал[],Представ[]};
#     ИП: ОГРНИП, ИНН, ФИО, Тип, ТипСокр, ДатаОГРНИП, ДатаРег,
#         Статус{Наим}, Регион, НасПункт, ОКВЭД{Код,Наим}.
#
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import re

import httpx

from .companies_util import inn_kind

BASE_URL = "https://api.checko.ru/v2"
TIMEOUT = 25.0

SETTING_KEY = "checko_api_key"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class CheckoError(Exception):
    """Понятная пользователю ошибка интеграции (без ключа и внутренностей)."""


# --------------------------------------------------------------------------
#  HTTP: запрос и разбор meta-обёртки
# --------------------------------------------------------------------------
def _request(api_key: str, inn: str) -> dict:
    """Запрос к API → распарсенный JSON. Ошибки — CheckoError с понятным
    текстом; ключ в сообщения никогда не попадает."""
    if not api_key:
        raise CheckoError("API-ключ Checko не задан — укажите его в настройках")
    kind = inn_kind(inn)
    if kind is None:
        raise CheckoError("ИНН должен содержать 10 цифр (ООО) или 12 цифр (ИП)")
    url = f"{BASE_URL}/company" if kind == "legal" else f"{BASE_URL}/entrepreneur"
    try:
        resp = httpx.get(url, params={"key": api_key, "inn": inn}, timeout=TIMEOUT)
    except Exception:                       # noqa: BLE001 — даже текст сетевой
        # ошибки не должен содержать URL с ключом
        raise CheckoError("Сервис Checko недоступен с сервера — попробуйте позже")
    if resp.status_code in (401, 403):
        raise CheckoError("Checko: ключ недействителен — проверьте API-ключ в "
                          "личном кабинете checko.ru → API")
    if resp.status_code == 429:
        raise CheckoError("Checko: слишком часто (техлимит 32 запроса/сек) "
                          "или исчерпан дневной лимит — повторите позже")
    if resp.status_code == 404:
        raise CheckoError("Checko: компания с таким ИНН не найдена в ЕГРЮЛ/ЕГРИП")
    if resp.status_code >= 400:
        raise CheckoError(f"Checko: ошибка сервиса (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except Exception:                       # noqa: BLE001
        raise CheckoError("Checko: сервер вернул не JSON — попробуйте позже")
    if not isinstance(data, dict):
        raise CheckoError("Checko: неожиданный формат ответа")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if str(meta.get("status") or "").lower() == "error":
        msg = str(meta.get("message") or "ошибка запроса")
        low = msg.lower()
        if "ключ" in low or "token" in low or "key" in low:
            raise CheckoError(f"Checko: {msg} — проверьте API-ключ в настройках")
        if "лимит" in low or "исчерп" in low or "превыш" in low:
            raise CheckoError(f"Checko: {msg} (бесплатный тариф — 100 запросов/день)")
        if "не найден" in low or "не сущест" in low:
            raise CheckoError("Checko: организация с таким ИНН не найдена в "
                              "ЕГРЮЛ/ЕГРИП")
        raise CheckoError(f"Checko: {msg[:200]}")
    return data


# --------------------------------------------------------------------------
#  Защитное чтение полей (структура может расширяться)
# --------------------------------------------------------------------------
def _d(node, *path, default=None):
    cur = node
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _s(value) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _find_key(node, *needles, max_depth=8, _counter=None):
    """Глубокий поиск первого словаря/значения по подстроке ключа
    (названия служебных блоков могут меняться между версиями API)."""
    if _counter is None:
        _counter = [0]
    if _counter[0] > 20000 or max_depth < 0:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if any(n in kl for n in needles) and v not in (None, "", [], {}):
                return v
        for v in node.values():
            _counter[0] += 1
            r = _find_key(v, *needles, max_depth=max_depth - 1, _counter=_counter)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node[:80]:
            _counter[0] += 1
            r = _find_key(v, *needles, max_depth=max_depth - 1, _counter=_counter)
            if r is not None:
                return r
    return None


def _deep_scan(node, needle, max_depth=8):
    """Все значения словарей, чей ключ содержит подстроку (для доп. ОКВЭД)."""
    out, stack, seen = [], [node], 0
    while stack and seen < 20000:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            for k, v in cur.items():
                if needle in str(k).lower() and isinstance(v, list):
                    out.extend(x for x in v if isinstance(x, dict))
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(x for x in cur[:80] if isinstance(x, (dict, list)))
    return out


def _status_text(status) -> str:
    """Статус: строка ИЛИ {Код, Наим} (справочник) — приводим к тексту."""
    if isinstance(status, dict):
        status = status.get("Наим") or status.get("Код")
    return _s(status)


def _okved_text(node) -> str:
    """ОКВЭД: {Код, Наим} или строка → «62.01 — Разработка ПО»."""
    if isinstance(node, dict):
        code, name = _s(node.get("Код")), _s(node.get("Наим"))
        if code and name:
            return f"{code} — {name}"
        return code or name
    return _s(node)


def _address_text(node) -> str:
    """ЮрАдрес: строка ИЛИ {АдресРФ, НасПункт} → одна строка."""
    if isinstance(node, dict):
        addr = _s(node.get("АдресРФ"))
        if not addr:
            addr = _s(node.get("НасПункт"))
        return addr
    return _s(node)


def _count_items(*lists) -> int:
    return sum(len(x) for x in lists if isinstance(x, list))


def _money(value) -> str:
    try:
        return f"{float(str(value).replace(' ', '').replace(chr(160), '')):,.0f}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return _s(value)


# --------------------------------------------------------------------------
#  Карточка: ответ Checko → сводка для UI (ключи card — английские, стабильные)
# --------------------------------------------------------------------------
def build_card(kind: str, payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    if kind == "legal":
        card = {
            "kind": "legal",
            "name_full": _s(_d(data, "НаимПолн", default=_d(data, "НаимСокр"))),
            "name_short": _s(data.get("НаимСокр")),
            "inn": _s(data.get("ИНН")),
            "kpp": _s(data.get("КПП")),
            "ogrn": _s(data.get("ОГРН")),
            "okpo": _s(data.get("ОКПО")),
            "reg_date": _s(_d(data, "ДатаОГРН", default=_d(data, "ДатаРег"))),
            "status": _status_text(data.get("Статус")),
            "region": _s(_d(data, "Регион", "Наим")),
            "address": _address_text(data.get("ЮрАдрес")),
            "okved": _okved_text(data.get("ОКВЭД")),
        }
        # недостоверность адреса и массовый адрес — факторы риска (в светофор)
        if isinstance(data.get("ЮрАдрес"), dict):
            ya = data["ЮрАдрес"]
            card["address_invalid"] = bool(ya.get("Недост"))
            card["address_invalid_note"] = _s(ya.get("НедостОпис"))
            card["mass_address_count"] = _count_items(ya.get("МассАдрес"))
        # руководитель: блок «Руковод…» (список {ФИО, Должность}) либо УпрОрг
        ruk = _find_key(data, "руковод")
        if isinstance(ruk, list) and ruk and isinstance(ruk[0], dict):
            card["director"] = _s(ruk[0].get("ФИО"))
            card["management_post"] = _s(ruk[0].get("Должность"))
        else:
            upr = data.get("УпрОрг")
            if isinstance(upr, dict):
                card["director"] = _s(upr.get("НаимСокр") or upr.get("НаимПолн"))
                card["management_post"] = "Управляющая организация"
        # учредители и филиалы
        uch = data.get("Учред")
        if isinstance(uch, dict):
            card["founders_count"] = _count_items(uch.get("ФЛ"),
                                                  uch.get("РосОрг"),
                                                  uch.get("ФЛОсущПрав"))
        pod = data.get("Подразд")
        if isinstance(pod, dict):
            card["branches_count"] = _count_items(pod.get("Филиал"),
                                                  pod.get("Представ"))
    else:  # индивидуальный предприниматель
        card = {
            "kind": "individual",
            "name_full": _s(data.get("ФИО")),
            "name_short": _s(data.get("ФИО")),
            "inn": _s(data.get("ИНН")),
            "kpp": "",
            "ogrn": _s(data.get("ОГРНИП")),
            "okpo": _s(data.get("ОКПО")),
            "reg_date": _s(_d(data, "ДатаОГРНИП", default=_d(data, "ДатаРег"))),
            "status": _status_text(data.get("Статус")),
            "region": _s(_d(data, "Регион", "Наим")),
            "address": _address_text(data.get("ЮрАдрес"))
                       or _s(data.get("НасПункт")),
            "okved": _okved_text(data.get("ОКВЭД")),
            "opf": _s(data.get("ТипСокр")) or _s(data.get("Тип"))
                   or "Индивидуальный предприниматель",
        }

    # --- общие блоки: капитал, ИФНС, контакты, доп. ОКВЭД (защитно) ---------
    cap = _find_key(data, "кап")   # УстКап/Капитал
    if cap not in (None, ""):
        card["capital"] = _money(cap if isinstance(cap, (int, float, str)) else "")
    tax = _find_key(data, "налогорг", "ифнс", "инспекц")
    if isinstance(tax, dict):
        card["tax_office"] = _s(tax.get("Наим"))
        card["tax_office_code"] = _s(tax.get("Код"))
    elif isinstance(tax, str) and tax:
        card["tax_office"] = _s(tax)
    # контакты: по ключам, затем резерв — первый e-mail в данных
    email_node = _find_key(data, "mail", "почт")
    if isinstance(email_node, dict):
        email_node = (email_node.get("Email") or email_node.get("Значение")
                      or email_node.get("Адрес"))
    if isinstance(email_node, list):
        email_node = next((x for x in email_node
                           if isinstance(x, (str, dict))), None)
        if isinstance(email_node, dict):
            email_node = (email_node.get("Email") or email_node.get("Значение")
                          or email_node.get("Адрес"))
    card["email"] = _s(email_node)
    if not card["email"]:
        m = EMAIL_RE.search(str(data)[:20000])
        card["email"] = m.group(0) if m else ""
    phone_node = _find_key(data, "телефон", "тел.")
    if isinstance(phone_node, dict):
        phone_node = (phone_node.get("Телефон") or phone_node.get("Номер")
                      or phone_node.get("Значение"))
    if isinstance(phone_node, list):
        phone_node = next((x for x in phone_node
                           if isinstance(x, (str, dict))), None)
        if isinstance(phone_node, dict):
            phone_node = (phone_node.get("Телефон") or phone_node.get("Номер")
                          or phone_node.get("Значение"))
    card["phone"] = _s(phone_node)
    # дополнительные ОКВЭД: списки {Код, Наим} в ключах «ОКВЭД…», кроме основного
    extras, seen_codes = [], {card["okved"].split(" — ")[0]}
    for item in _deep_scan(data, "оквэд"):
        code, name = _s(item.get("Код")), _s(item.get("Наим"))
        if code and code not in seen_codes:
            seen_codes.add(code)
            extras.append(f"{code} — {name}".strip(" —"))
        if len(extras) >= 5:
            break
    card["okved_extra"] = extras
    if not card.get("opf") and card["name_full"]:
        card["opf"] = ""                     # у ЮЛ ОПФ внутри НаимПолн
    return card


def fetch_card(api_key: str, inn: str) -> dict:
    """Данные компании/ИП по ИНН → {card, raw, meta}.

    meta: {status, today_request_count, balance} — для теста ключа в UI
    (остаток запросов виден сразу).
    """
    data = _request(api_key, inn)
    kind = inn_kind(inn) or "legal"
    card = build_card(kind, data)
    if not card["name_full"]:
        raise CheckoError("Checko: организация найдена, но сервис не вернул "
                          "наименование — пришлите ИНН в поддержку ymaster.ru")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta_out = {k: meta[k] for k in ("status", "today_request_count", "balance")
                if k in meta}
    return {"card": card, "raw": data, "meta": meta_out}


def test_key(api_key: str) -> dict:
    """Проверка ключа живым запросом (Сбербанк есть в ЕГРЮЛ всегда).
    Тратит 1 запрос из 100 дневных; ошибки — бесплатны."""
    result = fetch_card(api_key, "7707083893")
    c = result["card"]
    return {
        "ok": True,
        "card": {"name_full": c.get("name_full"), "inn": c.get("inn"),
                 "status": c.get("status"), "address": c.get("address"),
                 "okved": c.get("okved")},
        "meta": result.get("meta", {}),
        "message": ("Ключ работает: «{}» ({})".format(
            c.get("name_full"), c.get("status") or "статус не указан"))
                   + (f"; запросов сегодня: {result['meta']['today_request_count']}"
                      if result.get("meta", {}).get("today_request_count") is not None
                      else ""),
    }
