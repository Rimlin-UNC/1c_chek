# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.2: РЕАЛЬНЫЙ ответ proverkacheka.com
# (чек «ТД Вимос», 08.10.2026) разбирается ЦЕЛИКОМ — «минимум такой
# формат» гарантирован автотестом.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import re

# Настоящий ответ proverkacheka.com (прислал владелец, 08.10.2026)
REAL_PAYLOAD = {
    "messageFiscalSign": 9297260049989052000,
    "code": 3,
    "fiscalDocumentFormatVer": 4,
    "fiscalDriveNumber": "7380440902582955",
    "kktRegId": "0007993537061927    ",
    "userInn": "4703148343  ",
    "fiscalDocumentNumber": 9895,
    "dateTime": "2026-10-08T13:05:00",
    "fiscalSign": 3304159072,
    "shiftNumber": 103,
    "requestNumber": 47,
    "operationType": 1,
    "totalSum": 80730,
    "items": [{"itemsQuantityMeasure": 0, "name": "Порог-угол Д3 24х18мм алюм.анодированный серебро НЕ 0.9м",
               "price": 26910, "quantity": 3, "sum": 80730, "nds": 11,
               "paymentType": 4, "productType": 1}],
    "properties": {"propertyName": "Номер заказа", "propertyValue": "151939ПРИ"},
    "cashTotalSum": 0,
    "ecashTotalSum": 80730,
    "prepaidSum": 0,
    "creditSum": 0,
    "provisionSum": 0,
    "nds0": 0,
    "amountsReceiptNds": {"amountsNds": [{"nds": 11, "ndsSum": 14558}]},
    "appliedTaxationType": 1,
    "operator": "Касса самообслуживания",
    "retailPlaceAddress": "47 - Ленинградская область, м.р-н Приозерский,188760, г/п Приозерское, г. Приозерск, ул. Красноармейская, зд. 6 а. 2",
    "retailPlace": "Магазин \"TД Вимос\"",
    "user": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ \"СТРОЙТОРГОВЛЯ\"",
    "region": "47",
    "numberKkt": "00106925180632",
    "redefine_mask": 2,
    "metadata": {"id": 6567510110362718000, "ofdId": "ofd5",
                 "receiveDate": "2026-10-08T10:08:27Z", "subtype": "receipt",
                 "address": "188760,Россия,Ленинградская область,Приозерский м.р-н.,Приозерское г.п.,Приозерск г.,,Красноармейская ул.,,зд. 6а,,,,"},
}


class TestVersion1572:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.57.2"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.2':") < js.index("'1.57.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.2]") == 1
        assert ch.index("## [1.57.2]") < ch.index("## [1.57.1]")


class TestRealPayloadParsedFully:
    """Весь присленный ответ разбирается без потерь — «минимум такой формат»."""

    def setup_method(self):
        from app.services.external import parse_receipt_payload
        self.res = parse_receipt_payload(REAL_PAYLOAD, known_rub=807.30)

    def test_main_fields(self):
        r = self.res
        assert r.ok and r.found
        assert 'СТРОЙТОРГОВЛЯ' in r.merchant_name
        assert r.merchant_inn.strip() == "4703148343"
        assert "Красноармейская" in r.merchant_address
        assert r.cashier == "Касса самообслуживания"
        assert r.total_sum == 807.30
        assert r.ecash_sum == 807.30
        assert r.cash_sum is None                 # наличных 0 → пусто
        assert r.operation == 1
        assert r.date_time == dt.datetime(2026, 10, 8, 13, 5)

    def test_items(self):
        assert len(self.res.items) == 1
        it = self.res.items[0]
        assert it.name.startswith("Порог-угол Д3")
        assert it.price == 269.10
        assert it.total == 807.30
        assert it.vat_rate == "11"

    def test_properties_object_form_not_lost(self):
        """ГЛАВНОЕ: properties пришёл ОБЪЕКТОМ — раньше терялся молча."""
        ex = self.res.extra
        assert ex["properties"] == [{"name": "Номер заказа",
                                     "value": "151939ПРИ"}]

    def test_fiscal_and_kkt(self):
        ex = self.res.extra
        assert ex["kkt_reg_id"] == "0007993537061927"      # пробелы срезаны
        assert ex["number_kkt"] == "00106925180632"
        assert ex["fiscal_drive_number"] == "7380440902582955"
        assert ex["fiscal_document_number"] == 9895
        assert ex["fiscal_sign"] == 3304159072
        assert ex["shift_number"] == 103
        assert ex["request_number"] == 47

    def test_tax_nds_payments(self):
        ex = self.res.extra
        assert ex["taxation"] == "УСН (доходы)"
        assert ex["nds0"] == 0
        assert ex["nds_totals"] == [{"nds": 11, "ndsSum": 14558}]
        assert "prepaid_sum" not in ex             # нули не пишем
        assert "credit_sum" not in ex and "provision_sum" not in ex

    def test_service_and_source_fields(self):
        ex = self.res.extra
        assert ex["ffd_version"] == 4
        assert ex["ofd_id"] == "ofd5"
        assert ex["receive_date"] == "2026-10-08T10:08:27Z"
        assert ex["doc_subtype"] == "receipt"
        assert "Красноармейская" in ex["source_address"]
        assert ex["retail_place"] == 'Магазин "TД Вимос"'
        assert ex["region"] == "47"
        assert ex["message_fiscal_sign"] == 9297260049989052000
        assert ex["redefine_mask"] == 2
        assert ex["source_code"] == 3

    def test_items_meta(self):
        assert self.res.extra["items_meta"] == [
            {"paymenttype": 4, "producttype": 1,
             "itemsquantitymeasure": 0, "pos": 1}]

    def test_ext_stored_and_served(self):
        """Данные применяются к чеку и отдаются в API (v1.57.1)."""
        import json
        import uuid
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            rid = str(uuid.uuid4())
            db.add(Receipt(id=rid, qr_data="t=…", fn="7380440902582955",
                           fd="9895", fp="3304159072", total_sum=807.30,
                           receipt_date=dt.datetime(2026, 10, 8, 13, 5),
                           ext_json=json.dumps(self.res.extra,
                                               ensure_ascii=False)))
            db.commit()
            d = db.get(Receipt, rid).to_dict()
            assert d["ext"]["properties"] == [{"name": "Номер заказа",
                                               "value": "151939ПРИ"}]
            assert d["ext"]["message_fiscal_sign"] == 9297260049989052000
            db.delete(db.get(Receipt, rid))
            db.commit()
        finally:
            db.close()


class TestPropertiesListFormStillWorks:
    """Массив (старый вид из ответа августа) тоже принимается."""

    def test_list_form(self):
        from app.services.external import parse_receipt_payload
        payload = dict(REAL_PAYLOAD)
        payload["properties"] = [{"propertyName": "Номер заказа",
                                  "propertyValue": "140933ПРИ"}]
        res = parse_receipt_payload(payload, known_rub=807.30)
        assert res.extra["properties"] == [{"name": "Номер заказа",
                                            "value": "140933ПРИ"}]


class TestUiShowsAll:
    def test_new_labels_in_ext_block(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Контрольный знак сообщения" in js
        assert "Код ответа источника" in js
        assert "Служебная маска источника" in js


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Проверка чеков (ФНС и источники)"] == "1.57.2"
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет
