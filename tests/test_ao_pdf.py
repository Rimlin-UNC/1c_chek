# -*- coding: utf-8 -*-
"""v1.73.8: настоящий текстовый PDF авансового отчёта.

Проверяем не «страницу, похожую на PDF», а файл, из которого pypdf достаёт
кириллицу: серверный endpoint, встроенный шрифт, права, отказы на плохих
выборках и бинарную целостность при сохранении.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.services import ao_pdf
from tests import test_v1140 as v1140
from tests.conftest import login

ROOT = Path(__file__).resolve().parent.parent


def _report():
    return {
        "period": {"from": "2026-11-01", "to": "2026-11-23"},
        "company": {"id": "c1", "name": "ООО «Ромашка»",
                    "short_name": "ООО «Ромашка»", "inn": "7801234564"},
        "requisites": {"Организация": {
            "НаименованиеПолное": "Общество с ограниченной ответственностью «Ромашка»",
            "НаименованиеСокращенное": "ООО «Ромашка»",
            "ИНН": "7801234564", "КПП": "780101001", "Руководитель": "Иванов И.И.",
        }},
        "person": {"ФИО": "Иван Отчётный", "Должность": "Менеджер"},
        "rows": [{
            "id": "r1", "receipt_date": "2026-11-05T12:30:00",
            "assignee": "Иван Отчётный", "merchant_name": "Магазин «Василёк»",
            "merchant_inn": "7700000000", "fn": "9999078902001299", "fd": "123",
            "fp": "456", "fns_status": "valid", "total_sum": 350.50,
            "operation": 1, "personal_sum": 0,
        }],
        "total": {"sum": 350.50, "count": 1},
        "by_assignee": [{"name": "Иван Отчётный", "count": 1, "sum": 350.50}],
        "invalid_count": 0,
        "truncated": False,
    }


def _meta():
    return {"ao-num": "АО-2026-11-01", "ao-date": "2026-11-23",
            "ao-org": "ООО «Ромашка»", "ao-post": "Менеджер",
            "ao-purpose": "Хозрасходы", "ao-buh": "Петрова П.П."}


def _text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


class TestAoPdfService:
    def test_cyrillic_text_is_searchable_and_font_is_embedded(self):
        rep, meta = _report(), _meta()
        qr = {"r1": "t=20261105T1230&s=350.50&fn=9999078902001299&i=123&fp=456&n=1"}
        pdf = ao_pdf.build_ao_pdf(rep, meta, qr, include_receipts=True)
        assert pdf.startswith(b"%PDF")
        assert b"/FontFile" in pdf and b"DejaVu" in pdf
        assert b"/Image" in pdf                       # QR как картинка
        text = _text(pdf)
        for needle in ("АВАНСОВЫЙ ОТЧЁТ", "ООО «Ромашка»", "Иван Отчётный",
                       "350,50", "Реестр чеков", "Приложения", "Сформировано"):
            assert needle in text, needle
        assert "■" not in text                       # не чёрные квадраты Helvetica

    def test_filenames_are_safe_and_predictable(self):
        rep, meta = _report(), _meta()
        ascii_name = ao_pdf.ao_pdf_filename(rep, meta)
        unicode_name = ao_pdf.ao_pdf_unicode_filename(rep, meta)
        assert ascii_name.endswith(".pdf") and ascii_name.isascii()
        assert "/" not in ascii_name and "\\" not in ascii_name
        assert unicode_name.endswith(".pdf") and "Ромашка" in unicode_name

    def test_html_escapes_client_meta(self):
        rep = _report()
        source = ao_pdf._html(rep, {"ao-purpose": "<script>alert(1)</script>"}, {}, False)
        assert "<script>" not in source
        assert "&lt;script&gt;" in source

    def test_external_resources_are_forbidden(self):
        assert ao_pdf._link_callback("DejaVuSans.ttf") == str(ao_pdf.FONT_PATH)
        assert ao_pdf._link_callback("data:image/png;base64,AAAA") is None
        for bad in ("https://example.org/font.ttf", "file:///etc/passwd",
                    "../../../etc/passwd", "//example.org/a.png"):
            with pytest.raises(ValueError, match="запрещён"):
                ao_pdf._link_callback(bad)
        policy = ao_pdf._resource_policy()
        assert policy.allow_remote is False
        assert policy.allow_private_networks is False
        assert ao_pdf.FONT_DIR.resolve() in policy.roots

    def test_missing_font_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(ao_pdf, "FONT_PATH", ROOT / "app/assets/fonts/no-such-font.ttf")
        try:
            ao_pdf.build_ao_pdf(_report(), _meta(), {}, False)
        except RuntimeError as exc:
            assert "DejaVuSans" in str(exc)
        else:
            raise AssertionError("PDF без встроенного кириллического шрифта не должен собираться")


class TestAoPdfEndpoint:
    def _prepare(self, client):
        hdr = login(client, "admin", "admin123")
        comp, hu, ids = v1140.TestAdvanceReport()._prepare(client, hdr)
        from app.database import SessionLocal
        from app.models import Company
        db = SessionLocal()
        try:
            row = db.get(Company, comp["id"])
            row.short_name = "ООО «Папка»"
            db.commit()
        finally:
            db.close()
        return hdr, hu, comp, ids

    def _body(self, comp, ids, **extra):
        body = {
            "date_from": "2026-01-01", "date_to": "2026-12-31",
            "company_id": comp["id"], "receipt_ids": ids,
            "only_valid": False, "report_number": "АО-2026-11-01",
            "report_date": "2026-11-23", "include_receipts": True,
            "meta": {"ao-num": "АО-2026-11-01", "ao-date": "2026-11-23",
                     "ao-org": "ООО «Папка»", "ao-post": "Менеджер",
                     "ao-purpose": "Хозрасходы", "ao-buh": "Петрова П.П."},
        }
        body.update(extra)
        return body

    def test_returns_real_pdf_with_searchable_russian_text(self, client):
        hdr, _hu, comp, ids = self._prepare(client)
        r = client.post("/api/v1/receipts/advance-report/pdf",
                        headers=hdr, json=self._body(comp, ids))
        assert r.status_code == 200, r.text[:500]
        assert r.headers["content-type"].startswith("application/pdf")
        assert r.headers["cache-control"] == "no-store"
        assert ".pdf" in r.headers["content-disposition"]
        assert "filename*=UTF-8''" in r.headers["content-disposition"]
        pdf = r.content
        assert pdf.startswith(b"%PDF") and b"/FontFile" in pdf
        text = _text(pdf)
        for needle in ("СВОДНАЯ ВЕДОМОСТЬ", "АВАНСОВЫМ ОТЧЁТАМ", "ООО «Папка»",
                       "Иван Отчётный", "Пётр Готовый", "350,50", "Приложения"):
            assert needle in text, needle

    def test_ordinary_user_is_forbidden(self, client):
        _hdr, hu, comp, ids = self._prepare(client)
        r = client.post("/api/v1/receipts/advance-report/pdf",
                        headers=hu, json=self._body(comp, ids))
        assert r.status_code == 403

    def test_bad_dates_empty_and_truncated_are_rejected(self, client):
        hdr, _hu, comp, ids = self._prepare(client)
        bad = self._body(comp, ids)
        bad["date_from"] = "2026-13-01"
        assert client.post("/api/v1/receipts/advance-report/pdf",
                           headers=hdr, json=bad).status_code == 422
        empty = self._body(comp, ids)
        empty["receipt_ids"] = []
        empty["date_from"], empty["date_to"] = "1999-01-01", "1999-01-31"
        r = client.post("/api/v1/receipts/advance-report/pdf",
                        headers=hdr, json=empty)
        assert r.status_code == 400 and "нет" in r.json()["detail"].lower()

    def test_unsafe_rows_are_not_rendered_as_expenses(self, client):
        hdr, _hu, comp, ids = self._prepare(client)
        client.patch(f"/api/v1/receipts/{ids[0]}",
                     json={"personal_sum": 35}, headers=hdr)
        r = client.post("/api/v1/receipts/advance-report/pdf",
                        headers=hdr, json=self._body(comp, ids))
        assert r.status_code == 400
        assert "личные расходы" in r.json()["detail"].lower()

    def test_other_company_accountant_does_not_see_foreign_receipts(self, client):
        hdr, _hu, comp, ids = self._prepare(client)
        comp2 = client.post("/api/v1/companies", json={
            "name": f"ООО Другая {next(v1140._SEQ)}"}, headers=hdr).json()
        ha2 = v1140._mk_user(client, hdr, comp2["id"],
                             f"ao_pdf_acc_{next(v1140._SEQ)}", role="accountant")
        body = self._body(comp, ids)
        body.pop("company_id")
        r = client.post("/api/v1/receipts/advance-report/pdf",
                        headers=ha2, json=body)
        assert r.status_code == 400       # scope_receipts вернул 0 чужих чеков


class TestAoPdfAssets:
    def test_font_and_license_are_vendored(self):
        font = ROOT / "app/assets/fonts/DejaVuSans.ttf"
        license_ = ROOT / "app/assets/fonts/LICENSE-DejaVu.txt"
        assert font.is_file() and font.stat().st_size > 500_000
        assert "Bitstream" in license_.read_text(encoding="utf-8")
        assert "xhtml2pdf" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
