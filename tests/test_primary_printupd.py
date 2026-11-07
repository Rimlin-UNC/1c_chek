"""Печатная форма УПД (T-62, v1.70.114): сборка, заполняемые поля, API и изоляция компаний.

Проверяем то, что видит бухгалтер: реквизиты и строки из записи реестра, «Всего к оплате (9)»,
подписи, правку заполняемых полей и сохранение этих правок к документу. Только синтетика (ADR-14).
"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.primary import printupd
from app.primary.models import PrimaryDocument, PrimaryDocumentLine
from tests.conftest import login
from tests.test_primary_upd import _for, _upload
from tests.test_primary_upd import firms as _shared_firms  # фикстура компаний и бухгалтеров
from tests.test_v1140 import _mk_user


@pytest.fixture(name="firms")
def firms_fixture(_shared_firms):  # noqa: F811 — импортированная фикстура-обёртка
    """Та же фикстура под своим именем — ruff не видит переопределения импорта."""
    return _shared_firms


def _doc(**kw) -> PrimaryDocument:
    """Документ в памяти: html() и defaults() — чистые функции, база им не нужна."""
    doc = PrimaryDocument()
    doc.id = "doc-1"
    doc.kind = "УПД"
    doc.function = kw.get("function", "СЧФДОП")
    doc.format_version = "5.03"
    doc.doc_number = kw.get("number", "УТ-215")
    doc.doc_date = kw.get("day", date(2026, 10, 14))
    doc.correction_number = kw.get("correction_number", "")
    doc.correction_date = kw.get("correction_date")
    doc.seller_name, doc.seller_inn, doc.seller_kpp = kw.get("seller", ("ООО «СтройБаза»", "5001007329", "500101001"))
    doc.buyer_name, doc.buyer_inn, doc.buyer_kpp = kw.get("buyer", ("ООО «Ромашка-Строй»", "7704123450", "770401001"))
    doc.currency = "643"
    doc.total_without_vat = 6500.0
    doc.vat_total = 1430.0
    doc.total_with_vat = 7930.0
    doc.print_json = kw.get("print_json", "")
    doc.lines = [PrimaryDocumentLine(**line) for line in kw.get("lines", [
        {"position": 1, "name": "Цемент М500, мешок 50 кг", "unit_code": "796", "unit_name": "шт",
         "quantity": 10.0, "price": 500.0, "sum_without_vat": 5000.0, "vat_rate": "22", "vat_sum": 1100.0,
         "sum_with_vat": 6100.0, "item_code": "CEM-500"},
        {"position": 2, "name": "Доставка до объекта", "unit_code": "796", "unit_name": "шт",
         "quantity": 1.0, "price": 1500.0, "sum_without_vat": 1500.0, "vat_rate": "22", "vat_sum": 330.0,
         "sum_with_vat": 1830.0, "item_code": ""},
    ])]
    return doc


# ------------------------------------------------------------------ формат значений
def test_money_and_qty_like_paper_form():
    assert printupd.money(6100.0) == "6 100,00"
    assert printupd.money(105530.5) == "105 530,50"
    assert printupd.money(None) == "—"
    assert printupd.qty(10.0) == "10"
    assert printupd.qty(1.25) == "1,25"
    assert printupd.qty(1234.5) == "1 234,5"
    assert printupd.qty(None) == "—"


def test_date_in_words_and_vat_labels():
    assert printupd.date_words(date(2026, 10, 14)) == "14 октября 2026 г."
    assert printupd.date_words(date(2026, 1, 31)) == "31 января 2026 г."
    assert printupd.vat_label("22") == "22%"
    assert printupd.vat_label("22/122") == "22/122%"
    assert printupd.vat_label("none") == "без НДС"
    assert printupd.vat_label("") == "без НДС"
    assert printupd.currency_label("643") == "Российский рубль, 643"
    assert printupd.currency_label("") == "Российский рубль, 643"


# ------------------------------------------------------------------ значения по умолчанию
def test_defaults_from_document():
    doc = _doc()
    values = printupd.defaults(doc)
    assert values["status"] == "1"
    assert values["consignee"] == "ООО «Ромашка-Строй»"
    assert values["shipper"] == "он же"
    assert values["shipment_doc"] == "№ п/п 1–2 № УТ-215 от 14 октября 2026 г."
    assert values["currency"] == "Российский рубль, 643"
    assert values["sheets"] == "1"
    # «передаточный документ (акт)» без счёта-фактуры — статус 2
    assert printupd.defaults(_doc(function="ДОП"))["status"] == "2"
    assert printupd.defaults(_doc(function="СЧФ"))["status"] == "1"


def test_clean_keeps_only_known_fields_and_limits():
    saved = printupd.clean({
        "basis": "  Договор поставки № 12 от 01.02.2026  ", "status": "2", "sheets": "3",
        "trigger": "DROP TABLE primary_documents", "seller_address": "А" * 400, "signer_chief": None,
    })
    assert saved["basis"] == "Договор поставки № 12 от 01.02.2026"
    assert saved["status"] == "2" and saved["sheets"] == "3"
    assert "trigger" not in saved
    assert len(saved["seller_address"]) == printupd.LIMITS["seller_address"]
    assert saved["signer_chief"] == ""
    assert printupd.clean({"status": "3", "sheets": "много"}) == {}


def test_form_merges_saved_over_document():
    doc = _doc()
    doc.print_json = printupd.dump({"shipper": "ООО «Склад»", "basis": "Договор № 7"})
    values = printupd.form(doc, printupd.clean(printupd.stored(doc)))
    assert (values["shipper"], values["basis"]) == ("ООО «Склад»", "Договор № 7")
    assert values["consignee"] == "ООО «Ромашка-Строй»"  # значение документа не затёрто
    assert printupd.clean(printupd.stored(_doc(print_json="{не json"))) == {}


# ------------------------------------------------------------------ HTML
def test_html_has_form_requisites_lines_and_totals():
    page = printupd.html(_doc())
    assert page.startswith("<!doctype html>")
    assert "<title>УПД № УТ-215 от 14.10.2026</title>" in page
    for needle in ("Универсальный передаточный документ", "(1а)", "(5а)", "(8)", "Всего к оплате (9)",
                   "6 100,00", "1 830,00", "6 500,00", "1 430,00", "7 930,00", "22%", "без акциза",
                   "CEM-500", "Документ составлен на 1 листе",
                   "Руководитель организации или иное уполномоченное лицо",
                   "Основание передачи (сдачи) / получения (приёмки)", "/js/primary_print_page.js"):
        assert needle in page, needle
    assert page.count("<tr>") >= 2 + 14 + 1                   # шапка, строки таблицы, итоги


def test_html_escapes_values_and_marks_auto_print():
    doc = _doc(seller=("ООО «<script>alert(1)</script>»", "5001007329", "500101001"),
               lines=[{"position": 1, "name": "<b>Жирный</b> цемент", "unit_code": "796", "unit_name": "шт",
                       "quantity": 1.0, "price": 100.0, "sum_without_vat": 100.0, "vat_rate": "22",
                       "vat_sum": 22.0, "sum_with_vat": 122.0, "item_code": ""}])
    page = printupd.html(doc, {"basis": "Договор <n>", "signer_chief": "<i>Иванов</i>"}, auto_print=True)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>Жирный</b>" not in page and "&lt;b&gt;Жирный&lt;/b&gt;" in page
    assert "Договор &lt;n&gt;" in page and "&lt;i&gt;Иванов&lt;/i&gt;" in page
    assert 'data-print="1"' in page
    assert 'data-print="0"' in printupd.html(doc)


def test_html_shows_correction_and_signer_entrepreneur():
    doc = _doc(correction_number="2", correction_date=date(2026, 10, 20), function="ДОП")
    page = printupd.html(doc, {"signer_entrepreneur": "ИП Петров П. П., свидетельство 77 № 123456"})
    assert "№ 2 от 20.10.2026" in page
    assert 'Статус: <span class="status-code">2</span>' in page
    assert "Индивидуальный предприниматель" in page
    assert "ИП Петров П. П." in page


# ------------------------------------------------------------------ API
def _doc_id(client, firm) -> str:
    res = _upload(client, firm["hdr"], ("upd.xml", _for(firm, number=f"PR-{uuid4().hex[:6]}")))
    assert res.status_code == 200, res.text
    return res.json()["results"][0]["document"]["id"]


def test_print_form_api_returns_defaults_and_saves_edits(client, firms):
    _, a, _ = firms
    doc_id = _doc_id(client, a)
    res = client.get(f"/api/v1/primary/{doc_id}/print-form", headers=a["hdr"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["saved"] == {} and body["form"]["status"] == "1"
    assert body["fields"] == list(printupd.FIELDS)
    assert body["title"].startswith("УПД №")

    payload = {"status": "2", "seller_address": "141400, Московская обл., г. Тестовск, ул. Синтетическая, д. 1",
               "buyer_address": "119000, г. Москва, ул. Проверочная, д. 2", "shipper": "он же",
               "consignee": "ООО «Покупатель», 119000, г. Москва", "payment_doc": "№ 18 от 12.10.2026",
               "shipment_doc": "№ п/п 1–2 № УТ-15 от 10.03.2026", "contract_id": "",
               "currency": "Российский рубль, 643", "basis": "Договор поставки № 12 от 01.02.2026",
               "sheets": "1", "signer_chief_role": "Директор", "signer_chief": "Тестов Т. Т.",
               "signer_accountant_role": "Главный бухгалтер", "signer_accountant": "Проверкина П. П.",
               "signer_entrepreneur": ""}
    saved = client.put(f"/api/v1/primary/{doc_id}/print-form", json=payload, headers=a["hdr"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["form"]["basis"] == "Договор поставки № 12 от 01.02.2026"

    again = client.get(f"/api/v1/primary/{doc_id}/print-form", headers=a["hdr"]).json()
    assert again["saved"]["basis"] == payload["basis"] and again["form"]["seller_address"] == payload["seller_address"]

    page = client.get(f"/api/v1/primary/{doc_id}/print", headers=a["hdr"])
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Договор поставки № 12 от 01.02.2026" in page.text and "Тестов Т. Т." in page.text
    auto = client.get(f"/api/v1/primary/{doc_id}/print?print=1", headers=a["hdr"])
    assert 'data-print="1"' in auto.text


def test_print_form_isolation_and_roles(client, firms):
    hdr, a, b = firms
    doc_id = _doc_id(client, a)
    # чужой бухгалтер той же организации документ не видит
    assert client.get(f"/api/v1/primary/{doc_id}/print", headers=b["hdr"]).status_code == 404
    assert client.put(f"/api/v1/primary/{doc_id}/print-form", json={"basis": "чужое"},
                      headers=b["hdr"]).status_code == 404
    assert client.get(f"/api/v1/primary/{doc_id}/print-form", headers=b["hdr"]).status_code == 404
    # сотрудник без роли бухгалтера — нет доступа
    user_hdr = _mk_user(client, hdr, a["id"], f"prim_plain_{uuid4().hex[:8]}")
    assert client.get(f"/api/v1/primary/{doc_id}/print", headers=user_hdr).status_code == 403


def test_print_form_saves_json_in_document_column(client, firms):
    from app.database import SessionLocal
    from app.primary.models import PrimaryDocument as Doc

    _, a, _ = firms
    doc_id = _doc_id(client, a)
    client.put(f"/api/v1/primary/{doc_id}/print-form", json={"basis": "Договор № 5"}, headers=a["hdr"])
    with SessionLocal() as db:
        doc = db.get(Doc, doc_id)
        assert printupd.clean(printupd.stored(doc))["basis"] == "Договор № 5"
        assert "№ 5" in doc.print_json


def test_printed_page_shows_lines_of_real_document(client, firms):
    _, a, _ = firms
    doc_id = _doc_id(client, a)
    page = client.get(f"/api/v1/primary/{doc_id}/print", headers=a["hdr"]).text
    assert "Цемент М500, мешок 50 кг" in page and "Всего к оплате (9)" in page
    assert "770401001" in page                       # КПП покупателя из синтетического документа
    assert "Российский рубль, 643" in page
