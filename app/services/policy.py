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
import json
from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import AppSetting, Company, Receipt

# Коды нарушений (поле Receipt.policy_flags, через запятую)
LIMIT_CHECK = "limit_check"            # сумма к учёту выше лимита чека
LIMIT_MONTH = "limit_month"            # превышен месячный лимит сотрудника
FORBIDDEN_CATEGORY = "forbidden_category"   # статья запрещена политикой
FORBIDDEN_GOODS = "forbidden_goods"     # v1.70.93 (T-43): в чеке запрещённые товары

# Группы товаров (app/services/goods.py), запрещённые по умолчанию, — решение владельца
# 03.11.2026 (#28): алкоголь и подобные товары, продукты питания; профиль компаний —
# стройка. Своя настройка компании — запись app_settings «policy_goods:<компания>».
DEFAULT_FORBIDDEN_GOODS = ("Алкогольные напитки", "Табачные изделия", "Продукты питания", "Общепит")
GOODS_KEY = "policy_goods:"


def goods_groups() -> list[str]:
    """Все группы товаров классификатора — для настройки политики."""
    from . import goods
    return [group for _, group, _ in goods.RULES]


def forbidden_goods(db: Session, company_id: str | None) -> list[str]:
    """Запрещённые группы товаров компании (нет своей настройки — по умолчанию)."""
    row = db.get(AppSetting, GOODS_KEY + company_id) if company_id else None
    if row is None:
        return list(DEFAULT_FORBIDDEN_GOODS)
    try:
        value = json.loads(row.value or "[]")
    except ValueError:
        return list(DEFAULT_FORBIDDEN_GOODS)
    return [g for g in value if isinstance(g, str)]


def set_forbidden_goods(db: Session, company_id: str, groups: list[str]) -> list[str]:
    """Сохранить запрещённые группы компании; неизвестные группы отбрасываются."""
    allowed = set(goods_groups())
    clean = [g for g in dict.fromkeys(groups) if g in allowed]
    db.merge(AppSetting(key=GOODS_KEY + company_id, value=json.dumps(clean, ensure_ascii=False)))
    return clean


def item_group(item) -> str:
    """Группа товара позиции: сохранённая или по наименованию."""
    if getattr(item, "product_group", ""):
        return item.product_group
    from . import goods
    return goods.classify_name(item.name or "", getattr(item, "product_type", None))[1]


def goods_hits(receipt: Receipt, groups) -> list[dict]:
    """Позиции чека из запрещённых групп: [{name, group, total}]."""
    wanted = set(groups or ())
    if not wanted:
        return []
    return [{"name": it.name, "group": g, "total": float(it.total or 0)}
            for it in (receipt.items or []) if (g := item_group(it)) in wanted]


def goods_violation(receipt: Receipt, groups) -> bool:
    """Запрещённые позиции есть и не покрыты личной суммой сотрудника."""
    hits = goods_hits(receipt, groups)
    forbidden_total = sum(h["total"] for h in hits)
    return bool(hits) and round(forbidden_total, 2) > round(float(receipt.personal_sum or 0), 2)


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


def _flags(comp: Company, r: Receipt, month_sum: float | None,
           goods: list[str] | None = None, violation: bool | None = None) -> list[str]:
    """Правила: month_sum — сумма к учёту сотрудника за месяц ВКЛЮЧАЯ чек;
    goods — запрещённые группы товаров компании (v1.70.93, T-43)."""
    flags: list[str] = []
    acc = accountable_of(r.total_sum, r.personal_sum)
    if comp.limit_per_check and round(acc, 2) > round(comp.limit_per_check, 2):
        flags.append(LIMIT_CHECK)
    if (comp.limit_per_month and month_sum is not None
            and round(month_sum, 2) > round(comp.limit_per_month, 2)):
        flags.append(LIMIT_MONTH)
    if r.category_lc and r.category_lc in forbidden_lc(comp):
        flags.append(FORBIDDEN_CATEGORY)
    # violation — посчитано пачкой (_goods_violations), иначе — по позициям этого чека
    if violation if violation is not None else bool(goods) and goods_violation(r, goods):
        flags.append(FORBIDDEN_GOODS)
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
    return _flags(comp, receipt, month_sum, forbidden_goods(db, comp.id))


def refresh_receipt(db: Session, receipt: Receipt) -> list[str]:
    """Пересчитать и записать флаги чека (без commit — зовёт владелец)."""
    flags = flags_for(db, receipt)
    new = ",".join(flags)
    if (receipt.policy_flags or "") != new:
        receipt.policy_flags = new
    return flags


