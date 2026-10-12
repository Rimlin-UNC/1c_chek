# Ямастер Чек — тесты v1.62.0: «Политика расходов» (лимиты и комплаенс).
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки (docs/concept_roadmap_v160.md, Фаза 1 п.2):
#  - настройки: лимит ₽/чек и ₽/мес на сотрудника, запрещённые статьи расходов
#    (на каждую компанию; чтение — бухгалтер+, изменение — администратор);
#  - нарушения помечаются в списке чипом «⚖️ политика» и попадают в сводку
#    (policy_flagged в /dashboard/stats);
#  - блокировка МЯГКАЯ: статус чека не меняется, поток сдачи не ломается,
#    ФНС/пул/1С-замки не тронуты; в лимитах участвует «к учёту»
#    (total_sum − personal_sum);
#  - ноль доп. запросов к источникам: правила над уже полученными данными;
#  - пересчёт: при создании чека, правке, полных данных, массовом назначении,
#    перемещении между компаниями и сохранении политики (вся компания);
#  - миграции только добавляющие (companies ×3, receipts ×1);
#  - версии/WHATS_NEW/CHANGELOG/блоки/инструкция — по ритуалу.
import re
from datetime import datetime

import pytest

_SEQ = {"n": 62000}


# ------------------------------------------------------------------ версии
class TestVersion1620:
    """Строгий версионный замок релиза v1.62.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.62.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.61.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.62.0':") < js.index("'1.61.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.62.0]") == 1
        assert ch.index("## [1.62.0]") < ch.index("## [1.61.0]")

    def test_settings_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Настройки"] == "1.62.0"


# ------------------------------------------------------------------ правила
def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _company(db, name, check=None, month=None, cats=""):
    from app.models import Company
    _SEQ["n"] += 1
    c = Company(name=f"{name} {_SEQ['n']}", limit_per_check=check,
                limit_per_month=month, forbidden_categories=cats)
    db.add(c)
    db.flush()
    return c


def _receipt(db, comp, total, personal=0.0, assignee="", cat="",
             date=datetime(2026, 10, 10, 12, 0), status="verified", op=1,
             company_id="keep"):
    from app.models import Receipt
    _SEQ["n"] += 1
    k = _SEQ["n"]
    r = Receipt(qr_data=f"t=20261010T1200&s={total}&fn=99{k}&i={k}&fp={k}&n=1",
                fn=f"99{k}", fd=f"{k}", fp=f"{k}",
                receipt_date=date, total_sum=total, personal_sum=personal,
                operation=op, status=status,
                company_id=comp.id if company_id == "keep" else company_id,
                assignee=assignee, assignee_lc=assignee.casefold()[:200],
                category=cat, category_lc=cat.casefold())
    db.add(r)
    db.flush()
    return r


class TestPolicyRules:
    """Чистые правила политики: лимиты и запрещённые статьи."""

    def test_no_policy_no_flags(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "БезЛимитов")
            r = _receipt(db, c, 999999.0, assignee="Иванов")
            assert policy.flags_for(db, r) == []
        finally:
            db.rollback()
            db.close()

    def test_limit_check_and_personal(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "ЛимитЧека", check=1000.0)
            r = _receipt(db, c, 1500.0, assignee="Петров")
            assert "limit_check" in policy.flags_for(db, r)
            # личные покупки уменьшают «к учёту»: 1500 − 600 = 900 ≤ 1000
            r2 = _receipt(db, c, 1500.0, personal=600.0, assignee="Петров")
            assert "limit_check" not in policy.flags_for(db, r2)
        finally:
            db.rollback()
            db.close()

    def test_limit_month_by_assignee(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "ЛимитМесяц", month=2000.0)
            _receipt(db, c, 1500.0, assignee="Сидоров")
            r2 = _receipt(db, c, 800.0, assignee="Сидоров")
            assert "limit_month" in policy.flags_for(db, r2)
            # другой сотрудник и другой месяц — не в счёт
            r3 = _receipt(db, c, 800.0, assignee="Другой")
            r4 = _receipt(db, c, 800.0, assignee="Сидоров",
                          date=datetime(2026, 9, 10, 12, 0))
            assert "limit_month" not in policy.flags_for(db, r3)
            assert "limit_month" not in policy.flags_for(db, r4)
        finally:
            db.rollback()
            db.close()

    def test_month_ignores_failed_and_returns(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "МесяцЧистый", month=2000.0)
            _receipt(db, c, 1500.0, assignee="Фёдоров", status="failed")
            _receipt(db, c, 1500.0, assignee="Фёдоров", op=2)
            r = _receipt(db, c, 1800.0, assignee="Фёдоров")
            assert "limit_month" not in policy.flags_for(db, r)
        finally:
            db.rollback()
            db.close()

    def test_month_needs_assignee(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "МесяцБезСотрудника", month=1000.0)
            _receipt(db, c, 900.0, assignee="")
            r = _receipt(db, c, 900.0, assignee="")
            assert "limit_month" not in policy.flags_for(db, r)
        finally:
            db.rollback()
            db.close()

    def test_forbidden_category_casefold(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "ЗапретСтатей", cats="Алкоголь, Табак")
            r = _receipt(db, c, 500.0, cat="алкоголь")
            assert "forbidden_category" in policy.flags_for(db, r)
            r2 = _receipt(db, c, 500.0, cat="Канцелярия")
            assert policy.flags_for(db, r2) == []
        finally:
            db.rollback()
            db.close()

    def test_kopeck_precision(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "Копейки", check=1000.0)
            r_ok = _receipt(db, c, 1000.0, assignee="А")
            r_bad = _receipt(db, c, 1000.01, assignee="А")
            assert "limit_check" not in policy.flags_for(db, r_ok)
            assert "limit_check" in policy.flags_for(db, r_bad)
        finally:
            db.rollback()
            db.close()

    def test_refresh_receipt_and_company(self):
        from app.services import policy
        db = _db()
        try:
            c = _company(db, "Пересчёт")            # пока без политики
            r = _receipt(db, c, 500.0, cat="табак", assignee="Х")
            r2 = _receipt(db, c, 50.0, assignee="Х")
            # ввели политику (как при сохранении в настройках)
            c.limit_per_check = 100.0
            c.forbidden_categories = "Табак"
            db.flush()
            changed = policy.refresh_company(db, c.id)
            assert changed == 1                   # помечен только r
            assert r.policy_flags == "limit_check,forbidden_category"
            assert r2.policy_flags == ""
            # повторный пересчёт ничего не меняет
            assert policy.refresh_company(db, c.id) == 0
            # сняли лимит — флаг limit_check уходит (это тоже изменение)
            c.limit_per_check = None
            db.flush()
            assert policy.refresh_company(db, c.id) == 1
            assert r.policy_flags == "forbidden_category"
            # точечный пересчёт одного чека — по актуальной политике
            assert policy.refresh_receipt(db, r) == ["forbidden_category"]
            # чек без компании — флагов нет
            r3 = _receipt(db, c, 500.0, company_id=None)
            assert policy.flags_for(db, r3) == []
        finally:
            db.rollback()
            db.close()


# ------------------------------------------------------------------ API
class TestPolicyApi:
    """GET/PUT /settings/policy: админ управляет, бухгалтер читает."""

    def test_put_get_roundtrip(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c = client.post("/api/v1/companies", headers=H,
                          json={"name": f"Политика-Круг {_SEQ['n']}"})
        assert r_c.status_code == 201, r_c.text
        cid = r_c.json()["id"]
        r = client.put("/api/v1/settings/policy", headers=H, json={
            "company_id": cid, "limit_per_check": 5000.0,
            "limit_per_month": 60000.0,
            "forbidden_categories": ["Алкоголь", "Табак"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and "flagged" in body
        g = client.get("/api/v1/settings/policy?company_id=" + cid,
                       headers=H).json()
        assert g["company_id"] == cid
        assert g["limit_per_check"] == 5000.0
        assert g["limit_per_month"] == 60000.0
        assert [x.casefold() for x in g["forbidden_categories"]] == \
            ["алкоголь", "табак"]
        # снять лимиты нулём, очистить статьи
        r2 = client.put("/api/v1/settings/policy", headers=H, json={
            "company_id": cid, "limit_per_check": 0,
            "limit_per_month": 0, "forbidden_categories": []})
        assert r2.status_code == 200
        g2 = client.get("/api/v1/settings/policy?company_id=" + cid,
                        headers=H).json()
        assert g2["limit_per_check"] is None and g2["forbidden_categories"] == []

    def test_auth_required(self, client):
        # свежий клиент: сессионный хранит auth-cookie прошлых логинов
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c2:
            assert c2.get("/api/v1/settings/policy").status_code == 401
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/policy", headers=H,
                       json={"limit_per_check": -5})
        assert r.status_code == 422          # отрицательный лимит невозможен

    def test_flagged_receipt_via_manual_and_patch(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c = client.post("/api/v1/companies", headers=H,
                          json={"name": f"Политика-Тест {_SEQ['n']}"})
        assert r_c.status_code == 201, r_c.text
        cid = r_c.json()["id"]
        # политика: лимит чека 1000
        r0 = client.put("/api/v1/settings/policy", headers=H, json={
            "company_id": cid, "limit_per_check": 1000.0})
        assert r0.status_code == 200
        k = _SEQ["n"] * 7 + 13
        r = client.post("/api/v1/receipts/manual", headers=H, json={
            "date_time": "10.10.2026 12:00", "total_sum": 2500.0,
            "fn": f"920{k:07d}", "fd": f"82{k}", "fp": f"72{k}",
            "n": 1, "company_id": cid})
        assert r.status_code == 200, r.text
        rec = r.json()["receipt"]
        assert "limit_check" in (rec["policy_flags"] or [])
        # правка: статья из запрещённых → появляется второй флаг
        rp = client.put("/api/v1/settings/policy", headers=H, json={
            "company_id": cid, "forbidden_categories": ["Подарки"]})
        assert rp.status_code == 200
        p = client.patch(f"/api/v1/receipts/{rec['id']}", headers=H,
                         json={"category": "подарки"})
        assert p.status_code == 200, p.text
        assert set(p.json()["policy_flags"]) >= {"limit_check",
                                                 "forbidden_category"}
        # личная сумма снижает «к учёту» ниже лимита → флаг лимита уходит
        p2 = client.patch(f"/api/v1/receipts/{rec['id']}", headers=H,
                          json={"personal_sum": 2000.0})
        assert p2.status_code == 200
        assert "limit_check" not in p2.json()["policy_flags"]
        # список чеков отдаёт policy_flags
        lst = client.get("/api/v1/receipts", headers=H,
                         params={"q": f"920{k:07d}"}).json()
        rows = lst["items"] if isinstance(lst, dict) and "items" in lst else lst
        row = [x for x in rows if x["id"] == rec["id"]][0]
        assert "forbidden_category" in row["policy_flags"]
        # сводка считает нарушения (админ видит все пространства)
        st = client.get("/api/v1/dashboard/stats", headers=H).json()
        assert st["policy_flagged"] >= 1
        # вернуть пустую политику, чтобы не влиять на другие тесты воркера
        client.put("/api/v1/settings/policy", headers=H, json={
            "company_id": cid, "limit_per_check": 0,
            "forbidden_categories": []})


# ------------------------------------------------------------------ миграция
class TestMigration1620:
    """Добавляющие колонки: компании ×3, чеки ×1."""

    def test_columns_declared(self):
        src = open("app/database.py", encoding="utf-8").read()
        for col in ("limit_per_check", "limit_per_month",
                    "forbidden_categories", "policy_flags"):
            assert f"ADD COLUMN {col}" in src

    def test_columns_exist(self):
        from sqlalchemy import inspect
        from app.database import engine
        insp = inspect(engine)
        comps = {c["name"] for c in insp.get_columns("companies")}
        recs = {c["name"] for c in insp.get_columns("receipts")}
        assert {"limit_per_check", "limit_per_month",
                "forbidden_categories"} <= comps
        assert "policy_flags" in recs


# ------------------------------------------------------------------ инструкция
class TestManualSection:
    """Инструкция: раздел «Политика расходов» для бухгалтера."""

    def test_manual_has_policy_section(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.get("/api/v1/manual", headers=H)
        assert r.status_code == 200
        html = r.text
        assert "Политика расходов" in html
        assert "Лимит месяца" in html or "лимит месяца" in html.lower()
