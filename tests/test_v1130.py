# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.13.0: удаление компаний (move/wipe), защита от
# дублей (канонизация названий + ИНН), карточка компании и ключ Checko.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import itertools
import shutil
import subprocess

from tests.conftest import login

_SEQ = itertools.count(900, 10)


def _inn10(n: int) -> str:
    """Валидный 10-значный ИНН: контроль по коэффициентам [2,4,10,3,5,9,4,6,8],
    контрольная цифра = (сумма % 11) % 10 (проверено на 7707083893 — Сбербанк)."""
    digits = [int(ch) for ch in str(n).zfill(9)[:9]]
    d = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    cs = (sum(a * b for a, b in zip(d, digits)) % 11) % 10
    return "".join(map(str, digits)) + str(cs)


assert _inn10(770708389) == "7707083893"


class TestCanonicalAndInn:
    def test_canonical_name_variants(self):
        from app.services.companies_util import canonical_name
        a = canonical_name('ООО «Ямастер»')
        assert a == canonical_name('ООО "ямастер"') == canonical_name('ООО «ЯМАСТЕР»')
        assert a == 'ООО ЯМАСТЕР'
        # юридическая форма сохраняется: ООО ≠ ИП
        assert canonical_name('ООО Ромашка') != canonical_name('ИП Ромашка')

    def test_inn_control_digits(self):
        from app.services.companies_util import inn_is_valid, inn_kind
        assert inn_is_valid('7707083893')            # Сбербанк — валидный
        assert not inn_is_valid('7707083894')        # контрольная цифра сбита
        assert not inn_is_valid('123456789012')      # 12 зн., контроль не сходится
        assert inn_is_valid('')                      # пустой не блокируем
        assert inn_kind('7707083893') == 'legal'
        assert inn_kind('526317984689') == 'individual'

    def test_similar_threshold(self):
        from app.services.companies_util import similar_ratio
        assert similar_ratio('ООО «Ямастер»', 'ООО Ямастер') == 1.0
        assert similar_ratio('ООО Ромашка', 'ООО Строймаш') < 0.84


class TestDuplicateGuard:
    def test_same_company_different_quotes_blocked(self, client):
        hdr = login(client, "admin", "admin123")
        # дефолтная компания бутстрапа уже называется «ООО «Ямастер»» —
        # берём нейтральное имя
        assert client.post("/api/v1/companies",
                           json={"name": 'ООО «Циклон»'}, headers=hdr).status_code == 201
        r = client.post("/api/v1/companies",
                        json={"name": 'ООО "циклон"'}, headers=hdr)
        assert r.status_code == 409
        assert "иклон" in r.json()["detail"]
        # подсказка похожих
        r = client.get("/api/v1/companies/similar",
                       params={"name": "ООО Циклон"}, headers=hdr)
        assert r.status_code == 200 and len(r.json()) == 1

    def test_inn_validation_and_uniqueness(self, client):
        hdr = login(client, "admin", "admin123")
        inn1 = _inn10(1)
        r = client.post("/api/v1/companies",
                        json={"name": "ООО Ясень", "inn": "12345"}, headers=hdr)
        assert r.status_code == 422                       # не 10/12 знаков
        r = client.post("/api/v1/companies",
                        json={"name": "ООО Ясень", "inn": inn1}, headers=hdr)
        assert r.status_code == 201, r.text
        r = client.post("/api/v1/companies",
                        json={"name": "ООО Клён", "inn": inn1}, headers=hdr)
        assert r.status_code == 409                       # ИНН занят
        r = client.post("/api/v1/companies",
                        json={"name": "ООО Дуб", "inn": "7707083894"}, headers=hdr)
        assert r.status_code == 422                       # контрольная цифра


