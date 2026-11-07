"""T-78 (v1.70.111): время чеков, сдвинутое формой правки до 1.70.110, — отчёт и возврат по QR.

Сдвиг воспроизводится так, как его делала старая форма: время копии чека уходит на 5 (или 10)
часов назад, источник данных — «правка формой», и правка расходится в единую запись и Чек-Пул
тем же sync_registry. Отчёт ничего не меняет; возврат — только по кнопке администратора, с
резервной копией (SQLite) и строкой журнала на каждый чек. SQLite и PostgreSQL. Только
синтетические данные (ADR-14).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import AuditLog, Receipt
from app.pool.models import PoolReceipt
from app.services import backups
from app.unified import service, time_repair
from tests.test_ui_api_contract import (  # noqa: F401 — фикстуры баз и компаний
    Backend,
    _scanned_after_pool,
    backend,
    companies,
    pg_backend,
    sqlite_backend,
)

QR_TIME = datetime(2026, 9, 22, 15, 56)        # время кассы в строке QR (_scanned_after_pool)
API = "/api/v1/unified/receipt-time"


def _shift(store: Backend, receipt_id: str, delta: timedelta, *, source: str | None = None,
           edited: bool = True) -> None:
    """Как старая форма: время копии сдвинуто, правка ушла в единую запись и Чек-Пул."""
    with store.session() as db:
        receipt = db.get(Receipt, receipt_id)
        receipt.receipt_date = QR_TIME + delta
        if edited:
            receipt.details_source = "manual_edit"
        if source:
            receipt.source = source
        db.flush()
        service.sync_registry(db, receipt, None)
        db.commit()


def _times(store: Backend, receipt_id: str, pool_id: str | None) -> tuple:
    with store.session() as db:
        receipt = db.get(Receipt, receipt_id)
        doc = service.document_for_source(db, service.REGISTRY, receipt_id)
        pool = db.get(PoolReceipt, pool_id) if pool_id else None
        return receipt.receipt_date, doc.receipt_date, pool.receipt_date if pool else None


@pytest.fixture
def backup_calls(monkeypatch):
    calls = []

    def fake_backup(kind: str = "manual"):
        calls.append(kind)
        return f"/tmp/db-{kind}-t78.db"

    monkeypatch.setattr(backups, "create_backup", fake_backup)
    return calls


def test_shift_hours_rule():
    base = QR_TIME
    assert time_repair.shift_hours(base - timedelta(hours=5), base) == -5
    assert time_repair.shift_hours(base - timedelta(hours=5, seconds=37), base) == -5   # секунды потеряны
    assert time_repair.shift_hours(base - timedelta(hours=10), base) == -10           # два сохранения
    assert time_repair.shift_hours(base + timedelta(hours=3), base) == 3
    assert time_repair.shift_hours(base + timedelta(minutes=7), base) == 0             # исправление минут
    assert time_repair.shift_hours(base + timedelta(seconds=37), base) == 0            # секунды из ФНС
    assert time_repair.shift_hours(base - timedelta(hours=5, minutes=7), base) == 0    # не кратно часу
    assert time_repair.shift_hours(base - timedelta(hours=73), base) == 0              # не сдвиг поясом
    assert time_repair.qr_time("не QR") is None and time_repair.qr_time("") is None
    assert time_repair.qr_time("t=20260922T1556&s=100.00&fn=7000000000000001&i=3467&fp=2882574203&n=1") == base


def test_report_and_restore(client, backend, backup_calls):  # noqa: F811
    with backend.session() as db:
        shifted, pool_id = _scanned_after_pool(db, backend.company_id)
        twice, _ = _scanned_after_pool(db, backend.company_id, with_pool=False)
        minutes, _ = _scanned_after_pool(db, backend.company_id, with_pool=False)
        seconds, _ = _scanned_after_pool(db, backend.company_id, with_pool=False)
        manual, _ = _scanned_after_pool(db, backend.company_id, with_pool=False)
        untouched, _ = _scanned_after_pool(db, backend.company_id, with_pool=False)
    _shift(backend, shifted, timedelta(hours=-5))
    _shift(backend, twice, timedelta(hours=-10))
    _shift(backend, minutes, timedelta(minutes=7))
    _shift(backend, seconds, timedelta(seconds=37), edited=False)
    _shift(backend, manual, timedelta(hours=-3), source="manual")
    _shift(backend, untouched, timedelta(hours=-5), edited=False)
    assert _times(backend, shifted, pool_id) == (QR_TIME - timedelta(hours=5),) * 3   # сдвиг ушёл и в пул

    res = client.get(API, headers=backend.admin)
    assert res.status_code == 200, res.text
    data = res.json()
    items = {i["receipt_id"]: i for i in data["items"]}
    assert items[shifted]["hours"] == -5 and items[shifted]["fixable"] is True
    assert items[shifted]["qr_time"] == "2026-09-22T15:56:00" and items[shifted]["receipt_date"] == "2026-09-22T10:56:00"
    assert items[twice]["hours"] == -10 and items[twice]["fixable"] is True
    assert minutes not in items and seconds not in items
    assert items[manual]["fixable"] is False and items[manual]["reason"] == time_repair.REASON_MANUAL
    assert items[untouched]["fixable"] is False and items[untouched]["reason"] == time_repair.REASON_NOT_EDITED
    assert data["fixable"] >= 2 and data["shifted"] >= 4 and data["checked"] >= 6
    assert _times(backend, shifted, pool_id)[0] == QR_TIME - timedelta(hours=5)       # отчёт ничего не меняет
    assert backup_calls == []

    res = client.post(f"{API}/restore", headers=backend.admin, json={"ids": [shifted, twice, manual]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["restored"] == 2 and body["skipped"] == 1
    if backend.name == "SQLite":
        assert backup_calls == ["manual"] and body["backup"] == "db-manual-t78.db"
    else:
        assert backup_calls == [] and body["backup"] is None
    assert _times(backend, shifted, pool_id) == (QR_TIME, QR_TIME, QR_TIME)        # копия, единая запись, пул
    assert _times(backend, twice, None)[:2] == (QR_TIME, QR_TIME)
    assert _times(backend, manual, None)[0] == QR_TIME - timedelta(hours=3)         # ручной ввод не трогаем
    assert _times(backend, untouched, None)[0] == QR_TIME - timedelta(hours=5)
    with SessionLocal() as db:                                                      # журнал — в основной базе
        rows = db.query(AuditLog).filter(AuditLog.action == "receipt_time_restored",
                                         AuditLog.entity_id.in_([shifted, twice])).all()
        assert len(rows) == 2
    after = {i["receipt_id"] for i in client.get(API, headers=backend.admin).json()["items"]}
    assert shifted not in after and twice not in after and manual in after


def test_only_admin(client, backend):  # noqa: F811
    assert client.get(API, headers=backend.staff).status_code == 403
    assert client.post(f"{API}/restore", headers=backend.staff, json={}).status_code == 403


def test_no_backup_no_restore(client, sqlite_backend, monkeypatch):  # noqa: F811
    monkeypatch.setattr(backups, "create_backup", lambda kind="manual": None)
    store = sqlite_backend
    with store.session() as db:
        rid, pool_id = _scanned_after_pool(db, store.company_id)
    _shift(store, rid, timedelta(hours=-5))
    res = client.post(f"{API}/restore", headers=store.admin, json={"ids": [rid]})
    assert res.status_code == 500 and "резервную копию" in res.json()["detail"]
    assert _times(store, rid, pool_id)[0] == QR_TIME - timedelta(hours=5)
