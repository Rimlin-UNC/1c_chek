"""Первичка: выгрузка в 1С (T-60, v1.70.96).

Файл УПД 5.03 для типовой кнопки 1С «Загрузить → Из файла» собирается из реквизитов реестра
и проверяется по официальной схеме ФНС; пакет EnterpriseData — как у выгрузки чеков; API для
1С — с токеном компании (T-57). Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from uuid import uuid4

import pytest

from app.primary import export1c, schema, upd
from app.primary.models import PrimaryDocument, PrimaryDocumentLine
from tests import test_primary_upd as _upd
from tests.primary_samples import SELLER_INN, broken, inn10, inn12
from tests.test_primary_upd import _for, _upload

NOW = datetime(2026, 3, 11, 9, 30, 0, tzinfo=export1c.MSK)
# фикстура компаний и бухгалтеров — общая с tests/test_primary_upd.py
firms = _upd.firms


def _doc(**kw) -> PrimaryDocument:
    doc = PrimaryDocument(
        id=str(uuid4()), function="СЧФДОП", kind="УПД", doc_number="УТ-15", doc_date=date(2026, 3, 10),
        seller_name="ООО «СтройБаза»", seller_inn=SELLER_INN, seller_kpp="500101001",
        buyer_name="ООО «Ромашка-Строй»", buyer_inn=inn10(), buyer_kpp="770401001", currency="643",
        total_without_vat=6500.0, vat_total=1430.0, total_with_vat=7930.0, correction_number="", file_id="",
        source="xml", status="ok")
    doc.lines = [
        PrimaryDocumentLine(position=1, name="Цемент М500, мешок 50 кг", unit_code="796", unit_name="шт", quantity=10,
                            price=500, sum_without_vat=5000, vat_rate="22", vat_sum=1100, sum_with_vat=6100,
                            item_code="CEM-500"),
        PrimaryDocumentLine(position=2, name="Доставка до объекта", unit_code="796", unit_name="шт", quantity=1,
                            price=1500, sum_without_vat=1500, vat_rate="22", vat_sum=330, sum_with_vat=1830)]
    for key, value in kw.items():
        setattr(doc, key, value)
    return doc


def _valid(data: bytes) -> dict:
    result = schema.validate(data, "5.03")
    assert result["checked"] and result["valid"], result["errors"]
    return upd.parse(data)


# ------------------------------------------------------------------ файл УПД 5.03
def test_upd_file_passes_official_schema_and_reads_back():
    doc = _doc()
    name, data = export1c.upd_xml(doc, NOW)
    assert name == f"ON_NSCHFDOPPR_{doc.buyer_inn}_{SELLER_INN}_20260310_{doc.id}.xml"
    assert data.startswith(b'<?xml version="1.0" encoding="windows-1251"?>')
    back = _valid(data)
    assert (back["number"], back["date"], back["function"], back["format_version"]) == ("УТ-15", date(2026, 3, 10),
                                                                                         "СЧФДОП", "5.03")
    assert back["seller"] == {"kind": "ЮЛ", "name": "ООО «СтройБаза»", "inn": SELLER_INN, "kpp": "500101001"}
    assert back["buyer"]["inn"] == doc.buyer_inn and back["totals"] == {"without_vat": 6500.0, "with_vat": 7930.0,
                                                                       "vat": 1430.0}
    assert [(ln["name"], ln["quantity"], ln["price"], ln["vat_rate"], ln["vat_sum"], ln["item_code"])
            for ln in back["lines"]] == [("Цемент М500, мешок 50 кг", 10.0, 500.0, "22", 1100.0, "CEM-500"),
                                         ("Доставка до объекта", 1.0, 1500.0, "22", 330.0, "")]
    # проверки реестра на собранном файле — без ошибок (покупатель — своя компания)
    assert upd.status_of(upd.check(back, company_inn=doc.buyer_inn, today=date(2026, 3, 11))) == "ok"
    text = data.decode("cp1251")
    assert 'ДатаИнфПр="11.03.2026" ВремИнфПр="09.30.00"' in text and "Акциз" not in text and "Адрес" not in text
    assert export1c.SIGNER_NOTE in text and 'Фамилия="Не указана"' in text


@pytest.mark.parametrize(("kw", "check"), [
    ({"function": "СЧФ"}, lambda b: b["function"] == "СЧФ"),
    ({"function": "ДОП"}, lambda b: b["function"] == "ДОП"),
    ({"correction_number": "2", "correction_date": date(2026, 3, 12)},
     lambda b: (b["correction_number"], b["correction_date"]) == ("2", date(2026, 3, 12))),
    ({"seller_name": "ИП Иванов Иван Иванович", "seller_kpp": ""},
     lambda b: b["seller"]["kind"] == "ИП" and b["seller"]["name"] == "ИП Иванов Иван Иванович"),
    ({"buyer_name": "Петров Пётр", "buyer_inn": "", "buyer_kpp": ""},
     lambda b: b["buyer"] == {"kind": "ФЛ", "name": "Петров Пётр", "inn": "", "kpp": ""}),
    ({"currency": "978"}, lambda b: b["currency"] == "978"),
])
def test_upd_file_variants_pass_schema(kw, check):
    if kw.get("seller_name", "").startswith("ИП"):
        kw["seller_inn"] = inn12()
    assert check(_valid(export1c.upd_xml(_doc(**kw), NOW)[1]))


def test_without_vat_odd_values_and_symbols():
    doc = _doc(vat_total=0.0, total_with_vat=6500.0)
    for ln in doc.lines:
        ln.vat_rate, ln.vat_sum, ln.sum_with_vat = "none", None, ln.sum_without_vat
    doc.lines[0].name = "Плита 2 м³ \x01 ☺"           # вне windows-1251 — ссылками, управляющий символ — долой
    doc.lines[0].unit_code, doc.lines[0].quantity = "м3", 0      # не код ОКЕИ и нулевое количество — не пишутся
    back = _valid(export1c.upd_xml(doc, NOW)[1])
    assert back["lines"][0]["name"] == "Плита 2 м³ ☺" and back["lines"][0]["quantity"] is None
    assert [ln["vat_rate"] for ln in back["lines"]] == ["none", "none"] and back["totals"]["vat"] == 0.0
    assert "<БезНДС>без НДС</БезНДС></СумНалВсего>" in export1c.upd_xml(doc, NOW)[1].decode("cp1251")


def test_fraction_rates_and_missing_vat_sum_is_derived():
    doc = _doc()
    doc.lines[0].vat_rate, doc.lines[1].vat_rate = "22/122", "agent"
    doc.lines[0].vat_sum = None                         # сумма НДС выводится из сумм с НДС и без
    back = _valid(export1c.upd_xml(doc, NOW)[1])
    assert [ln["vat_rate"] for ln in back["lines"]] == ["22/122", "agent"] and back["lines"][0]["vat_sum"] == 1100.0


@pytest.mark.parametrize(("kw", "reason"), [
    ({"doc_number": ""}, "нет номера или даты"),
    ({"doc_date": None}, "нет номера или даты"),
    ({"seller_inn": "", "seller_name": "Foreign Trading Ltd"}, "у продавца нет ИНН"),
    ({"seller_inn": "0012345678"}, "ИНН продавца 0012345678 не по формату"),
    ({"seller_kpp": "12"}, "КПП продавца 12 не по формату"),
    ({"seller_name": ""}, "нет наименования продавца"),
    ({"seller_name": "ИП Иванов", "seller_inn": "500100732259"}, "нет фамилии и имени"),
    ({"currency": "999"}, "валюта с кодом 999"),
    ({"correction_number": "1а", "correction_date": date(2026, 3, 12)}, "исправления не по формату"),
    ({"function": "КСЧФ"}, "не выгружается"),
    ({"lines": []}, "нет строк"),
])
def test_documents_that_cannot_be_honest_files_are_skipped_with_reason(kw, reason):
    with pytest.raises(export1c.ExportSkip, match=reason):
        export1c.upd_xml(_doc(**kw), NOW)


def test_negative_sums_and_unknown_rate_are_skipped():
    doc = _doc()
    doc.lines[1].sum_without_vat = -10.0
    with pytest.raises(export1c.ExportSkip, match="отрицательная сумма"):
        export1c.upd_xml(doc, NOW)
    doc = _doc()
    doc.lines[0].vat_rate = "13"
    with pytest.raises(export1c.ExportSkip, match="ставка НДС «13» не из перечня"):
        export1c.upd_xml(doc, NOW)


def test_prepare_reports_each_document_and_file_id_is_stable():
    good, bad = _doc(), _doc(doc_number="")
    prepared = export1c.prepare([good, bad], NOW)
    assert prepared[0]["name"].endswith(".xml") and prepared[0]["xsd"] is True
    assert prepared[1]["reason"] == "нет номера или даты документа"
    later = datetime(2026, 5, 1, 12, 0, tzinfo=export1c.MSK)
    assert export1c.upd_xml(good, later)[0] == prepared[0]["name"]          # повторная выгрузка — тот же ИдФайл
    archive = zipfile.ZipFile(io.BytesIO(export1c.zip_files([prepared[0]])))
    assert archive.namelist() == [prepared[0]["name"]]


def test_enterprise_data_objects_for_upd_invoice_and_primary_only():
    doc = _doc()
    objects = export1c.ed_objects(doc)
    assert [o["ИмяПКО"] for o in objects] == ["ПоступлениеТоваровУслуг", "СчетФактураПолученный"]
    receipt, invoice = objects
    assert receipt["КлючевыеСвойства"]["Ссылка"] == f"ymaster-primary-{doc.id}"
    assert receipt["КлючевыеСвойства"]["Контрагент"]["ИНН"] == SELLER_INN and receipt["СуммаДокумента"] == 7930.0
    assert [r["СтавкаНДС"] for r in receipt["Товары"]] == ["НДС22", "НДС22"]
    assert invoice["ДокументОснование"] == {"ИмяПКО": "ПоступлениеТоваровУслуг", "Ссылка": f"ymaster-primary-{doc.id}"}
    assert invoice["УПД"] is True and invoice["СуммаНДС"] == 1430.0 and invoice["КодВидаОперации"] == "01"
    assert [o["ИмяПКО"] for o in export1c.ed_objects(_doc(function="ДОП"))] == ["ПоступлениеТоваровУслуг"]
    only_invoice = export1c.ed_objects(_doc(function="СЧФ"))
    assert [o["ИмяПКО"] for o in only_invoice] == ["СчетФактураПолученный"] and only_invoice[0]["УПД"] is False
    assert export1c.vat_1c("none") == "БезНДС" and export1c.vat_1c("22/122") == "НДС22_122"
    package = json.loads(export1c.ed_json([doc]))
    assert package["ФорматВерсии"] == "1.3" and len(package["Данные"]["Контейнер"]) == 2
    xml = export1c.ed_xml([doc])
    assert '<Object ИмяПКО="СчетФактураПолученный">' in xml and "<СтавкаНДС>НДС22</СтавкаНДС>" in xml


# ------------------------------------------------------------------ выгрузка из программы
def _created(res) -> list[dict]:
    assert res.status_code == 200, res.text
    return [r["document"] for r in res.json()["results"] if r["result"] == "created"]


def test_export_check_download_marks_and_does_not_repeat(client, firms):
    _, a, _ = firms
    tag = uuid4().hex[:6]
    good = _created(_upload(client, a["hdr"], ("g.xml", _for(a, number=f"E-{tag}"))))[0]
    bad_xml = _for(a, number=f"X-{tag}", seller=("ООО «Ошибка»", broken(SELLER_INN), "500101001"))
    bad = _created(_upload(client, a["hdr"], ("b.xml", bad_xml)))[0]
    assert bad["status"] == "errors"
    check = client.post("/api/v1/primary/export/check", headers=a["hdr"], json={}).json()
    assert (check["count"], check["skipped"], check["errors_excluded"]) == (1, [], 1)
    res = client.post("/api/v1/primary/export", headers=a["hdr"], json={})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/xml") and res.headers["x-exported"] == "1"
    assert f'filename="ON_NSCHFDOPPR_{a["inn"]}_' in res.headers["content-disposition"]
    assert _valid(res.content)["number"] == f"E-{tag}"
    listed = client.get("/api/v1/primary?exported=true", headers=a["hdr"]).json()["items"]
    assert [d["id"] for d in listed] == [good["id"]] and listed[0]["exported_at"]
    assert bad["id"] in [d["id"] for d in client.get("/api/v1/primary?exported=false", headers=a["hdr"]).json()["items"]]
    again = client.post("/api/v1/primary/export", headers=a["hdr"], json={})
    assert again.status_code == 404 and "Нет документов для выгрузки" in again.json()["detail"]
    # отмеченный вручную выгружается повторно; документ с ошибками — только с include_errors
    repeat = client.post("/api/v1/primary/export/check", headers=a["hdr"], json={"ids": [good["id"]]}).json()
    assert (repeat["count"], repeat["already_exported"]) == (1, 1)
    both = client.post("/api/v1/primary/export", headers=a["hdr"],
                       json={"ids": [good["id"], bad["id"]], "include_errors": True})
    assert both.status_code == 200 and both.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(both.content)).namelist()
    assert len(names) == 2 and all(n.startswith("ON_NSCHFDOPPR_") for n in names)


def test_export_respects_company_scope_and_enterprise_data_formats(client, firms):
    _, a, b = firms
    tag = uuid4().hex[:6]
    doc_a = _created(_upload(client, a["hdr"], ("a.xml", _for(a, number=f"S-{tag}"))))[0]
    stranger = client.post("/api/v1/primary/export", headers=b["hdr"], json={"ids": [doc_a["id"]]})
    assert stranger.status_code == 404                               # чужой документ не виден и не отмечается
    detail = client.get(f"/api/v1/primary/{doc_a['id']}", headers=a["hdr"]).json()
    assert detail["exported_at"] is None
    res = client.post("/api/v1/primary/export", headers=a["hdr"], json={"ids": [doc_a["id"]], "format": "json"})
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/json")
    package = res.json()
    assert [o["ИмяПКО"] for o in package["Данные"]["Контейнер"]] == ["ПоступлениеТоваровУслуг",
                                                                     "СчетФактураПолученный"]
    assert package["Данные"]["Контейнер"][0]["КлючевыеСвойства"]["Номер"] == f"S-{tag}"
    xml = client.post("/api/v1/primary/export", headers=a["hdr"], json={"ids": [doc_a["id"]], "format": "xml"})
    assert xml.status_code == 200 and "<EnterpriseData" in xml.text and f"S-{tag}" in xml.text


def test_export_check_lists_reasons_for_documents_without_honest_file(client, firms):
    _, a, _ = firms
    tag = uuid4().hex[:6]
    payload = {"source": "manual", "draft": {
        "function": "СЧФДОП", "number": f"M-{tag}", "date": "2026-03-10",
        "seller": {"name": "Foreign Trading Ltd"}, "buyer": {"name": "ООО «Покупатель»", "inn": a["inn"], "kpp": ""},
        "totals": {"without_vat": 100, "vat": 0, "with_vat": 100},
        "lines": [{"name": "Образец", "quantity": 1, "price": 100, "sum_without_vat": 100, "vat_rate": "none",
                   "sum_with_vat": 100}]}}
    created = client.post("/api/v1/primary/confirm", headers=a["hdr"], json=payload)
    assert created.status_code == 200, created.text
    doc = created.json()["document"]
    check = client.post("/api/v1/primary/export/check", headers=a["hdr"],
                        json={"ids": [doc["id"]], "include_errors": True}).json()
    assert check["count"] == 0 and "у продавца нет ИНН" in check["skipped"][0]["reason"]
    assert f"M-{tag}" in check["skipped"][0]["title"]
    res = client.post("/api/v1/primary/export", headers=a["hdr"], json={"ids": [doc["id"]], "include_errors": True})
    assert res.status_code == 422 and "у продавца нет ИНН" in res.json()["detail"]
    assert client.get(f"/api/v1/primary/{doc['id']}", headers=a["hdr"]).json()["exported_at"] is None


# ------------------------------------------------------------------ API для 1С (токен компании, T-57)
def _tokens(client, hdr, company_id: str) -> tuple[str, str]:
    general = "global-" + uuid4().hex
    assert client.put("/api/v1/settings/onec", headers=hdr, json={"api_token": general}).status_code == 200
    issued = client.post(f"/api/v1/settings/onec/companies/{company_id}/token", headers=hdr)
    assert issued.status_code == 200, issued.text
    return general, issued.json()["token"]


def test_onec_primary_pull_ack_and_scope(client, firms):
    admin, a, b = firms
    tag = uuid4().hex[:6]
    doc_a = _created(_upload(client, a["hdr"], ("a.xml", _for(a, number=f"P-{tag}"))))[0]
    doc_b = _created(_upload(client, b["hdr"], ("b.xml", _for(b, number=f"Q-{tag}"))))[0]
    general, token_a = _tokens(client, admin, a["id"])
    own = {"X-API-Token": token_a}
    pulled = client.get("/onec/v1/primary/pull", headers=own)
    assert pulled.status_code == 200, pulled.text
    body = pulled.json()
    ids = [d["id"] for d in body["documents"]]
    assert doc_a["id"] in ids and doc_b["id"] not in ids and body["format"] == "ymaster-primary-v1"
    item = next(d for d in body["documents"] if d["id"] == doc_a["id"])
    assert item["number"] == f"P-{tag}" and [ln["vat_1c"] for ln in item["lines"]] == ["НДС22", "НДС22"]
    assert item["upd_file"].startswith(f"ON_NSCHFDOPPR_{a['inn']}_")
    assert client.get(f"/onec/v1/primary/pull?company_id={b['id']}", headers=own).status_code == 403
    assert client.get("/onec/v1/ping", headers=own).json()["pending_primary"] >= 1
    # файл УПД документа — только своей компании
    upd_file = client.get(f"/onec/v1/primary/{doc_a['id']}/upd", headers=own)
    assert upd_file.status_code == 200 and _valid(upd_file.content)["number"] == f"P-{tag}"
    assert client.get(f"/onec/v1/primary/{doc_b['id']}/upd", headers=own).status_code == 404
    # ack: чужой документ токен компании не отмечает; свой — отмечает, повторно не отдаётся
    assert client.post("/onec/v1/primary/ack", headers=own, json={"ids": [doc_b["id"]]}).json() == {"acknowledged": 0}
    assert client.post("/onec/v1/primary/ack", headers=own, json={"ids": [doc_a["id"]]}).json() == {"acknowledged": 1}
    assert client.post("/onec/v1/primary/ack", headers=own, json={"ids": [doc_a["id"]]}).json() == {"acknowledged": 0}
    assert doc_a["id"] not in [d["id"] for d in client.get("/onec/v1/primary/pull", headers=own).json()["documents"]]
    assert client.post("/onec/v1/primary/ack", headers=own, json={}).status_code == 422
    # общий токен — как раньше: видит документы любой компании (в пределах company_id)
    other = client.get(f"/onec/v1/primary/pull?company_id={b['id']}", headers={"X-API-Token": general}).json()
    assert doc_b["id"] in [d["id"] for d in other["documents"]]
    assert client.get("/onec/v1/primary/pull", headers={"X-API-Token": "wrong"}).status_code == 401


def test_onec_primary_enterprise_data_scope_and_errors(client, firms):
    admin, a, b = firms
    tag = uuid4().hex[:6]
    _created(_upload(client, a["hdr"], ("a.xml", _for(a, number=f"R-{tag}"))))
    _created(_upload(client, a["hdr"], ("e.xml", _for(a, number=f"F-{tag}",
                                                       seller=("ООО «Ошибка»", broken(SELLER_INN), "500101001")))))
    _created(_upload(client, b["hdr"], ("b.xml", _for(b, number=f"W-{tag}"))))
    _, token_a = _tokens(client, admin, a["id"])
    own = {"X-API-Token": token_a}
    package = client.get("/onec/v1/primary/enterprise-data", headers=own)
    assert package.status_code == 200, package.text
    numbers = {o["КлючевыеСвойства"]["Номер"] for o in package.json()["Данные"]["Контейнер"]}
    assert f"R-{tag}" in numbers and f"F-{tag}" not in numbers and f"W-{tag}" not in numbers
    with_errors = client.get("/onec/v1/primary/enterprise-data?include_errors=true", headers=own).json()
    assert f"F-{tag}" in {o["КлючевыеСвойства"]["Номер"] for o in with_errors["Данные"]["Контейнер"]}
    xml = client.get("/onec/v1/primary/enterprise-data?format=xml", headers=own)
    assert xml.status_code == 200 and f"R-{tag}" in xml.text and f"W-{tag}" not in xml.text


def test_audit_phrases_for_export_and_onec():
    from app.services.audit_human import humanize_audit
    assert humanize_audit("primary_exported", {"count": 3, "skipped": 1, "format": "upd"}) == (
        "первичка выгружена в 1С", "документов: 3, пропущено: 1, формат: файлы УПД")
    assert humanize_audit("primary_exported", {"count": 2, "format": "json"})[1] == "документов: 2, формат: EnterpriseData JSON"
    assert humanize_audit("onec_primary_pull", {"count": 5}) == ("1С забрала УПД и счета-фактуры", "количество: 5")
    assert humanize_audit("onec_primary_ack", {"count": 5}) == ("1С подтвердила загрузку УПД", "количество: 5")


def test_paper_pdf_becomes_upd_file_for_1c(client, firms):
    """Бумажный УПД (PDF печатной формы) → черновик → сверка → файл УПД 5.03 для 1С по схеме ФНС."""
    from tests.test_primary_recognize import FIXTURE, _recognize
    _, a, _ = firms
    body = _recognize(client, a["hdr"], "upd.pdf", FIXTURE.read_bytes()).json()
    draft = dict(body["draft"], buyer=dict(body["draft"]["buyer"], inn=a["inn"]))
    saved = client.post("/api/v1/primary/confirm", headers=a["hdr"], json={
        "source": "pdf", "token": body["token"], "file_name": "upd.pdf", "file_sha256": body["file_sha256"],
        "draft": draft})
    assert saved.status_code == 200, saved.text
    doc = saved.json()["document"]
    res = client.post("/api/v1/primary/export", headers=a["hdr"], json={"ids": [doc["id"]]})
    assert res.status_code == 200, res.text
    back = _valid(res.content)
    assert (back["number"], back["date"], len(back["lines"])) == ("УТ-215", date(2026, 10, 14), 3)
    assert back["totals"]["with_vat"] == 105530.0 and back["seller"]["inn"] == body["draft"]["seller"]["inn"]
    assert [ln["name"] for ln in back["lines"]] == [ln["name"] for ln in body["draft"]["lines"]]
