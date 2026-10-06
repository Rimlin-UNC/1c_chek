# Ямастер Чек — тесты v1.33.0: «Чек-Пул» этап 4 — гео, отрасли, панель модерации.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 4 (docs/dev_plan_checkpool.md):
#  - адрес места расчёта → регион/город (geo_accuracy="address"); «ул. Тверская»
#    НЕ даёт Тверскую область;
#  - fallback ИНН → Checko → регион (geo_accuracy="inn");
#  - отрасль: сеть из словаря сильнее ключевых слов; доминирование по сумме позиций;
#  - покрытие: ≥70% чеков с регионом, ≥60% с отраслью (критерий этапа);
#  - модерация в 2 клика: approve pending → verified + балл; reject → rejected;
#  - поиск/фильтры/выгрузка/разметка/дообогащение в панели; ядро не затронуто.
import re
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from app.database import SessionLocal, engine
from app.pool import ingest as pool
from app.pool.geo import (enrich_receipt, industry_name, match_industry,
                          normalize_item_name, region_name, resolve_region)
from app.pool.models import (PoolAchievement, PoolReferral,
                             PoolWithdrawal,
                             PoolConsent, PoolFingerprint, PoolItem,
                             PoolIpLog, PoolPoint, PoolReceipt, PoolSignal,
                             PoolToken, PoolUser, ensure_pool_schema)
from app.services.external import ExternalItem, ExternalResult

ensure_pool_schema(engine)

_SEQ = {"n": 13000}


def _qr(sum_rub="800.00"):
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return (f"t=20260915T1200&s={sum_rub}&fn=9999078905{k:04d}"
            f"&i=67{k:03d}&fp=782{k:03d}&n=1")


