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
    assert printupd.currency_label("643") == "Российский рубль, код - 643"  # так печатает бланк ЭДО
    assert printupd.currency_label("") == "Российский рубль, код - 643"


# ------------------------------------------------------------------ значения по умолчанию
def test_defaults_from_document():
    doc = _doc()
    values = printupd.defaults(doc)
    assert values["status"] == "1"
    assert values["consignee"] == "ООО «Ромашка-Строй»"
    assert values["shipper"] == "он же"
    assert values["shipment_doc"] == "№ п/п 1–2 № УТ-215 от 14 октября 2026 г."
    assert values["currency"] == "Российский рубль, код - 643"
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
    """Бланк один в один: шапка (2)–(8), 16 граф, «Всего к оплате», подписи, блок передачи товара."""
    page = printupd.html(_doc())
    assert page.startswith("<!doctype html>")
    assert "<title>УПД № УТ-215 от 14.10.2026</title>" in page
    for needle in ("Универсальный передаточный документ", "(2а)", "(2б)", "(5а)", "(6б)", "(7)", "(8)",
                   "Всего к оплате", "6 100,00", "1 830,00", "6 500,00", "1 430,00", "7 930,00", "22%",
                   "без акциза", "CEM-500", "Руководитель организации",
                   "или иное уполномоченное лицо", "Основание передачи (сдачи)/получения приемки",
                   "Товар (груз) передал/услуги, результаты работ, права сдал",
                   "Товар (груз) получил/услуги, результаты работ, права принял",
                   "Ответственный за правильность оформления факта хозяйственной жизни",
                   "Наименование экономического субъекта — составителя документа",
                   "ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ", "ОТПРАВЛЕНО", "УТВЕРЖДЕНО",
                   "(банковские реквизиты продавца)", "(банковские реквизиты покупателя)",
                   "к постановлению Правительства Российской Федерации от 26.12.2011 № 1137",
                   "от 23.01.2026 № 26", "/js/primary_print_page.js"):
        assert needle in page, needle
    assert "постановления от 19.08.2017 № 981" not in page          # редакция формы — 2026 года
    assert page.count("<tr") >= 2 + 16 + 1


def test_table_has_sixteen_columns_of_approved_form():
    """В бланке 16 граф: раньше не хватало 10, 10а и 11 (страна происхождения и прослеживаемость)."""
    assert printupd.COLUMNS == ("А", "1", "1а", "1б", "2", "2а", "3", "4", "5", "6", "7", "8", "9",
                                "10", "10а", "11")
    assert len(printupd.COLUMN_WIDTHS) == 16 and abs(sum(printupd.COLUMN_WIDTHS) - 813.2) < 1
    page = printupd.html(_doc())
    for number in printupd.COLUMNS:
        assert f">{number}</th>" in page, number
    for title in ("Страна происхож-<br>дения товара", "Цифро-<br>вой код", "Краткое<br>наимено-<br>вание",
                  "Регистрационный<br>номер декларации на"):
        assert title in page, title


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
    assert "Цемент М500, мешок 50 кг" in page and "Всего к оплате" in page
    assert "770401001" in page                       # КПП покупателя из синтетического документа
    assert "Российский рубль, код - 643" in page