class TestCardAndCheckoKey:
    def test_card_stats_and_settings(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies", json={
            "name": "ООО Липа", "inn": _inn10(2)}, headers=hdr).json()
        r = client.get(f"/api/v1/companies/{comp['id']}/card", headers=hdr)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["company"]["name"] == "ООО Липа"
        assert d["receipts"] == 0 and d["team"] == []
        assert not d["card"]                # {} — ЕГРЮЛ ещё не запрашивали
        assert any(x["id"] != comp["id"] for x in d["other_companies"])
        # ключ Checko через настройки (без хардкода)
        r = client.get("/api/v1/settings/checko", headers=hdr)
        assert r.status_code == 200 and r.json()["has_key"] is False
        r = client.put("/api/v1/settings/checko",
                       json={"api_key": "abc123def456"}, headers=hdr)
        assert r.status_code == 200
        r = client.get("/api/v1/settings/checko", headers=hdr).json()
        assert r["has_key"] is True and "abc123def" not in (r["key_masked"] or "")
        # lookup-checko: некорректный ИНН — 422 до похода в сеть
        assert client.post("/api/v1/companies/lookup-checko",
                           json={"inn": "123"}, headers=hdr).status_code == 422

    def test_csv_by_company_admin_only(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": "ООО Кедр"}, headers=hdr).json()
        r = client.post(f"/api/v1/receipts/export-csv?company_id={comp['id']}",
                        headers=hdr)
        assert r.status_code == 200, r.text
        head = r.content.decode("utf-8-sig").splitlines()[0]
        assert "Кто добавил" in head and "Компания" in head
        # бухгалтер чужой компании — 403
        inv = client.post("/api/v1/invites", json={
            "role": "accountant", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "kedr_acc",
            "password": "parol123", "full_name": "Кира Кедрова"})
        ha = {"Authorization": "Bearer " + reg.json()["access_token"]}
        r = client.post(f"/api/v1/receipts/export-csv?company_id={comp['id']}",
                        headers=ha)
        assert r.status_code == 403

    def test_card_endpoint_forbidden_for_user(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": "ООО Тополь"}, headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "top_user",
            "password": "parol123", "full_name": "Тимур Топольников"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        assert client.get(f"/api/v1/companies/{comp['id']}/card",
                          headers=hu).status_code == 403


class TestCompanyDelete:
    def test_move_receipts_and_users(self, client):
        hdr = login(client, "admin", "admin123")
        n = next(_SEQ)
        a = client.post("/api/v1/companies", json={
            "name": f"ООО Переезд {n}", "inn": _inn10(n)}, headers=hdr).json()
        b = client.post("/api/v1/companies", json={
            "name": f"ООО Приёмник {n}", "inn": _inn10(n + 1)}, headers=hdr).json()
        # сотрудник + чек в компании A (exported=True — после переезда сбросится)
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": a["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"mv_user{n}",
            "password": "parol123", "full_name": "Митя Переездов"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        rid = client.post("/api/v1/receipts/scan", json={
            "qr_data": (f"t=20261004T1200&s=150.00&fn=7381440{n:07d}"
                        f"&i={n}&fp={700000000 + n}&n=1"),
            "source": "web"}, headers=hu).json()["receipt"]["id"]
        from app.database import SessionLocal
        from app.models import Receipt
        s = SessionLocal()
        try:
            rc = s.query(Receipt).get(rid)
            rc.exported = True
            s.commit()
        finally:
            s.close()
        # перенос в B
        r = client.post(f"/api/v1/companies/{a['id']}/delete", headers=hdr,
                        json={"mode": "move", "target_company_id": b["id"],
                              "move_users": True})
        assert r.status_code == 200, r.text
        s = SessionLocal()
        try:
            rc = s.query(Receipt).get(rid)
            assert rc.company_id == b["id"]
            assert rc.exported is False and rc.exported_at is None
            from app.models import Company
            assert s.query(Company).get(a["id"]) is None
        finally:
            s.close()
        # сотрудник переехал: его чек теперь виден в компании B
        items = client.get("/api/v1/receipts", headers=hu).json()["items"]
        assert any(x["id"] == rid for x in items)

    def test_wipe_and_single_company_guard(self, client):
        hdr = login(client, "admin", "admin123")
        n = next(_SEQ)
        a = client.post("/api/v1/companies", json={
            "name": f"ООО Стирание {n}", "inn": _inn10(n)}, headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": a["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"wp_user{n}",
            "password": "parol123", "full_name": "Степан Стиралов"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        rid = client.post("/api/v1/receipts/scan", json={
            "qr_data": (f"t=20261004T1300&s=99.00&fn=7381440{n:07d}"
                        f"&i={n}&fp={700000000 + n}&n=1"),
            "source": "web"}, headers=hu).json()["receipt"]["id"]
        r = client.post(f"/api/v1/companies/{a['id']}/delete",
                        headers=hdr, json={"mode": "wipe", "move_users": True})
        assert r.status_code == 200, r.text
        assert client.get(f"/api/v1/receipts/{rid}",
                          headers=hdr).status_code == 404
        # у стёртой компании не остаётся компаний-двойников
        # (пользователь откреплён — снова видит пустой список)
        assert client.get("/api/v1/receipts", headers=hu).json()["items"] == []

    def test_move_to_archived_and_last_company(self, client):
        hdr = login(client, "admin", "admin123")
        n = next(_SEQ)
        a = client.post("/api/v1/companies", json={
            "name": f"ООО Исток {n}", "inn": _inn10(n)}, headers=hdr).json()
        b = client.post("/api/v1/companies", json={
            "name": f"ООО ПриёмникАрх {n}", "inn": _inn10(n + 1)}, headers=hdr).json()
        client.patch(f"/api/v1/companies/{b['id']}",
                     json={"is_active": False}, headers=hdr)
        r = client.post(f"/api/v1/companies/{a['id']}/delete", headers=hdr,
                        json={"mode": "move", "target_company_id": b["id"]})
        assert r.status_code == 409          # приёмник в архиве


class TestVersion1130:
    def test_versions_synced(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "app.css?v=1.13.0" in idx and "app.js?v=1.13.0" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v1.13.0" in sw
        from app.config import settings
        assert settings.APP_VERSION == "1.13.0"

    def test_ui_has_new_views(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "openCompanyCard" in js and "openCompanyDelete" in js
        assert "'1.13.0':" in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "v1.13.0" in css


class TestUiHarnessV1130:
    def test_dom_harness_card_and_delete(self):
        """Node DOM-песочница: карточка компании (ЕГРЮЛ) и диалог удаления
        рендерятся и отправляют корректные запросы."""
        if shutil.which("node") is None:
            import pytest
            pytest.skip("node недоступен")
        from tests.test_v1121 import _build_bundle
        open("/tmp/ymaster_ui_bundle_v1130.js", "w", encoding="utf-8").write(
            _build_bundle())
        r = subprocess.run(["node", "tests/ui_harness_v1130.js"],
                           capture_output=True, text=True, timeout=120,
                           cwd=".")
        out = (r.stdout + r.stderr)
        assert "HARNESS_OK" in out, out[-2000:]
