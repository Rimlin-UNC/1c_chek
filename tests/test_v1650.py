# Ямастер Чек — тесты v1.65.0: PDF-чеки (электронные чеки из почты/банка).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки (docs/concept_roadmap_v160.md, Фаза 1 п.5):
#  - текстовые PDF идут в ТОТ ЖЕ конвейер: QR из текста → обычный приём
#    (source="pdf", дедуп, авто-проверки, правила v1.61);
#  - лимит размера (PDF_MAX_MB=5) и числа страниц (50);
#  - сканированный PDF без текстового слоя → честный отказ
#    «пришлите фото/QR», OCR на сервере НЕ делаем (концепция);
#  - пайплайн печати чеков (printpack, v1.24+) НЕ тронут;
#  - pypdf — в requirements.txt (контроль зависимостей);
#  - версии/WHATS_NEW/CHANGELOG/блок «Сканирование»/инструкция — ритуал.
import io
import re

import pytest

_SEQ = {"n": 65000}


# ------------------------------------------------------------------ версии
class TestVersion1650:
    """Строгий версионный замок релиза v1.65.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.65.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.64.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.65.0':") < js.index("'1.64.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.65.0]") == 1
        assert ch.index("## [1.65.0]") < ch.index("## [1.64.0]")

    def test_scan_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Сканирование чеков"] == "1.65.0"


# ------------------------------------------------------------------ помощник
def _pdf(*text_lines):
    """Минимальный ВАЛИДНЫЙ одностраничный PDF с текстом (без зависимостей).

    pypdf.extract_text() вытаскивает строки из content-stream (Tj/T*)."""
    parts = ["BT /F1 14 Tf 72 760 Td 16 TL"]
    for ln in text_lines:
        esc = (ln.replace("\\", r"\\")
                 .replace("(", r"\(").replace(")", r"\)"))
        parts.append(f"({esc}) Tj T*")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def _qr_line():
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return (f"t=20261005T1200&s=1250.00&fn=928{k:07d}"
            f"&i=65{k:03d}&fp=165{k:03d}&n=1")


# ------------------------------------------------------------------ сервис
class TestPdfService:
    """Извлечение QR-строк из текстового слоя PDF."""

    def test_extracts_qr_from_text_layer(self):
        from app.services import pdf_receipts as pr
        qr = _qr_line()
        texts = pr.extract_qr_texts(_pdf("ООО «Ромашка»", "КАССОВЫЙ ЧЕК",
                                         qr, "ИТОГ: 1250.00"))
        assert texts == [qr]

    def test_no_text_layer_raises_honest(self):
        from app.services import pdf_receipts as pr
        try:
            pr.extract_qr_texts(_pdf())            # пустая страница
            raise AssertionError("ожидали ValueError")
        except ValueError as e:
            assert "QR" in str(e) or "текста" in str(e)

    def test_not_a_pdf_raises(self):
        from app.services import pdf_receipts as pr
        for bad in (b"", b"not a pdf at all", b"%PDF-1.4 garbage"):
            try:
                pr.extract_qr_texts(bad)
                raise AssertionError("ожидали ValueError")
            except ValueError:
                pass

    def test_page_limit(self):
        from app.services import pdf_receipts as pr
        # многстраничный PDF соберём конкатенацией объектов нельзя —
        # поэтому лимит проверяем на большом числе строк одной страницы:
        qr = _qr_line()
        many = _pdf(*([qr] * 10))                  # валидный PDF
        texts = pr.extract_qr_texts(many)
        assert len(texts) == 1                     # дубли строк схлопываются
        # лимит страниц: подменяем константу
        old = pr.MAX_PDF_PAGES
        pr.MAX_PDF_PAGES = 0
        try:
            try:
                pr.extract_qr_texts(_pdf("строка"))
                raise AssertionError("ожидали ValueError")
            except ValueError as e:
                assert "страниц" in str(e)
        finally:
            pr.MAX_PDF_PAGES = old

    def test_only_qr_like_strings(self):
        from app.services import pdf_receipts as pr
        qr = _qr_line()
        texts = pr.extract_qr_texts(_pdf(
            "Чек № 12345 от 05.10.2026", "random text without markers", qr))
        assert texts == [qr]


# ------------------------------------------------------------------ API
class TestPdfEndpoint:
    """POST /receipts/scan/pdf — тот же конвейер, что скан и фото."""

    def test_pdf_receipt_flow(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        qr = _qr_line()
        r = client.post("/api/v1/receipts/scan/pdf", headers=H,
                        files={"file": ("чек.pdf", _pdf("КАССОВЫЙ ЧЕК", qr),
                                        "application/pdf")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["duplicate"] is False
        rec = body["receipt"]
        assert rec["source"] == "pdf"
        assert rec["fn"].endswith(qr.split("&fn=")[1].split("&")[0])
        # дедуп: тот же PDF повторно — «уже есть»
        r2 = client.post("/api/v1/receipts/scan/pdf", headers=H,
                         files={"file": ("чек.pdf", _pdf(qr),
                                         "application/pdf")})
        assert r2.status_code == 200
        assert r2.json()["duplicate"] is True

    def test_pdf_without_qr_is_422(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/scan/pdf", headers=H,
                        files={"file": ("чек.pdf", _pdf("Текст без QR"),
                                        "application/pdf")})
        assert r.status_code == 422
        assert "QR" in r.json()["detail"]

    def test_pdf_without_text_layer_is_422(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/scan/pdf", headers=H,
                        files={"file": ("скан.pdf", _pdf(),
                                        "application/pdf")})
        assert r.status_code == 422
        assert "QR" in r.json()["detail"]

    def test_not_pdf_is_422(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/scan/pdf", headers=H,
                        files={"file": ("фото.png", b"\x89PNG fake",
                                        "image/png")})
        assert r.status_code == 422
        assert "PDF" in r.json()["detail"]

    def test_too_big_is_413(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        big = b"%PDF-1.4\n" + b"\x00" * (6 * 1024 * 1024)
        r = client.post("/api/v1/receipts/scan/pdf", headers=H,
                        files={"file": ("большой.pdf", big,
                                        "application/pdf")})
        assert r.status_code == 413
        assert "5" in r.json()["detail"]           # в сообщении лимит 5 МБ

    def test_print_pipeline_untouched(self):
        """Пайплайн ПЕЧАТИ чеков (printpack, v1.24+) не тронут."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function whenImagesReady(root)" in js
        assert "qrList = data.items.slice(0, 300)" in js
        assert "packCut" in js


class TestPdfUI:
    """Интерфейс: кнопка и приём PDF на странице сканирования."""

    def test_static_markers(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "btn-pdf" in js and "Загрузить PDF-чек" in js
        assert 'accept="image/*,.pdf,application/pdf"' in js
        assert "application/pdf" in js             # клиентская проверка типа
        assert "не изображение и не PDF" in js     # честный тост


class TestManualSection:
    """Инструкция: раздел «Чек из PDF» для всех."""

    def test_manual_has_pdf_section(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.get("/api/v1/manual", headers=H)
        assert r.status_code == 200
        d = r.json()
        titles = {s["title"] for s in d["sections"]}
        assert "Чек из PDF" in titles
        sec = [s for s in d["sections"] if s["title"] == "Чек из PDF"][0]
        assert sec["audience"] == "all"
        assert sec["image"] == "/img/manual/pdfcheck.jpg"
        img = open("app/static/img/manual/pdfcheck.jpg", "rb").read()
        assert img[:2] == b"\xff\xd8"              # настоящий JPEG
