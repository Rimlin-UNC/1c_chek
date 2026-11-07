"""Пакет для других учётных систем (T-68, v1.70.100): JSON, CSV и УПД 5.03 за месяц одним архивом.

Проверяется состав и целостность архива (контрольные суммы в manifest.json), правила записи CSV для Excel,
файлы УПД по схеме ФНС и изоляция компаний. Данные синтетические (ADR-14).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from uuid import uuid4

from app.exportpkg import service as pkg
from app.services import audit_human
from tests import test_docreq as _dr
from tests import test_primary_upd as _upd

firms = _dr.firms
client_firm = _dr.client_firm
PERIOD = _dr.PERIOD
URL = "/api/v1/export/package"


def _open(res) -> zipfile.ZipFile:
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(res.content))


def _rows(archive: zipfile.ZipFile, name: str) -> list[list[str]]:
    raw = archive.read(name)
    assert raw.startswith(b"\xef\xbb\xbf")                        # метка BOM — Excel открывает сразу
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))


def test_package_structure_counts_and_checksums(client, client_firm):
    admin, a = client_firm["admin"], client_firm["a"]
    res = client.get(f"{URL}?period={PERIOD}&company_id={a['id']}", headers=admin)
    archive = _open(res)
    assert f"-{PERIOD}.zip" in res.headers["content-disposition"]
    names = set(archive.namelist())
    for name in ("manifest.json", "FORMAT.md", "receipts.json", "receipts.csv", "receipt_items.csv",
                 "primary_documents.json", "primary_lines.csv", "bank_operations.csv", "reimbursements.json"):
        assert name in names, name
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format"] == "ymaster-chek-package" and manifest["format_version"] == "1.0"
    assert manifest["period"] == {"month": PERIOD, "title": "октябрь 2026", "from": "2026-10-01", "to": "2026-10-31"}
    assert manifest["counts"]["receipts"] == 1 and manifest["counts"]["primary_documents"] == 1
    for entry in manifest["files"]:                                # целостность — по SHA-256
        assert hashlib.sha256(archive.read(entry["name"])).hexdigest() == entry["sha256"]
    assert {e["name"] for e in manifest["files"]} | {"manifest.json"} == names
    # УПД с ошибкой и без строк — не выгружается, причина — в manifest
    assert manifest["skipped_upd"] and manifest["skipped_upd"][0]["reason"] == "в документе нет строк"
    receipts = json.loads(archive.read("receipts.json"))
    assert receipts[0]["total_sum"] == 1250.0 and receipts[0]["fns_status"] == "не найден"
    assert receipts[0]["date"] == "2026-10-10T12:00:00" and receipts[0]["qr"] == "t=synthetic"
    rows = _rows(archive, "receipts.csv")
    assert rows[0][:4] == ["Идентификатор", "Дата и время", "Признак расчёта", "Сумма, ₽"]
    assert rows[1][1] == "10.10.2026 12:00" and rows[1][3] == "1250,00" and rows[1][14] == "нет"
    assert "## Чеки — `receipts`" in archive.read("FORMAT.md").decode("utf-8")


def test_package_with_upd_file_and_lines(client, firms):
    admin, a, _ = firms
    number = f"П-{uuid4().hex[:6]}"
    up = _upd._upload(client, a["hdr"], ("upd.xml", _upd._for(a, number=number, day="14.10.2026")))
    assert up.status_code == 200, up.text
    archive = _open(client.get(f"{URL}?period={PERIOD}", headers=a["hdr"]))      # бухгалтер — своя компания
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["counts"]["upd_files"] == 1 and manifest["counts"]["primary_lines"] == 2
    docs = json.loads(archive.read("primary_documents.json"))
    doc = next(d for d in docs if d["number"] == number)
    assert doc["upd_file"].startswith("upd/ON_NSCHFDOPPR_") and doc["upd_file"] in archive.namelist()
    xml = archive.read(doc["upd_file"])
    assert xml.startswith(b'<?xml version="1.0" encoding="windows-1251"?>') and 'ВерсФорм="5.03"'.encode("cp1251") in xml
    lines = _rows(archive, "primary_lines.csv")
    assert lines[0][:3] == ["Документ", "№", "Наименование"] and len(lines) == 3
    # повторная выгрузка не меняет данные: те же записи (кроме времени в manifest и файлах УПД)
    again = json.loads(_open(client.get(f"{URL}?period={PERIOD}", headers=a["hdr"])).read("primary_documents.json"))
    assert again == docs


def test_package_isolation_and_validation(client, firms):
    admin, a, b = firms
    assert client.get(f"{URL}?period={PERIOD}&company_id={a['id']}", headers=b["hdr"]).status_code == 404
    assert client.get(f"{URL}?period={PERIOD}", headers=admin).status_code == 400           # выберите компанию
    assert client.get(f"{URL}?period=2026-13", headers=a["hdr"]).status_code == 422
    emp = _dr._mk_user(client, admin, a["id"], f"pkg_emp_{uuid4().hex[:8]}", role="user")
    assert client.get(f"{URL}?period={PERIOD}", headers=emp).status_code == 403
    own = _open(client.get(f"{URL}?period={PERIOD}", headers=b["hdr"]))
    assert json.loads(own.read("manifest.json"))["company"]["inn"] == b["inn"]
    # назначенный клиент — пакет прямо с экрана «Клиенты», без переключения (ADR-30)
    acc_b = client.get("/api/v1/auth/me", headers=b["hdr"]).json()["id"]
    client.put(f"/api/v1/clients/access/{acc_b}", headers=admin, json={"company_ids": [a["id"]]})
    theirs = _open(client.get(f"{URL}?period={PERIOD}&company_id={a['id']}", headers=b["hdr"]))
    assert json.loads(theirs.read("manifest.json"))["company"]["inn"] == a["inn"]


def test_csv_value_rules():
    assert pkg._csv_value("total_sum", 1250.5) == "1250,50"
    assert pkg._csv_value("quantity", 1.5) == "1,5"
    assert pkg._csv_value("date", "2026-10-10T12:00:00") == "10.10.2026 12:00"
    assert pkg._csv_value("date", "2026-10-10") == "10.10.2026"
    assert pkg._csv_value("exported", True) == "да" and pkg._csv_value("x", None) == ""
    assert pkg.to_csv([], [("a", "Колонка")]).decode("utf-8") == "\ufeffКолонка\r\n"


def test_audit_phrase_export_package():
    action, note = audit_human.humanize_audit("export_package", {
        "company": "ООО «Тест»", "period": "2026-10", "receipts": 12, "primary_documents": 3, "upd_files": 2,
        "bank_operations": 4})
    assert action == "выгружен пакет для других учётных систем"
    assert note == ("компания: „ООО «Тест»“, месяц: 2026-10, чеков: 12, документов: 3, файлов УПД: 2, "
                    "банковских операций: 4")
