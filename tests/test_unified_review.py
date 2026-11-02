"""Экран разбора конфликтов единого реестра (#9): API карточки, фильтры, решение.

Конфликт создаётся так же, как в жизни: одна фискальная тройка у двух компаний
(Чек-Пул у одной, реестр у другой) — второй источник получает REVIEW_OWNERSHIP.
"""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import Company, Receipt, utcnow
from app.pool.models import PoolReceipt
from app.services import rbac
from app.unified import models as unified_models
from app.unified import service
from app.unified.models import REVIEW_BLOCKING, REVIEW_OWNERSHIP, UnifiedDocument
from tests.conftest import login
from tests.test_unified_receipts import _identity, _pool, _registry
from tests.test_v1140 import _mk_user

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js" / "unified_review.js"
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
INDEX = ROOT / "app" / "static" / "index.html"


@pytest.fixture
def two_companies(client):
    hdr = login(client, "admin", "admin123")
    c1 = client.post("/api/v1/companies", headers=hdr, json={"name": "Разбор А " + uuid4().hex}).json()["id"]
    c2 = client.post("/api/v1/companies", headers=hdr, json={"name": "Разбор Б " + uuid4().hex}).json()["id"]
    a1 = _mk_user(client, hdr, c1, "review_a_" + uuid4().hex[:10], role="accountant")
    a2 = _mk_user(client, hdr, c2, "review_b_" + uuid4().hex[:10], role="accountant")
    yield hdr, c1, c2, a1, a2
    client.cookies.clear()


def _ownership_conflict(pool_company: str, registry_company: str) -> tuple[str, str, str]:
    """Спор о владельце: копия в пуле у одной компании, в реестре — у другой."""
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp, company=db.get(Company, pool_company))
        reg = _registry(db, fn, fd, fp, company=db.get(Company, registry_company))
        db.flush()
        service.register_source(db, service.POOL, pool)
        doc = service.register_source(db, service.REGISTRY, reg)
        db.commit()
        assert doc.review_flags & REVIEW_OWNERSHIP, "конфликт принадлежности не возник"
        assert doc.ownership.company_id is None
        return doc.id, pool.id, reg.id


def _conflict_ids(client, hdr, flag: int = 0) -> set[str]:
    """Все id из списка конфликтов (с перебором страниц)."""
    ids: set[str] = set()
    page = 1
    while True:
        params = {"conflicts": "true", "page": page, "per_page": 100}
        if flag:
            params["flag"] = flag
        res = client.get("/api/v1/unified/documents", headers=hdr, params=params)
        assert res.status_code == 200, res.text
        body = res.json()
        ids.update(item["id"] for item in body["items"])
        if page * body["per_page"] >= body["total"]:
            return ids
        page += 1


def _review(client, hdr, doc_id: str) -> dict:
    res = client.get(f"/api/v1/unified/documents/{doc_id}/review", headers=hdr)
    assert res.status_code == 200, res.text
    return res.json()


class TestConflictList:
    def test_flag_filter_selects_ownership_conflicts(self, two_companies, client):
        hdr, c1, c2, _a1, _a2 = two_companies
        doc_id, _pool_id, _reg_id = _ownership_conflict(c1, c2)
        assert doc_id in _conflict_ids(client, hdr)
        assert doc_id in _conflict_ids(client, hdr, flag=REVIEW_OWNERSHIP)
        assert doc_id not in _conflict_ids(client, hdr, flag=unified_models.REVIEW_DATE), \
            "фильтр по дате не должен показывать спор о владельце"

    def test_conflict_is_hidden_from_both_companies(self, two_companies, client):
        _hdr, c1, c2, a1, a2 = two_companies
        doc_id, _pool_id, _reg_id = _ownership_conflict(c1, c2)
        for staff in (a1, a2):
            assert doc_id not in _conflict_ids(client, staff)
            assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=staff).status_code == 404

    def test_deleted_document_leaves_conflict_list(self, two_companies, client):
        """Удалённый документ разобрать нельзя — в списке его нет, как и в счётчике."""
        hdr, c1, c2, _a1, _a2 = two_companies
        doc_id, _pool_id, _reg_id = _ownership_conflict(c1, c2)
        assert doc_id in _conflict_ids(client, hdr)
        with SessionLocal() as db:
            db.get(UnifiedDocument, doc_id).deleted_at = utcnow()
            db.commit()
        assert doc_id not in _conflict_ids(client, hdr)
        card = _review(client, hdr, doc_id)
        assert card["deleted"] is True and card["can_resolve"] is False

    def test_flag_parameter_is_bounded(self, two_companies, client):
        hdr = two_companies[0]
        res = client.get("/api/v1/unified/documents", headers=hdr, params={"flag": 256})
        assert res.status_code == 422


