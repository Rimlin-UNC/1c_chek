# -*- coding: utf-8 -*-
"""v1.73.8 / v1.73.9: настоящий текстовый PDF авансового отчёта.

Проверяем не «страницу, похожую на PDF», а файл, из которого pypdf достаёт
кириллицу: серверный endpoint, встроенный шрифт, права, отказы на плохих
выборках, бинарную целостность при сохранении, вёрстку АО-1 и полные
itemized копии всех чеков в двух колонках с QR и разрезанием по позициям.
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


def _image_count(pdf: bytes) -> int:
    """Считаем QR по XObject-ресурсам страниц, а не через `PageObject.images`.

    Проверка должна видеть физические `/Subtype /Image`, включая картинки
    внутри Form XObject, и не зависеть от того, как конкретная версия pypdf
    материализует удобное свойство `page.images`.
    """
    def deref(obj):
        return obj.get_object() if hasattr(obj, "get_object") else obj

    total = 0
    for page in PdfReader(io.BytesIO(pdf)).pages:
        resources = deref(page.get("/Resources") or {})
        xobjects = deref(resources.get("/XObject") or {})
        stack = [deref(value) for value in xobjects.values()]
        seen: set[int] = set()
        while stack:
            obj = stack.pop()
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            subtype = str(obj.get("/Subtype") or "")
            if subtype == "/Image":
                total += 1
                continue
            if subtype != "/Form":
                continue
            sub_resources = deref(obj.get("/Resources") or {})
            sub_xobjects = deref(sub_resources.get("/XObject") or {})
            stack.extend(deref(value) for value in sub_xobjects.values())
    return total


def _collisions(pdf: bytes) -> list[tuple[int, str, str]]:
    """Грубая, но полезная проверка наложений текста: координаты pypdf
    складываем из cm + tm (только tm даёт ложные тысячи пересечений).
    """
    out: list[tuple[int, str, str]] = []
    identity = (1, 0, 0, 1, 0, 0)
    for page_no, page in enumerate(PdfReader(io.BytesIO(pdf)).pages, 1):
        frags: list[tuple[float, float, float, str]] = []

        def visit(text, cm, tm, _font, size):
            s = str(text or "").strip()
            if s:
                c = cm or identity
                m = tm or identity
                frags.append((c[4] + m[4], c[5] + m[5],
                              float(size or 8), s))

        page.extract_text(visitor_text=visit)
        for i, (x1, y1, s1, t1) in enumerate(frags):
            w1 = len(t1) * s1 * 0.48
            for x2, y2, s2, t2 in frags[i + 1:]:
                w2 = len(t2) * s2 * 0.48
                overlap = min(x1 + w1, x2 + w2) - max(x1, x2)
                if (abs(y1 - y2) < 0.8 and abs(x1 - x2) > 0.5
                        and overlap > 0.3 * min(w1, w2)):
                    out.append((page_no, t1[:30], t2[:30]))
    return out


def _many_report(count: int = 42) -> tuple[dict, dict, dict]:
    """42 строки с длинными ФН/ФП и продавцом — тот случай, который ломал
    старую таблицу «Реестр чеков» и давал почти пустую хвостовую страницу.
    """
    rows = []
    for i in range(count):
        rows.append({
            "id": f"r{i + 1}", "receipt_date": f"2026-10-{1 + i % 30:02d}T09:08:00",
            "assignee": "Шпаков Роман Владимирович",
            "merchant_name": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «СТРОЙТОРГОВЛЯ»",
            "merchant_inn": "4703148343", "fn": "7384440900770272",
            "fd": str(116386 + i), "fp": str(2895916071 + i),
            "fns_status": "valid", "total_sum": 1974.0 + i,
            "operation": 1, "personal_sum": 0, "category": "Хозрасходы",
        })
    total = sum(r["total_sum"] for r in rows)
    rep = {
        "period": {"from": "2026-10-01", "to": "2026-10-31"},
        "company": {"id": "c1", "name": "ООО «Ямастер»",
                    "short_name": "ООО «Ямастер»", "inn": "4712030034"},
        "requisites": {"Организация": {
            "НаименованиеПолное": "Общество с ограниченной ответственностью «Ямастер»",
            "НаименованиеСокращенное": "ООО «Ямастер»",
            "ИНН": "4712030034", "КПП": "471201001", "ОКПО": "44903513",
            "Руководитель": "Шпаков Роман Владимирович",
        }},
        "person": {"ФИО": "Шпаков Роман Владимирович", "Должность": "Генеральный директор"},
        "rows": rows, "total": {"sum": total, "count": count},
        "by_assignee": [{"name": "Шпаков Роман Владимирович",
                         "count": count, "sum": total}],
        "invalid_count": 0, "truncated": False,
    }
    meta = {"ao-num": "АО-2026-11-07", "ao-date": "2026-11-24",
            "ao-org": "ООО «Ямастер»", "ao-dept": "ИТР",
            "ao-post": "Генеральный директор",
            "ao-purpose": "На хозяйственные расходы",
            "ao-buh": "Шпаков Роман Владимирович",
            "ao-head": "Шпаков Роман Владимирович",
            "attachmentPages": 22}
    qr = {r["id"]: (f"t=20261001T0908&s=1974.00&fn={r['fn']}"
                    f"&i={r['fd']}&fp={r['fp']}&n=1") for r in rows}
    return rep, meta, qr


def _cards_for(rep: dict, qr: dict) -> dict:
    """Карточки в формате receipt_card.build_card для полных копий."""
    cards = {}
    for i, row in enumerate(rep["rows"], 1):
        cards[row["id"]] = {"document": {
            "operation_name": "Приход",
            "date_time": row["receipt_date"],
            "seller": {"name": row["merchant_name"], "inn": row["merchant_inn"],
                       "address": "188760, Ленинградская область, г. Приозерск",
                       "place": "Магазин стройматериалов"},
            "kkt": {"fn": row["fn"], "fd": row["fd"], "fp": row["fp"],
                    "shift": 42, "request": i, "reg_id": "0001234567890123",
                    "ffd": "1.2"},
            "cashier": {"name": "СИС. АДМИНИСТРАТОР", "inn": ""},
            "money": {"total": row["total_sum"], "cash": row["total_sum"],
                      "ecash": 0, "prepaid": 0, "credit": 0, "provision": 0},
            "tax": {"taxation_name": "УСН",
                    "nds": [{"rate_name": "22 %", "sum": 434.28}]},
            "items": [
                {"pos": 1, "name": "Плинтус напольный 70мм Деконика",
                 "quantity": 3, "price": 259.0, "total": 777.0,
                 "nds_name": "22 %", "nds_sum": 140.0, "measure": "шт"},
                {"pos": 2, "name": "Порог-угол Д13 40х20мм алюминий",
                 "quantity": 2, "price": 449.0, "total": 898.0,
                 "nds_name": "22 %", "nds_sum": 161.0, "measure": "шт"},
                {"pos": 3, "name": "Порог А5-37мм алюминированный",
                 "quantity": 1, "price": 299.0, "total": 299.0,
                 "nds_name": "22 %", "nds_sum": 54.0, "measure": "шт"},
            ],
            "properties": [],
            "taxation_name": "УСН",
            "receipt_note": {"fns_status": "valid"},
            "qr": qr[row["id"]],
        }}
    return cards


class TestAoPdfService:
    def test_cyrillic_text_is_searchable_and_font_is_embedded(self):
        rep, meta = _report(), _meta()
        qr = {"r1": "t=20261105T1230&s=350.50&fn=9999078902001299&i=123&fp=456&n=1"}
        pdf = ao_pdf.build_ao_pdf(rep, meta, qr, include_receipts=True)
        assert pdf.startswith(b"%PDF")
        assert b"/FontFile" in pdf and b"DejaVu" in pdf
        assert _image_count(pdf) > 0                # QR как картинка
        text = _text(pdf)
        for needle in ("АВАНСОВЫЙ ОТЧЁТ", "Форма по ОКУД", "0302001",
                       "Оборотная сторона", "ООО «Ромашка»", "Иван Отчётный",
                       "350,50", "Расписка", "Приложения", "Сформировано"):
            assert needle in text, needle
        assert "Реестр чеков" not in text             # старый упрощённый шаблон ушёл
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
        policy = ao_pdf._resource_policy(96)
        assert policy.allow_remote is False
        assert policy.allow_private_networks is False
        assert policy.max_resources == 96
        assert policy.max_render_seconds >= 240
        assert ao_pdf.FONT_DIR.resolve() in policy.roots

    def test_missing_font_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(ao_pdf, "FONT_PATH", ROOT / "app/assets/fonts/no-such-font.ttf")
        try:
            ao_pdf.build_ao_pdf(_report(), _meta(), {}, False)
        except RuntimeError as exc:
            assert "DejaVuSans" in str(exc)
        else:
            raise AssertionError("PDF без встроенного кириллического шрифта не должен собираться")

    def test_amount_in_words_matches_print_form(self):
        assert ao_pdf._amount_in_words(201366.34) == (
            "двести одна тысяча триста шестьдесят шесть рублей 34 коп.")
        assert ao_pdf._amount_in_words(1000.01) == "одна тысяча рублей 01 коп."
        assert ao_pdf._amount_in_words(0) == "ноль рублей 00 коп."


class TestAoPdfLayout:
    def test_ao1_front_back_and_column_widths(self):
        rep, meta, _qr = _many_report(1)
        source = ao_pdf._html(rep, meta, {}, False)
        assert source.count("<colgroup>") >= 4
        assert "overflow-wrap: anywhere" in source
        assert "display: table-header-group" in source
        assert "0302001" in source and "Расписка" in source
        assert "Реестр чеков" not in source
        assert "на <b>22</b> лист(ах)" in source

    def test_all_42_itemized_copies_qr_and_no_text_overlaps(self):
        rep, meta, qr = _many_report(42)
        cards = _cards_for(rep, qr)
        pdf = ao_pdf.build_ao_pdf(rep, meta, qr, include_receipts=True,
                                  cards_by_id=cards)
        reader = PdfReader(io.BytesIO(pdf))
        assert 8 <= len(reader.pages) <= 27
        last = reader.pages[-1].extract_text() or ""
        assert len(last.strip()) > 300, "хвостовая страница почти пустая"
        assert _collisions(pdf) == []
        text = _text(pdf)
        assert "Оборотная сторона формы № АО-1" in text
        assert "Документ, подтверждающий расходы" in text
        assert "Показаны QR первых 40 чеков из 42" not in text
        assert "Приложены полные копии всех 42 чеков с QR" in text
        assert text.count("КАССОВЫЙ ЧЕК") == 42
        assert text.count("Копия по данным системы") == 42
        assert text.count("Плинтус напольный") == 42       # itemized позиции
        assert "116427" in text                            # 42-й чек есть в реестре
        assert "83769,00" in text
        assert _image_count(pdf) == 42
        assert not hasattr(ao_pdf, "PDF_QR_LIMIT")

    def test_long_receipt_is_split_between_items_with_continuations(self):
        rep, meta, qr = _many_report(1)
        cards = _cards_for(rep, qr)
        doc = cards["r1"]["document"]
        doc["items"] = [
            {"pos": n, "name": f"Товар {n:03d}", "quantity": 2,
             "price": 100.0 + n, "total": 200.0 + 2 * n,
             "nds_name": "22 %", "nds_sum": 40.0 + n, "measure": "шт"}
            for n in range(1, 41)
        ]
        pdf = ao_pdf.build_ao_pdf(rep, meta, qr, include_receipts=True,
                                  cards_by_id=cards)
        text = _text(pdf)
        assert text.count("КАССОВЫЙ ЧЕК") == 1
        assert "Товар 001" in text and "Товар 040" in text
        assert "— продолжение приложения № 1" in text
        assert "↓ продолжение — часть 2" in text
        assert "Копия по данным системы" in text
        assert _image_count(pdf) == 1

    def test_missing_card_renders_safe_fallback_copy(self):
        rep, meta, qr = _many_report(2)
        pdf = ao_pdf.build_ao_pdf(rep, meta, qr, include_receipts=True)
        text = _text(pdf)
        assert text.count("КАССОВЫЙ ЧЕК") == 2
        assert text.count("Позиции недоступны — полный документ ФНС не получен.") == 2
        assert _image_count(pdf) == 2

    def test_qr_images_survive_low_global_pillow_limit(self, monkeypatch):
        """OCR-воркер когда-то оставлял MAX_IMAGE_PIXELS=10_000 в общем процессе.

        xhtml2pdf тогда молча выбрасывал 164×164 QR как «decompression bomb»;
        PDF оставался без картинок, хотя текст и вёрстка проходили проверки.
        """
        from PIL import Image
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000)
        rep, meta, qr = _many_report(2)
        pdf = ao_pdf.build_ao_pdf(rep, meta, qr, include_receipts=True)
        assert _image_count(pdf) == 2
        assert Image.MAX_IMAGE_PIXELS == 10_000


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
                       "Иван Отчётный", "Пётр Готовый", "350,50", "Приложения",
                       "КАССОВЫЙ ЧЕК", "Копия по данным системы",
                       "Позиции недоступны — полный документ ФНС не получен."):
            assert needle in text, needle
        assert text.count("КАССОВЫЙ ЧЕК") == 2
        assert _image_count(pdf) == 2

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
