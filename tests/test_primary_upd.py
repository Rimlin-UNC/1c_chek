"""Первичка: электронные УПД и счета-фактуры 5.01–5.03 (T-53, v1.70.94).

Разбор, проверки (реквизиты, ИНН/КПП, арифметика, ставка НДС на переходе к 22 %,
формат 5.03 с 01.04.2026), API загрузки с изоляцией компаний, дубли, ZIP-архивы
операторов ЭДО и пределы. Только синтетические документы (ADR-14).
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from uuid import uuid4

import pytest

from app.primary import upd
from tests.conftest import login
from tests.primary_samples import CEMENT, DELIVERY, SELLER_INN, broken, inn10, inn12, upd_xml
from tests.test_v1140 import _mk_user


def _codes(issues: list[dict], level: str | None = None) -> set[str]:
    return {i["code"] for i in issues if level is None or i["level"] == level}


def _parsed(**kw) -> dict:
    return upd.parse(upd_xml(**kw))


# ------------------------------------------------------------------ разбор
def test_parse_503_reads_requisites_lines_and_totals():
    buyer_inn = inn10()
    doc = _parsed(buyer=("ООО «Ромашка-Строй»", buyer_inn, "770401001"))
    assert (doc["format_version"], doc["function"], doc["kind"]) == ("5.03", "СЧФДОП", "УПД")
    assert (doc["number"], doc["date"].isoformat(), doc["currency"]) == ("УТ-15", "2026-03-10", "643")
    assert doc["seller"] == {"kind": "ЮЛ", "name": "ООО «СтройБаза»", "inn": SELLER_INN, "kpp": "500101001"}
    assert doc["buyer"]["inn"] == buyer_inn and doc["buyer"]["name"] == "ООО «Ромашка-Строй»"
    first = doc["lines"][0]
    assert (first["name"], first["quantity"], first["price"], first["sum_without_vat"]) == \
        ("Цемент М500, мешок 50 кг", 10.0, 500.0, 5000.0)
    assert (first["vat_rate"], first["vat_sum"], first["sum_with_vat"], first["item_code"]) == \
        ("22", 1100.0, 6100.0, "CEM-500")
    assert (first["unit_code"], first["unit_name"]) == ("796", "шт")
    assert doc["totals"] == {"without_vat": 6500.0, "with_vat": 7930.0, "vat": 1430.0}
    assert upd.check(doc, company_inn=buyer_inn) == []


def test_marking_codes_are_not_read():
    doc = _parsed()
    assert CEMENT["kiz"] not in json.dumps(doc, ensure_ascii=False, default=str)


def test_utf8_and_windows1251_give_same_result():
    same = {"file_id": "ON_NSCHFDOPPR_same", "buyer": ("ООО «Б»", inn10("770000000"), "")}
    a = upd.parse(upd_xml(encoding="utf-8", **same))
    b = upd.parse(upd_xml(encoding="windows-1251", **same))
    assert a == b and a["seller"]["name"] == "ООО «СтройБаза»"


def test_parse_501_names_and_correction():
    doc = _parsed(version="5.01", function="СЧФ", day="15.12.2025", correction=("2", "20.12.2025"))
    assert (doc["number"], doc["date"].isoformat(), doc["kind"]) == ("УТ-15", "2025-12-15", "Счёт-фактура")
    assert (doc["correction_number"], doc["correction_date"].isoformat()) == ("2", "2025-12-20")
    lines = [dict(CEMENT, rate="20%", vat="1000.00", total="6000.00")]
    issues = upd.check(_parsed(version="5.01", function="СЧФ", day="15.12.2025", correction=("2", "20.12.2025"),
                               lines=lines))
    assert _codes(issues, "warning") == set() and "correction" in _codes(issues, "info")


def test_individual_and_foreign_parties():
    ip_inn = inn12()
    doc = _parsed(seller=("ИП", ip_inn, ("Петров", "Пётр", "Петрович")), buyer=("ИН", "Foreign Ltd"),
                  function="ДОП")
    assert doc["seller"] == {"kind": "ИП", "name": "ИП Петров Пётр Петрович", "inn": ip_inn, "kpp": ""}
    assert doc["buyer"]["kind"] == "ИН" and doc["buyer"]["name"] == "Foreign Ltd"
    assert _codes(upd.check(doc), "error") == set()


def test_without_vat_line():
    line = dict(DELIVERY, rate="без НДС", vat="без НДС", total="1500.00")
    doc = _parsed(lines=[line], totals={"vat": "0.00"})
    assert (doc["lines"][0]["vat_rate"], doc["lines"][0]["vat_sum"]) == ("none", 0.0)
    assert _codes(upd.check(doc), "warning") == set()


@pytest.mark.parametrize("raw, message", [
    (b"not xml at all", "не XML"),
    ("<?xml version='1.0'?><Документ/>".encode(), "Корневой элемент"),
    ("<?xml version='1.0'?><Файл ВерсФорм='5.03'/>".encode(), "нет элемента"),
    (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><x>&lol;</x>', "не XML"),
])
def test_rejects_garbage_and_dtd(raw, message):
    with pytest.raises(upd.UpdError, match=message):
        upd.parse(raw)


def test_size_limit():
    with pytest.raises(upd.UpdError, match="5 МБ"):
        upd.parse(b" " * (upd.MAX_BYTES + 1))


@pytest.mark.parametrize("raw, expected", [
    ("22%", "22"), ("20%", "20"), ("10 %", "10"), ("без НДС", "none"), ("БЕЗ НДС", "none"),
    ("22/122", "22/122"), ("5%", "5"), ("7/107", "7/107"), ("0%", "0"),
    ("НДС исчисляется налоговым агентом", "agent"),
])
def test_vat_rate_names(raw, expected):
    assert upd.vat_rate(raw) == expected


# ------------------------------------------------------------------ проверки
def test_inn_checksum_and_kpp_format():
    issues = upd.check(_parsed(seller=("ООО «СтройБаза»", broken(SELLER_INN), "50010100X")))
    assert "inn" in _codes(issues, "error") and "kpp" in _codes(issues, "warning")
    assert upd.status_of(issues) == "errors"


def test_arithmetic_of_lines_and_totals():
    bad = dict(CEMENT, base="5100.00", vat="1122.00", total="6222.00")
    issues = upd.check(_parsed(lines=[bad, dict(DELIVERY, vat="300.00", total="1800.00")],
                               totals={"base": "9999.00"}))
    warnings = _codes(issues, "warning")
    assert {"line_product", "line_vat", "total_without_vat"} <= warnings
    assert upd.status_of(issues) == "warnings"


def test_line_total_mismatch_and_rounding_tolerance():
    tolerant = dict(CEMENT, qty="3", price="33.333", base="100.00", vat="22.00", total="122.00")
    assert _codes(upd.check(_parsed(lines=[tolerant])), "warning") == set()
    wrong = dict(CEMENT, total="6200.00")
    assert "line_total" in _codes(upd.check(_parsed(lines=[wrong])), "warning")


def test_fraction_rate_checks_vat_inside_total():
    line = {"name": "Аванс", "rate": "22/122", "vat": "22.00", "total": "122.00", "code": ""}
    assert _codes(upd.check(_parsed(lines=[line])), "warning") == set()
    line_bad = dict(line, vat="20.00")
    assert "line_vat" in _codes(upd.check(_parsed(lines=[line_bad])), "warning")


def test_vat_rate_transition_to_22():
    old = [dict(CEMENT, rate="20%", vat="1000.00", total="6000.00")]
    assert "vat_20_in_2026" in _codes(upd.check(_parsed(lines=old, day="15.01.2026")), "warning")
    assert "vat_20_in_2026" not in _codes(upd.check(_parsed(lines=old, day="25.12.2025")))
    assert "vat_22_before_2026" in _codes(upd.check(_parsed(day="25.12.2025")), "warning")


def test_format_503_required_from_april_2026():
    assert "version_503" in _codes(upd.check(_parsed(version="5.01", day="01.04.2026")), "warning")
    assert "version_503" not in _codes(upd.check(_parsed(version="5.01", day="31.03.2026")))
    assert "version" in _codes(upd.check(_parsed(version="4.00")), "warning")


def test_required_requisites():
    issues = upd.check(_parsed(number="", day="", lines=[]))
    assert {"number", "date", "lines"} <= _codes(issues, "error")
    assert "buyer" in _codes(upd.check(_parsed(buyer=None)), "error")
    assert "buyer" not in _codes(upd.check(_parsed(buyer=None, function="ДОП")))
    assert "seller" in _codes(upd.check(_parsed(seller=None)), "error")
    nameless = dict(CEMENT, name="")
    assert "line_name" in _codes(upd.check(_parsed(lines=[nameless])), "error")


def test_buyer_title_and_corrections_are_not_primary():
    assert "buyer_title" in _codes(upd.check(_parsed(file_id="ON_NSCHFDOPPOK_x_y_20260310_1")), "error")
    assert "correction_doc" in _codes(upd.check(_parsed(function="КСЧФ")), "error")
    assert "function" in _codes(upd.check(_parsed(function="ЧТОТО")), "error")


def test_buyer_must_be_our_company():
    issues = upd.check(_parsed(buyer=("ООО «Чужая»", inn10(), "770401001")), company_inn=inn10())
    assert "buyer_company" in _codes(issues, "warning")


def test_kpp_required_in_invoice_and_same_party():
    seller_no_kpp = ("ООО «СтройБаза»", SELLER_INN, "")
    assert "kpp_missing" in _codes(upd.check(_parsed(seller=seller_no_kpp)), "warning")
    assert "kpp_missing" not in _codes(upd.check(_parsed(seller=seller_no_kpp, function="ДОП")))
    same = upd.check(_parsed(buyer=("ООО «СтройБаза»", SELLER_INN, "500101001")))
    assert "same_party" in _codes(same, "warning")


def test_future_date_and_early_correction():
    assert "date_future" in _codes(upd.check(_parsed(day="10.03.2030"), today=date(2026, 11, 4)), "warning")
    assert "date_future" not in _codes(upd.check(_parsed(day="05.11.2026"), today=date(2026, 11, 4)))
    assert "correction_date" in _codes(upd.check(_parsed(correction=("1", "01.03.2026"))), "warning")
    assert "correction_date" not in _codes(upd.check(_parsed(correction=("1", "12.03.2026"))))


# ------------------------------------------------------------------ API
@pytest.fixture
def firms(client):
    hdr = login(client, "admin", "admin123")
    out = []
    for letter in "AB":
        for _ in range(5):          # v1.70.96: случайный ИНН совпал с уже созданным — берём другой
            inn = inn10()
            res = client.post("/api/v1/companies", headers=hdr, json={"name": f"Первичка {letter} {uuid4().hex}",
                                                                      "inn": inn})
            if res.status_code != 409 or "таким ИНН" not in res.text:
                break
        assert res.status_code in (200, 201), res.text
        company_id = res.json()["id"]
        acc = _mk_user(client, hdr, company_id, f"prim_{letter.lower()}_{uuid4().hex[:10]}", role="accountant")
        out.append({"id": company_id, "inn": inn, "hdr": acc})
    yield hdr, out[0], out[1]
    client.cookies.clear()


def _upload(client, hdr, *files, company_id=None):
    data = {"company_id": company_id} if company_id else None
    payload = [("files", (name, body, "application/octet-stream")) for name, body in files]
    return client.post("/api/v1/primary/upload", headers=hdr, files=payload, data=data)


def _for(firm, **kw) -> bytes:
    return upd_xml(buyer=("ООО «Покупатель»", firm["inn"], "770401001"), **kw)


def test_upload_list_detail_delete(client, firms):
    _, a, _ = firms
    res = _upload(client, a["hdr"], ("upd.xml", _for(a, number=f"T-{uuid4().hex[:6]}")))
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["created"], body["duplicates"], body["rejected"]) == (1, 0, 0)
    doc = body["results"][0]["document"]
    assert doc["company_id"] == a["id"] and doc["status"] == "ok" and doc["lines_count"] == 2
    assert (doc["total_without_vat"], doc["vat_total"], doc["total_with_vat"]) == (6500.0, 1430.0, 7930.0)
    listed = client.get("/api/v1/primary", headers=a["hdr"]).json()
    assert doc["id"] in [d["id"] for d in listed["items"]] and listed["summary"]["ok"] >= 1
    detail = client.get(f"/api/v1/primary/{doc['id']}", headers=a["hdr"]).json()
    assert [ln["name"] for ln in detail["lines"]] == [CEMENT["name"], DELIVERY["name"]]
    # v1.70.95: единственное сведение — файл прошёл официальную схему ФНС 5.03
    assert detail["lines"][0]["vat_rate"] == "22" and [i["code"] for i in detail["issues"]] == ["xsd_ok"]
    assert CEMENT["kiz"] not in json.dumps(detail, ensure_ascii=False)
    assert client.delete(f"/api/v1/primary/{doc['id']}", headers=a["hdr"]).json() == {"ok": True}
    assert client.get(f"/api/v1/primary/{doc['id']}", headers=a["hdr"]).status_code == 404


def test_duplicates_same_file_same_document_and_correction(client, firms):
    _, a, _ = firms
    number = f"D-{uuid4().hex[:6]}"
    first = _for(a, number=number)
    assert _upload(client, a["hdr"], ("a.xml", first)).json()["created"] == 1
    again = _upload(client, a["hdr"], ("a.xml", first)).json()
    assert again["duplicates"] == 1 and again["results"][0]["message"] == "Этот документ уже загружен"
    other_file = _for(a, number=number, file_id="ON_NSCHFDOPPR_other_20260310_2")
    assert _upload(client, a["hdr"], ("b.xml", other_file)).json()["duplicates"] == 1
    fixed = _for(a, number=number, correction=("1", "12.03.2026"))
    assert _upload(client, a["hdr"], ("c.xml", fixed)).json()["created"] == 1


def test_rejected_files_are_not_stored(client, firms):
    _, a, _ = firms
    before = client.get("/api/v1/primary", headers=a["hdr"]).json()["total"]
    res = _upload(client, a["hdr"], ("x.txt", b"hello"), ("pok.xml", _for(a, file_id="ON_NSCHFDOPPOK_1")),
                  ("k.xml", _for(a, function="КСЧФ"))).json()
    assert res["rejected"] == 3 and res["created"] == 0
    assert "титул покупателя" in res["results"][1]["message"]
    assert client.get("/api/v1/primary", headers=a["hdr"]).json()["total"] == before


def test_zip_archive_from_edo_operator(client, firms):
    _, a, _ = firms
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/upd1.xml", _for(a, number=f"Z1-{uuid4().hex[:6]}"))
        zf.writestr("docs/upd2.xml", _for(a, number=f"Z2-{uuid4().hex[:6]}"))
        zf.writestr("docs/upd1.xml.sig", b"signature")
        zf.writestr("docs/print.pdf", b"%PDF-1.4")
    res = _upload(client, a["hdr"], ("pack.zip", buf.getvalue())).json()
    assert res["created"] == 2 and res["rejected"] == 0
    assert all(r["file"].startswith("pack.zip/docs/upd") for r in res["results"])
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", b"-")
    res = _upload(client, a["hdr"], ("empty.zip", empty.getvalue())).json()
    assert res["rejected"] == 1 and "нет XML" in res["results"][0]["message"]


def _encrypted_zip(data: bytes) -> bytes:
    """Архив с флагом шифрования: zipfile не шифрует, бит ставим в заголовках."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("enc.xml", data)
    raw = bytearray(buf.getvalue())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        raw[raw.find(signature) + offset] |= 0x1
    return bytes(raw)


