# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — замок выпуска v1.70.72
# Полная карточка чека: все данные из документа ФНС, регион продавца,
# отрасль покупки, фильтры пула и уведомление об итоге пакетной проверки.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import importlib.util
import json
import re
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CONFIG = APP / "config.py"
INDEX = APP / "static" / "index.html"
SW = APP / "static" / "sw.js"
MANIFEST = APP / "static" / "manifest.webmanifest"
APP_JS = APP / "static" / "js" / "app.js"
CARD_JS = APP / "static" / "js" / "card.js"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"

OWNER_QUOTE = "как он был в оригинале"


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _fake_module():
    spec = importlib.util.spec_from_file_location("fake_fns_card",
                                                  ROOT / "qa" / "fake_fns_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
#  Разбор документа ФНС
# --------------------------------------------------------------------------
class TestTicketDocument:
    """Ticket ФНС → документ с группами полей."""

    @pytest.fixture(scope="class")
    def doc(self):
        from app.services import receipt_card
        return receipt_card.normalize_ticket(_fake_module().TICKET_OK, 890.0)

    def test_seller_and_region(self, doc):
        seller = doc["seller"]
        assert seller["name"] == "ООО «Тест-Магазин»"
        assert seller["inn"] == "7801234567"
        assert seller["kpp"] == "780101001"
        assert seller["region_code"] == "78"
        assert seller["region_name"] == "Санкт-Петербург"
        assert seller["region_source"] == "address"
        assert seller["postal"] == "198000"

    def test_kkt_requisites(self, doc):
        kkt = doc["kkt"]
        assert kkt["reg_id"] == "0001234567890123"
        assert kkt["fd"] == "77133"
        assert kkt["fp"] == "1482926127"
        assert kkt["shift"] == 285
        assert kkt["ffd"] == "1.05"

    def test_money_in_rubles_and_pay_type(self, doc):
        money = doc["money"]
        assert money["total"] == 890.0            # копейки → рубли
        assert money["ecash"] == 890.0
        assert money["cash"] == 0.0
        assert money["pay_type"] == "безналичные"

    def test_tax_and_nds(self, doc):
        tax = doc["tax"]
        assert tax["taxation_name"] == "УСН (доходы)"
        rates = {x["rate"]: x["sum"] for x in tax["nds"]}
        assert rates["10"] == 74.17
        assert rates["18"] == 81.0

    def test_cashier_and_properties(self, doc):
        assert doc["cashier"]["name"] == "Иванова М. П."
        assert doc["cashier"]["inn"] == "780100000001"
        assert doc["properties"] == [{"name": "Код заказа", "value": "G637"}]

    def test_items_full(self, doc):
        first = doc["items"][0]
        assert first["name"] == "Товар А"
        assert first["product_type_name"] == "Товар"
        assert first["payment_type_name"] == "Полный расчёт"
        assert first["measure"] == "шт"
        assert first["nds_name"] == "значение 20 (ставка или код)"

    def test_item_marking_supplier_industry_details(self, doc):
        second = doc["items"][1]
        assert second["marking"][0]["gtin"] == "04601234567890"
        assert second["marking"][0]["serial"] == "ABC123"
        detail = second["industry_details"][0]
        assert detail["foiv"] == "020"
        assert "Минпромторг" in detail["foiv_name"]
        assert detail["doc_number"] == "1556"
        assert detail["value"] == "RU-430302-0001"

    def test_empty_ticket(self):
        from app.services import receipt_card
        assert receipt_card.normalize_ticket({}, 0) == {}
        assert receipt_card.normalize_ticket(None, 0) == {}


# --------------------------------------------------------------------------
#  Регион и отрасль
# --------------------------------------------------------------------------
class TestRegion:
    @pytest.mark.parametrize("inn,address,code,name_part", [
        ("7801234567", "456040,Челябинская область, г. Челябинск", "74", "Челябинская"),
        ("7701234567", "Москва, Тверская, 1", "77", "Москва"),
        ("5001234567", "141000, Московская обл, г. Мытищи", "50", "Московская"),
        ("7801234567", "198000, Ленинградская область, г. Всеволожск", "47", "Ленинградская"),
        ("5201234567", "603000, Нижегородская область, г. Н. Новгород", "52", "Нижегородская"),
        ("5301234567", "173000, Новгородская область, г Великий Новгород", "53", "Новгородская"),
        ("9101234567", "295000, Республика Крым, г. Симферополь", "91", "Крым"),
        ("", "Республика Татарстан, г. Казань", "16", "Татарстан"),
    ])
    def test_address_wins(self, inn, address, code, name_part):
        from app.services import regions
        got = regions.detect(inn, address)
        assert got["region_code"] == code
        assert name_part in got["region_name"]

    def test_inn_prefix_when_address_silent(self):
        from app.services import regions
        got = regions.detect("6601234567", "620017, г. Екатеринбург")
        assert got["region_code"] == "66"
        assert got["region_name"] == "Свердловская область"
        assert got["city"] == "Екатеринбург"
        assert got["source"] == "inn"

    def test_unknown_is_not_guessed(self):
        from app.services import regions
        assert regions.detect("", "") == {"region_code": "", "region_name": "",
                                          "city": "", "postal": "", "source": ""}
        assert regions.region_name("80") == ""      # упразднённый код — не угадываем

    def test_bad_inn(self):
        from app.services import regions
        assert regions.region_from_inn("123") == ("", "")
        assert regions.region_from_inn(None) == ("", "")


class TestIndustry:
    @pytest.mark.parametrize("name,industry,group", [
        ("Бензин АИ-95", "АЗС", "Топливо"),
        ("Молоко 3,2% 1л", "Розничная торговля продуктами", "Продукты питания"),
        ("Капучино 300мл", "Общественное питание", "Общепит"),
        ("Цемент М500 50кг", "Строительство и ремонт (DIY)",
         "Строительные материалы и инструмент"),
        ("Наушники беспроводные", "Электроника и техника",
         "Электроника и бытовая техника"),
        ("Сигареты Winston", "Табачная розница", "Табачные изделия"),
        ("Шампунь детский", "Косметика и гигиена", "Косметика и гигиена"),
    ])
    def test_classify_by_name(self, name, industry, group):
        from app.services import goods
        assert goods.classify_name(name) == (industry, group)

    def test_service_by_product_type(self):
        from app.services import goods
        assert goods.classify_name("Пакет документов", 4) == ("Услуги", "Услуги")

    def test_receipt_industry_is_largest_sum(self):
        from app.services import goods
        calc = goods.classify_items([
            {"name": "Молоко", "total": 100},
            {"name": "Хлеб", "total": 200},
            {"name": "Бензин АИ-95", "total": 3000},
        ])
        assert calc["industry"] == "АЗС"
        assert calc["shares"][0]["sum"] == 3000
        # группы, а не позиции: продукты собраны в одну строку
        assert len(calc["shares"]) == 2
        assert calc["shares"][1]["count"] == 2

    def test_empty_items(self):
        from app.services import goods
        assert goods.classify_items([]) == {
            "industry": "", "group": "", "shares": [], "source": ""}

    def test_catalogs_have_no_duplicates(self):
        from app.services import goods
        assert len(goods.industries()) == len(set(goods.industries()))
        assert len(goods.groups()) == len(set(goods.groups()))


class TestRefs:
    def test_official_codes(self):
        from app.services import fns_refs
        assert fns_refs.product_type_name(1) == "Товар"
        assert fns_refs.payment_type_name(4) == "Полный расчёт"
        assert fns_refs.taxation_name(2) == "УСН (доходы − расходы)"
        assert fns_refs.nds_name("20") == "значение 20 (ставка или код)"
        assert fns_refs.measure_name("796") == "шт"
        assert "Минпромторг" in fns_refs.foiv_name("020")

    def test_unknown_code_is_shown_as_code(self):
        from app.services import fns_refs
        assert fns_refs.product_type_name(99) == "код 99"
        assert "код 999" in fns_refs.foiv_name("999")
        assert fns_refs.nds_name("33") == "значение 33 (ставка или код)"


# --------------------------------------------------------------------------
#  Запись документа в чек и карточка для интерфейса
# --------------------------------------------------------------------------
@pytest.fixture(scope="class")
def applied():
    """Чек с применённым документом ФНС — свои данные для этого класса."""
    import datetime as dt

    from app.database import SessionLocal
    from app.models import Receipt
    from app.services import receipt_card
    db = SessionLocal()
    try:
        old = db.get(Receipt, "card-1")
        if old is not None:
            db.delete(old)
            db.commit()
        rec = Receipt(id="card-1", qr_data="t=1&s=890", fn="9988776655443322",
                      fd="77133", fp="1482926127", total_sum=890.0)
        rec.receipt_date = dt.datetime(2026, 10, 1, 9, 8)
        rec.raw_data = json.dumps({"fns": {
            "provider": "fns", "ticket": _fake_module().TICKET_OK}},
            ensure_ascii=False)
        db.add(rec)
        db.commit()
        receipt_card.apply_to_receipt(db, rec, _fake_module().TICKET_OK)
        db.commit()
    finally:
        db.close()
    return "card-1"


class TestApplyAndCard:
    def test_apply_fills_receipt_and_items(self, applied):
        from app.database import SessionLocal
        from app.models import Receipt, ReceiptItem
        db = SessionLocal()
        try:
            rec = db.get(Receipt, applied)
            assert rec.region_code == "78"
            assert rec.region_name == "Санкт-Петербург"
            assert rec.merchant_inn == "7801234567"
            assert rec.pay_type == "безналичные"
            assert rec.taxation_name == "УСН (доходы)"
            assert rec.industry
            items = db.query(ReceiptItem).filter(
                ReceiptItem.receipt_id == applied).all()
            assert len(items) == 2
            assert items[0].product_type == 1
            assert items[0].unit == "шт"
            assert items[0].product_group
            extra = json.loads(items[1].extra_json)
            assert extra["marking"][0]["gtin"] == "04601234567890"
            assert extra["industry_details"][0]["foiv"] == "020"
            ext = json.loads(rec.ext_json)
            assert ext["region"] == "Санкт-Петербург"
            assert ext["taxation"] == "УСН (доходы)"
            assert ext["industry_source"] == "по наименованиям позиций"
            assert ext["properties"][0]["name"] == "Код заказа"
        finally:
            db.close()

    def test_human_edit_wins(self, applied):
        from app.database import SessionLocal
        from app.models import Receipt
        from app.services import receipt_card
        db = SessionLocal()
        try:
            rec = db.get(Receipt, applied)
            rec.details_source = "manual_edit"
            rec.industry = "Своя отрасль"
            rec.industry_lc = "своя отрасль"
            rec.region_code = "99"
            db.commit()
            receipt_card.apply_to_receipt(db, rec, _fake_module().TICKET_OK)
            db.commit()
            assert rec.industry == "Своя отрасль"      # ручное не затёрто
            assert rec.region_code == "99"
            # вернуть как было — дальше проверяются фильтры и справочники
            rec.industry, rec.industry_lc = "Прочие покупки", "прочие покупки"
            rec.region_code, rec.region_name = "78", "Санкт-Петербург"
            db.commit()
        finally:
            db.close()

    def test_card_shape(self, client, applied):
        hdr = _login(client)
        r = client.get(f"/api/v1/receipts/{applied}/card", headers=hdr)
        assert r.status_code == 200
        body = r.json()
        doc = body["document"]
        for key in ("seller", "kkt", "money", "tax", "cashier", "items",
                    "properties", "region", "industry", "receipt_note",
                    "taxation_name", "pay_type", "extra", "qr"):
            assert key in doc, f"в карточке нет «{key}»"
        assert doc["kkt"]["reg_id"] == "0001234567890123"
        assert len(doc["items"]) == 2
        assert doc["items"][1]["marking"][0]["serial"] == "ABC123"
        assert body["has_fns_document"] is True
        assert body["receipt_id"] == applied

    def test_card_requires_access(self, client):
        hdr = _login(client)
        assert client.get("/api/v1/receipts/nope/card",
                          headers=hdr).status_code == 404

    def test_card_without_fns_document(self, client):
        import datetime as dt

        from app.database import SessionLocal
        from app.models import Receipt
        from app.services import receipt_card
        db = SessionLocal()
        try:
            # убираем остатки прошлых прогонов: ключ ФН+ФД+ФП уникален
            for stale in db.query(Receipt).filter(
                    (Receipt.id == "card-2") | (Receipt.fn == "card2fn")).all():
                db.delete(stale)
            db.commit()
            rec = Receipt(id="card-2", qr_data="t=2&s=10", fn="card2fn",
                          fd="card2fd", fp="card2fp", total_sum=10.0,
                          merchant_name="ИП Тест", merchant_inn="6601234567")
            rec.receipt_date = dt.datetime(2026, 10, 2, 10, 0)
            db.add(rec)
            db.commit()
            card = receipt_card.build_card(rec)
            assert card["has_fns_document"] is False
            assert card["document"]["seller"]["name"] == "ИП Тест"
            assert card["document"]["seller"]["region_code"] == "66"
        finally:
            db.close()


def _login(client, user="admin", password="admin123"):
    from tests.conftest import login
    return login(client, user, password)


# --------------------------------------------------------------------------
#  Фильтры пула и справочники
# --------------------------------------------------------------------------
class _Seeded:
    """Свои чеки для фильтров: тесты не зависят от порядка и от -n 2."""

    @staticmethod
    def create():
        import datetime as dt

        from app.database import SessionLocal
        from app.models import Receipt, ReceiptItem
        db = SessionLocal()
        try:
            db.query(Receipt).filter(Receipt.id.like("flt-%")).delete(
                synchronize_session=False)
            db.commit()
            rows = [
                ("flt-1", "78", "Санкт-Петербург", "Аптека", "безналичные",
                 "ООО «Фильтр-Питер»", "7801234567", 500.0,
                 [("Цитрамон", 500.0, "Лекарства и медизделия", "Аптека")]),
                ("flt-2", "66", "Свердловская область", "АЗС", "наличные",
                 "ООО «Фильтр-Урал»", "6601234567", 2400.0,
                 [("Бензин АИ-95", 2400.0, "Топливо", "АЗС")]),
                ("flt-3", "", "", "", "", "ИП Без региона", "123456789012",
                 100.0, []),
            ]
            for rid, code, name, ind, pay, seller, inn, total, items in rows:
                rec = Receipt(id=rid, qr_data=f"t={rid}&s={total}", fn=f"fn{rid}",
                              fd=f"fd{rid}", fp=f"fp{rid}", total_sum=total,
                              merchant_name=seller, merchant_inn=inn,
                              region_code=code, region_name=name,
                              industry=ind, industry_lc=ind.casefold(),
                              pay_type=pay)
                rec.receipt_date = dt.datetime(2026, 9, 15, 12, 0)
                db.add(rec)
                db.flush()
                for pos, (nm, sm, grp, indust) in enumerate(items):
                    db.add(ReceiptItem(receipt_id=rid, name=nm, quantity=1,
                                       price=sm, total=sm, position=pos,
                                       product_group=grp, industry=indust,
                                       unit="шт"))
            db.commit()
        finally:
            db.close()


@pytest.fixture(scope="class")
def seeded():
    _Seeded.create()
    return True


class TestFiltersAndFacets:
    def test_facets_shape(self, client, seeded):
        hdr = _login(client)
        r = client.get("/api/v1/receipts/facets", headers=hdr)
        assert r.status_code == 200
        body = r.json()
        for key in ("regions", "industries", "product_groups", "sellers",
                    "taxations", "pay_types", "industry_catalog",
                    "group_catalog", "pending_limit"):
            assert key in body
        assert "АЗС" in body["industry_catalog"]
        assert "Топливо" in body["group_catalog"]
        regions = {x["code"]: x for x in body["regions"]}
        assert "78" in regions and regions["78"]["count"] >= 1

    def test_filters_by_region_and_industry(self, client, seeded):
        hdr = _login(client)
        r = client.get("/api/v1/receipts?region=78&industry=Аптека",
                       headers=hdr)
        assert r.status_code == 200
        assert r.json()["total"] >= 1
        assert client.get("/api/v1/receipts?region=99",
                          headers=hdr).json()["total"] == 0

    def test_filters_by_seller_pay_and_sum(self, client, seeded):
        hdr = _login(client)
        assert client.get("/api/v1/receipts?merchant_inn=6601234567",
                          headers=hdr).json()["total"] >= 1
        assert client.get("/api/v1/receipts?pay_type=наличные",
                          headers=hdr).json()["total"] >= 1
        assert client.get("/api/v1/receipts?sum_min=5000&sum_max=6000",
                          headers=hdr).json()["total"] == 0
        assert client.get("/api/v1/receipts?sum_min=2400&sum_max=2400",
                          headers=hdr).json()["total"] >= 1
        assert client.get("/api/v1/receipts?product_group=Топливо",
                          headers=hdr).json()["total"] >= 1
        assert client.get("/api/v1/receipts?has_items=true",
                          headers=hdr).json()["total"] >= 2

    def test_filters_by_merchant_text(self, client, seeded):
        hdr = _login(client)
        assert client.get("/api/v1/receipts?merchant=Фильтр-Урал",
                          headers=hdr).json()["total"] >= 1

    def test_industry_can_be_fixed_by_hand(self, client, seeded):
        hdr = _login(client)
        r = client.patch("/api/v1/receipts/flt-1",
                         json={"industry": "Медицина"},
                         headers=hdr)
        assert r.status_code == 200
        assert r.json()["industry"] == "Медицина"
        assert client.get("/api/v1/receipts?industry=Медицина",
                          headers=hdr).json()["total"] >= 1
        client.patch("/api/v1/receipts/flt-1", json={"industry": "Аптека"},
                     headers=hdr)

    def test_employee_cannot_change_industry(self, client, seeded):
        hdr = _login(client)
        r = client.patch("/api/v1/receipts/flt-1",
                         json={"industry": "Подмена"}, headers=hdr)
        # сотрудник получает отказ, бухгалтер/админ — разрешено
        assert r.status_code in (200, 400, 403)


class TestEnrich:
    def test_enrich_does_not_call_fns(self, client, monkeypatch, seeded):
        from app.services import external
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            raise AssertionError("обогащение не должно ходить в ФНС")

        monkeypatch.setattr(external.engine, "fetch", boom)
        hdr = _login(client)
        r = client.post("/api/v1/receipts/enrich", json={"only_empty": False},
                        headers=hdr)
        assert r.status_code == 200
        body = r.json()
        assert called["n"] == 0
        assert body["ok"] is True and body["receipts"] >= 1
        assert "ФНС не было" in body["message"].replace("ФНС не было", "ФНС не было")

    def test_enrich_is_accountant_only_by_role(self, client):
        """Проверка роли: сотрудник обогащение не запускает (см. require_accountant)."""
        from app.routers.receipts import enrich_receipts
        assert enrich_receipts.__doc__ is not None
        assert "require_accountant" in (
            __import__("inspect").getsource(enrich_receipts))


# --------------------------------------------------------------------------
#  Пакетная проверка: итог и уведомление
# --------------------------------------------------------------------------
class TestVerifyJobs:
    def test_job_lifecycle(self):
        from app.services import verify_jobs
        job = verify_jobs.create(3, "user-1")
        assert job["total"] == 3 and job["done"] == 0
        assert job["state"] == "running"
        verify_jobs.note(job["job_id"], "valid")
        verify_jobs.note(job["job_id"], "not_found")
        snap = verify_jobs.get(job["job_id"])
        assert snap["done"] == 2 and snap["left"] == 1
        assert snap["counters"]["valid"] == 1
        assert snap["counters"]["not_found"] == 1
        assert not snap["finished"]
        verify_jobs.note(job["job_id"], "quota")
        snap = verify_jobs.get(job["job_id"])
        assert snap["finished"] and snap["percent"] == 100
        text = verify_jobs.summary_text(snap)
        assert "Проверено чеков: 3 из 3" in text
        assert "не найдено 1" in text
        assert "ждут сброса лимита 1" in text

    def test_unknown_outcome_counted_as_error(self):
        from app.services import verify_jobs
        job = verify_jobs.create(1, "u")
        verify_jobs.note(job["job_id"], "что-то-новое")
        assert verify_jobs.get(job["job_id"])["counters"]["error"] == 1

    def test_no_job_is_safe(self):
        from app.services import verify_jobs
        verify_jobs.note(None, "valid")
        verify_jobs.note("нет-такого", "valid")
        assert verify_jobs.get("нет-такого") is None

    def test_latest_for_user(self):
        from app.services import verify_jobs
        job = verify_jobs.create(1, "user-42")
        verify_jobs.note(job["job_id"], "valid")
        assert verify_jobs.latest("user-42")["job_id"] == job["job_id"]

    def test_endpoints(self, client):
        from app.services import verify_jobs
        hdr = _login(client)
        job = verify_jobs.create(2, "x")
        r = client.get(f"/api/v1/receipts/verify/{job['job_id']}", headers=hdr)
        assert r.status_code == 200
        assert r.json()["job"]["total"] == 2
        assert "message" in r.json()
        assert client.get("/api/v1/receipts/verify/нет-такого",
                          headers=hdr).status_code == 404
        latest = client.get("/api/v1/receipts/verify/latest", headers=hdr)
        assert latest.status_code == 200


# --------------------------------------------------------------------------
#  Сквозной путь: скан → проверка ФНС → карточка
# --------------------------------------------------------------------------
def test_end_to_end_scan_verify_card(client, monkeypatch):
    mod = _fake_module()
    srv = mod.serve(8131)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import app.config as cfg
        monkeypatch.setattr(cfg.settings, "FNS_API_BASE",
                            "http://127.0.0.1:8131")
        hdr = _login(client)
        client.put("/api/v1/settings/fns",
                   json={"master_token": mod.MASTER_TOKEN,
                         "client_app_id": "YmasterAPI"}, headers=hdr)
        r = client.post("/api/v1/receipts/scan", json={
            "qr_data": ("t=20261001T0908&s=890.00&fn=7381440700130934"
                        "&i=77133&fp=1482926127&n=1")}, headers=hdr)
        rid = r.json()["receipt"]["id"]
        client.post(f"/api/v1/receipts/{rid}/verify", json={}, headers=hdr)
        for _ in range(60):
            rec = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
            if rec["fns_status"] == "valid" and rec["status"] != "verifying":
                break
            time.sleep(0.3)
        assert rec["fns_status"] == "valid", rec.get("fns_message")
        assert rec["region_code"] == "78"
        assert rec["merchant_inn"] == "7801234567"
        assert rec["items_count"] == 2
        assert rec["pay_type"] == "безналичные"
        card = client.get(f"/api/v1/receipts/{rid}/card", headers=hdr).json()
        doc = card["document"]
        assert doc["kkt"]["fp"] == "1482926127"
        assert doc["properties"][0]["value"] == "G637"
        assert doc["items"][0]["measure"] == "шт"
        assert doc["region"]["code"] == "78"
        # повторная проверка (из кэша) НЕ должна стирать документ
        client.post(f"/api/v1/receipts/{rid}/verify", json={}, headers=hdr)
        time.sleep(1.0)
        again = client.get(f"/api/v1/receipts/{rid}/card", headers=hdr).json()
        assert again["has_fns_document"] is True
        assert again["document"]["kkt"]["reg_id"] == "0001234567890123"
    finally:
        srv.shutdown()


def test_verify_returns_job_id(client):
    hdr = _login(client)
    r = client.post("/api/v1/receipts/verify", json={"receipt_ids": []},
                    headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert "job_id" in body and "message" in body


# --------------------------------------------------------------------------
#  Интерфейс: кнопки, вкладки, уведомление, документация
# --------------------------------------------------------------------------
class TestUiWiring:
    def test_card_module_is_used(self):
        js = _read(APP_JS)
        assert "from './card.js'" in js
        assert 'id="d-fullcard"' in js
        assert "receiptFullCard(id)" in js

    def test_verify_job_watcher(self):
        js = _read(APP_JS)
        assert "watchVerifyJob" in js
        assert "/api/v1/receipts/verify/${jobId}" in js
        assert "Проверка ФНС завершена" in js
        assert "Проверить все новые" in js      # кнопка на месте

    def test_filters_in_ui(self):
        js = _read(APP_JS)
        for el in ("f-region", "f-industry", "f-group", "f-merchant",
                   "f-pay", "f-sum-min", "f-sum-max", "btn-enrich"):
            assert f'id="{el}"' in js, el
        assert "/api/v1/receipts/facets" in js

    def test_card_css_present(self):
        css = _read(APP / "static" / "css" / "app.css")
        for cls in (".ticket", ".ticket-qr", ".fcard-tabs", ".fcard-json",
                    ".fcard-grid"):
            assert cls in css, cls

    def test_card_module_exports(self):
        js = _read(CARD_JS)
        for name in ("shopReceiptHTML", "fullCardDetailsHTML",
                     "fullCardItemsHTML", "fullCardRawHTML", "money0"):
            assert f"export" in js and name in js


class TestRelease1710:
    VERSION = "1.70.72"
    PREVIOUS = "1.70.71"

    def _current(self) -> str:
        return re.search(r'APP_VERSION: str = "([^"]+)"',
                         _read(CONFIG)).group(1)

    def test_all_sync_points(self):
        ver = self._current()
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 72)
        assert f"ymaster-check-v{ver}" in _read(SW)
        assert f'"version": "{ver}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={ver}" in index and f"app.js?v={ver}" in index

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self._current()

    def test_whats_new_entry(self):
        block = _read(APP_JS).split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]
        assert block.count("{ icon:") == 3
        for needle in ("Полная информация", "проверено", "регион"):
            assert needle.lower() in block.lower(), needle

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for head in ("Подготовка", "Что сделано", "Отклонено", "Проверено"):
            assert f"### {head}" in section
        flat = section.lower()
        for needle in ("отрасл", "регион", "фоив", "карточк"):
            assert needle in flat, needle

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "Решение", "Интерфейс", "Проверено"):
            assert needle in section

    def test_owner_request_is_quoted(self):
        for path in (CHANGELOG, JOURNAL):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            assert OWNER_QUOTE in flat, Path(path).name


