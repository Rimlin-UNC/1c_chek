# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сервис «Политика расходов» (v1.62.0, Фаза 1 концепции).
# Лимиты и комплаенс НАД УЖЕ полученными данными чека (ноль доп. запросов
# к источникам): лимит ₽ на чек, лимит ₽ в месяц на сотрудника,
# запрещённые статьи расходов. Нарушение — МЯГКАЯ пометка policy_flags:
# статус чека не меняется, поток сдачи не ломается; чип в списке и
# счётчик в сводке. В суммах участвует «к учёту»
# (total_sum − personal_sum). Политика принадлежит компании; чек без
# компании не проверяется. Пересчёт: при создании/правке чека, полных
# данных, массовом назначении, перемещении и сохранении политики.
# ======================================================================
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import Company, Receipt

# Коды нарушений (поле Receipt.policy_flags, через запятую)
LIMIT_CHECK = "limit_check"            # сумма к учёту выше лимита чека
LIMIT_MONTH = "limit_month"            # превышен месячный лимит сотрудника
FORBIDDEN_CATEGORY = "forbidden_category"   # статья запрещена политикой


def parse_categories(raw: str | None) -> list[str]:
    """«Алкоголь, Табак» → ["Алкоголь", "Табак"] (пробелы, лимит 50)."""
    return [p.strip()[:100] for p in (raw or "").split(",") if p.strip()][:50]


def forbidden_lc(company: Company) -> set[str]:
    """Запрещённые статьи в нормализованном виде (регистр не важен)."""
    return {c.casefold() for c in parse_categories(getattr(
        company, "forbidden_categories", ""))}


def accountable_of(total_sum, personal_sum) -> float:
    """Сумма к учёту: чек минус личные покупки (не меньше нуля)."""
    acc = float(total_sum or 0) - float(personal_sum or 0)
    return acc if acc > 0 else 0.0


def _month_window(d: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """Календарный месяц бизнес-даты чека (без часовых поясов)."""
    if not d:
        return None
    start = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ny, nm = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, dt.datetime(ny, nm, 1)


def _flags(comp: Company, r: Receipt, month_sum: float | None) -> list[str]:
    """Правила: month_sum — сумма к учёту сотрудника за месяц ВКЛЮЧАЯ чек."""
    flags: list[str] = []
    acc = accountable_of(r.total_sum, r.personal_sum)
    if comp.limit_per_check and round(acc, 2) > round(comp.limit_per_check, 2):
        flags.append(LIMIT_CHECK)
    if (comp.limit_per_month and month_sum is not None
            and round(month_sum, 2) > round(comp.limit_per_month, 2)):
        flags.append(LIMIT_MONTH)
    if r.category_lc and r.category_lc in forbidden_lc(comp):
        flags.append(FORBIDDEN_CATEGORY)
    return flags


def flags_for(db: Session, receipt: Receipt) -> list[str]:
    """Коды нарушений политики для одного чека (без записи в базу)."""
    comp = db.get(Company, receipt.company_id) if receipt.company_id else None
    if comp is None:
        return []
    month_sum = None
    if comp.limit_per_month and (receipt.assignee or "").strip()             and receipt.operation == 1 and receipt.status != "failed"             and receipt.receipt_date is not None:
        window = _month_window(receipt.receipt_date)
        if window:
            start, end = window
            others = (db.query(Receipt)
                      .filter(Receipt.company_id == comp.id,
                              Receipt.assignee_lc == (receipt.assignee_lc or ""),
                              Receipt.receipt_date >= start,
                              Receipt.receipt_date < end,
                              Receipt.operation == 1,
                              Receipt.status != "failed",
                              Receipt.id != receipt.id)
                      .all())
            month_sum = accountable_of(receipt.total_sum,
                                       receipt.personal_sum) + sum(
                accountable_of(o.total_sum, o.personal_sum) for o in others)
    return _flags(comp, receipt, month_sum)


def refresh_receipt(db: Session, receipt: Receipt) -> list[str]:
    """Пересчитать и записать флаги чека (без commit — зовёт владелец)."""
    flags = flags_for(db, receipt)
    new = ",".join(flags)
    if (receipt.policy_flags or "") != new:
        receipt.policy_flags = new
    return flags


def refresh_company(db: Session, company_id: str) -> int:
    """Массовый пересчёт политики по всей компании (после смены правил).

    Возвращает число чеков, у которых пометка изменилась. Commit — за
    вызывающим. Правила те же (_flags), суммы месяца считаются одним
    проходом по чекам компании.
    """
    comp = db.get(Company, company_id)
    if comp is None:
        return 0
    rows = db.query(Receipt).filter(Receipt.company_id == company_id).all()
    sums: dict[tuple[str, tuple[int, int]], float] = defaultdict(float)
    for r in rows:
        if (r.operation == 1 and r.status != "failed"
                and (r.assignee or "").strip() and r.receipt_date is not None):
            window = _month_window(r.receipt_date)
            if window:
                key = ((r.assignee_lc or ""), (window[0].year, window[0].month))
                sums[key] += accountable_of(r.total_sum, r.personal_sum)
    changed = 0
    for r in rows:
        month_sum = None
        if comp.limit_per_month and (r.assignee or "").strip() \
                and r.receipt_date is not None:
            window = _month_window(r.receipt_date)
            if window:
                month_sum = sums.get(
                    ((r.assignee_lc or ""), (window[0].year, window[0].month)))
        flags = _flags(comp, r, month_sum)
        new = ",".join(flags)
        if (r.policy_flags or "") != new:
            r.policy_flags = new
            changed += 1
    return changed