def _ticket(address="", merchant="", items=None, total=800.0):
    return ExternalResult(
        ok=True, source="test", found=True, message="OK",
        date_time=datetime(2026, 9, 15, 12, 0),
        total_sum=total, operation=1,
        merchant_name=merchant, merchant_inn="7704001275",
        merchant_address=address,
        items=items or [ExternalItem(name="Кофе Латте", quantity=1.0,
                                     price=total, total=total,
                                     vat_rate="10", vat_sum=total // 11)],
        raw={},
    )


def _enable(db, on=True):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1" if on else "0")


def _db():
    return SessionLocal()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _wipe(db):
    """Чистим контур веба/кабинета + чеки-тесты Этапа 4 (источник test/tg НЕ трогаем)."""
    for _ in range(40):
        try:
            vids = [u.id for u in db.query(PoolUser.id)
                    .filter(PoolUser.vid.isnot(None)).all()]
            emails = [u.id for u in db.query(PoolUser.id)
                      .filter(PoolUser.email != "").all()]
            recs = [r[0] for r in db.query(PoolReceipt.id).filter(
                (PoolReceipt.source == "web")
                | (PoolReceipt.source == "test")
                | (PoolReceipt.pool_user_id.in_([*vids, *emails] or [""]))).all()]
            if recs:
                db.query(PoolItem).filter(PoolItem.receipt_id.in_(recs)).delete(
                    synchronize_session=False)
                db.query(PoolReceipt).filter(PoolReceipt.id.in_(recs)).delete(
                    synchronize_session=False)
            ids = [*vids, *emails]
            if ids:
                db.query(PoolPoint).filter(PoolPoint.user_id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(PoolSignal).delete(synchronize_session=False)
            db.query(PoolIpLog).delete(synchronize_session=False)
            db.query(PoolFingerprint).delete(synchronize_session=False)
            db.query(PoolToken).delete(synchronize_session=False)
            db.query(PoolAchievement).delete(synchronize_session=False)
            db.query(PoolWithdrawal).delete(synchronize_session=False)
            db.query(PoolReferral).delete(synchronize_session=False)
            db.query(PoolConsent).delete(synchronize_session=False)
            db.query(PoolUser).filter(
                (PoolUser.vid.isnot(None)) | (PoolUser.email != "")).delete(
                synchronize_session=False)
            _enable(db, False)
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1330: не удалось очистить данные пула")


@pytest.fixture(autouse=True)
def _clean():
    db = _db()
    _wipe(db)
    db.close()
    yield
    db = _db()
    _wipe(db)
    db.close()


class TestGeo:
    def test_region_from_address(self):
        code, city, acc = resolve_region(
            "188306, Ленинградская обл, г Тихвин, ул Советская, 12")
        assert code == "47" and city == "Тихвин" and acc == "address"
        assert region_name("47") == "Ленинградская область"

    def test_street_does_not_become_region(self):
        """«ул. Тверская» в Москве — НЕ Тверская область."""
        code, city, _ = resolve_region("г. Москва, ул. Тверская, 1")
        assert code == "77" and city == "Москва"

    def test_city_fallback(self):
        assert resolve_region("630007, г Новосибирск, ул Ленина, 12")[0] == "54"
        assert resolve_region("Спб, Невский пр-т 5")[0] == "78"

    def test_unknown_address(self):
        assert resolve_region("деревня Непонятно, дом без номера") == ("", "", "")
        assert resolve_region("") == ("", "", "")

    def test_inn_fallback_via_checko(self, monkeypatch):
        """Нет адреса — берём юр. адрес по ИНН (Checko), точность inn."""
        from app.pool import geo
        from app.services import appsettings, checko
        monkeypatch.setattr(checko, "fetch_card", lambda key, inn: {
            "card": {"address": "630007, Новосибирская область, г Новосибирск, ул Ленина, 12"},
            "raw": {}, "meta": {}})
        db = _db()
        try:
            appsettings.set_setting(db, "checko_api_key", "test-key-123")
            code, city, acc = geo.resolve_region_by_inn(db, "7704001275")
        finally:
            appsettings.set_setting(db, "checko_api_key", "")
            db.close()
        assert code == "54" and city == "Новосибирск" and acc == "inn"

    def test_enrich_receipt_sets_fields(self):
        db = _db()
        try:
            _enable(db, True)
            u = PoolUser(vid="geo-enrich-vid")
            db.add(u)
            db.flush()
            parsed, err = pool.precheck_ingest(db, _qr(), u)
            assert err is None and parsed is not None
            db.commit()
            r = PoolReceipt(pool_user_id=u.id, source="test",
                            fn=parsed.fn, fd=parsed.fd, fp=parsed.fp,
                            total_sum=800.0, status="verified",
                            merchant_name="ПЯТЕРОЧКА",
                            merchant_address="188306, Ленинградская обл, "
                                             "г Тихвин, ул Советская, 12")
            db.add(r)
            db.flush()
            enrich_receipt(db, r, items=[])
            assert r.region_code == "47" and r.geo_accuracy == "address"
            assert r.city == "Тихвин" and r.industry == "food"
            db.rollback()                     # чек-заготовку не сохраняем
        finally:
            db.close()
        assert match_industry("ПЯТЕРОЧКА", [])[0] == "food"


class TestIndustry:
    def test_chain_over_keywords(self):
        items = [type("I", (), {"name": "Кофе Латте", "total": 300.0})()]
        assert match_industry("Магнит", items)[0] == "food"      # сеть сильнее

    def test_keywords_dominant_by_sum(self):
        items = [type("I", (), {"name": n, "total": t})() for n, t in
                 [("Шампунь", 250.0), ("Хлеб Бородинский", 900.0)]]
        assert match_industry("", items)[0] == "food"

    def test_cafe_by_items(self):
        items = [type("I", (), {"name": n, "total": t})() for n, t in
                 [("Кофе Латте", 300.0), ("Круассан", 150.0)]]
        code, _ = match_industry("", items)
        assert code == "cafe" and industry_name(code) == "Кафе и рестораны"

    def test_no_match(self):
        assert match_industry("", [type("I", (), {"name": "Штука", "total": 1.0})()]) == ("", "")

    def test_normalize_names(self):
        assert normalize_item_name("КОФЕ   JACOBS 3В1") == "Кофе Jacobs 3В1"
        assert normalize_item_name("  Хлеб   белый  ") == "Хлеб белый"


class TestPipelineEnrichment:
    def test_ingest_enriches_e2e(self, monkeypatch):
        """Приём через пайплайн: адрес + сеть → регион и отрасль сразу."""
        monkeypatch.setattr(pool, "_fetch_details",
                            lambda *a, **k: _ticket(
                                address="г Казань, ул Баумана, 1",
                                merchant="Пятёрочка",
                                items=[ExternalItem(name="КОФЕ ЗЕРНОВОЙ",
                                                    quantity=1.0, price=500.0,
                                                    total=500.0)]))
        db = _db()
        _enable(db, True)
        u = PoolUser(vid="geo-e2e-vid")
        db.add(u)
        db.flush()
        qr = _qr()
        res = pool.ingest_receipt(db, qr, "test", u)
        db.close()
        assert res["result"] == "verified", res
        db = _db()
        r = db.query(PoolReceipt).filter_by(id=res["receipt_id"]).one()
        assert r.region_code == "16" and r.city == "Казань"
        assert r.industry == "food"          # сеть Пятёрочка > ключевое слово «кофе»
        item = db.query(PoolItem).filter_by(receipt_id=r.id).first()
        assert item.name == "Кофе Зерновой"  # нормализация КАПСа
        db.close()

    def test_coverage_criteria(self, monkeypatch):
        """Критерий этапа: ≥70% с регионом, ≥60% с отраслью (на потоке чеков)."""
        tickets = [
            _ticket(address="г Вологда, ул Мира, 5", merchant="Магнит"),      # +регион +отрасль
            _ticket(address="г Тула, пр Ленина, 10", merchant="Роснефть"),    # +регион +отрасль
            _ticket(address="г Омск, ул Ленина, 3", merchant="Аптека 36,6"),  # +регион +отрасль
            _ticket(address="г Самара, ул Полевая, 1", merchant=""),          # +регион, отрасль по позициям
            _ticket(address="", merchant="ВкусВилл"),                         # отрасль есть, региона нет
        ]
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: tickets.pop(0))
        db = _db()
        _enable(db, True)
        u = PoolUser(vid="geo-cover-vid")
        db.add(u)
        db.flush()
        for _ in range(5):
            res = pool.ingest_receipt(db, _qr(), "test", u)
            assert res["result"] == "verified"
        ov = pool.overview(db)
        db.close()
        assert ov["geo_coverage"] >= 70, ov
        assert ov["industry_coverage"] >= 60, ov


class TestModeration:
    def _mk_pending(self, user_vid="mod-vid"):
        db = _db()
        u = PoolUser(vid=user_vid)
        db.add(u)
        db.flush()
        r = PoolReceipt(pool_user_id=u.id, source="web", fn=_qr()[22:],
                        fd="1", fp="1", total_sum=600000.0, status="pending",
                        status_message="Сумма-аномалия — ручная проверка")
        db.add(r)
        db.flush()
        rid = r.id
        db.commit()
        db.close()
        return rid

    def test_approve_awards_point(self, client):
        from tests.conftest import login
        rid = self._mk_pending()
        H = login(client, "admin", "admin123")
        r = client.post(f"/api/v1/pool-admin/receipt/{rid}/moderate",
                        headers=H, json={"action": "approve"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "verified" and body["points_awarded"] == 1
        db = _db()
        row = db.get(PoolReceipt, rid)
        assert row.status == "verified" and row.points_awarded == 1
        pt = db.query(PoolPoint).filter_by(ref_id=rid, reason="receipt").first()
        assert pt is not None and pt.delta == 1
        db.close()

    def test_reject_with_comment(self, client):
        from tests.conftest import login
        rid = self._mk_pending("mod-vid-2")
        H = login(client, "admin", "admin123")
        r = client.post(f"/api/v1/pool-admin/receipt/{rid}/moderate",
                        headers=H, json={"action": "reject",
                                         "comment": "данные не подтвердились"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        db = _db()
        row = db.get(PoolReceipt, rid)
        assert row.status == "rejected" and "не подтвердились" in row.status_message
        assert row.points_awarded == 0
        db.close()

    def test_approve_only_pending(self, client):
        from tests.conftest import login
        rid = self._mk_pending("mod-vid-3")
        H = login(client, "admin", "admin123")
        client.post(f"/api/v1/pool-admin/receipt/{rid}/moderate",
                    headers=H, json={"action": "approve"})
        r2 = client.post(f"/api/v1/pool-admin/receipt/{rid}/moderate",
                         headers=H, json={"action": "approve"})
        assert r2.status_code == 409

    def test_manual_marking(self, client):
        from tests.conftest import login
        rid = self._mk_pending("mod-vid-4")
        H = login(client, "admin", "admin123")
        r = client.patch(f"/api/v1/pool-admin/receipt/{rid}", headers=H,
                         json={"region_code": "47", "city": "Тихвин",
                               "industry": "food"})
        assert r.status_code == 200
        db = _db()
        row = db.get(PoolReceipt, rid)
        assert row.region_code == "47" and row.geo_accuracy == "manual"
        db.close()
        # неизвестные коды отклоняются
        assert client.patch(f"/api/v1/pool-admin/receipt/{rid}", headers=H,
                            json={"region_code": "999"}).status_code == 422
        assert client.patch(f"/api/v1/pool-admin/receipt/{rid}", headers=H,
                            json={"industry": "нет-такой"}).status_code == 422

    def test_enrich_missing_backfill(self, client):
        from tests.conftest import login
        db = _db()
        u = PoolUser(vid="mod-vid-5")
        db.add(u)
        db.flush()
        r = PoolReceipt(pool_user_id=u.id, source="web", fn=_qr()[22:],
                        fd="2", fp="2", total_sum=100.0, status="verified",
                        merchant_name="",
                        merchant_address="г Вологда, ул Мира, 5")
        db.add(r)
        db.flush()
        db.add(PoolItem(receipt_id=r.id, name="Круассан", total=100.0))
        rid = r.id
        db.commit()
        db.close()
        H = login(client, "admin", "admin123")
        res = client.post("/api/v1/pool-admin/enrich-missing", headers=H, json={})
        assert res.status_code == 200
        body = res.json()
        assert body["processed"] >= 1
        db = _db()
        row = db.get(PoolReceipt, rid)
        assert row.region_code == "35" and row.industry == "cafe"
        db.close()


class TestSearchExport:
    def _seed(self):
        _SEQ["n"] += 1
        k = _SEQ["n"]
        db = _db()
        u = PoolUser(vid=f"srch-vid-{k}")
        db.add(u)
        db.flush()
        rows = [
            PoolReceipt(pool_user_id=u.id, source="web", fn=f"7000000{k:04d}001",
                        fd="1", fp=str(k) + "1", total_sum=100.0,
                        status="verified",
                        merchant_name="Пятёрочка", merchant_inn="7712345678",
                        region_code="47", industry="food"),
            PoolReceipt(pool_user_id=u.id, source="web", fn=f"7000000{k:04d}002",
                        fd="1", fp=str(k) + "2", total_sum=200.0,
                        status="pending",
                        merchant_name="Кафе Солнце", merchant_inn="4799"),
        ]
        db.add_all(rows)
        db.commit()
        db.close()

    def test_search_by_fn_inn_merchant(self, client):
        from tests.conftest import login
        self._seed()
        H = login(client, "admin", "admin123")
        by_fn = client.get("/api/v1/pool-admin/receipts?q=7000000",
                           headers=H).json()
        assert by_fn["total"] >= 2
        by_inn = client.get("/api/v1/pool-admin/receipts?q=7712345678",
                            headers=H).json()
        assert by_inn["total"] >= 1
        assert by_inn["items"][0]["merchant_name"] == "Пятёрочка"
        by_name = client.get("/api/v1/pool-admin/receipts?q=солнце",
                             headers=H).json()
        assert by_name["total"] >= 1
        assert by_name["items"][0]["status"] == "pending"

    def test_filters_missing(self, client):
        from tests.conftest import login
        self._seed()
        H = login(client, "admin", "admin123")
        mg = client.get("/api/v1/pool-admin/receipts?missing_geo=true&q=7000000",
                        headers=H).json()
        assert mg["total"] == 1 and mg["items"][0]["merchant_name"] == "Кафе Солнце"
        mi = client.get("/api/v1/pool-admin/receipts?missing_industry=true&q=7000000",
                        headers=H).json()
        assert mi["total"] == 1
        st = client.get("/api/v1/pool-admin/receipts?status=pending&q=7000000",
                        headers=H).json()
        assert st["total"] == 1

    def test_dicts(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        d = client.get("/api/v1/pool-admin/dicts", headers=H).json()
        codes = [r["code"] for r in d["regions"]]
        assert "47" in codes and "77" in codes and len(d["regions"]) == 89
        ind = [i["code"] for i in d["industries"]]
        assert "food" in ind and "cafe" in ind

    def test_export_csv(self, client):
        from tests.conftest import login
        self._seed()
        H = login(client, "admin", "admin123")
        r = client.get("/api/v1/pool-admin/export.csv", headers=H)
        assert r.status_code == 200
        text = r.content.decode("utf-8-sig")
        assert "Регион" in text and "Ленинградская область" in text
        assert "Пятёрочка" in text


class TestVersion1330:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # Пин конкретной версии перенесён в tests/test_v1340.py (тест текущей версии)
        # v1.33.0: assert ver == "1.33.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.33.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.33.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "гео, отрасли и модерация (v1.33.0)" in manual

    def test_admin_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "pooladmin: viewPoolAdmin" in js and "pooladmin: 'Чек-Пул: модерация'" in js
        assert "pooladmin: isAdmin()" in js
        assert "/api/v1/pool-admin/dicts" in js
        assert "/moderate" in js and "enrich-missing" in js and "export.csv" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'data-view="pooladmin"' in idx
        mn = open("app/main.py", encoding="utf-8").read()
        assert "router_public" in mn

    def test_geo_data_offline(self):
        """Справочники внутри программы: 89 регионов, отрасли, сети."""
        from app.pool.geo import industries, regions, _load
        assert len(regions()) == 89
        assert len(industries()) >= 15
        assert len(_load("chains")) >= 40

    def test_core_untouched(self):
        core = open("app/models.py", encoding="utf-8").read()
        assert "pool_" not in core
        geodir = open("app/pool/geo/__init__.py", encoding="utf-8").read()
        assert "resolve_region" in geodir and "enrich_receipt" in geodir
