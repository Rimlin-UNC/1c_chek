# Ямастер Чек — тесты v1.63.0: самообучающийся маппинг
# («запомнить для поставщика ИНН»). ООО «Ямастер» | ymaster.ru
#
# Критерии приёмки (docs/concept_roadmap_v160.md, Фаза 1 п.3):
#  - правило-кандидат = компания + ИНН поставщика + поле
#    (статья/сотрудник/комментарий); кнопка «Запомнить» в карточке чека;
#  - автоприменение к будущим чекам — ТОЛЬКО в пустые поля, человеческие
#    значения не затираются; статусы не трогаются; ноль доп. запросов;
#  - массовое «Применить к старым» — тем же правилом по пустым полям;
#  - новая таблица merchant_rules (добавляющая, create_all); замки
#    1С-маппинга (mapping_settings, v1.20/v1.21) НЕ тронуты;
#  - версии/WHATS_NEW/CHANGELOG/блоки/инструкция — по ритуалу.
import re

import pytest

_SEQ = {"n": 63000}


# ------------------------------------------------------------------ версии
class TestVersion1630:
    """Строгий версионный замок релиза v1.63.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.63.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.62.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.63.0':") < js.index("'1.62.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.63.0]") == 1
        assert ch.index("## [1.63.0]") < ch.index("## [1.62.0]")

    def test_settings_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Настройки"] == "1.63.0"


# ------------------------------------------------------------------ правила
def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _company(db, name):
    from app.models import Company
    _SEQ["n"] += 1
    c = Company(name=f"{name} {_SEQ['n']}")
    db.add(c)
    db.flush()
    return c


def _receipt(db, comp, inn="", cat="", assignee="", comment=""):
    from app.models import Receipt
    _SEQ["n"] += 1
    k = _SEQ["n"]
    r = Receipt(qr_data=f"t&s&fn=9{k}&i={k}&fp={k}&n=1",
                fn=f"9{k}", fd=f"{k}", fp=f"{k}",
                receipt_date=__import__("datetime").datetime(2026, 10, 10, 12, 0),
                total_sum=500.0, operation=1, status="verified",
                company_id=comp.id if comp else None,
                merchant_inn=inn, category=cat, category_lc=cat.casefold(),
                assignee=assignee, assignee_lc=assignee.casefold(),
                comment=comment)
    db.add(r)
    db.flush()
    return r


def _rule(db, comp, inn, field, value):
    from app.models import MerchantRule
    r = MerchantRule(company_id=comp.id, merchant_inn=inn, field=field,
                     value=value, merchant_name="Пятёрочка")
    db.add(r)
    db.flush()
    return r


class TestMerchantRules:
    """Правила: автозаполнение пустых полей, границы, backfill."""

    def test_apply_fills_empty_only(self):
        from app.services import merchant_rules as mr
        db = _db()
        try:
            c = _company(db, "Автозаполнение")
            _rule(db, c, "7704001275", "category", "Канцелярия")
            r_empty = _receipt(db, c, inn="7704001275")
            applied = mr.apply_rules(db, r_empty)
            assert applied == ["category"]
            assert r_empty.category == "Канцелярия"
            # непустое поле человеческое — не трогаем
            r_set = _receipt(db, c, inn="7704001275", cat="Прочее")
            assert mr.apply_rules(db, r_set) == []
            assert r_set.category == "Прочее"
        finally:
            db.rollback()
            db.close()

    def test_scope_company_and_inn(self):
        from app.services import merchant_rules as mr
        db = _db()
        try:
            c1 = _company(db, "Границы1")
            c2 = _company(db, "Границы2")
            _rule(db, c1, "7704001276", "assignee", "Иванов")
            # та же компания + тот же ИНН → применяется
            r_ok = _receipt(db, c1, inn="7704001276")
            assert mr.apply_rules(db, r_ok) == ["assignee"]
            # другая компания (тот же ИНН) → нет
            r_other = _receipt(db, c2, inn="7704001276")
            assert mr.apply_rules(db, r_other) == []
            # тот же поставщик в другой сети/ИНН → нет
            r_inn = _receipt(db, c1, inn="7704009999")
            assert mr.apply_rules(db, r_inn) == []
            # чек без компании → нет
            r_none = _receipt(db, None, inn="7704001276")
            assert mr.apply_rules(db, r_none) == []
            # чек без ИНН → нет
            r_noinn = _receipt(db, c1, inn="")
            assert mr.apply_rules(db, r_noinn) == []
        finally:
            db.rollback()
            db.close()

    def test_apply_counts_and_truncates(self):
        from app.services import merchant_rules as mr
        db = _db()
        try:
            c = _company(db, "Счётчик")
            rule = _rule(db, c, "7704001277", "comment", "к" * 3000)
            r = _receipt(db, c, inn="7704001277")
            assert mr.apply_rules(db, r) == ["comment"]
            assert len(r.comment) == 2000          # обрезано до лимита поля
            assert rule.applied_count == 1
            r2 = _receipt(db, c, inn="7704001277")
            mr.apply_rules(db, r2)
            assert rule.applied_count == 2
        finally:
            db.rollback()
            db.close()

    def test_backfill_counts_empty_only(self):
        from app.services import merchant_rules as mr
        db = _db()
        try:
            c = _company(db, "КСтарым")
            r_old_empty = _receipt(db, c, inn="7704001278")
            r_old_set = _receipt(db, c, inn="7704001278", cat="Уже есть")
            rule = _rule(db, c, "7704001278", "category", "Транспорт")
            updated = mr.backfill(db, rule)
            assert updated == 1
            assert r_old_empty.category == "Транспорт"
            assert r_old_set.category == "Уже есть"
            assert rule.applied_count == 1
            # повторный backfill — обновлять нечего
            assert mr.backfill(db, rule) == 0
        finally:
            db.rollback()
            db.close()


# ------------------------------------------------------------------ API
class TestMerchantRulesApi:
    """Поток: правка → «Запомнить» → новый чек заполнился сам."""

    def test_remember_and_autofill_flow(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c = client.post("/api/v1/companies", headers=H,
                          json={"name": f"Маппинг-Тест {_SEQ['n']}"})
        assert r_c.status_code == 201, r_c.text
        cid = r_c.json()["id"]

        def _manual():
            _SEQ["n"] += 1
            k = _SEQ["n"]
            r = client.post("/api/v1/receipts/manual", headers=H, json={
                "date_time": "10.10.2026 12:00", "total_sum": 700.0,
                "fn": f"930{k:07d}", "fd": f"83{k}", "fp": f"73{k}",
                "n": 1, "company_id": cid})
            assert r.status_code == 200, r.text
            return r.json()["receipt"]

        # 1) правка первого чека + «Запомнить» (значения из формы)
        rec1 = _manual()
        p = client.patch(f"/api/v1/receipts/{rec1['id']}", headers=H, json={
            "merchant_inn": "7704001280", "category": "Канцелярия"})
        assert p.status_code == 200, p.text
        rm = client.post(f"/api/v1/receipts/{rec1['id']}/remember-merchant",
                         headers=H, json={
                             "fields": ["category", "assignee"],
                             "values": {"assignee": "Иванов И.И."}})
        assert rm.status_code == 200, rm.text
        body = rm.json()
        assert body["ok"] is True
        assert "7704001280" in body["message"]
        assert body["skipped"] == []           # assignee пришёл из values

        # 2) список правил
        lst = client.get("/api/v1/receipts/merchant-rules", headers=H).json()
        mine = [x for x in lst if x["merchant_inn"] == "7704001280"]
        assert {x["field"] for x in mine} == {"category", "assignee"}

        # 3) новый чек того же поставщика заполняется сам (пустые поля)
        rec2 = _manual()
        p2 = client.patch(f"/api/v1/receipts/{rec2['id']}", headers=H,
                          json={"merchant_inn": "7704001280"})
        assert p2.status_code == 200, p2.text
        d2 = p2.json()
        assert d2["category"] == "Канцелярия"
        assert d2["assignee"] == "Иванов И.И."
        # непустое поле правила не трогают
        p3 = client.patch(f"/api/v1/receipts/{rec2['id']}", headers=H,
                          json={"comment": "свой комментарий"})
        assert p3.json()["comment"] == "свой комментарий"

        # 4) «⏪ к старым»: чек без заполненной статьи — до правила
        rec4 = _manual()
        client.patch(f"/api/v1/receipts/{rec4['id']}", headers=H,
                     json={"merchant_inn": "7704001281"})
        rec5 = _manual()
        client.patch(f"/api/v1/receipts/{rec5['id']}", headers=H, json={
            "merchant_inn": "7704001281", "category": "Прочее"})
        cat_rule = [x for x in client.get(
            "/api/v1/receipts/merchant-rules",
            headers=H).json() if x["merchant_inn"] == "7704001280"
            and x["field"] == "category"][0]
        # правило другого ИНН не применяется к 7704001281 — создадим своё
        rm2 = client.post(f"/api/v1/receipts/{rec5['id']}/remember-merchant",
                          headers=H, json={"fields": ["category"]})
        assert rm2.status_code == 200
        rule2 = [x for x in client.get(
            "/api/v1/receipts/merchant-rules", headers=H).json()
            if x["merchant_inn"] == "7704001281"][0]
        bf = client.post(f"/api/v1/receipts/merchant-rules/"
                         f"{rule2['id']}/backfill", headers=H)
        assert bf.status_code == 200, bf.text
        assert bf.json()["updated"] >= 1       # rec4 заполнен задним числом
        got = client.get(f"/api/v1/receipts/{rec4['id']}",
                         headers=H).json()
        assert got["category"] == "Прочее"

        # 5) чек без ИНН — честный отказ
        rec3 = _manual()
        r422 = client.post(f"/api/v1/receipts/{rec3['id']}/remember-merchant",
                           headers=H, json={"fields": ["category"]})
        assert r422.status_code == 422
        assert "ИНН" in r422.json()["detail"]

        # 6) удаление правила
        dr = client.delete(
            f"/api/v1/receipts/merchant-rules/{cat_rule['id']}", headers=H)
        assert dr.status_code == 200, dr.text
        lst2 = [x for x in client.get("/api/v1/receipts/merchant-rules",
                                      headers=H).json()
                if x["merchant_inn"] == "7704001280"]
        assert "category" not in {x["field"] for x in lst2}

    def test_unauthorized(self, client):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c2:
            assert c2.get("/api/v1/receipts/merchant-rules").status_code == 401
            assert c2.post("/api/v1/receipts/x/remember-merchant",
                           json={"fields": ["category"]}).status_code == 401


# ------------------------------------------------------------------ миграция
class TestMigration1630:
    """Новая таблица merchant_rules — добавляющая (create_all)."""

    def test_table_and_columns(self):
        from sqlalchemy import inspect
        from app.database import engine
        insp = inspect(engine)
        assert "merchant_rules" in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("merchant_rules")}
        assert {"id", "merchant_inn", "merchant_name", "company_id",
                "field", "value", "applied_count", "created_by",
                "created_at"} <= cols

    def test_mapping_settings_untouched(self):
        """Замки 1С-маппинга (v1.20/v1.21): mapping_settings без изменений."""
        from sqlalchemy import inspect
        from app.database import engine
        cols = {c["name"] for c in inspect(engine).get_columns("mapping_settings")}
        assert {"source_field", "target_object", "target_field",
                "transform", "is_active"} <= cols


# ------------------------------------------------------------------ инструкция
class TestManualSection:
    """Инструкция: раздел про правила поставщиков."""

    def test_manual_has_merchant_rules(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.get("/api/v1/manual", headers=H)
        assert r.status_code == 200
        html = r.text
        assert "Запомнить для поставщика" in html
        assert "Правила поставщиков" in html
