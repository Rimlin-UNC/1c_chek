# -*- coding: utf-8 -*-
"""
Ямастер Чек — тесты. ООО «Ямастер» | ymaster.ru | info@ymaster.ru

Запуск:  python3 -m pytest tests/ -v
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.qr import (ParsedQR, QRParseError, build_qr_string,
                             looks_like_receipt_qr, parse_qr)
from app.services import exporter


# ---------------------------------------------------------------------------
#  Разбор QR (54-ФЗ)
# ---------------------------------------------------------------------------
class TestQrParsing:
    def test_standard_string(self):
        p = parse_qr("t=20250901T1430&s=1520.50&fn=9999078902001234&i=54321&fp=812345678&n=1")
        assert p.fn == "9999078902001234"
        assert p.fd == "54321"
        assert p.fp == "812345678"
        assert p.total_sum == 1520.50
        assert p.date_time == dt.datetime(2025, 9, 1, 14, 30)
        assert p.operation == 1

    def test_semicolon_separator(self):
        p = parse_qr("t=20250901T1430;s=100;fn=9999078902001234;i=1;fp=1234567890;n=2")
        assert p.fd == "1"
        assert p.total_sum == 100.0
        assert p.operation == 2

    def test_url_format(self):
        p = parse_qr("https://receipt.example.ru/?fn=9999078902001234&i=54321&fp=812345678&s=99.99&t=20250901T1430")
        assert p.fn == "9999078902001234"
        assert p.total_sum == 99.99

    def test_fpd_alias(self):
        p = parse_qr("t=20250901T1430&s=50&fn=9999078902001234&i=54321&fpd=812345678&n=1")
        assert p.fp == "812345678"

    def test_sum_with_comma(self):
        p = parse_qr("t=20250901T1430&s=1520,50&fn=9999078902001234&i=54321&fp=812345678")
        assert p.total_sum == 1520.50

    def test_missing_fn_raises(self):
        with pytest.raises(QRParseError):
            parse_qr("t=20250901T1430&s=100&i=54321&fp=812345678")

    def test_bad_fn_raises(self):
        with pytest.raises(QRParseError):
            parse_qr("t=20250901T1430&s=100&fn=abc&i=54321&fp=812345678")

    def test_garbage_raises(self):
        with pytest.raises(QRParseError):
            parse_qr("просто случайный текст из жизни")

    def test_short_raises(self):
        with pytest.raises(QRParseError):
            parse_qr("t=1")

    def test_looks_like(self):
        assert looks_like_receipt_qr("t=20250901T1430&s=100&fn=9999078902001234&i=54321&fp=812345678")
        assert not looks_like_receipt_qr("https://example.com")

    def test_build_roundtrip(self):
        qr = build_qr_string(dt.datetime(2025, 9, 22, 10, 0), 799.99,
                             "9999078902001234", "12345", "987654321")
        p = parse_qr(qr)
        assert p.total_sum == 799.99
        assert p.date_time == dt.datetime(2025, 9, 22, 10, 0)

    def test_dedup_key(self):
        p = parse_qr("t=20250901T1430&s=100&fn=9999078902001234&i=54321&fp=812345678")
        assert p.dedup_key == "9999078902001234|54321|812345678"


# ---------------------------------------------------------------------------
#  Выгрузка EnterpriseData
# ---------------------------------------------------------------------------
class TestExporter:
    def _receipt(self):
        class R:
            id = "abcd1234-0000"
            fn, fd, fp = "9999078902001234", "54321", "812345678"
            receipt_date = dt.datetime(2025, 9, 1, 14, 30)
            created_at = dt.datetime(2025, 9, 2, 9, 0)
            total_sum = 1520.50
            operation = 1
            qr_data = "t=20250901T1430&s=1520.50&fn=9999078902001234&i=54321&fp=812345678&n=1"
            fns_status = "valid"
            items = []
        return R()

    def test_json_structure(self):
        r = self._receipt()
        data = exporter.build_enterprise_data_json([r], [], "ПоступлениеТоваровУслуг")
        import json
        d = json.loads(data)
        assert d["ФорматВерсии"] == "1.3"
        assert d["Данные"]["Контейнер"][0]["ФискальныеРеквизитыЧека"]["ФН"] == r.fn
        assert d["Данные"]["Контейнер"][0]["КлючевыеСвойства"]["СуммаДокумента"] == 1520.50

    def test_xml_structure(self):
        r = self._receipt()
        xml = exporter.build_enterprise_data_xml([r], [], "АвансовыйОтчет")
        assert "EnterpriseData" in xml
        assert "9999078902001234" in xml
        assert "АвансовыйОтчет" in xml

    def test_transforms(self):
        assert exporter.apply_transform(100.0, "kopecks") == 10000
        assert exporter.apply_transform("1", "operation_sign") == "Приход"
        assert exporter.apply_transform("2", "operation_sign") == "ВозвратПрихода"
        assert exporter.apply_transform(dt.datetime(2025, 9, 1, 14, 30), "date_iso") == "2025-09-01T14:30:00"
        assert exporter.apply_transform("x", "constant", "Фикс") == "Фикс"
        assert exporter.apply_transform("x", "direct") == "x"

    def test_push_payload(self):
        r = self._receipt()
        payload = exporter.build_push_payload([r], [], "ПоступлениеТоваровУслуг")
        assert payload["format"] == "ymaster-push-v1"
        assert payload["receipts"][0]["fn"] == r.fn
        assert payload["receipts"][0]["sum_kopecks"] == 152050


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