# ------------------------------------------------------------------ v1.73.4: реквизиты из XML в бланке
REAL_EDO = """<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="ON_NSCHFDOPPR_2be66c22_2BM-7704217370-774301001_20260908_1eb2e289_0_0_0_0_0_00" ВерсФорм="5.03">
  <Документ КНД="1115131" Функция="СЧФДОП" ДатаИнфПр="08.09.2026" НаимЭконСубСост="ООО &quot;Интернет Решения&quot;">
    <СвСчФакт НомерДок="118050913" ДатаДок="07.09.2026">
      <СвПрод><ИдСв><СвЮЛУч НаимОрг="Интернет Решения, ООО" ИННЮЛ="7704217370" КПП="997750001"/></ИдСв>
        <Адрес><АдрИнф КодСтр="643" НаимСтран="Россия" АдрТекст="123112, г. Москва, Пресненская наб., д. 10"/></Адрес>
      </СвПрод>
      <ГрузОт><ОнЖе>он же</ОнЖе></ГрузОт>
      <ГрузПолуч><ИдСв><СвЮЛУч НаимОрг="ООО &quot;ЯМАСТЕР&quot;" ИННЮЛ="4712030034" КПП="471201001"/></ИдСв>
        <Адрес><АдрИнф КодСтр="643" АдрТекст="ПРИОЗЕРСК, 188760, Ленинградская Область, ул. Гастелло, д. 4"/></Адрес>
      </ГрузПолуч>
      <ДокПодтвОтгрНом РеквНаимДок="УПД" РеквНомерДок="118050913" РеквДатаДок="07.09.2026"/>
      <СвПокуп><ИдСв><СвЮЛУч НаимОрг="ООО &quot;ЯМАСТЕР&quot;" ИННЮЛ="4712030034" КПП="471201001"/></ИдСв>
        <Адрес><АдрИнф КодСтр="643" АдрТекст="Ленинградская обл., г. Приозерск, ул. Гастелло, д. 4"/></Адрес>
      </СвПокуп>
      <ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/>
    </СвСчФакт>
    <ТаблСчФакт>
      <СведТов НомСтр="1" НаимТов="Прокладка трапециевидная" ОКЕИ_Тов="796" НаимЕдИзм="шт" КолТов="1"
                ЦенаТов="155.74" СтТовБезНДС="155.74" НалСт="22%" СтТовУчНал="190.00">
        <ДопСведТов КодТов="трапеция"/><Акциз><БезАкциз>без акциза</БезАкциз></Акциз>
        <СумНал><СумНал>34.26</СумНал></СумНал>
      </СведТов>
      <ВсегоОпл СтТовБезНДСВсего="155.74" СтТовУчНалВсего="190.00"><СумНалВсего><СумНал>34.26</СумНал></СумНалВсего></ВсегоОпл>
    </ТаблСчФакт>
    <СвПродПер><СвПер СодОпер="Товары переданы" ДатаПер="07.09.2026">
      <ОснПер РеквНаимДок="Счет-Оферта" РеквНомерДок="72000648-0275" РеквДатаДок="02.09.2026"/></СвПер></СвПродПер>
    <Подписант Должн="Руководитель группы" ТипПодпис="1" СпосПодтПолном="4">
      <ФИО Фамилия="Мельникова" Имя="Анна" Отчество="Валерьевна"/>
    </Подписант>
  </Документ>
</Файл>""".encode("cp1251")


def test_parse_fills_print_requisites_from_edo_xml():
    """Адреса, основание передачи, документ об отгрузке и подписант — из самого XML, не руками."""
    from app.primary import upd

    parsed = upd.parse(REAL_EDO)
    assert parsed["file_id"].startswith("ON_NSCHFDOPPR_")
    assert parsed["seller_print"]["address"].startswith("123112, г. Москва")
    assert parsed["buyer_print"]["address"].startswith("Ленинградская обл.")
    assert parsed["shipper"] == "он же"
    assert parsed["consignee"].startswith('ООО "ЯМАСТЕР"')
    assert parsed["shipment_doc"] == "УПД № 118050913 от 07.09.2026"
    assert parsed["basis"] == "Счет-Оферта № 72000648-0275 от 02.09.2026"
    assert parsed["signer"] == {"role": "Руководитель группы", "name": "Мельникова Анна Валерьевна"}


def test_form_shows_requisites_from_xml_on_the_blank():
    """Бланк печатает то, что пришло в XML: адреса сторон, основание, подписанта и документ об отгрузке."""
    from app.primary import upd

    doc = _doc(print_json=printupd.dump({}, printupd.auto_from(upd.parse(REAL_EDO))))
    values = printupd.form(doc)
    assert values["basis"] == "Счет-Оферта № 72000648-0275 от 02.09.2026"
    assert values["transfer_name"] == "Мельникова Анна Валерьевна"
    assert values["seller_address"].startswith("123112")
    page = printupd.html(doc)
    for needle in ("123112, г. Москва", "Ленинградская обл., г. Приозерск",
                   "Счет-Оферта № 72000648-0275 от 02.09.2026", "Мельникова Анна Валерьевна",
                   "УПД № 118050913 от 07.09.2026"):
        assert needle in page, needle


def test_saved_edits_do_not_erase_xml_requisites():
    """Правка одного поля не стирает реквизиты из XML: они лежат рядом под ключом «auto»."""
    from app.primary import upd

    doc = _doc(print_json=printupd.dump({"seller_bank": "Р/с 40802…"}, printupd.auto_from(upd.parse(REAL_EDO))))
    values = printupd.form(doc)
    assert values["seller_bank"] == "Р/с 40802…"
    assert values["basis"] == "Счет-Оферта № 72000648-0275 от 02.09.2026"
    assert printupd.stored(doc) == {"seller_bank": "Р/с 40802…"}     # «auto» не считается правкой


