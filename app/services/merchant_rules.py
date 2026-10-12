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