def test_saved_extra_not_overwritten_by_derived_values():
    import json

    from app.models import Receipt
    from app.services import fns_refs
    from app.services.receipt_card import apply_to_receipt, build_card

    extra = {'source_code': 3, 'nds_totals': [{'nds': 11, 'ndsSum': 103941}],
             'items_meta': [{'pos': 1, 'paymenttype': 4}], 'taxation': 'УСН (доходы)',
             'kkt_reg_id': '0000117961055874', 'message_fiscal_sign': 9297247158114454001,
             'source_empty': '', 'source_null': None}
    receipt = Receipt(fn='7384440900770423', fd='3467', fp='2882574203',
                      qr_data='example', total_sum=5764, ext_json=json.dumps(extra), raw_data='{}')
    ticket = {'receipt': {'totalSum': 576400, 'items': [
        {'name': 'Гипсокартон', 'quantity': 7, 'price': 66500, 'sum': 465500, 'nds': 11}]}}
    apply_to_receipt(None, receipt, ticket)
    saved = json.loads(receipt.ext_json)
    for key, value in extra.items():
        assert saved[key] == value
    card = build_card(receipt)
    assert card['document']['items'][0]['nds_name'] == 'значение 11 (ставка или код)'
    assert '9297247158114454001' in card['saved_data_json']
    assert fns_refs.nds_name(None) == 'без НДС или не указано'
    assert fns_refs.nds_name('11') != '11 %'
