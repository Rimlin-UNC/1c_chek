# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.61.0: «понимающий» конвейер правил (Фаза 1).
# После получения полных данных чека (движок источников) работает новый
# сервис integrity.py: суммы позиций vs итог, цены×количество vs сумма
# позиции, дубли позиций. Нарушения → чек уходит на ручную проверку
# (pending) с человеческой причиной. Существующие статусы НЕ понижаются;
# ручные правки бухгалтера (manual_edit) не трогаются; лишних запросов
# к внешним источникам конвейер не порождает.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re
from types import SimpleNamespace


def _it(name, qty, price, total, vat_rate="none", vat_sum=0.0):
    return SimpleNamespace(name=name, quantity=qty, price=price,
                           total=total, vat_rate=vat_rate, vat_sum=vat_sum)


def _res(items, total):
    """Полный макет ExternalResult: все поля, которые читают конвейеры."""
    return SimpleNamespace(ok=True, found=True, source="fns_api",
                           message="OK", items=items, total_sum=total,
                           date_time=None, operation=1,
                           merchant_name="", merchant_inn="",
                           merchant_address="", cashier="",
                           cash_sum=0.0, ecash_sum=0.0,
                           extra=None, raw={})


class TestVersion1610:
    """Строгий версионный замок релиза v1.61.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.62.0: структурная проверка — строгий замок версии в test_v1620
        assert tuple(int(x) for x in ver.split(".")) >= (1, 61, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.60.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.61.0':") < js.index("'1.60.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.61.0]") == 1
        assert ch.index("## [1.61.0]") < ch.index("## [1.60.0]")

    def test_checks_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 61, 0)


class TestIntegrityRules:
    """Чистые правила: товары не страдают, нарушения ловим."""

    def setup_method(self):
        from app.services import integrity
        integrity.reset_rules()

    def test_ok_receipt(self):
        from app.services.integrity import check_items
        items = [_it("Кофе", 2, 350, 700, "10", 70), _it("Торт", 1, 550, 550)]
        v = check_items(items, 1250.0)
        assert v.ok is True and v.problems == []

    def test_items_total_mismatch(self):
        from app.services.integrity import check_items
        items = [_it("Кофе", 2, 350, 700), _it("Торт", 1, 550, 550)]
        v = check_items(items, 2000.0)            # позиции 1250, итог 2000
        assert v.ok is False
        assert any("Итог" in p for p in v.problems)

    def test_position_price_x_qty_mismatch(self):
        from app.services.integrity import check_items
        items = [_it("Кофе", 2, 350, 999)]        # 2×350=700 ≠ 999
        v = check_items(items, 999.0)
        assert v.ok is False
        assert any("×" in p for p in v.problems)

    def test_duplicate_items(self):
        from app.services.integrity import check_items
        items = [_it("Кофе", 1, 350, 350), _it("Кофе", 1, 350, 350)]
        v = check_items(items, 700.0)
        assert v.ok is False
        assert any("дубл" in p.lower() for p in v.problems)

    def test_rounding_tolerance(self):
        """Расхождение ≤ 0,99 ₽ (копейки/скидки) — не нарушение."""
        from app.services.integrity import check_items
        items = [_it("Кофе", 2, 350, 700)]
        assert check_items(items, 700.99).ok is True
        assert check_items(items, 701.50).ok is False

    def test_empty_items_ok(self):
        from app.services.integrity import check_items
        assert check_items([], 100.0).ok is True

    def test_last_wins(self):
        """При нескольких нарушениях одного типа — rule указывает на них."""
        from app.services.integrity import check_items
        items = [_it("Кофе", 2, 350, 999), _it("Кофе", 2, 350, 999)]
        v = check_items(items, 1998.0)
        # суммы позиций (1998) сходятся с итогом — нарушение только в математике
        assert "Итог" not in " ".join(v.problems)
        assert v.rule == "position_math"


class TestPoolIngestHook:
    """Чек-Пул: при приёме чека правила могут увести в ручную проверку."""

    def test_verified_receipt_downgraded_by_rule(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.pool import ingest as pool
        from tests.test_v1310 import _enable
        bad = _res([_it("Кофе", 2, 350, 999)], 999.0)   # 2×350 ≠ 999
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: bad)
        db = SessionLocal()
        _enable(db, True)
        db.commit()
        db.close()
        c = _client()
        b = c.post("/api/v1/public/pool/check", json={
            "qr_text": "t=20260905T1430&s=999.00&fn=9280440300528080"
                       "&i=12350&fp=67893&n=1",
            "offerta": True, "hp": "", "form_ms": 9000}).json()
        assert b["ok"] and b["accepted"]
        row = None
        for _ in range(40):
            db = SessionLocal()
            try:
                from app.pool.models import PoolReceipt
                row = db.query(PoolReceipt).filter_by(
                    fn="9280440300528080").first()
            finally:
                db.close()
            if row is not None:
                break
            import time; time.sleep(0.25)
        assert row is not None
        assert row.status == "pending", row.status_message
        assert "не сходятся" in (row.status_message or "")

    def test_good_receipt_verified(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.pool import ingest as pool
        from tests.test_v1310 import _enable
        good = _res([_it("Кофе", 2, 350, 700), _it("Торт", 1, 550, 550)],
                    1250.0)
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: good)
        db = SessionLocal()
        _enable(db, True)
        db.commit()
        db.close()
        c = _client()
        b = c.post("/api/v1/public/pool/check", json={
            "qr_text": "t=20260905T1430&s=1250.00&fn=9280440300528081"
                       "&i=12351&fp=67894&n=1",
            "offerta": True, "hp": "", "form_ms": 9000}).json()
        assert b["ok"] and b["accepted"]
        row = None
        for _ in range(40):
            db = SessionLocal()
            try:
                from app.pool.models import PoolReceipt
                row = db.query(PoolReceipt).filter_by(
                    fn="9280440300528081").first()
            finally:
                db.close()
            if row is not None:
                break
            import time; time.sleep(0.25)
        assert row is not None
        assert row.status == "verified", row.status_message


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import init_db
    init_db()
    with TestClient(app) as c:
        return c


class TestCoreFetchHook:
    """Ядро: «📥 Данные сервисов» тоже пропускает чек через правила."""

    def test_apply_flags_pending_on_mismatch(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.models import Receipt
        import app.routers.receipts as rr
        db = SessionLocal()
        try:
            r = Receipt(qr_data="t=20260101T1000&s=1998.00&fn=1&i=2&fp=3&n=1",
                        fn="1", fd="2", fp="3",
                        receipt_date=__import__("datetime").datetime(2026, 1, 1, 10, 0),
                        total_sum=1998.0, status="verified")
            db.add(r)
            db.commit()
            rid = r.id
        finally:
            db.close()
        bad = _res([_it("Кофе", 2, 350, 999)], 1998.0)
        db = SessionLocal()
        try:
            r = db.get(Receipt, rid)
            rr._apply_external_result(db, r, bad)
            assert r.status == "pending"
            assert "не сходятся" in (r.fns_message or "")
            # только-up: флаг полных данных не понижается
            assert r.full_data is True
        finally:
            db.close()

    def test_manual_edit_not_touched(self, client):
        """Ручные правки бухгалтера (manual_edit) конвейер не трогает."""
        from app.database import SessionLocal
        from app.models import Receipt
        import app.routers.receipts as rr
        db = SessionLocal()
        try:
            r = Receipt(qr_data="t=20260101T1000&s=1998.00&fn=4&i=5&fp=6&n=1",
                        fn="4", fd="5", fp="6",
                        receipt_date=__import__("datetime").datetime(2026, 1, 1, 10, 0),
                        total_sum=1998.0, status="verified",
                        details_source="manual_edit")
            db.add(r)
            db.commit()
            rid = r.id
        finally:
            db.close()
        bad = _res([_it("Кофе", 2, 350, 999)], 1998.0)
        db = SessionLocal()
        try:
            r = db.get(Receipt, rid)
            rr._apply_external_result(db, r, bad)
            assert r.status == "verified"        # человек прав — машина молчит
            assert "не сходится" not in (r.fns_message or "")
        finally:
            db.close()


class TestManualSection:
    """Инструкция: раздел «Проверка в ФНС» рассказывает про правила."""

    def test_manual_mentions_rules(self):
        from app.services.manual_content import build_manual
        html = "".join(s.get("html", "") for s in build_manual()["sections"])
        assert "проверка данных чека" in html.lower() or \
               "суммы позиций" in html.lower()
