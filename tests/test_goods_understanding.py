"""Понимание чеков (T-69, v1.70.101, ADR-31): словарь с исключениями, читаемые названия, модель, каскад,
память исправлений бухгалтера, профили компании и политика. Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import gzip
import json
import random
import time
from datetime import datetime
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.goods import model as goods_model
from app.goods import names, profiles, service
from app.goods.models import GoodsMemory
from app.models import Receipt, ReceiptItem
from app.services import goods, policy
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity
from tests.test_v1140 import _mk_user

companies = _base.companies
EVAL = "tests/fixtures/goods_eval.tsv"


def _eval_rows() -> list[tuple[str, str]]:
    rows = []
    with open(EVAL, encoding="utf-8") as fh:
        for line in fh:
            if line.strip() and not line.startswith("#"):
                cat, name = line.rstrip("\n").split("\t")
                rows.append((cat, name))
    return rows


# --- словарь: ошибки прежней версии исправлены ---------------------------------------------------------
FIXED = {
    "Джинсы мужские": "clothes",                    # было «Алкоголь» («джин» внутри слова)
    "Виноград кишмиш": "food",                      # было «Алкоголь» («вино»)
    "Брусника замороженная": "food",                # было «Стройматериалы» («брус»)
    "Порошок стиральный Tide 3 кг": "chemicals",    # было «Электроника» («стиральн»)
    "Гель для душа Nivea": "hygiene",               # было «Лекарства» («гель для»)
    "Перчатки резиновые хозяйственные": "household",  # было «Одежда»
    "Корм для животных": "pets",                    # было «Продукты»
    "Соль йодированная": "food",                    # было «Лекарства» («йод»)
    "Набор для бадминтона": "sport",                # было «Лекарства» («бад»)
    "Кабель USB Type-C": "gadgets",                 # было «Стройматериалы» («кабел»)
    "Доска разделочная": "household",               # было «Стройматериалы» («доска»)
    "Капсулы для стирки Ariel": "chemicals",        # было «Лекарства» («капсул»)
    "Краска для волос Palette": "hygiene",          # было «Стройматериалы» («краска»)
    "Ремень ГРМ": "auto",                           # было «Одежда» («ремень»)
    "Наушники вкладыши проводные": "gadgets",       # «проводные» — не «провод»
    "Стол обеденный": "furniture",                  # «обеденный» — не «обед»
    "Аренда бетономешалки": "rent",                 # услуга в начале названия
    "Ремонт телефона": "repair",
    "Изолента ПВХ синяя": "build_electric",         # было «Прочее»
    "Хомут нейлоновый 200 мм": "build_fasteners",
    "Перфоратор Makita HR2470": "build_tools",
    "Шуруповерт аккумуляторный": "build_tools",     # не «шуруп»
    "Жидкие гвозди Момент": "build_finish",         # не «гвозди»
    "Труба профильная 40х20": "build_base",         # металлопрокат, а не сантехника
    "Кофе молотый Jacobs 250г": "food",             # в магазине — продукты
    "Капучино 300мл": "catering",
}


@pytest.mark.parametrize(("name", "code"), list(FIXED.items()))
def test_dictionary_fixes(name, code):
    found = goods.match(name)
    assert found is not None and found[0].code == code, (name, found and found[0].code)


def test_groups_and_rules_stay_compatible():
    assert [g for _, g, _ in goods.RULES][0] == "Строительные материалы и инструмент"
    assert len(goods.RULES) == 19 and len(goods.categories()) == 37
    assert set(goods.groups()) == {g for _, g, _ in goods.RULES} | {"Прочее"}
    for cat in goods.categories():                     # категория принадлежит одной из прежних групп
        assert cat.group in goods.groups() and cat.icon and cat.title


def test_dictionary_is_fast():
    names_ = [n for _, n in _eval_rows()]
    start = time.perf_counter()
    for name in names_:
        goods.match(name)
    per_item = (time.perf_counter() - start) / len(names_)
    assert per_item < 0.002, per_item                  # было ~0,4 мс: шаблоны компилировались на каждый вызов


# --- читаемые названия --------------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "display", "pack"), [
    ("[М] 4607001234567 ЦЕМЕНТ М500 Д0 50КГ", "Цемент М500 Д0, 50 кг", "50 кг"),
    ("Арт. 123456 КРАСКА ВД-АК 2180 БЕЛАЯ 14КГ", "Краска ВД-АК 2180 белая, 14 кг", "14 кг"),
    ("САМОРЕЗ ПО МЕТАЛЛУ 4,2Х16 20ШТ/УП", "Саморез по металлу 4,2х16, 20 шт", "20 шт"),
    ("Молоко Простоквашино 3,2% 930мл", "Молоко Простоквашино 3,2%, 930 мл", "930 мл"),
    ("Вода питьевая 6х1,5л б/г", "Вода питьевая без газа, 6 × 1,5 л", "6 × 1,5 л"),
    ("Пакет д/мусора 60л 20шт", "Пакет для мусора 60 л, 20 шт", "20 шт"),
    ("ГИПСОКАРТОН ГКЛ 12,5ММ 2500Х1200", "Гипсокартон ГКЛ 12,5 мм 2500х1200", ""),
    ("ROCKWOOL ЛАЙТ БАТТС 50ММ", "ROCKWOOL лайт баттс 50 мм", ""),
    ("Бензин АИ-95", "Бензин АИ-95", ""),
    ("Шампунь 2 в 1 Head&Shoulders 400мл", "Шампунь 2 в 1 Head&Shoulders, 400 мл", "400 мл"),
    ("Календарь настольный 2027 г.", "Календарь настольный 2027 г.", ""),
    ("Смесь штукатурная Ротбанд 30 кг (арт. 10002)", "Смесь штукатурная Ротбанд, 30 кг", "30 кг"),
])
def test_readable_names(raw, display, pack):
    got = names.readable(raw)
    assert (got["display"], got["pack"]) == (display, pack)
    assert got["original"] == raw                      # исходная строка ФНС не теряется


def test_readable_never_loses_the_name():
    assert names.readable("4607001234567")["display"] == "4607001234567"   # от названия не остаётся пусто
    assert names.readable("")["display"] == ""


def test_name_key_joins_packs_and_case():
    assert names.name_key("ЦЕМЕНТ М500 Д0 50КГ") == names.name_key("Цемент М500 Д0 25 кг") == "цемент"
    assert names.name_key("Смесь штукатурная Ротбанд 30 кг") == "смесь штукатурная ротбанд"
    assert names.name_key("4607001234567") == ""


# --- модель -------------------------------------------------------------------------------------------
def test_model_file_is_small_and_valid():
    raw = goods_model.MODEL_PATH.read_bytes()
    assert len(raw) <= 5 * 1024 * 1024                 # критерий T-69: модель — единицы МБ
    data = json.loads(gzip.decompress(raw))
    assert data["format"] == goods_model.FORMAT
    assert data["classes"] == [c.code for c in goods.categories()]
    assert "okpd2" in data["sources"] and data["metrics"]["eval_clean"]["rows"] == len(_eval_rows())


def test_model_understands_abbreviations_and_brands():
    m = goods_model.load()
    assert m is not None
    for name, group in [("ШПАКЛ. ФИНИШН. ВЕТОНИТ 20КГ", "Строительные материалы и инструмент"),
                        ("Смартфон Xiaomi Redmi", "Электроника и бытовая техника"),
                        ("Масло Лукойл Генезис 5W-40", "Автотовары")]:
        best = m.predict(name)["top"][0][0]
        assert goods.category(best).group == group, (name, best)


def test_cascade_accuracy_on_held_out_set():
    """Проверочный набор модель не видела: каскад точнее словаря и честно молчит, когда не знает."""
    rows = _eval_rows()
    answered = correct = dict_correct = 0
    for cat, name in rows:
        v = service.understand(name)
        hit = goods.match(name)
        dict_correct += bool(hit and hit[0].code == cat)
        if v.source != "none":
            answered += 1
            correct += v.category == cat
    assert answered / len(rows) >= 0.97
    assert correct / answered >= 0.97                  # точность, когда каскад отвечает
    assert correct > dict_correct                      # лучше одного словаря


def test_model_keeps_working_with_cashier_noise():
    rows = _eval_rows()
    rng = random.Random(7)
    m = goods_model.load()
    ok_model = ok_dict = 0
    for cat, name in rows:
        words = name.split()
        j = max(range(len(words)), key=lambda i: len(words[i]))
        if len(words[j]) >= 6:
            words[j] = words[j][:rng.randint(3, 5)] + "."            # касса сокращает главное слово
        noisy = " ".join(words).upper()
        ok_model += goods.category(m.predict(noisy)["top"][0][0]).group == goods.category(cat).group
        hit = goods.match(noisy)
        ok_dict += bool(hit and hit[0].group == goods.category(cat).group)
    assert ok_model / len(rows) >= 0.85
    assert ok_model > ok_dict + len(rows) * 0.2        # сокращения словарь не понимает, модель — да


def test_model_is_fast_enough():
    m = goods_model.load()
    names_ = [n for _, n in _eval_rows()]
    start = time.perf_counter()
    for name in names_:
        m.predict(name)
    assert (time.perf_counter() - start) / len(names_) < 0.005     # миллисекунды на позицию


# --- каскад -------------------------------------------------------------------------------------------
def test_cascade_order_memory_fns_dictionary_model():
    assert service.understand("Цемент М500", memory={"цемент": "build_finish"}).source == "memory"
    v = service.understand("Пакет документов", product_type=4)
    assert (v.source, v.group) == ("fns", "Услуги")
    v = service.understand("Доставка до объекта", product_type=4)
    assert (v.source, v.category) == ("fns", "delivery")
    v = service.understand("Цемент М500 50кг")
    assert (v.source, v.category) == ("dictionary", "build_base") and "цемент" in v.note
    v = service.understand("Ротбанд 30кг")
    assert v.source == "dictionary"
    v = service.understand("ШПАКЛ. ФИНИШН. 20КГ")
    assert v.source in ("model", "none")


def test_receipt_type_and_seller_explain_unknown_items():
    items = [{"name": "Цемент М500 50кг", "total": 600}, {"name": "Песок строительный 40кг", "total": 200},
             {"name": "Абвгд ёжз", "total": 100}]
    out = service.understand_receipt(items)
    assert out[2].source == "context" and out[2].group == "Строительные материалы и инструмент"
    assert "по другим товарам чека" in out[2].note and out[2].score < 0.9   # догадка — не для политики
    seller = service.understand_receipt([{"name": "Абвгд ёжз", "total": 100}], merchant_name="ООО «Леруа Мерлен Восток»")
    assert seller[0].source == "context" and "Леруа Мерлен" in seller[0].note
    alone = service.understand_receipt([{"name": "Абвгд ёжз", "total": 100}])
    assert alone[0].source == "none" and alone[0].category == "other"       # без подсказок — честно «не определено»


def test_cafe_and_shop_swap():
    cafe = service.understand_receipt([{"name": "Чай черный 0,4", "total": 150}], merchant_name="Шоколадница")
    assert cafe[0].category == "catering" and cafe[0].source == "context"
    shop = service.understand_receipt([{"name": "Кофе американо", "total": 150}], merchant_name="Пятёрочка")
    assert shop[0].category == "food"


def test_profiles_articles_and_defaults():
    assert profiles.default_forbidden("general") == policy.DEFAULT_FORBIDDEN_GOODS
    assert "Продукты питания" not in profiles.default_forbidden("catering")
    assert profiles.article_for("construction", "build_tools") == "Инструмент и инвентарь"
    assert profiles.article_for("office", "food") == "Хозяйственные расходы"
    hint = profiles.article_hint("construction", [{"category": "build_base", "total": 900},
                                                  {"category": "food", "total": 100}])
    assert hint["article"] == "Материалы" and hint["share"] == 0.9
    assert profiles.article_hint("general", [{"category": "food", "total": 100}]) == {}


# --- запись, API и память -----------------------------------------------------------------------------
def _receipt(db, company_id, items, merchant="ООО «Ромашка»"):
    fn, fd, fp = _identity()
    total = sum(t for _, t in items)
    r = Receipt(fn=fn, fd=fd, fp=fp, qr_data=f"t=20261022T1556&s={total}&fn={fn}&i={fd}&fp={fp}&n=1",
                receipt_date=datetime(2026, 10, 22, 15, 56), total_sum=total, operation=1,
                company_id=company_id, merchant_name=merchant, merchant_inn="4703148343")
    for pos, (name, amount) in enumerate(items):
        r.items.append(ReceiptItem(name=name, quantity=1, price=amount, total=amount, position=pos))
    db.add(r)
    db.flush()
    service.classify_receipt(db, r)
    db.commit()
    return r.id


def test_classify_writes_source_score_note_and_summary(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid = _receipt(db, c1, [("ЦЕМЕНТ М500 Д0 50КГ", 600.0), ("Песок строительный 40кг", 200.0),
                                ("Абвгд ёжз", 50.0)])
    data = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
    first = data["items"][0]
    assert first["display_name"] == "Цемент М500 Д0, 50 кг" and first["name"] == "ЦЕМЕНТ М500 Д0 50КГ"
    assert (first["category"], first["category_source"], first["category_certain"]) == ("build_base", "dictionary", True)
    assert data["items"][2]["category_source"] == "context" and data["items"][2]["category_certain"] is False
    assert data["industry"] == "Строительство и ремонт (DIY)"
    assert data["goods"]["categories"][0]["category"] == "build_base"
    assert data["goods"]["article_hint"]["article"] == "Материалы"
    card = client.get(f"/api/v1/receipts/{rid}/card", headers=hdr).json()
    assert card["document"]["items"][0]["category_title"] == "Стройматериалы"


def test_correction_is_remembered_and_applied(companies, client):
    hdr, c1, c2, a1, a2 = companies
    with SessionLocal() as db:
        r1 = _receipt(db, c1, [("Смесь Абвгд 25кг", 500.0)])
        r2 = _receipt(db, c1, [("СМЕСЬ АБВГД 40КГ", 700.0)])
        other = _receipt(db, c2, [("Смесь Абвгд 25кг", 500.0)])
        item1 = db.query(ReceiptItem).filter_by(receipt_id=r1).one().id
        item_other = db.query(ReceiptItem).filter_by(receipt_id=other).one().id
    res = client.put(f"/api/v1/goods/items/{item1}", headers=a1, json={"category": "build_finish"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["remembered"] and body["applied"] == 1 and body["item"]["category_source"] == "manual"
    with SessionLocal() as db:
        twin = db.query(ReceiptItem).filter_by(receipt_id=r2).one()
        assert (twin.goods_category, twin.group_source) == ("build_finish", "memory")
        foreign = db.get(ReceiptItem, item_other)
        assert foreign.group_source != "memory"        # память — по компании (ADR-20)
        # новый чек компании сразу получает категорию из памяти
        r3 = _receipt(db, c1, [("Смесь Абвгд 5 кг", 90.0)])
        assert db.query(ReceiptItem).filter_by(receipt_id=r3).one().group_source == "memory"
        mem = db.query(GoodsMemory).filter_by(company_id=c1, name_key="смесь абвгд").one()
        assert mem.hits >= 1
    # чужая позиция — «не найдена», сотрудник категорию не меняет
    assert client.put(f"/api/v1/goods/items/{item_other}", headers=a1, json={"category": "food"}).status_code == 404
    user = _mk_user(client, hdr, c1, "goods_u_" + uuid4().hex[:8])
    assert client.put(f"/api/v1/goods/items/{item1}", headers=user, json={"category": "food"}).status_code == 403
    assert client.put(f"/api/v1/goods/items/{item1}", headers=a1, json={"category": "nope"}).status_code == 400
    # повторный разбор ручную правку не трогает
    with SessionLocal() as db:
        r = db.get(Receipt, r1)
        service.classify_receipt(db, r, force=True)
        assert r.items[0].group_source == "manual"
        db.rollback()
    listing = client.get("/api/v1/goods/memory", headers=a1).json()
    row = next(x for x in listing["items"] if x["name_key"] == "смесь абвгд")
    assert client.get("/api/v1/goods/memory", headers=a2).json()["items"] == [] or \
        all(x["name_key"] != "смесь абвгд" for x in client.get("/api/v1/goods/memory", headers=a2).json()["items"])
    assert client.delete(f"/api/v1/goods/memory/{row['id']}", headers=a2).status_code == 404
    assert client.delete(f"/api/v1/goods/memory/{row['id']}", headers=a1).status_code == 200


def test_unsure_guess_is_not_a_policy_violation(companies):
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid = _receipt(db, c1, [("Цемент М500 50 кг", 600.0), ("Пиво светлое 0,5", 120.0)])
        r = db.get(Receipt, rid)
        assert policy.FORBIDDEN_GOODS in policy.flags_for(db, r)
        beer = r.items[1]
        beer.group_source, beer.group_score = "model", 0.6          # догадка модели
        db.flush()
        assert policy.FORBIDDEN_GOODS not in policy.flags_for(db, r)
        assert policy.refresh_receipts(db, [r]) >= 0 and "forbidden_goods" not in (r.policy_flags or "")
        beer.group_score = 0.95                                     # очень уверенная модель — считается
        assert policy.FORBIDDEN_GOODS in policy.flags_for(db, r)
        db.rollback()


def test_profile_in_policy_settings(companies, client):
    hdr, c1, _, _, _ = companies
    got = client.get(f"/api/v1/settings/policy?company_id={c1}", headers=hdr).json()
    assert got["goods_profile"] == "general" and got["goods_own"] is False
    assert {p["code"] for p in got["goods_profiles"]} >= {"general", "construction", "catering", "office"}
    res = client.put("/api/v1/settings/policy", headers=hdr, json={"company_id": c1, "goods_profile": "catering"})
    assert res.status_code == 200, res.text
    got = client.get(f"/api/v1/settings/policy?company_id={c1}", headers=hdr).json()
    assert got["goods_profile"] == "catering" and "Продукты питания" not in got["forbidden_goods"]
    client.put("/api/v1/settings/policy", headers=hdr, json={"company_id": c1, "forbidden_goods": ["Алкогольные напитки"]})
    assert client.get(f"/api/v1/settings/policy?company_id={c1}", headers=hdr).json()["goods_own"] is True
    client.put("/api/v1/settings/policy", headers=hdr, json={"company_id": c1, "reset_goods": True})
    got = client.get(f"/api/v1/settings/policy?company_id={c1}", headers=hdr).json()
    assert got["goods_own"] is False and got["forbidden_goods"] == list(profiles.default_forbidden("catering"))
    assert client.put("/api/v1/settings/policy", headers=hdr,
                      json={"company_id": c1, "goods_profile": "космос"}).status_code == 400


def test_manual_items_get_categories(companies, client):
    hdr, c1, _, a1, _ = companies
    with SessionLocal() as db:
        rid = _receipt(db, c1, [("Товар", 100.0)])
    res = client.patch(f"/api/v1/receipts/{rid}", headers=a1, json={"items": [
        {"name": "Перфоратор Bosch", "quantity": 1, "price": 9000, "total": 9000}]})
    assert res.status_code == 200, res.text
    item = client.get(f"/api/v1/receipts/{rid}", headers=a1).json()["items"][0]
    assert (item["category"], item["category_source"]) == ("build_tools", "dictionary")


def test_catalog_lists_categories_profiles_and_model(client, companies):
    hdr = companies[0]
    data = client.get("/api/v1/goods/catalog", headers=hdr).json()
    assert len(data["categories"]) == 38 and data["categories"][-1]["code"] == "other"
    assert data["model"]["loaded"] is True and data["sources"]["context"] == "по чеку и продавцу"
    client.cookies.clear()                                          # без входа — 401
    assert client.get("/api/v1/goods/catalog").status_code == 401