def test_zip_encrypted_or_broken_is_explained_not_500(client, firms):
    _, a, _ = firms
    res = _upload(client, a["hdr"], ("enc.zip", _encrypted_zip(_for(a)))).json()
    assert res["rejected"] == 1 and "зашифрован" in res["results"][0]["message"]
    res = _upload(client, a["hdr"], ("bad.zip", b"PK\x03\x04garbage")).json()
    assert res["rejected"] == 1 and res["results"][0]["message"] == "Архив повреждён"


def test_company_isolation(client, firms):
    _, a, b = firms
    doc = _upload(client, a["hdr"], ("i.xml", _for(a, number=f"I-{uuid4().hex[:6]}"))).json()["results"][0]["document"]
    assert doc["id"] not in [d["id"] for d in client.get("/api/v1/primary", headers=b["hdr"]).json()["items"]]
    assert client.get(f"/api/v1/primary/{doc['id']}", headers=b["hdr"]).status_code == 404
    assert client.delete(f"/api/v1/primary/{doc['id']}", headers=b["hdr"]).status_code == 404
    forced = _upload(client, b["hdr"], ("f.xml", _for(a, number=f"F-{uuid4().hex[:6]}")), company_id=a["id"]).json()
    stored = forced["results"][0]["document"]
    assert stored["company_id"] == b["id"] and stored["status"] == "warnings"
    assert "buyer_company" in _codes(forced["results"][0]["issues"], "warning")


