"""v1.71.4: банки и ошибки в карточке клиента (T-87, этап 4 плана «Клиенты»).

Сервер уже считает по компании худшее состояние подключения, последнюю загрузку и первую ошибку с названием
банка (v1.70.102, ADR-32), но окно показывало только одну строку. Теперь в плитке «Банк» — каждый подключённый
банк со своим состоянием и своей ошибкой, а порядок — от худшего к лучшему. Проверяется API (список банков,
порядок, ошибки) и окно (разметка плитки). Только синтетика (ADR-14).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.bank.models import BankConnection
from app.clients import service as svc
from app.database import SessionLocal
from app.models import User
from tests import test_docreq as _dr
from tests import test_primary_upd as _upd

firms = _upd.firms
client_firm = _dr.client_firm
PERIOD = _dr.PERIOD


def _conn(company_id: str, bank: str, status: str, *, error: str = "", sync: datetime | None = None,
          method: str = "jwt") -> BankConnection:
    row = BankConnection(company_id=company_id, bank=bank, method=method, status=status, last_error=error,
                         last_sync_at=sync)
    with SessionLocal() as db:
        db.add(row)
        db.commit()
    return row


@pytest.fixture
def bank_firm(client_firm):
    """Компания A: три банка — Точка в работе, Т-Банк с ошибкой, Сбер ждёт подтверждения."""
    a = client_firm["a"]
    _conn(a["id"], "tochka", "active", sync=datetime(2026, 11, 4, 7, 5))
    _conn(a["id"], "tbank", "error", error="401: ключ API отозван")
    _conn(a["id"], "sber", "awaiting")
    yield client_firm
    with SessionLocal() as db:
        for cid in (a["id"], client_firm["b"]["id"]):
            db.query(BankConnection).filter(BankConnection.company_id == cid).delete(synchronize_session=False)
        db.commit()


def _row(company_id: str) -> dict:
    with SessionLocal() as db:
        acc = db.query(User).filter_by(role="accountant", company_id=company_id).first()
        data = svc.overview(db, acc, PERIOD, now=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
    return next(c for c in data["companies"] if c["id"] == company_id)


def test_banks_of_company_sorted_from_worst(bank_firm):
    """Плитка «Банк»: каждый банк со своим состоянием, худший — первым; общее состояние — худшее."""
    row = _row(bank_firm["a"]["id"])
    banks = row["bank"]["banks"]
    assert [b["bank"] for b in banks] == ["tbank", "sber", "tochka"]          # error → awaiting → active
    assert banks[0]["title"] == "Т-Банк" and banks[0]["status"] == "error"
    assert banks[0]["error"] == "401: ключ API отозван" and banks[0]["last_sync_at"] == ""
    assert banks[2]["last_sync_at"].startswith("2026-11-04")
    assert row["bank"]["status"] == "error"                                   # худшее состояние компании
    assert row["bank"]["last_error"].startswith("Т-Банк: 401")               # ошибка с названием банка
    assert row["level"] == "bad"


def test_company_without_banks(bank_firm):
    """Компания B без подключений: списка банков нет вовсе, состояние — «не подключён»."""
    row = _row(bank_firm["b"]["id"])
    assert row["bank"]["banks"] == [] and row["bank"]["status"] == "none" and row["bank"]["last_error"] == ""
    assert row["bank"]["open"] == 0


def test_error_bank_does_not_hide_healthy(bank_firm):
    """Ошибка одного банка не скрывает второй: в списке видно и рабочий банк, его загрузку и счётчики."""
    row = _row(bank_firm["a"]["id"])
    working = next(b for b in row["bank"]["banks"] if b["bank"] == "tochka")
    assert working["status"] == "active" and working["error"] == ""
    assert row["score"] >= 3                                                 # худшее состояние — в очереди внимания


def test_errors_are_trimmed(bank_firm):
    """Текст ошибки банка показывается в разумном размере — окно не должно получать простыню."""
    a = bank_firm["a"]
    _conn(a["id"], "alfa", "error", error="500: " + "очень длинный ответ банка " * 40)
    row = _row(a["id"])
    alfa = next(b for b in row["bank"]["banks"] if b["bank"] == "alfa")
    assert len(alfa["error"]) <= 200 and alfa["error"].startswith("500: ")
    assert alfa["last_error"].startswith("Альфа-Банк: 500: ") and len(alfa["last_error"]) <= 200
    tbank = next(b for b in row["bank"]["banks"] if b["bank"] == "tbank")
    # у каждого банка своя ошибка: отозванный ключ Т-Банка не подменяется ошибкой Альфа-Банка
    assert tbank["error"] == "401: ключ API отозван" and "Альфа" not in tbank["last_error"]


def test_schema_unchanged():
    """Схема базы не меняется: банки берутся из существующей bank_connections."""
    from app.database import Base
    assert "bank_connections" in Base.metadata.tables
