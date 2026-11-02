"""v1.70.82: стабильный выпуск единого реестра чеков.

Проверяется то, что делает обновление с v1.70.80 безопасным на боевой базе:
единый реестр не блокирует штатную работу со старыми чеками, результат ФНС
не теряется из-за конфликта канонической записи, расхождение после отката
на старую версию не прячет чек у своей компании и не открывает его чужой.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import Company, FnsLog, Receipt, ReceiptItem
from app.pool.models import PoolItem, PoolReceipt
from app.services import appsettings
from app.unified import backfill, service
from app.unified.models import UnifiedDocument, UnifiedOrigin
from tests.conftest import login
from tests.test_v1140 import _mk_user

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.70.82"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _identity():
    digits = str(int(uuid4().hex, 16) % 10**15).zfill(15)
    return "8" + digits, "4821", "1937465028"


def _pool(db, fn, fd, fp, *, company=None, total=100.0):
    row = PoolReceipt(
        fn=fn, fd=fd, fp=fp, qr_data=f"t=20261015T1200&s={total}&fn={fn}&i={fd}&fp={fp}&n=1",
        receipt_date=datetime(2026, 10, 15, 12, 0), total_sum=total, operation=1,
        merchant_name="Магазин у дома", merchant_inn="7701234567", status="verified",
        full_data=True, assigned_company_id=company.id if company else None,
    )
    db.add(row)
    db.flush()
    db.add(PoolItem(receipt_id=row.id, name="Хлеб", quantity=1, price=total, total=total))
    return row


def _registry(db, fn, fd, fp, *, company=None, total=100.0):
    row = Receipt(
        fn=fn, fd=fd, fp=fp, qr_data=f"t=20261015T1200&s={total}&fn={fn}&i={fd}&fp={fp}&n=1",
        receipt_date=datetime(2026, 10, 15, 12, 0), total_sum=total, operation=1,
        company_id=company.id if company else None,
        merchant_name="Магазин у дома", merchant_inn="7701234567",
    )
    db.add(row)
    db.flush()
    db.add(ReceiptItem(receipt_id=row.id, name="Хлеб", quantity=1, price=total, total=total, position=0))
    return row


def _cleanup(db, rid=None, pid=None):
    if rid:
        db.query(FnsLog).filter_by(receipt_id=rid).delete(synchronize_session=False)
        db.query(ReceiptItem).filter_by(receipt_id=rid).delete(synchronize_session=False)
        db.query(Receipt).filter_by(id=rid).delete(synchronize_session=False)
    if pid:
        db.query(PoolItem).filter_by(receipt_id=pid).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pid).delete(synchronize_session=False)
    db.commit()


def _legacy_overlap(c_pool: str, c_registry: str):
    """Старая база: один фискальный чек в пуле (компания B) и в реестре (A).

    Пуловая копия уже попала в единый реестр, реестровая — ещё нет (до
    переноса архива), суммы расходятся: живая регистрация обязана упасть.
    """
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp, company=db.get(Company, c_pool), total=100.0)
        db.commit()
        service.register_source(db, service.POOL, pool, strict=True)
        db.commit()
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c_registry), total=120.0)
        db.commit()
        return reg.id, pool.id


@pytest.fixture
def companies(client):
    hdr = login(client, "admin", "admin123")
    c1 = client.post("/api/v1/companies", headers=hdr, json={"name": "Выпуск A " + uuid4().hex}).json()["id"]
    c2 = client.post("/api/v1/companies", headers=hdr, json={"name": "Выпуск B " + uuid4().hex}).json()["id"]
    a1 = _mk_user(client, hdr, c1, "rel_a_" + uuid4().hex[:10], role="accountant")
    a2 = _mk_user(client, hdr, c2, "rel_b_" + uuid4().hex[:10], role="accountant")
    with SessionLocal() as db:
        appsettings.set_setting(db, "pool_enabled", "1")
        appsettings.set_setting(db, "pool_pick_monthly_limit", "100")
    yield hdr, c1, c2, a1, a2
    client.cookies.clear()


# ==========================================================================
# 1. Работа со старыми чеками после обновления (без переноса архива)
# ==========================================================================
class TestLegacyDataAfterUpdate:
    def test_patch_of_unregistered_conflicting_receipt_is_not_blocked(self, companies, client):
        _, c1, c2, a1, _ = companies
        rid, pid = _legacy_overlap(c2, c1)
        res = client.patch(f"/api/v1/receipts/{rid}", headers=a1,
                           json={"comment": "правка после обновления"})
        assert res.status_code == 200, res.text
        with SessionLocal() as db:
            assert db.get(Receipt, rid).comment == "правка после обновления"
            # конфликт не склеен молча: чек остаётся в прежнем режиме до разбора
            assert service.document_for_source(db, service.REGISTRY, rid) is None
            _cleanup(db, rid, pid)

    def test_patch_of_plain_legacy_receipt_registers_it(self, companies, client):
        _, c1, _, a1, _ = companies
        fn, fd, fp = _identity()
        with SessionLocal() as db:
            rid = _registry(db, fn, fd, fp, company=db.get(Company, c1)).id
            db.commit()
        res = client.patch(f"/api/v1/receipts/{rid}", headers=a1, json={"category": "Хозрасходы"})
        assert res.status_code == 200, res.text
        with SessionLocal() as db:
            doc = service.document_for_source(db, service.REGISTRY, rid)
            assert doc is not None and doc.ownership.company_id == c1
            db.query(UnifiedOrigin).filter_by(document_id=doc.id).delete(synchronize_session=False)
            _cleanup(db, rid)

    def test_legacy_receipt_without_fp_is_registered_not_crashed(self, companies, client):
        """Найдено репетицией: новый документ без полной тройки падал на None | 32."""
        from app.unified.models import REVIEW_INCOMPLETE_IDENTITY

        _, c1, _, a1, _ = companies
        fn, fd, _ = _identity()
        with SessionLocal() as db:
            rid = _registry(db, fn, fd, "", company=db.get(Company, c1)).id
            db.commit()
        res = client.patch(f"/api/v1/receipts/{rid}", headers=a1, json={"comment": "чек без ФП"})
        assert res.status_code == 200, res.text
        with SessionLocal() as db:
            doc = service.document_for_source(db, service.REGISTRY, rid)
            assert doc is not None and doc.identity_key is None
            assert doc.review_flags & REVIEW_INCOMPLETE_IDENTITY
            assert doc.ownership.company_id == c1
        # неполная тройка — пометка, а не конфликт: перенос работает, как в 1.70.80
        hdr = companies[0]
        c2 = companies[2]
        moved = client.post("/api/v1/receipts/move", headers=hdr, json={"receipt_ids": [rid], "company_id": c2})
        assert moved.status_code == 200, moved.text
        with SessionLocal() as db:
            doc = service.document_for_source(db, service.REGISTRY, rid)
            assert db.get(Receipt, rid).company_id == c2 and doc.ownership.company_id == c2
            db.query(UnifiedOrigin).filter_by(document_id=doc.id).delete(synchronize_session=False)
            _cleanup(db, rid)

    def test_backfill_survives_incomplete_identity(self, companies):
        from app.unified.models import REVIEW_INCOMPLETE_IDENTITY

        _, c1, _, _, _ = companies
        fn, fd, _ = _identity()
        with SessionLocal() as db:
            rid = _registry(db, fn, fd, "", company=db.get(Company, c1)).id
            db.commit()
            result = backfill.backfill(db)
            assert result["registered"] >= 1
            doc = service.document_for_source(db, service.REGISTRY, rid)
            assert doc is not None and doc.review_flags & REVIEW_INCOMPLETE_IDENTITY
            db.query(UnifiedOrigin).filter_by(document_id=doc.id).delete(synchronize_session=False)
            _cleanup(db, rid)

    def test_fns_result_survives_unified_conflict(self, companies, monkeypatch):
        from app.routers import receipts as rr
        from app.services.fns import FnsResult

        _, c1, c2, _, _ = companies
        rid, pid = _legacy_overlap(c2, c1)
        calls = []

        def fake_check(*args, **kwargs):
            calls.append((args, kwargs))
            return FnsResult(status="valid", message="Чек найден в ФНС", ok=True,
                             raw={"provider": "fns", "items": []})

        monkeypatch.setattr(rr, "check_receipt", fake_check)
        rr._verify_one_sync(rid)
        assert len(calls) == 1, "запрос в ФНС выполняется один раз"
        with SessionLocal() as db:
            row = db.get(Receipt, rid)
            assert row.fns_status == "valid", row.fns_message
            assert row.status == "verified"
            assert db.query(FnsLog).filter_by(receipt_id=rid).count() == 1
            _cleanup(db, rid, pid)

    def test_external_details_survive_unified_conflict(self, companies):
        from app.routers import receipts as rr
        from app.services.external import ExternalResult

        _, c1, c2, _, _ = companies
        rid, pid = _legacy_overlap(c2, c1)
        res = ExternalResult(ok=True, source="test", found=True, message="ok",
                             merchant_address="г. Москва, ул. Тестовая, 1")
        with SessionLocal() as db:
            rr._apply_external_result(db, db.get(Receipt, rid), res)
        with SessionLocal() as db:
            assert db.get(Receipt, rid).merchant_address == "г. Москва, ул. Тестовая, 1"
            _cleanup(db, rid, pid)


# ==========================================================================
# 2. Откат на старую версию и повторное обновление (расхождение проекций)
# ==========================================================================
class TestDriftAfterRollback:
    def _registered(self, company_id):
        fn, fd, fp = _identity()
        with SessionLocal() as db:
            reg = _registry(db, fn, fd, fp, company=db.get(Company, company_id))
            db.commit()
            service.register_source(db, service.REGISTRY, reg, strict=True)
            db.commit()
            doc = service.document_for_source(db, service.REGISTRY, reg.id)
            return reg.id, doc.id

    def test_receipt_moved_by_old_version_stays_with_new_company(self, companies, client):
        _, c1, c2, a1, a2 = companies
        rid, doc_id = self._registered(c1)
        with SessionLocal() as db:   # старая версия переносит чек напрямую в реестре
            db.query(Receipt).filter_by(id=rid).update({Receipt.company_id: c2},
                                                       synchronize_session=False)
            db.commit()
        assert client.get(f"/api/v1/receipts/{rid}", headers=a2).status_code == 200
        listed = client.get("/api/v1/receipts", headers=a2, params={"per_page": 100, "ids": rid}).json()
        assert [r["id"] for r in listed["items"]] == [rid]
        # прежняя компания не видит чек ни в реестре, ни в едином API
        assert client.get(f"/api/v1/receipts/{rid}", headers=a1).status_code == 404
        assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=a1).status_code == 404
        mine = client.get("/api/v1/unified/documents", headers=a1, params={"per_page": 100}).json()
        assert doc_id not in [d["id"] for d in mine["items"]]
        with SessionLocal() as db:
            db.query(UnifiedOrigin).filter_by(document_id=doc_id).delete(synchronize_session=False)
            _cleanup(db, rid)

    def test_quarantined_document_still_hidden_from_companies(self, companies, client):
        hdr, c1, c2, a1, a2 = companies
        fn, fd, fp = _identity()
        with SessionLocal() as db:
            pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
            reg = _registry(db, fn, fd, fp, company=db.get(Company, c2))
            db.commit()
            backfill.backfill(db)
            rid, pid = reg.id, pool.id
        assert client.get(f"/api/v1/receipts/{rid}", headers=a2).status_code == 404
        assert client.get(f"/api/v1/receipts/{rid}", headers=hdr).status_code == 200
        mine = client.get("/api/v1/pool-company/mine", headers=a1).json()
        assert pid not in [r["id"] for r in mine.get("items", [])]
        with SessionLocal() as db:
            doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
            db.query(UnifiedOrigin).filter_by(document_id=doc.id).delete(synchronize_session=False)
            _cleanup(db, rid, pid)


# ==========================================================================
# 2б. Сверка при старте: канон догоняет изменения, сделанные старой версией
# ==========================================================================
class TestReconcileAfterRollback:
    @staticmethod
    def _doc_for(db, kind, source_id):
        return service.document_for_source(db, kind, source_id)

    def test_moved_receipt_owner_follows_registry_and_admin_can_move_back(self, companies, client):
        from app.unified import reconcile
        from app.unified.models import UnifiedEvent

        hdr, c1, c2, a1, _ = companies
        rid, doc_id = TestDriftAfterRollback()._registered(c1)
        with SessionLocal() as db:
            db.query(Receipt).filter_by(id=rid).update({Receipt.company_id: c2}, synchronize_session=False)
            db.commit()
            result = reconcile.reconcile(db)
            db.commit()
            assert result["owners_fixed"] >= 1
            doc = db.get(UnifiedDocument, doc_id)
            assert doc.ownership.company_id == c2
            assert db.query(UnifiedEvent).filter_by(document_id=doc_id, event_type="reconcile").count() == 1
        # раньше «вернуть в A» молча ничего не делало: канон считал чек уже там
        res = client.post("/api/v1/receipts/move", headers=hdr, json={"receipt_ids": [rid], "company_id": c1})
        assert res.status_code == 200, res.text
        assert client.get(f"/api/v1/receipts/{rid}", headers=a1).status_code == 200
        with SessionLocal() as db:
            assert db.get(Receipt, rid).company_id == c1
            assert db.get(UnifiedDocument, doc_id).ownership.company_id == c1
            db.query(UnifiedOrigin).filter_by(document_id=doc_id).delete(synchronize_session=False)
            _cleanup(db, rid)

    def test_receipt_deleted_by_old_version_closes_document(self, companies):
        from app.unified import reconcile

        _, c1, _, _, _ = companies
        rid, doc_id = TestDriftAfterRollback()._registered(c1)
        with SessionLocal() as db:
            _cleanup(db, rid)                       # старая версия удаляет чек
            reconcile.reconcile(db)
            db.commit()
            doc = db.get(UnifiedDocument, doc_id)
            assert doc.deleted_at is not None and doc.ownership.company_id is None
            assert db.query(UnifiedOrigin).filter_by(document_id=doc_id).count() == 0

    def test_released_pool_receipt_can_be_claimed_again(self, companies, client):
        from app.unified import reconcile

        _, c1, c2, _, a2 = companies
        fn, fd, fp = _identity()
        with SessionLocal() as db:
            pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
            db.commit()
            service.register_source(db, service.POOL, pool, strict=True)
            db.commit()
            pid = pool.id
            # старая версия снимает закрепление только в пуле
            db.query(PoolReceipt).filter_by(id=pid).update(
                {PoolReceipt.assigned_company_id: None}, synchronize_session=False)
            db.commit()
            reconcile.reconcile(db)
            db.commit()
            assert self._doc_for(db, service.POOL, pid).ownership.company_id is None
        res = client.post("/api/v1/pool-company/assign", headers=a2, json={"receipt_ids": [pid]})
        assert res.status_code == 200, res.text
        with SessionLocal() as db:
            assert db.get(PoolReceipt, pid).assigned_company_id == c2
            assert self._doc_for(db, service.POOL, pid).ownership.company_id == c2
            doc = self._doc_for(db, service.POOL, pid)
            db.query(UnifiedOrigin).filter_by(document_id=doc.id).delete(synchronize_session=False)
            _cleanup(db, pid=pid)

    def test_old_version_can_delete_company_referenced_by_unified_registry(self, companies):
        """Без внешних ключей на companies удаление компании старым кодом не падает."""
        from sqlalchemy import text

        from app.unified import reconcile

        _, _, c2, _, _ = companies
        with SessionLocal() as db:
            gone = Company(name="Удаляемая " + uuid4().hex)
            db.add(gone)
            db.commit()
            gone_id = gone.id
        rid, doc_id = TestDriftAfterRollback()._registered(gone_id)
        with SessionLocal() as db:   # ровно как старая версия: перенос чеков и DELETE
            db.query(Receipt).filter_by(company_id=gone_id).update(
                {Receipt.company_id: c2}, synchronize_session=False)
            db.execute(text("DELETE FROM companies WHERE id = :id"), {"id": gone_id})
            db.commit()
            reconcile.reconcile(db)
            db.commit()
            assert db.get(UnifiedDocument, doc_id).ownership.company_id == c2
            db.query(UnifiedOrigin).filter_by(document_id=doc_id).delete(synchronize_session=False)
            _cleanup(db, rid)

    def test_ambiguous_copies_are_left_for_admin(self, companies):
        from app.unified import reconcile

        _, c1, c2, _, _ = companies
        fn, fd, fp = _identity()
        with SessionLocal() as db:
            pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
            reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
            db.commit()
            backfill.backfill(db)
            rid, pid = reg.id, pool.id
            doc_id = self._doc_for(db, service.REGISTRY, rid).id
            # старая версия перенесла только реестровую копию
            db.query(Receipt).filter_by(id=rid).update({Receipt.company_id: c2}, synchronize_session=False)
            db.commit()
            first = reconcile.reconcile(db)
            db.commit()
            assert first["ambiguous"] >= 1
            assert db.get(UnifiedDocument, doc_id).ownership.company_id == c1   # без догадок
            second = reconcile.reconcile(db)
            db.commit()
            assert {k: v for k, v in second.items() if k != "ambiguous"} == {
                "dangling_removed": 0, "documents_closed": 0, "owners_fixed": 0, "authors_fixed": 0}
            db.query(UnifiedOrigin).filter_by(document_id=doc_id).delete(synchronize_session=False)
            _cleanup(db, rid, pid)


# ==========================================================================
# 3. Supabase демо не мешает рабочему меню
# ==========================================================================
class TestSupabaseMenu:
    def test_nav_item_is_hidden_until_contour_enabled(self):
        html = read("app/static/index.html")
        item = re.search(r'<a href="#/supabase"[^>]*>', html).group(0)
        assert "supabase-only" in item and "hidden" in item
        js = read("app/static/js/app.js")
        assert "/api/v1/supabase-demo/config" in js
        assert "$$('.supabase-only')" in js


# ==========================================================================
# 4. Выпуск
# ==========================================================================
class TestRelease:
    """Выпуск 1.70.82 — исторические проверки.

    v1.70.83: точные пины перенесены в tests/test_v17083.py. Здесь остаётся то,
    что верно и после следующих выпусков: версия только растёт, запись о
    выпуске на месте, точки синхронизации совпадают с текущей версией.
    """

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert current in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw
        assert sw.count(f"?v={current}") == 2

    def test_changelog_entry_is_stable(self):
        md = read("CHANGELOG.md")
        assert "WIP" not in md.split("\n", 1)[0]
        entry = md.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        for needle in ("резервн", "откат", "1.70.80"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        assert f"'{VERSION}': [" in read("app/static/js/app.js")

    def test_journal_entry(self):
        assert f"## v{VERSION} —" in read("docs/ui_journal.md")

    def test_changed_blocks_are_reported(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Единый реестр чеков", "Чек-Пул", "Роли и доступ", "Сканирование чеков"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        # версия блока не может опережать версию программы
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