def _goods_violations(db: Session, receipts) -> set[str]:
    """Чеки пачки с запрещёнными товарами, не покрытыми личной суммой (v1.70.93, T-43).

    Один запрос нужных колонок позиций на 500 чеков; группа — сохранённая или по
    наименованию, одинаковые наименования классифицируются один раз.
    """
    from ..models import ReceiptItem
    from . import goods as goods_service
    by_company: dict[str, list[Receipt]] = defaultdict(list)
    for r in receipts:
        if r.company_id:
            by_company[r.company_id].append(r)
    found: set[str] = set()
    known: dict[tuple, str] = {}
    for company_id, rows in by_company.items():
        wanted = set(forbidden_goods(db, company_id))
        if not wanted:
            continue
        personal = {r.id: float(r.personal_sum or 0) for r in rows}
        ids = list(personal)
        totals: dict[str, float] = defaultdict(float)
        for i in range(0, len(ids), 500):
            for rid, name, group, ptype, total in (
                    db.query(ReceiptItem.receipt_id, ReceiptItem.name, ReceiptItem.product_group,
                             ReceiptItem.product_type, ReceiptItem.total)
                    .filter(ReceiptItem.receipt_id.in_(ids[i:i + 500]))):
                if not group:
                    group = known.get((name, ptype))
                    if group is None:
                        group = known[(name, ptype)] = goods_service.classify_name(name or "", ptype)[1]
                if group in wanted:
                    totals[rid] += float(total or 0)
        found |= {rid for rid, total in totals.items() if round(total, 2) > round(personal[rid], 2)}
    return found


def refresh_receipts(db: Session, receipts) -> int:
    """Пакетный refresh_receipt (перенос чеков, T-33): те же флаги, что при
    вызове refresh_receipt для каждого чека, но сумма месяца считается одним
    запросом на компанию, а не отдельным запросом на каждый чек.

    Сумма месяца — как во flags_for: чек плюс другие чеки той же компании и
    того же сотрудника (assignee_lc) за календарный месяц, приход, не «failed».
    «Другие» = итог группы минус сам чек; порядок сложения на результат не
    влияет: _flags сравнивает с лимитом после округления до копеек.
    Возвращает число чеков, у которых пометка изменилась. Commit — за вызывающим.
    """
    receipts = list(receipts)
    violations = _goods_violations(db, receipts)     # v1.70.93: запрещённые товары — пачкой
    eligible: dict[str, list[Receipt]] = defaultdict(list)
    for r in receipts:
        comp = db.get(Company, r.company_id) if r.company_id else None
        if (comp is not None and comp.limit_per_month and (r.assignee or "").strip()
                and r.operation == 1 and r.status != "failed" and r.receipt_date is not None
                and _month_window(r.receipt_date)):
            eligible[comp.id].append(r)
    # {(компания, assignee_lc, (год, месяц)): {id чека: сумма к учёту}} — по базе
    groups: dict[tuple, dict[str, float]] = defaultdict(dict)
    for company_id, rows in eligible.items():
        windows = [_month_window(r.receipt_date) for r in rows]
        start = min(w[0] for w in windows)
        end = max(w[1] for w in windows)
        names = sorted({r.assignee_lc or "" for r in rows})
        for i in range(0, len(names), 500):
            found = (db.query(Receipt.id, Receipt.assignee_lc, Receipt.receipt_date,
                              Receipt.total_sum, Receipt.personal_sum)
                     .filter(Receipt.company_id == company_id,
                             Receipt.assignee_lc.in_(names[i:i + 500]),
                             Receipt.receipt_date >= start,
                             Receipt.receipt_date < end,
                             Receipt.operation == 1,
                             Receipt.status != "failed"))
            for rid, name, date, total, personal in found:
                key = (company_id, name, (date.year, date.month))
                groups[key][rid] = accountable_of(total, personal)
    totals = {key: sum(values.values()) for key, values in groups.items()}
    eligible_ids = {r.id for rows in eligible.values() for r in rows}
    changed = 0
    for r in receipts:
        comp = db.get(Company, r.company_id) if r.company_id else None
        if comp is None:
            flags: list[str] = []
        else:
            month_sum = None
            if r.id in eligible_ids:
                window = _month_window(r.receipt_date)
                key = (comp.id, r.assignee_lc or "", (window[0].year, window[0].month))
                others = totals.get(key, 0.0) - groups.get(key, {}).get(r.id, 0.0)
                month_sum = accountable_of(r.total_sum, r.personal_sum) + others
            flags = _flags(comp, r, month_sum, violation=r.id in violations)
        new = ",".join(flags)
        if (r.policy_flags or "") != new:
            r.policy_flags = new
            changed += 1
    return changed


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
    violations = _goods_violations(db, rows)         # v1.70.93: запрещённые товары — пачкой
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
        flags = _flags(comp, r, month_sum, violation=r.id in violations)
        new = ",".join(flags)
        if (r.policy_flags or "") != new:
            r.policy_flags = new
            changed += 1
    return changed