def test_admin_upload_finds_client_by_buyer_inn(client, firms):
    hdr, a, b = firms
    res = _upload(client, hdr, ("auto.xml", _for(b, number=f"A-{uuid4().hex[:6]}"))).json()
    assert res["results"][0]["document"]["company_id"] == b["id"]
    chosen = _upload(client, hdr, ("pick.xml", _for(b, number=f"P-{uuid4().hex[:6]}")), company_id=a["id"]).json()
    assert chosen["results"][0]["document"]["company_id"] == a["id"]
    unknown = upd_xml(number=f"U-{uuid4().hex[:6]}")
    assert _upload(client, hdr, ("u.xml", unknown)).json()["results"][0]["document"]["company_id"] is None
    only_a = client.get(f"/api/v1/primary?company_id={a['id']}", headers=hdr).json()["items"]
    assert {d["company_id"] for d in only_a} == {a["id"]}


def test_filters_and_status_summary(client, firms):
    _, a, _ = firms
    mark = uuid4().hex[:6]
    bad = dict(CEMENT, base="5100.00")
    _upload(client, a["hdr"], ("ok.xml", _for(a, number=f"S-{mark}-1")),
            ("warn.xml", _for(a, number=f"S-{mark}-2", lines=[bad])))
    res = client.get(f"/api/v1/primary?q=S-{mark}", headers=a["hdr"]).json()
    assert res["total"] == 2 and res["summary"]["ok"] == 1 and res["summary"]["warnings"] == 1
    only = client.get(f"/api/v1/primary?q=S-{mark}&status=warnings", headers=a["hdr"]).json()
    assert [d["doc_number"] for d in only["items"]] == [f"S-{mark}-2"]
    dated = client.get(f"/api/v1/primary?q=S-{mark}&date_from=2026-03-11", headers=a["hdr"]).json()
    assert dated["total"] == 0


def test_roles_and_limits(client, firms):
    hdr, a, _ = firms
    user = _mk_user(client, hdr, a["id"], f"prim_u_{uuid4().hex[:10]}", role="user")
    assert _upload(client, user, ("u.xml", _for(a))).status_code == 403
    assert client.get("/api/v1/primary", headers=user).status_code == 403
    too_many = [(f"{i}.xml", b"<x/>") for i in range(51)]
    assert _upload(client, a["hdr"], *too_many).status_code == 400