class TestReviewCard:
    def test_card_lists_copies_candidates_and_named_journal(self, two_companies, client):
        hdr, c1, c2, _a1, _a2 = two_companies
        doc_id, pool_id, reg_id = _ownership_conflict(c1, c2)
        card = _review(client, hdr, doc_id)
        assert card["can_resolve"] is True and card["deleted"] is False
        assert card["review_flags"] & REVIEW_OWNERSHIP
        copies = {(c["kind"], c["id"]): c["company_id"] for c in card["copies"]}
        assert copies == {("pool", pool_id): c1, ("registry", reg_id): c2}
        assert [c["company_id"] for c in card["candidates"]] == [c1, c2]
        assert card["candidates"][0]["sources"] == ["pool"]
        assert card["candidates"][1]["sources"] == ["registry"]
        assert all(c["company_name"].startswith("Разбор ") for c in card["candidates"])
        assert [e["type"] for e in card["events"]] == ["register", "register"]
        assert all("actor_name" in e and "to_company_name" in e for e in card["events"])

    def test_card_is_admin_only(self, two_companies, client):
        hdr, c1, c2, a1, _a2 = two_companies
        doc_id, _pool_id, _reg_id = _ownership_conflict(c1, c2)
        assert client.get(f"/api/v1/unified/documents/{doc_id}/review", headers=a1).status_code == 403
        assert client.get("/api/v1/unified/documents/нет-такого/review", headers=hdr).status_code == 404

    def test_card_reads_without_side_effects(self, two_companies, client):
        """GET карточки ничего не меняет: версия и ссылки на копии прежние."""
        hdr, c1, c2, _a1, _a2 = two_companies
        doc_id, _pool_id, _reg_id = _ownership_conflict(c1, c2)
        first = _review(client, hdr, doc_id)
        second = _review(client, hdr, doc_id)
        assert first["version"] == second["version"]
        assert first["origins"] == second["origins"]