def test_entrepreneur_requisites_for_act_only_form():
    """Статус 2 (только акт): ИП в подписях, ОГРНИП и дата присвоения — как в бланке ИП Федорова."""
    from app.primary import upd

    xml = ('<?xml version="1.0" encoding="windows-1251"?><Файл ИдФайл="ON_NSCHFDOPPR_ip" ВерсФорм="5.03">'
           '<Документ КНД="1115131" Функция="ДОП"><СвСчФакт НомерДок="32" ДатаДок="06.04.2026">'
           '<СвПрод><ИдСв><СвИП ИННФЛ="532113502270"><ФИО Фамилия="Федоров" Имя="Андрей" Отчество="Сергеевич"/>'
           '<ОГРНИП ДатаОГРНИП="18.05.2018">318532100013002</ОГРНИП></СвИП></ИдСв></СвПрод>'
           '<СвПокуп><ИдСв><СвЮЛУч НаимОрг="ООО &quot;ЯМАСТЕР&quot;" ИННЮЛ="4712030034" КПП="471201001"/></ИдСв>'
           '</СвПокуп></СвСчФакт></Документ></Файл>').encode("cp1251")
    parsed = upd.parse(xml)
    values = printupd.auto_from(parsed)
    assert values["seller_ogrnip"] == "318532100013002, 18.05.2018"
    assert values["signer_entrepreneur"] == "Федоров Андрей Сергеевич"
    assert printupd.status_title("ДОП") == "2"


def test_row_5b_is_always_on_the_blank_after_5a():
    """Строка (5б) есть в бланке всегда, сразу после (5а): пустая печатается прочерком.

    В PDF из ЭДО нумерация шапки идёт (2), (2а), (2б), (3), (4), (5), (5а), (5б), (6)…
    Раньше (5б) появлялась только при заполненном поле — и нумерация «прыгала».
    """
    page = printupd.html(_doc())
    assert page.index(">(5а)<") < page.index(">(5б)<") < page.index(">(6)<")
    assert "К счёту-фактуре (счетам-фактурам)" in page
    filled = printupd.html(_doc(print_json=printupd.dump({"payment_advance": "№ 77 от 01.10.2026"})))
    assert "№ 77 от 01.10.2026" in filled and filled.count("(5б)") == 1


def test_long_row_5b_label_takes_full_width():
    """Длинная подпись (5б) печатается во всю ширину шапки, значение — справа у номера строки.

    Иначе подпись либо уезжает за правый край бланка, либо ломается на десять строк.
    """
    page = printupd.html(_doc())
    assert 'class="hl hl-wrap" colspan="2"' in page
    assert 'class="hv-r"' in page


def test_seller_subject_uses_full_name_from_document():
    """«Составитель документа» — из Документ/@НаимЭконСубСост, как в PDF из ЭДО."""
    from app.primary import upd

    parsed = upd.parse(REAL_EDO)
    assert parsed["subject"] == 'ООО "Интернет Решения"'
    values = printupd.auto_from(parsed)
    assert values["seller_subject"] == 'ООО "Интернет Решения"'
    assert values["buyer_subject"] == 'ООО "ЯМАСТЕР", ИНН 4712030034'


def test_chief_signature_is_electronic_when_signer_known():
    """Подписант известен → в блоке подписей «электронно» и его ФИО, как в PDF из ЭДО."""
    from app.primary import upd

    parsed = upd.parse(REAL_EDO)
    values = printupd.auto_from(parsed)
    assert values["signer_chief"] == "Мельникова Анна Валерьевна"
    assert values["signer_chief_role"] == "Руководитель группы"
    page = printupd.html(_doc(print_json=printupd.dump({}, values)))
    assert page.count("электронно") >= 3          # руководитель, ИП и обе строки передачи
    assert "Мельникова Анна Валерьевна" in page


def test_partial_save_does_not_erase_saved_fields(client, firms):
    """Частичный PUT (одно поле) не стирает остальные правки — их зовут боты и скрипты."""
    _, a, _ = firms
    doc_id = _doc_id(client, a)
    first = client.put(f"/api/v1/primary/{doc_id}/print-form", headers=a["hdr"],
                       json={"basis": "Договор № 7 от 01.02.2026", "sheets": "2"})
    assert first.status_code == 200, first.text
    second = client.put(f"/api/v1/primary/{doc_id}/print-form", headers=a["hdr"],
                        json={"seller_bank": "Р/с 40702810400000000123"})
    assert second.status_code == 200, second.text
    form = client.get(f"/api/v1/primary/{doc_id}/print-form", headers=a["hdr"]).json()
    assert form["saved"]["seller_bank"] == "Р/с 40702810400000000123"
    assert form["saved"]["basis"] == "Договор № 7 от 01.02.2026"      # не затёрто
    assert form["saved"]["sheets"] == "2"
    # явная очистка из окна печати сохраняется: поле пришло пустым, значит бухгалтер его стёр
    cleared = client.put(f"/api/v1/primary/{doc_id}/print-form", headers=a["hdr"], json={"sheets": ""})
    assert cleared.json()["form"]["sheets"] == "1"                    # вернулось значение по умолчанию
    assert cleared.json()["form"]["basis"] == "Договор № 7 от 01.02.2026"
