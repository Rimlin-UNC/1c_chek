# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.55.0: максимум данных из proverkacheka (ext_json),
# цепочка источников без «своих шлюзов», электронный чек после скана,
# HTML-чек в письме, мобильный фикс главной.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

# Реальный ответ proverkacheka.com /api/v1/check/get (прислал владелец)
PROVERKA_PAYLOAD = {
    "code": 3,
    "nds0": 0,
    "user": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СТРОЙТОРГОВЛЯ"',
    "items": [
        {"nds": 11, "sum": 13410, "name": "Розетка Оптима РА16-374 2-х мест.. белая. с з/к. 250В. 16А",
         "price": 13410, "quantity": 1, "paymentType": 4, "productType": 1,
         "itemsQuantityMeasure": 0},
        {"nds": 11, "sum": 9810, "name": "Вилка эл. с заземлением угловая с ушком черная 16А А0112 UNIVersal",
         "price": 9810, "quantity": 1, "paymentType": 4, "productType": 1,
         "itemsQuantityMeasure": 0},
    ],
    "region": "47",
    "userInn": "4703148343  ",
    "dateTime": "2026-08-17T15:28:00",
    "kktRegId": "0007993537061927    ",
    "metadata": {"id": 6492275452577315844, "ofdId": "ofd5",
                 "address": "188760,Россия,Ленинградская область,Приозерский м.р-н.,Приозерское г.п.,Приозерск г.,,Красноармейская ул.,,зд. 6а,,,,",
                 "subtype": "receipt", "receiveDate": "2026-08-17T12:29:31Z"},
    "operator": "Касса самообслуживания",
    "totalSum": 23220,
    "creditSum": 0,
    "numberKkt": "00106925180632",
    "fiscalSign": 1291708960,
    "prepaidSum": 0,
    "properties": [{"propertyName": "Номер заказа", "propertyValue": "140933ПРИ"}],
    "retailPlace": 'Магазин "TД Вимос"',
    "shiftNumber": 50,
    "cashTotalSum": 0,
    "provisionSum": 0,
    "ecashTotalSum": 23220,
    "operationType": 1,
    "requestNumber": 64,
    "amountsReceiptNds": {"amountsNds": [{"nds": 11, "ndsSum": 4187}]},
    "fiscalDriveNumber": "7380440902582955",
    "retailPlaceAddress": "47 - Ленинградская область, м.р-н Приозерский,188760, г/п Приозерское, г. Приозерск, ул. Красноармейская, зд. 6 а. 2",
    "appliedTaxationType": 1,
    "fiscalDocumentNumber": 5321,
    "fiscalDocumentFormatVer": 4,
    "user_data": {"qrraw": "t=20260817t1528&s=232.20&fn=7380440902582955&i=5321&fp=1291708960&n=1",
                  "id": "40480267", "promo_id": "0", "date_scan": "",
                  "date_create": "2026-10-05 12:33:00"},
}


class TestVersion1550:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.55.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.54.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.55.0':" in js
        assert js.index("'1.55.0':") < js.index("'1.54.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.55.0]") == 1
        assert ch.index("## [1.55.0]") < ch.index("## [1.54.0]")


class TestProverkachekaMaxData:
    def test_parse_real_payload(self):
        """Реальный ответ proverkacheka → максимум полей извлекается."""
        from app.services.external import parse_receipt_payload
        res = parse_receipt_payload(PROVERKA_PAYLOAD, known_rub=232.20)
        assert res.found and res.ok
        assert "СТРОЙТОРГОВЛЯ" in res.merchant_name
        assert res.merchant_inn.strip() == "4703148343"
        assert "Красноармейская" in res.merchant_address
        assert res.cashier == "Касса самообслуживания"
        assert res.total_sum == 232.20
        assert res.ecash_sum == 232.20
        assert len(res.items) == 2
        assert "Розетка Оптима" in res.items[0].name
        assert res.items[0].price == 134.10
        assert res.date_time is not None
        ex = res.extra
        assert ex["retail_place"] == 'Магазин "TД Вимос"'
        assert ex["kkt_reg_id"] == "0007993537061927"
        assert ex["shift_number"] == 50
        assert ex["request_number"] == 64
        assert ex["region"] == "47"
        assert ex["taxation"] == "УСН (доходы)"
        assert ex["ofd_id"] == "ofd5"
        assert ex["receive_date"] == "2026-08-17T12:29:31Z"
        assert ex["fiscal_drive_number"] == "7380440902582955"
        assert ex["fiscal_document_number"] == 5321
        assert ex["fiscal_sign"] == 1291708960
        assert ex["source_receipt_id"] == "40480267"
        assert ex["properties"] == [{"name": "Номер заказа",
                                     "value": "140933ПРИ"}]
        assert ex["nds_totals"] == [{"nds": 11, "ndsSum": 4187}]

    def test_ext_json_column_and_apply(self):
        from app.models import Receipt
        assert hasattr(Receipt, "ext_json")
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert "receipt.ext_json = json.dumps(res.extra" in src
        assert "receipts ADD COLUMN ext_json" in open("app/database.py",
                                                      encoding="utf-8").read()


