"""Проверка контрагента по ИНН и БИК (T-80, ADR-48).

Используется готовый блок Checko/ЕГРЮЛ программы; сетевой вызов инъектируется,
поэтому тесты и локальная разработка работают без ключа и без интернета. Квота
Checko не обходится: запрос идёт только по кнопке бухгалтера. БИК сверяется с
локальным справочником распространённых банков без внешнего запроса.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from ..services import checko
from ..services.companies_util import inn_is_valid, inn_kind, normalize_inn, risk_assessment

BIK_DIRECTORY: dict[str, dict[str, str]] = {
    "044525225": {"bank_name": "ПАО Сбербанк", "corr_account": "30101810400000000225"},
    "044525974": {"bank_name": "АО «Тинькофф Банк»", "corr_account": "30101810145250000974"},
    "044525593": {"bank_name": "АО «Альфа-Банк»", "corr_account": "30101810200000000593"},
    "044525187": {"bank_name": "ПАО «ВТБ»", "corr_account": "30101810700000000187"},
}


def lookup_bik(bik: str) -> dict:
    value = re.sub(r"\D", "", str(bik or ""))
    return dict(BIK_DIRECTORY.get(value, {}))


def _risk(card: dict, inn: str) -> dict:
    result = risk_assessment(card or None, inn)
    if not card:
        return {"level": "yellow", "reasons": [
            "Данные ЕГРЮЛ не заполнены — проверка по ИНН недоступна"]}
    if card.get("address_invalid") or (card.get("mass_address_count") or 0) >= 10:
        result["level"] = "red"
        if card.get("address_invalid") and not any("Недостоверный" in r for r in result["reasons"]):
            result["reasons"].append("Недостоверный юридический адрес (ЕГРЮЛ)")
    return result


def verify_by_inn(inn: str, fetch: Callable[[str, dict], dict] | None = None,
                  card: dict | None = None, raw: dict | None = None) -> dict:
    """Возвращает карточку и светофор. `fetch(kind, raw)` — инъекция Checko.

    `raw` может быть сырым ответом Checko или обёрткой `{card, raw}`: оба вида
    встречаются в готовом блоке программы. Некорректный ИНН не уходит в сеть.
    """
    value = normalize_inn(inn)
    if not inn_is_valid(value):
        return {"inn": value, "kind": None, "card": {}, "risk": {
            "level": "yellow", "reasons": ["Некорректный ИНН — проверьте контрольные цифры"]}}
    kind = inn_kind(value) or "legal"
    data = dict(card or {})
    if not data and fetch is not None:
        data = dict(fetch(kind, dict(raw or {})) or {})
    elif not data and raw:
        payload = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
        if isinstance(raw.get("card"), dict):
            data = dict(raw["card"])
        else:
            data = dict(checko.build_card(kind, payload) or {})
    if not data:
        return {"inn": value, "kind": kind, "card": {}, "risk": _risk({}, value)}
    return {"inn": value, "kind": kind, "card": data, "risk": _risk(data, value),
            "bik": {}}