class TestResolveOwnership:
    def test_assign_company_moves_every_copy_and_logs_reason(self, two_companies, client):
        hdr, c1, c2, a1, a2 = two_companies
        doc_id, pool_id, reg_id = _ownership_conflict(c1, c2)
        card = _review(client, hdr, doc_id)
        res = client.post(f"/api/v1/unified/documents/{doc_id}/resolve-ownership", headers=hdr, json={
            "company_id": c1, "expected_version": card["version"], "reason": "Оплачен картой компании А"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["company_id"] == c1
        assert not body["review_flags"] & REVIEW_OWNERSHIP
        assert body["version"] == card["version"] + 1
        with SessionLocal() as db:
            assert db.get(Receipt, reg_id).company_id == c1
            assert db.get(PoolReceipt, pool_id).assigned_company_id == c1
        after = _review(client, hdr, doc_id)
        last = after["events"][-1]
        assert last["type"] == "resolve_ownership"
        assert last["details"]["reason"] == "Оплачен картой компании А"
        assert last["to_company_id"] == c1 and last["to_company_name"].startswith("Разбор А")
        assert last["actor_name"], "в журнале должно быть имя администратора"
        assert after["can_resolve"] is False
        assert doc_id not in _conflict_ids(client, hdr)
        assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=a1).status_code == 200
        assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=a2).status_code == 404

    def test_leave_without_owner_detaches_every_copy(self, two_companies, client):
        hdr, c1, c2, a1, a2 = two_companies
        doc_id, pool_id, reg_id = _ownership_conflict(c1, c2)
        version = _review(client, hdr, doc_id)["version"]
        res = client.post(f"/api/v1/unified/documents/{doc_id}/resolve-ownership", headers=hdr, json={
            "company_id": None, "expected_version": version, "reason": "Чек не относится ни к одной компании"})
        assert res.status_code == 200, res.text
        assert res.json()["company_id"] is None
        with SessionLocal() as db:
            assert db.get(Receipt, reg_id).company_id is None
            assert db.get(PoolReceipt, pool_id).assigned_company_id is None
        for staff in (a1, a2):
            assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=staff).status_code == 404

    def test_rejects_stale_version_short_reason_repeat_and_non_admin(self, two_companies, client):
        hdr, c1, c2, a1, _a2 = two_companies
        doc_id, _pool_id, _reg_id = _ownership_conflict(c1, c2)
        url = f"/api/v1/unified/documents/{doc_id}/resolve-ownership"
        version = _review(client, hdr, doc_id)["version"]
        stale = client.post(url, headers=hdr, json={"company_id": c1, "expected_version": version - 1,
                                                    "reason": "Устаревшая карточка"})
        assert stale.status_code == 409 and "Версия чека изменилась" in stale.json()["detail"]
        short = client.post(url, headers=hdr, json={"company_id": c1, "expected_version": version, "reason": "ab"})
        assert short.status_code == 422
        staff = client.post(url, headers=a1, json={"company_id": c1, "expected_version": version,
                                                   "reason": "Бухгалтер не решает споры"})
        assert staff.status_code == 403
        ok = client.post(url, headers=hdr, json={"company_id": c2, "expected_version": version,
                                                 "reason": "Решение администратора"})
        assert ok.status_code == 200, ok.text
        again = client.post(url, headers=hdr, json={"company_id": c1, "expected_version": version + 1,
                                                    "reason": "Повторное решение"})
        assert again.status_code == 409 and "нет конфликта принадлежности" in again.json()["detail"]


class TestScreenIsWired:
    """Экран подключён: меню, маршрут, пространство роли, биты пометок."""

    def test_js_flags_match_server_bits(self):
        src = JS.read_text(encoding="utf-8")
        found = {int(bit): (code, blocking == "true") for bit, code, blocking in re.findall(
            r"\{ bit: (\d+), code: '([a-z]+)', title: '[^']+', blocking: (true|false)", src)}
        server = {getattr(unified_models, name): name for name in dir(unified_models)
                  if name.startswith("REVIEW_") and name != "REVIEW_BLOCKING"}
        assert set(found) == set(server), (found, server)
        assert {bit for bit, (_code, blocking) in found.items() if blocking} == \
            {bit for bit in server if bit & REVIEW_BLOCKING}
        assert "export const OWNERSHIP_BIT = 8;" in src and REVIEW_OWNERSHIP == 8

    def test_menu_route_and_role_space(self):
        html = INDEX.read_text(encoding="utf-8")
        assert re.search(r'<a href="#/conflicts" data-view="conflicts" class="nav-item admin-only hidden">', html)
        assert 'id="nav-conflicts-badge"' in html
        app = APP_JS.read_text(encoding="utf-8")
        # v1.70.85: в том же импорте — badgeCount (значок считает споры и заявки)
        imported = re.search(r"import \{([^}]*)\} from './unified_review\.js';", app)
        assert imported and {"setConflictsBadge", "viewUnifiedReview"} <= {
            name.strip() for name in imported.group(1).split(",")}
        assert "conflicts: viewUnifiedReview," in app
        assert "conflicts" in rbac.space_views("admin")
        for role in ("accountant", "manager", "user", "donor"):
            assert "conflicts" not in rbac.space_views(role), role
