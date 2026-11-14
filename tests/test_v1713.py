"""v1.71.3: согласования авансовых отчётов в сводке клиентов (T-86, этап 3 плана «Клиенты», ADR-30).

На экране «Клиенты» появляется плитка «Согласования»: сколько отчётов ждут моего хода, сколько из них
просрочено (срок этапа истёк), сколько отчётов в работе и сколько заведено за отчётный месяц. Флаг
`approval_settings.enabled` компании уважается: у компании без согласования цифр нет. Правило «чей ход» —
то же, что на экране «Согласования» (одна функция, без второго мнения).

Поведение маршрута — tests/test_approvals.py (SQLite и PostgreSQL). Только синтетика (ADR-14).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.approvals.models import AdvanceApproval, AdvanceApprovalEvent, ApprovalSettings
from app.clients import service as svc
from app.database import SessionLocal
from app.models import User
from tests import test_docreq as _dr
from tests import test_primary_upd as _upd

firms = _upd.firms                  # фикстура-зависимость: компании и бухгалтеры (общая с test_docreq)
client_firm = _dr.client_firm
PERIOD = _dr.PERIOD
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)   # как в API: aware UTC
NAIVE = NOW.replace(tzinfo=None)                     # как в базе: naive UTC


def _approval(company_id: str, employee_id: str, *, status: str = "pending", stage: str = "accountant",
              deadline: datetime | None = None, created: datetime | None = None,
              finished: datetime | None = None, total: float = 1000.0) -> AdvanceApproval:
    """Отчёт прямо в базе: маршрут, подписи и сроки проверяет tests/test_approvals.py."""
    row = AdvanceApproval(id=str(uuid4()), company_id=company_id, employee_id=employee_id,
                          employee_name="Сотрудник Согласований", number=f"АО-{uuid4().hex[:6]}",
                          date_from="2026-10-01", date_to="2026-10-31", receipts_count=2, total=total,
                          snapshot="{}", doc_hash="0" * 64, status=status, stage=stage,
                          deadline_at=deadline, finished_at=finished,
                          created_at=created or datetime(2026, 10, 20, 9, 0))
    with SessionLocal() as db:
        db.add(row)
        db.commit()
    return row


def _clean(*company_ids: str) -> None:
    with SessionLocal() as db:
        for model in (AdvanceApprovalEvent, AdvanceApproval, ApprovalSettings):
            db.query(model).filter(model.company_id.in_(company_ids)).delete(synchronize_session=False)
        db.commit()


def _overview(company_id: str, now: datetime = NOW) -> dict:
    with SessionLocal() as db:
        acc = db.query(User).filter_by(role="accountant", company_id=company_id).first()
        return svc.overview(db, acc, PERIOD, now=now)


@pytest.fixture
def firms_approvals(client, client_firm):
    """Компания A: согласование включено и пять отчётов; в компании B согласование выключено, отчёт есть."""
    a, b = client_firm["a"], client_firm["b"]
    emp = client_firm["emp"]
    with SessionLocal() as db:
        db.add(ApprovalSettings(company_id=a["id"], enabled=True))
        db.commit()
    _approval(a["id"], emp, stage="accountant", deadline=NAIVE + timedelta(hours=10),   # ждёт бухгалтера
              created=datetime(2026, 10, 20, 9, 0))
    _approval(a["id"], emp, stage="accountant", deadline=NAIVE - timedelta(hours=2),    # ждёт и просрочен
              created=datetime(2026, 10, 21, 9, 0))
    _approval(a["id"], emp, stage="manager", deadline=NAIVE + timedelta(hours=10),      # ждёт руководителя
              created=datetime(2026, 10, 22, 9, 0))
    _approval(a["id"], emp, status="approved", stage="", deadline=None,                 # согласован в октябре
              created=datetime(2026, 10, 5, 9, 0), finished=datetime(2026, 10, 6, 9, 0))
    _approval(a["id"], emp, status="draft", stage="employee", deadline=None,            # черновик сотрудника
              created=datetime(2026, 10, 6, 9, 0))
    _approval(b["id"], emp, stage="accountant", deadline=NAIVE - timedelta(hours=3),    # чужая компания
              created=datetime(2026, 10, 20, 9, 0))
    yield client_firm
    _clean(a["id"], b["id"])


def test_tile_counts_todo_overdue_month_and_work(firms_approvals):  # noqa: ARG001 — нужна фикстура
    """Пять чисел плитки: ждут моего хода, из них просрочено, в работе, заведено за месяц, вкл/выкл."""
    row = next(c for c in _overview(firms_approvals["a"]["id"])["companies"] if c["id"] == firms_approvals["a"]["id"])
    assert row["approvals"] == {"enabled": True, "todo": 2, "overdue": 1, "work": 4, "month": 5}
    other = next(c for c in _overview(firms_approvals["b"]["id"])["companies"] if c["id"] == firms_approvals["b"]["id"])
    assert other["approvals"] == {"enabled": False, "todo": 0, "overdue": 0, "work": 0, "month": 0}


def test_approvals_raise_attention_and_totals(firms_approvals):
    """Просроченное согласование поднимает компанию в очереди внимания и попадает в итоги сводки."""
    a = firms_approvals["a"]
    before = _overview(a["id"])
    row_before = next(c for c in before["companies"] if c["id"] == a["id"])
    _approval(a["id"], firms_approvals["emp"], stage="accountant", deadline=NAIVE - timedelta(hours=6),
              created=datetime(2026, 10, 25, 9, 0))
    after = _overview(a["id"])
    row_after = next(c for c in after["companies"] if c["id"] == a["id"])
    assert row_after["approvals"]["todo"] == row_before["approvals"]["todo"] + 1
    assert row_after["approvals"]["overdue"] == row_before["approvals"]["overdue"] + 1
    assert row_after["score"] > row_before["score"]
    assert after["totals"]["approvals_todo"] == before["totals"]["approvals_todo"] + 1
    assert after["totals"]["approvals_overdue"] == before["totals"]["approvals_overdue"] + 1


def test_approvals_off_show_no_numbers(client_firm):
    """Согласование в компании выключено — отчёты в сводке не считаются (нули на экране не рисуются)."""
    a, b = client_firm["a"], client_firm["b"]
    _approval(a["id"], client_firm["emp"], stage="accountant", deadline=NAIVE - timedelta(hours=6))
    try:
        row = next(c for c in _overview(a["id"])["companies"] if c["id"] == a["id"])
        assert row["approvals"] == {"enabled": False, "todo": 0, "overdue": 0, "work": 0, "month": 0}
    finally:
        _clean(a["id"], b["id"])


def test_accountant_b_does_not_see_company_a(client_firm):
    """Бухгалтер другой компании не видит ни отчётов, ни компании: чужие обработки не всплывают."""
    a, b = client_firm["a"], client_firm["b"]
    _approval(a["id"], client_firm["emp"], stage="accountant", deadline=NAIVE - timedelta(hours=3))
    try:
        data = _overview(b["id"])
        assert [c["id"] for c in data["companies"]] == [b["id"]]
        assert data["companies"][0]["approvals"]["todo"] == 0
        assert data["totals"]["approvals_overdue"] == 0
    finally:
        _clean(a["id"], b["id"])


def test_same_turn_rule_as_approvals_screen(client, firms_approvals):
    """Счётчик «ждут меня» совпадает с «Ждут меня» на экране «Согласования» — правило одно на двоих."""
    a = firms_approvals["a"]
    page = client.get("/api/v1/approvals?box=todo", headers=a["hdr"])
    assert page.status_code == 200, page.text
    row = next(c for c in _overview(a["id"])["companies"] if c["id"] == a["id"])
    assert page.json()["counts"]["todo"] == row["approvals"]["todo"] == 2


def test_schema_unchanged():
    """Схема базы не меняется: согласования считаются по существующим таблицам."""
    from app.database import Base
    assert {"advance_approvals", "advance_approval_events", "approval_settings"} <= set(Base.metadata.tables)
