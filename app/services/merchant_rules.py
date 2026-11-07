# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — самообучающийся маппинг (v1.63.0, Фаза 1 концепции).
# «Запомнить для поставщика (ИНН)»: бухгалтер поправил статью/сотрудника/
# комментарий → правило-кандидат (merchant_rules, ключ компания+ИНН+
# поле) → автозаполнение ПУСТЫХ полей будущих чеков этого поставщика.
# Человеческие значения правилами НЕ затираются (та же философия, что у
# manual_edit). Ноль доп. запросов к источникам. Поле comment тоже
# заполняется, только если пусто. Статусы чеков не трогаем.
# ======================================================================
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import MerchantRule, Receipt

# Правило можно запомнить только для этих полей (макс. длина значения)
RULE_FIELDS: dict[str, int] = {
    "category": 100,
    "assignee": 200,
    "comment": 2000,
}

FIELD_LABELS: dict[str, str] = {
    "category": "статья",
    "assignee": "сотрудник",
    "comment": "комментарий",
}


def _empty(value) -> bool:
    return not (str(value or "")).strip()


def apply_rules(db: Session, receipt: Receipt) -> list[str]:
    """Автозаполнение ПУСТЫХ полей чека правилами его поставщика.

    Возвращает список применённых полей. Правила ищутся по связке
    (company_id, merchant_inn); applied_count растёт только при реальном
    применении. Commit — за вызывающим.
    """
    inn = (getattr(receipt, "merchant_inn", "") or "").strip()
    if not inn or receipt.company_id is None:
        return []
    rules = (db.query(MerchantRule)
             .filter(MerchantRule.company_id == receipt.company_id,
                     MerchantRule.merchant_inn == inn).all())
    return _apply_loaded(receipt, rules)


def _apply_loaded(receipt: Receipt, rules) -> list[str]:
    """Применить уже загруженные правила поставщика к одному чеку."""
    applied: list[str] = []
    for rule in rules:
        field = rule.field
        if field not in RULE_FIELDS:
            continue
        if not _empty(getattr(receipt, field, None)):
            continue                     # человеческое значение не трогаем
        setattr(receipt, field, (rule.value or "")[:RULE_FIELDS[field]])
        rule.applied_count = (rule.applied_count or 0) + 1
        applied.append(field)
    return applied


def apply_rules_many(db: Session, receipts) -> int:
    """Пакетный apply_rules (перенос чеков, T-33): правила ищутся одним
    запросом на компанию, а не отдельным запросом на каждый чек. Результат
    тот же: на связку (компания, ИНН, поле) правило одно (UniqueConstraint),
    поэтому порядок правил не влияет. Возвращает число заполненных полей.
    """
    receipts = list(receipts)
    wanted: dict[str, set[str]] = {}
    for r in receipts:
        inn = (getattr(r, "merchant_inn", "") or "").strip()
        if inn and r.company_id is not None:
            wanted.setdefault(r.company_id, set()).add(inn)
    loaded: dict[tuple[str, str], list[MerchantRule]] = {}
    for company_id, inns in wanted.items():
        names = sorted(inns)
        for i in range(0, len(names), 500):
            for rule in (db.query(MerchantRule)
                         .filter(MerchantRule.company_id == company_id,
                                 MerchantRule.merchant_inn.in_(names[i:i + 500]))):
                loaded.setdefault((company_id, rule.merchant_inn), []).append(rule)
    applied = 0
    for r in receipts:
        inn = (getattr(r, "merchant_inn", "") or "").strip()
        if inn and r.company_id is not None:
            applied += len(_apply_loaded(r, loaded.get((r.company_id, inn), [])))
    return applied


def backfill(db: Session, rule: MerchantRule) -> int:
    """«Применить к старым»: заполнить пустые поля у старых чеков того же
    поставщика (та же компания). Возвращает число обновлённых чеков."""
    if rule.field not in RULE_FIELDS:
        return 0
    rows = (db.query(Receipt)
            .filter(Receipt.company_id == rule.company_id,
                    Receipt.merchant_inn == rule.merchant_inn).all())
    updated = 0
    for r in rows:
        if not _empty(getattr(r, rule.field, None)):
            continue
        setattr(r, rule.field, (rule.value or "")[:RULE_FIELDS[rule.field]])
        rule.applied_count = (rule.applied_count or 0) + 1
        updated += 1
    return updated
