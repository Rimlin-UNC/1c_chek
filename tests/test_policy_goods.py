"""Политика: запрещённые товары в позициях чека (T-43, v1.70.93).

Решение владельца 03.11.2026 (#28): запрет на алкоголь и подобные товары, продукты
питания; профиль компаний — стройка. Отметка мягкая: статус чека не меняется.
Позиции, которые сотрудник отметил как личные (личная сумма), нарушением не
считаются. Классификатор (app/services/goods.py) сначала распознаёт стройматериалы:
«Щебень крупный» и «Соль техническая» раньше попадали в продукты питания.
Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Company, Receipt, ReceiptItem
from app.services import goods, policy
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity, _registry

companies = _base.companies

CONSTRUCTION = ["Щебень гранитный крупный фр.20-40", "Соль техническая 50 кг", "Мука известняковая",
                "Смесь штукатурная Ротбанд 30 кг", "Песок речной мытый", "Гипсокартон Кнауф 12,5",
                "Цемент М500 50 кг", "Арматура А500С 12 мм", "Профнастил С8 оцинкованный",
                "Перчатки рабочие х/б", "Доска обрезная 50х150", "Кисть малярная 50 мм",
                "Газобетон D500 600х300х200", "Пеноплекс 50 мм", "Саморезы по металлу 4,2х16",
                "Кабель ВВГнг 3х2,5", "Сетка кладочная 50х50"]
FORBIDDEN = {"Пиво светлое 0,5": "Алкогольные напитки", "Водка 0,5 л": "Алкогольные напитки",
             "Сигареты Parliament": "Табачные изделия", "Стики HEETS": "Табачные изделия",
             "Хлеб нарезной": "Продукты питания", "Крупа гречневая": "Продукты питания",
             "Рис круглозёрный": "Продукты питания", "Вода питьевая 5 л": "Продукты питания"}


@pytest.mark.parametrize("name", CONSTRUCTION)
def test_construction_goods_are_never_forbidden(name):
    group = goods.classify_name(name)[1]
    assert group not in policy.DEFAULT_FORBIDDEN_GOODS, (name, group)


@pytest.mark.parametrize(("name", "group"), list(FORBIDDEN.items()))
def test_forbidden_goods_are_recognised(name, group):
    assert goods.classify_name(name)[1] == group


def _receipt(db, company_id, items, personal_sum=0.0):
    fn, fd, fp = _identity()
    r = _registry(db, fn, fd, fp, company=db.get(Company, company_id))
    r.items.clear()
    for pos, (name, total) in enumerate(items):
        r.items.append(ReceiptItem(name=name, quantity=1, price=total, total=total, position=pos))
    r.total_sum = sum(t for _, t in items)
    r.personal_sum = personal_sum
    db.flush()
    return r


def test_default_policy_flags_beer_but_not_cement_and_respects_personal_sum(companies):
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        mixed = _receipt(db, c1, [("Цемент М500 50 кг", 600.0), ("Пиво светлое 0,5", 120.0)])
        assert policy.FORBIDDEN_GOODS in policy.flags_for(db, mixed)
        assert [h["name"] for h in policy.goods_hits(mixed, policy.forbidden_goods(db, c1))] == ["Пиво светлое 0,5"]
        personal = _receipt(db, c1, [("Цемент М500 50 кг", 600.0), ("Пиво светлое 0,5", 120.0)], 120.0)
        assert policy.FORBIDDEN_GOODS not in policy.flags_for(db, personal)     # сотрудник отметил как личное
        clean = _receipt(db, c1, [("Щебень гранитный крупный фр.20-40", 900.0), ("Соль техническая 50 кг", 300.0)])
        assert policy.FORBIDDEN_GOODS not in policy.flags_for(db, clean)
        db.rollback()


def test_company_setting_via_api(companies, client):
    hdr, c1, _, _, _ = companies
    got = client.get(f"/api/v1/settings/policy?company_id={c1}", headers=hdr).json()
    assert got["forbidden_goods"] == list(policy.DEFAULT_FORBIDDEN_GOODS)
    assert "Строительные материалы и инструмент" in got["goods_groups"]
    res = client.put("/api/v1/settings/policy", headers=hdr,
                     json={"company_id": c1, "forbidden_goods": ["Алкогольные напитки", "Нет такой группы"]})
    assert res.status_code == 200, res.text
    assert client.get(f"/api/v1/settings/policy?company_id={c1}", headers=hdr).json()["forbidden_goods"] == \
        ["Алкогольные напитки"]
    with SessionLocal() as db:
        bread = _receipt(db, c1, [("Хлеб нарезной", 60.0)])
        beer = _receipt(db, c1, [("Пиво светлое 0,5", 120.0)])
        assert policy.FORBIDDEN_GOODS not in policy.flags_for(db, bread)   # продукты компания разрешила
        assert policy.FORBIDDEN_GOODS in policy.flags_for(db, beer)
        db.rollback()


def test_fns_items_refresh_policy_and_card_names_them(companies, client):
    hdr, c1, _, _, _ = companies
    from app.services.receipt_card import apply_to_receipt
    with SessionLocal() as db:
        r = _receipt(db, c1, [])
        rid = r.id
        ticket = {"receipt": {"totalSum": 72000, "items": [
            {"name": "Цемент М500 50 кг", "quantity": 1, "price": 60000, "sum": 60000, "nds": 11},
            {"name": "Пиво светлое 0,5", "quantity": 1, "price": 12000, "sum": 12000, "nds": 11}]}}
        apply_to_receipt(db, r, ticket, force_items=True)
        db.commit()
        assert policy.FORBIDDEN_GOODS in (db.get(Receipt, rid).policy_flags or "")
    card = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
    assert [h["name"] for h in card["policy_goods"]] == ["Пиво светлое 0,5"]
    assert card["policy_goods"][0]["group"] == "Алкогольные напитки"
