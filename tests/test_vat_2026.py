"""Ставки НДС: коды тега 1199 и выгрузка в 1С (v1.70.93).

С 05.01.2026 в табл. 8 ФФД добавлены коды 11 — НДС 22 % и 12 — расчётная 22/122
(приказ ФНС от 08.12.2025 № ЕД-7-20/1050@), с 01.01.2025 — 7 (5 %), 8 (7 %), 9 (5/105),
10 (7/107). В позициях из фискальных данных хранится код, у ручных — ставка. До 1.70.93
выгрузка в 1С писала «НДС» + значение как есть: «НДС1» вместо «НДС20», «НДС11» вместо
«НДС22», а XML — голый код. Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from app.database import SessionLocal
from app.models import Company, Receipt, ReceiptItem
from app.services import exporter, fns_refs
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity, _registry

companies = _base.companies


@pytest.mark.parametrize(("code", "rate"), [
    ("1", "20"), ("2", "10"), ("3", "20/120"), ("4", "10/110"), ("5", "0"), ("6", "none"),
    ("7", "5"), ("8", "7"), ("9", "5/105"), ("10", "7/107"), ("11", "22"), ("12", "22/122"),
    (11, "22"), ("11.0", "22"),
])
def test_tag_1199_codes(code, rate):
    assert fns_refs.vat_canonical(code, coded=True) == rate


def test_ambiguous_values_depend_on_source():
    # 5, 7 и 10 бывают и кодом, и ставкой: без источника — ставка (ручной ввод)
    assert [fns_refs.vat_canonical(v) for v in ("5", "7", "10")] == ["5", "7", "10"]
    # коды, которые ставками не бывают, расшифровываются всегда
    assert [fns_refs.vat_canonical(v) for v in ("1", "11", "12")] == ["20", "22", "22/122"]
    assert [fns_refs.vat_canonical(v) for v in (None, "", "none", "20", "22/122")] == \
        ["none", "none", "none", "20", "22/122"]
    assert fns_refs.nds_name("11", coded=True) == "22 %" and fns_refs.nds_name("12") == "22/122"
    assert fns_refs.nds_name("10") == "значение 10 (ставка или код)"


def _receipt(db, company_id, source, rates):
    fn, fd, fp = _identity()
    r = _registry(db, fn, fd, fp, company=db.get(Company, company_id))
    r.details_source = source
    r.items.clear()
    for pos, rate in enumerate(rates):
        r.items.append(ReceiptItem(name=f"Позиция {pos}", quantity=1, price=100, total=100,
                                   vat_rate=rate, vat_sum=0, position=pos))
    db.flush()
    return r


def test_export_maps_codes_and_rates_to_1c(companies):
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        fiscal = _receipt(db, c1, "fns_api", ["11", "12", "1", "10", "6"])
        manual = _receipt(db, c1, "", ["10", "20", "none"])
        ed = json.loads(exporter.build_enterprise_data_json([fiscal, manual], [], "ПоступлениеТоваровУслуг"))
        rates = [row["СтавкаНДС"] for doc in _docs(ed) for row in doc["Товары"]]
        assert rates == ["НДС22", "НДС22_122", "НДС20", "НДС7_107", "БезНДС", "НДС10", "НДС20", "БезНДС"]
        xml = exporter.build_enterprise_data_xml([fiscal], [], "ПоступлениеТоваровУслуг")
        xml = xml if isinstance(xml, str) else xml.decode("utf-8")
        assert [el.text for el in ET.fromstring(xml).iter() if el.tag.endswith("СтавкаНДС")] == \
            ["НДС22", "НДС22_122", "НДС20", "НДС7_107", "БезНДС"]
        push = exporter.build_push_payload([fiscal], [], "ПоступлениеТоваровУслуг")
        assert [it["vat_rate"] for it in json.loads(json.dumps(push, default=str))["receipts"][0]["items"]] == \
            ["22", "22/122", "20", "7/107", "none"]
        db.rollback()


def _docs(ed):
    """Документы пакета EnterpriseData JSON — где бы ни лежал их список."""
    found = []
    def walk(node):
        if isinstance(node, dict):
            if "Товары" in node:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(ed)
    return found


def test_card_edit_turns_codes_into_rates(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid = _receipt(db, c1, "fns_api", ["11", "2"]).id
        db.commit()
    items = [{"name": "Гипсокартон", "quantity": 1, "price": 100, "total": 100, "vat_rate": "11", "vat_sum": 18.03},
             {"name": "Хлеб", "quantity": 1, "price": 50, "total": 50, "vat_rate": "2", "vat_sum": 4.55}]
    assert client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"items": items}).status_code == 200
    with SessionLocal() as db:
        r = db.get(Receipt, rid)
        assert r.details_source == "manual_edit"
        assert [(it.vat_rate, it.vat_sum) for it in r.items] == [("22", 18.03), ("10", 4.55)]
        assert [exporter.vat_1c(it, r) for it in r.items] == ["НДС22", "НДС10"]
