"""Кандидаты из УПД/счетов-фактур и выписки (T-80, ADR-48).

Найденный в документе контрагент остаётся предложением: карточка создаётся
только действием бухгалтера, иначе recordings из старых документов возникли бы
без проверки человека.
"""
from __future__ import annotations

import json

from ..services.companies_util import normalize_inn


def _candidate(name: str, inn: str, kpp: str, source: str, company_id: str) -> dict | None:
    value = normalize_inn(inn)
    if not value or not (name or "").strip():
        return None
    return {"name": str(name).strip(), "inn": value, "kpp": str(kpp or ""),
            "source": source, "company_id": company_id}


def collect_candidates(documents, operations, incomes,
                       company_id: str | None = None,
                       receipts=None) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def _push(cand: dict | None) -> None:
        if not cand:
            return
        key = (cand["inn"], cand["name"], cand["source"])
        if key not in seen:
            seen.add(key)
            out.append(cand)

    def add(item) -> None:
        if company_id and getattr(item, "company_id", None) not in (None, "", company_id):
            return
        cid = getattr(item, "company_id", None) or company_id or ""
        for cand in (
            _candidate(getattr(item, "seller_name", ""), getattr(item, "seller_inn", ""),
                       getattr(item, "seller_kpp", ""), "upd", cid),
            _candidate(getattr(item, "buyer_name", ""), getattr(item, "buyer_inn", ""),
                       getattr(item, "buyer_kpp", ""), "buyer_upd" if False else "upd", cid),
        ):
            _push(cand)

    for doc in documents or []:
        add(doc)
    for item in list(operations or []) + list(incomes or []):
        if company_id and getattr(item, "company_id", None) not in (None, "", company_id):
            continue
        _push(_candidate(getattr(item, "counterparty_name", ""),
                         getattr(item, "counterparty_inn", ""), "", "bank",
                         getattr(item, "company_id", None) or company_id or ""))

    for rec in receipts or []:
        r_cid = getattr(rec, "company_id", None)
        if company_id and r_cid != company_id:
            continue
        cid = r_cid or company_id or ""
        seller_kpp = ""
        ext_raw = getattr(rec, "ext_json", "") or ""
        if ext_raw:
            try:
                ext_obj = json.loads(ext_raw)
                if isinstance(ext_obj, dict):
                    seller_kpp = str(ext_obj.get("seller_kpp") or "")
            except ValueError:
                seller_kpp = ""
        _push(_candidate(getattr(rec, "merchant_name", ""),
                         getattr(rec, "merchant_inn", ""),
                         seller_kpp, "receipt", cid))
        for r_item in getattr(rec, "items", None) or []:
            extra_raw = getattr(r_item, "extra_json", "") or ""
            if not extra_raw:
                continue
            try:
                it_extra = json.loads(extra_raw)
            except ValueError:
                continue
            sup = it_extra.get("supplier") if isinstance(it_extra, dict) else None
            if isinstance(sup, dict):
                _push(_candidate(sup.get("name", ""), sup.get("inn", ""),
                                 "", "receipt", cid))
    return out
