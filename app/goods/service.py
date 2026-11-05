"""Каскад понимания чеков (T-69, v1.70.101, ADR-31).

Категория каждой позиции — по первой уверенной ступени:
1. память компании — бухгалтер уже исправлял этот товар («цемент» → «Стройматериалы»);
2. признак ФНС — тег 1212: работа или услуга (официальный код, не угадывание);
3. словарь — слово-признак с исключениями (app/services/goods.py);
4. модель в духе FastText — символьные n-граммы, если она уверена (app/goods/model.py);
5. «тип чека» и продавец — идея Data Fusion: группа, набравшая большую часть суммы уверенных позиций,
   и отрасль сети продавца уточняют сомнительные позиции; в кафе «кофе» — блюдо, в магазине — продукты;
6. иначе — «не определено» честно, без выдумки.

У каждой позиции сохраняются источник, уверенность и причина («по слову «цемент»», «модель — 82 %»).
Исправление бухгалтера — главнее всего: каскад его не перезаписывает. Догадка модели или контекста ниже
порога не считается нарушением политики (view.POLICY_MIN). Исходное название из ФНС не меняется.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..services import goods
from . import model as goods_model
from . import names, profiles
from .view import CERTAIN, SOURCES, default_category, item_view, legacy_category

MODEL_MIN = 0.5        # ниже — модель только подсказывает контексту
REFINE_MIN = 0.9       # словарь дал группу, а модель так уверена в другой категории той же группы
CONTEXT_MIN = 0.45     # доля группы после учёта чека и продавца, с которой принимаем догадку
TYPE_SHARE = 0.5       # «тип чека»: группа набрала не меньше половины суммы уверенных позиций
SWAP_SHARE = 0.6       # кафе ↔ магазин: доля, с которой «тип чека» меняет «кофе» на продукты
MEMORY_DAYS = 120      # исправление сразу применяется к таким же позициям за этот срок
MEMORY_SCAN = 5000     # и не больше стольких позиций за раз

# Отрасль сети из справочника Чек-Пула (app/pool/geo/data/chains.json) → группа товаров
CHAIN_GROUP = {
    "construction": "Строительные материалы и инструмент", "food": "Продукты питания", "cafe": "Общепит",
    "pharma": "Лекарства и медизделия", "fuel": "Топливо", "auto": "Автотовары",
    "electronics": "Электроника и бытовая техника", "clothes": "Одежда и обувь",
    "beauty": "Косметика и гигиена", "household": "Бытовая химия и хозтовары", "kids": "Детские товары",
    "pet": "Зоотовары", "books": "Канцелярия и книги", "sport": "Спорттовары",
    "alcohol": "Алкогольные напитки", "telecom": "Услуги",
}
# В кафе «кофе» и «блин» — блюдо, в магазине — продукты (словарь не знает, где куплено)
SWAP = {("catering", "Продукты питания"): "food", ("food", "Общепит"): "catering"}


class GoodsError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Verdict:
    category: str
    source: str
    score: float
    note: str
    probs: dict = field(default_factory=dict)        # вероятности модели — только для контекста

    @property
    def group(self) -> str:
        return goods.category(self.category).group


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _model_within(name: str, group: str) -> str | None:
    """Лучшая категория модели внутри группы (для услуг по признаку ФНС)."""
    m = goods_model.load()
    if m is None:
        return None
    probs = m.predict(name)["probs"]
    inside = [c.code for c in goods.group_categories(group)]
    best = max(inside, key=lambda c: probs.get(c, 0.0)) if inside else None
    return best if best and probs.get(best, 0.0) >= 0.2 else None


def understand(name: str, product_type=None, memory: dict | None = None) -> Verdict:
    """Категория одной позиции без учёта чека (ступени 1–4)."""
    key = names.name_key(name)
    if memory and key and key in memory:
        return Verdict(memory[key], "memory", 1.0, "как отметил бухгалтер для этого товара")
    pt = _int(product_type)
    if pt in (3, 4):
        kind = "работа" if pt == 3 else "услуга"
        found = goods.match(name, groups=("Услуги",))
        if found:
            return Verdict(found[0].code, "fns", 1.0, f"ФНС: {kind}; по слову «{found[1]}»")
        return Verdict(_model_within(name, "Услуги") or "services_other", "fns", 1.0, f"ФНС: {kind}")
    found = goods.match(name)
    m = goods_model.load()
    if found:
        # Словарь точен в группе; категорию внутри группы уточняет очень уверенная модель:
        # «ГРУНТОКВА ЦЕРЕЗИТ» — словарь узнал марку (стройка), модель — грунтовку (отделка).
        if m is not None:
            best, prob = m.predict(name)["top"][0]
            if (best != found[0].code and prob >= REFINE_MIN
                    and goods.category(best).group == found[0].group):
                return Verdict(best, "dictionary", 0.97,
                               f"по слову «{found[1]}»; вид товара — по модели, {round(prob * 100)} %")
        return Verdict(found[0].code, "dictionary", 0.97, f"по слову «{found[1]}»")
    if m is None:
        return Verdict(goods.OTHER, "none", 0.0, "не определено — укажите категорию")
    pred = m.predict(name)
    best, prob = pred["top"][0]
    if prob >= MODEL_MIN:
        title = goods.category(best).title
        return Verdict(best, "model", round(prob, 3), f"похоже на «{title}» — {round(prob * 100)} %",
                       pred["probs"])
    return Verdict(goods.OTHER, "none", round(prob, 3), "не определено — укажите категорию", pred["probs"])


def chain_of(merchant_name: str) -> tuple[str, str]:
    """(группа товаров сети, название сети) по справочнику сетей Чек-Пула; не нашли — ('', '')."""
    if not (merchant_name or "").strip():
        return "", ""
    try:
        from ..pool.geo import match_industry
        code, chain = match_industry(merchant_name, [])
    except Exception:                                         # noqa: BLE001 — справочник не ломает разбор
        return "", ""
    return (CHAIN_GROUP.get(code, ""), chain) if chain else ("", "")


def receipt_type(items: list[dict], verdicts: list[Verdict]) -> tuple[str, float]:
    """«Тип чека»: группа с наибольшей суммой среди уверенных позиций и её доля."""
    sums: dict[str, float] = {}
    for it, v in zip(items, verdicts):
        if v.source in CERTAIN or v.source in ("model", "context"):
            amount = max(_float(it.get("total")), 0.0) or 0.01   # без сумм — считаем по числу позиций
            sums[v.group] = sums.get(v.group, 0.0) + amount
    total = sum(sums.values())
    if total <= 0:
        return "", 0.0
    best = max(sums, key=lambda g: sums[g])
    return best, sums[best] / total


def _context(v: Verdict, rtype: str, chain_group: str, chain_name: str, profile: str) -> Verdict:
    """Сомнительная позиция: группа — по модели с поправкой на «тип чека» и продавца."""
    if not rtype and not chain_group:
        return v
    focus_groups = {goods.category(c).group for c in profiles.focus(profile)}
    group_p: dict[str, float] = {}
    for code, p in (v.probs or {}).items():
        g = goods.category(code).group
        group_p[g] = group_p.get(g, 0.0) + p
    if not group_p:
        all_groups = [g for g in goods.groups() if g != goods.DEFAULT_GROUP]
        group_p = {g: 1.0 / len(all_groups) for g in all_groups}
    for g in group_p:
        weight = 1.0 + (3.0 if g == rtype else 0.0) + (3.0 if g == chain_group else 0.0)
        weight += 1.0 if g in focus_groups else 0.0
        group_p[g] *= weight
    norm = sum(group_p.values()) or 1.0
    best = max(group_p, key=lambda g: group_p[g])
    share = group_p[best] / norm
    if share < CONTEXT_MIN or best not in (rtype, chain_group):
        return v
    inside = [c.code for c in goods.group_categories(best)]
    if v.probs and inside and max(v.probs.get(c, 0.0) for c in inside) >= 0.05:
        code = max(inside, key=lambda c: v.probs.get(c, 0.0))
    else:
        code = default_category(best)
    reasons = []
    if best == rtype:
        reasons.append(f"по другим товарам чека («{best}»)")
    if best == chain_group and chain_name:
        reasons.append(f"продавец — {chain_name}")
    return Verdict(code, "context", round(share, 3), "; ".join(reasons) or "по чеку и продавцу", v.probs)


def understand_receipt(items: list[dict], merchant_name: str = "", memory: dict | None = None,
                       profile: str = profiles.DEFAULT, fixed: list | None = None) -> list[Verdict]:
    """Категории всех позиций чека (ступени 1–6).

    items: [{"name", "total", "product_type"}]; fixed — уже известные вердикты (ручные правки и разобранные
    раньше позиции): они не пересчитываются, но участвуют в «типе чека».
    """
    verdicts = [fixed[i] if fixed and i < len(fixed) and fixed[i] is not None
                else understand(it.get("name", ""), it.get("product_type"), memory)
                for i, it in enumerate(items)]
    rtype, rshare = receipt_type(items, verdicts)
    chain_group, chain_name = chain_of(merchant_name)
    for i, v in enumerate(verdicts):
        if fixed and i < len(fixed) and fixed[i] is not None:
            continue
        if v.source == "dictionary" and v.category in ("catering", "food"):
            target = SWAP.get((v.category, chain_group))
            reason = f"продавец — {chain_name}" if target else ""
            if not target and rshare >= SWAP_SHARE:
                target = SWAP.get((v.category, rtype))
                reason = f"по другим товарам чека («{rtype}»)" if target else ""
            if target:
                verdicts[i] = Verdict(target, "context", 0.85, f"{v.note}; {reason}")
            continue
        if v.source == "none":
            verdicts[i] = _context(v, rtype if rshare >= TYPE_SHARE else "", chain_group, chain_name, profile)
    return verdicts


# --------------------------------------------------------------------------
#  Запись в позиции чека
# --------------------------------------------------------------------------
def _write(row, v: Verdict) -> None:
    cat = goods.category(v.category)
    row.goods_category = cat.code
    row.product_group = cat.group[:100]
    row.industry = cat.industry[:100]
    row.group_source = v.source
    row.group_score = round(float(v.score), 3)
    row.group_note = (v.note or "")[:160]


def _stored(row) -> Verdict | None:
    """Вердикт уже разобранной позиции (или None — разобрать заново)."""
    code = getattr(row, "goods_category", "") or ""
    if not code:
        return None
    return Verdict(code, getattr(row, "group_source", "") or "dictionary",
                   _float(getattr(row, "group_score", None)) or 0.0, getattr(row, "group_note", "") or "")


def memory_for(db: Session | None, company_id: str | None, item_names) -> dict[str, str]:
    """Память компании для этих позиций: {ключ товара: категория}."""
    if db is None or not company_id:
        return {}
    keys = sorted({names.name_key(n) for n in item_names} - {""})
    if not keys:
        return {}
    from .models import GoodsMemory
    rows = (db.query(GoodsMemory.name_key, GoodsMemory.category)
            .filter(GoodsMemory.company_id == company_id, GoodsMemory.name_key.in_(keys)).all())
    return dict(rows)


def _bump_hits(db: Session, company_id: str, keys: set[str]) -> None:
    from .models import GoodsMemory
    if keys:
        (db.query(GoodsMemory)
         .filter(GoodsMemory.company_id == company_id, GoodsMemory.name_key.in_(sorted(keys)))
         .update({GoodsMemory.hits: GoodsMemory.hits + 1}, synchronize_session=False))


def refresh_summary(receipt) -> None:
    """Отрасль и группа чека — по категориям позиций (ручную правку отрасли не трогаем)."""
    rows = list(receipt.items or [])
    if not rows or (receipt.details_source == "manual_edit" and receipt.industry):
        return
    calc = goods.classify_items([{"name": r.name, "total": r.total, "product_type": r.product_type,
                                  "group": r.product_group, "industry": r.industry} for r in rows])
    if calc.get("industry"):
        receipt.industry = calc["industry"][:100]
        receipt.industry_lc = calc["industry"].casefold()[:100]
        receipt.product_group = (calc.get("group") or "")[:100]


def classify_receipt(db: Session | None, receipt, *, force: bool = False) -> dict:
    """Разобрать позиции чека каскадом и записать категории.

    Позиции с ручной правкой не трогаются; уже разобранные — только при force. Возвращает {"items": N}.
    """
    rows = list(receipt.items or [])
    if not rows:
        return {"items": 0}
    todo = {id(r) for r in rows
            if (getattr(r, "group_source", "") or "") != "manual"
            and (force or not (getattr(r, "goods_category", "") or ""))}
    if not todo:
        refresh_summary(receipt)
        return {"items": 0}
    company_id = getattr(receipt, "company_id", None)
    memory = memory_for(db, company_id, [r.name for r in rows])
    profile = profiles.get(db, company_id) if db is not None else profiles.DEFAULT
    fixed = [None if id(r) in todo else _stored(r) for r in rows]
    verdicts = understand_receipt(
        [{"name": r.name or "", "total": r.total, "product_type": getattr(r, "product_type", None)} for r in rows],
        getattr(receipt, "merchant_name", "") or "", memory, profile, fixed)
    used: set[str] = set()
    changed = 0
    for row, v in zip(rows, verdicts):
        if id(row) not in todo:
            continue
        _write(row, v)
        changed += 1
        if v.source == "memory":
            used.add(names.name_key(row.name))
    if db is not None and company_id and used:
        _bump_hits(db, company_id, used)
    refresh_summary(receipt)
    return {"items": changed}


def summary(db: Session | None, receipt) -> dict:
    """Позиции по категориям (иконки, суммы) и подсказка статьи расходов для карточки чека."""
    by_cat: dict[str, dict] = {}
    article_items = []
    for row in receipt.items or []:
        view = item_view(row)
        code = view["category"]
        cat = by_cat.setdefault(code, {"category": code, "title": view["category_title"],
                                       "icon": view["category_icon"], "group": view["category_group"],
                                       "count": 0, "sum": 0.0})
        cat["count"] += 1
        cat["sum"] = round(cat["sum"] + _float(row.total), 2)
        article_items.append({"category": code, "total": row.total})
    profile = profiles.get(db, getattr(receipt, "company_id", None)) if db is not None else profiles.DEFAULT
    out = {"categories": sorted(by_cat.values(), key=lambda c: -c["sum"]), "profile": profile,
           "profile_title": profiles.PROFILES[profile]["title"]}
    if not (getattr(receipt, "category", "") or "").strip():
        hint = profiles.article_hint(profile, article_items)
        if hint:
            out["article_hint"] = hint
    return out


# --------------------------------------------------------------------------
#  Исправление бухгалтера и память компании
# --------------------------------------------------------------------------
def valid_category(code: str) -> bool:
    return code == goods.OTHER or any(c.code == code for c in goods.categories())


def _apply_memory(db: Session, company_id: str, key: str, code: str, skip_item: str) -> tuple[int, set[str]]:
    """Такие же позиции компании за MEMORY_DAYS дней (кроме ручных правок) → категория из памяти."""
    from ..models import Receipt, ReceiptItem
    since = dt.datetime.utcnow() - dt.timedelta(days=MEMORY_DAYS)
    rows = (db.query(ReceiptItem)
            .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
            .filter(Receipt.company_id == company_id, Receipt.created_at >= since,
                    ReceiptItem.id != skip_item,
                    or_(ReceiptItem.group_source.is_(None), ReceiptItem.group_source != "manual"))
            .order_by(Receipt.created_at.desc()).limit(MEMORY_SCAN).all())
    applied = 0
    touched: set[str] = set()
    verdict = Verdict(code, "memory", 1.0, "как отметил бухгалтер для этого товара")
    for row in rows:
        if names.name_key(row.name) != key:
            continue
        if (row.goods_category or "") == code and (row.group_source or "") == "memory":
            continue
        _write(row, verdict)
        applied += 1
        touched.add(row.receipt_id)
    return applied, touched


def correct_item(db: Session, user, receipt, item, code: str, *, remember: bool = True) -> dict:
    """Бухгалтер выбрал категорию позиции: записать, запомнить для компании и применить к таким же."""
    from ..models import Receipt, utcnow
    from ..services import policy
    from .models import GoodsMemory
    if not valid_category(code):
        raise GoodsError(400, "Нет такой категории товаров")
    cat = goods.category(code)
    who = (getattr(user, "full_name", "") or getattr(user, "username", "") or "бухгалтер").strip()
    _write(item, Verdict(cat.code, "manual", 1.0,
                         f"исправлено вручную: {who}, {dt.date.today().strftime('%d.%m.%Y')}"))
    key = names.name_key(item.name)
    applied, touched = 0, {receipt.id}
    remembered = bool(remember and key and receipt.company_id)
    if remembered:
        row = (db.query(GoodsMemory)
               .filter(GoodsMemory.company_id == receipt.company_id, GoodsMemory.name_key == key).first())
        sample = names.readable(item.name)["display"][:200]
        if row is None:
            db.add(GoodsMemory(company_id=receipt.company_id, name_key=key, category=cat.code,
                               sample_name=sample, created_by=getattr(user, "id", "") or ""))
        else:
            row.category, row.sample_name, row.updated_at = cat.code, sample, utcnow()
        db.flush()
        applied, more = _apply_memory(db, receipt.company_id, key, cat.code, item.id)
        touched |= more
    for rid in sorted(touched):
        r = receipt if rid == receipt.id else db.get(Receipt, rid)
        if r is None:
            continue
        refresh_summary(r)
        try:
            policy.refresh_receipt(db, r)
        except Exception:                                     # noqa: BLE001 — политика не отменяет правку
            logging.getLogger("ymaster").debug("goods: policy refresh", exc_info=True)
    title = names.readable(item.name)["display"] or item.name
    msg = f"«{title}» — {cat.title}"
    if remembered:
        msg += "; запомнено для компании"
        if applied:
            msg += f", ещё {applied} таких же позиций в {len(touched) - 1 or 1} чеках"
    return {"ok": True, "message": msg, "item": {"id": item.id, **item_view(item)},
            "remembered": remembered, "applied": applied, "receipts": len(touched)}


def list_memory(db: Session, company_id: str, limit: int = 500) -> list[dict]:
    from .models import GoodsMemory
    rows = (db.query(GoodsMemory).filter(GoodsMemory.company_id == company_id)
            .order_by(GoodsMemory.updated_at.desc()).limit(limit).all())
    return [r.to_dict() for r in rows]


def forget(db: Session, company_id: str, memory_id: str) -> dict:
    """Удалить запись памяти (категории позиций остаются как есть — их можно поправить вручную)."""
    from .models import GoodsMemory
    row = db.get(GoodsMemory, memory_id[:36])
    if row is None or row.company_id != company_id:
        raise GoodsError(404, "Запись не найдена")
    data = row.to_dict()
    db.delete(row)
    return data


def catalog() -> dict:
    """Справочник для интерфейса: категории по группам, источники, профили, сведения о модели."""
    cats = [{"code": c.code, "title": c.title, "group": c.group, "industry": c.industry, "icon": c.icon}
            for c in goods.categories()]
    other = goods.category(goods.OTHER)
    cats.append({"code": other.code, "title": other.title, "group": other.group, "industry": other.industry,
                 "icon": other.icon})
    return {"categories": cats, "groups": goods.groups(), "sources": SOURCES,
            "profiles": profiles.catalog(), "model": goods_model.info()}


__all__ = ["Verdict", "understand", "understand_receipt", "classify_receipt", "correct_item", "summary",
           "memory_for", "list_memory", "forget", "catalog", "chain_of", "legacy_category"]