class TestSourcesChain:
    def test_default_chain_no_custom(self, monkeypatch):
        from app.services.external import ExternalFetchEngine
        from app.database import SessionLocal
        monkeypatch.setattr(
            "app.services.external.settings.FNS_MASTER_TOKEN", "", raising=False)
        eng = ExternalFetchEngine()
        db = SessionLocal()
        try:
            chain = eng.provider_chain(db)
        finally:
            db.close()
        assert "custom" not in " ".join(chain)
        assert chain[-1] == "mock"
        # proverkacheka не влезает раньше официальных источников
        if "proverkacheka" in chain:
            for p in ("fns_api", "fns_app", "crpt", "ofd_ru"):
                if p in chain:
                    assert chain.index("proverkacheka") > chain.index(p)

    def test_chain_ignores_custom_even_if_configured(self, monkeypatch):
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "external_order": "custom,proverkacheka",
            "custom_sources": [{"name": "x", "url": "https://x"}],
            "proverkacheka_token": "tok", "fns_master_token": "",
        })
        chain = eng.provider_chain(None)
        assert all(not c.startswith("custom") for c in chain)
        assert "proverkacheka" in chain

    def test_settings_card_text_updated(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "ofd_ru,custom,proverkacheka" not in js   # порядок без custom
        assert "ext-custom-urls" not in js and "external_custom_urls" not in js
        assert "отдаёт максимум полей" in js


class TestEcheckFlow:
    def test_public_receipt_includes_items(self):
        src = open("app/pool/router_public.py", encoding="utf-8").read()
        assert '_receipt_public(r, db)' in src
        assert '"items"' in src and "PoolItem" in src
        assert '"fd": r.fd' in src and '"merchant_inn": r.merchant_inn' in src

    def test_receipt_email_html(self):
        src = open("app/pool/router_public.py", encoding="utf-8").read()
        assert "html=_html" in src
        assert "ЭЛЕКТРОННЫЙ ЧЕК" in src
        assert "#/my" in src                       # приглашение в кабинет

    def test_echeck_ui_and_guard(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function renderEcheck(anchor, r)" in js
        assert "ЭЛЕКТРОННЫЙ ЧЕК" in js
        assert "Прислать HTML-чек" in js
        assert "ec-cabinet" in js and "showPoolScreen" in js
        # страховка от «белого экрана» при сбое отрисовки
        assert "Promise.resolve((renderers[view]" in js
        assert "showPublicScreen();" in js.split("const _fallback")[1][:400]
        # e-check появляется после проверки чека
        assert "renderEcheck(box, full);" in js

    def test_login_and_landing_intact(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="login-form"' in idx and 'id="login-passkey"' in idx
        assert "Сканируйте чеки" in idx            # лендинг на месте

    def test_mobile_css_fixes(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        # экран входа прокручивается — «белый экран» невозможен
        assert "overflow-y: auto" in css.split(".login-screen {")[1][:400]
        assert ".login-screen > * { margin: auto; }" in css
        assert ".echeck-wrap" in css and "echeckIn" in css
        assert "max-width: 920px" in css


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Проверка чеков (ФНС и источники)"] == "1.55.0"
        assert reg["Чек-Пул"] == "1.55.0"
        assert reg["Обновления"] == "1.53.0"       # не задет
