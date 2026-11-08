"""Кандидаты из УПД/счетов-фактур и выписки (T-80, ADR-48).

Найденный в документе контрагент остаётся предложением: карточка создаётся
только действием бухгалтера, иначе recordings из старых документов возникли бы
без проверки человека.
"""
from __future__ import annotations

from ..services.companies_util import normalize_inn


def _candidate(name: str, inn: str, kpp: str, source: str, company_id: str) -> dict | None:
    value = normalize_inn(inn)
    if not value or not (name or "").strip():
        return None
    return {"name": str(name).strip(), "inn": value, "kpp": str(kpp or ""),
            "source": source, "company_id": company_id}


def collect_candidates(documents, operations, incomes, company_id: str | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(item) -> None:
        if company_id and getattr(item, "company_id", None) not in (None, "", company_id):
            return
        cid = getattr(item, "company_id", None) or company_id or ""
        for cand in (
            _candidate(getattr(item, "seller_name", ""), getattr(item, "seller_inn", ""),
                       getattr(item, "seller_kpp", ""), "upd", cid),
            _candidate(getattr(item, "buyer_name", ""), getattr(item, "buyer_inn", ""),
                       getattr(item, "buyer_kpp", ""), "upd", cid),
        ):
            key = (cand["inn"], cand["name"], cand["source"]) if cand else ()
            if cand and key not in seen:
                seen.add(key)
                out.append(cand)

    for doc in documents or []:
        add(doc)
    for item in list(operations or []) + list(incomes or []):
        if company_id and getattr(item, "company_id", None) not in (None, "", company_id):
            continue
        cand = _candidate(getattr(item, "counterparty_name", ""),
                          getattr(item, "counterparty_inn", ""), "", "bank",
                          getattr(item, "company_id", None) or company_id or "")
        key = (cand["inn"], cand["name"], cand["source"]) if cand else ()
        if cand and key not in seen:
            seen.add(key)
            out.append(cand)
    return out
