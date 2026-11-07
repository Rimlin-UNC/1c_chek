"""Эскалация напоминаний (v1.70.99): сотрудник не прислал чек или документ N дней — запрос видит руководитель.

Модуль 5 видения владельца («умные напоминания с эскалацией»): сначала напоминание сотруднику, затем
руководителю. Каналы подменены (ничего не уходит наружу), данные синтетические (ADR-14).
"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from app.database import SessionLocal
from app.docreq import service as svc
from app.docreq.models import DocRequest, DocRequestSettings
from tests import test_docreq as _dr

firms = _dr.firms
client_firm = _dr.client_firm
channels = _dr.channels
at, PERIOD = _dr.at, _dr.PERIOD


def _age_receipt_request(cid: str, now, days: int) -> str:
    """Запрос по чеку сотрудника «создан» days дней назад относительно момента напоминания."""
    with SessionLocal() as db:
        svc.detect(db, cid, PERIOD)
        req = db.query(DocRequest).filter_by(company_id=cid, period=PERIOD, kind="receipt").one()
        req.created_at = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        db.commit()
        return req.id


def test_late_personal_request_goes_to_manager_once_logged(client_firm, channels):
    cid = client_firm["a"]["id"]
    _dr._enable(cid, escalate_days=3, auto_detect=False)
    moment = at(4, 10, 5)
    rid = _age_receipt_request(cid, moment, 4)
    with SessionLocal() as db:
        svc.tick(db, now=moment, company_id=cid)
    by_target = {(c, t): text for c, t, text in channels}
    boss, emp, mail = by_target[("telegram", "1001")], by_target[("telegram", "1002")], by_target[("email", "buh@client.example")]
    assert "⚠ Чек на 1 250,00 ₽" in boss and "Сотрудник Тестов не прислал(а) документ 4 дн." in boss
    assert "⚠" not in emp and "Чек на 1 250,00 ₽" in emp                     # сотруднику — как раньше
    assert "Сотрудник Тестов не прислал(а) документ 4 дн." in mail             # общая почта клиента — тоже
    events = _dr._events(cid, kind="escalated")
    assert len(events) == 1 and events[0].request_id == rid and events[0].slot == "D-1"
    assert "Руководитель Тестов" in events[0].recipient and "buh@client.example" in events[0].recipient
    assert events[0].text.endswith("— передано руководителю")
    # следующее напоминание: руководитель снова видит запрос, но событие «передано» не дублируется
    first = len(channels)
    with SessionLocal() as db:
        svc.tick(db, now=at(5, 10, 1), company_id=cid)
    again = [text for c, t, text in channels[first:] if (c, t) == ("telegram", "1001")]
    # между тиками 23 ч 56 мин: полных дней по-прежнему 4
    assert len(again) == 1 and "⚠ Чек на 1 250,00 ₽" in again[0] and "документ 4 дн." in again[0]
    assert len(_dr._events(cid, kind="escalated")) == 1


@pytest.mark.usefixtures("channels")
def test_fresh_request_and_disabled_escalation_stay_personal(client_firm):
    cid = client_firm["a"]["id"]
    _dr._enable(cid, escalate_days=3, auto_detect=False)
    moment = at(4, 10, 5)
    _age_receipt_request(cid, moment, 2)                    # 2 дня < 3 — ещё не передаём
    with SessionLocal() as db:
        entries = svc.plan(db, cid, PERIOD, now=moment)
    boss = next(e for e in entries if "user" in e and e["user"].id == client_firm["boss"])
    assert boss["escalated"] == 0 and not any(r.kind == "receipt" for r in boss["items"])
    with SessionLocal() as db:
        late = svc.plan(db, cid, PERIOD, now=moment + timedelta(days=2))
        assert next(e for e in late if "user" in e and e["user"].id == client_firm["boss"])["escalated"] == 1
        row = db.get(DocRequestSettings, cid)
        row.escalate_days = 0                               # 0 — не передавать никогда
        db.commit()
        off = svc.plan(db, cid, PERIOD, now=moment + timedelta(days=20))
    assert all(e.get("escalated", 0) == 0 for e in off)
    assert not _dr._events(cid, kind="escalated")


def test_settings_api_escalate_days(client, client_firm):
    a = client_firm["a"]
    base = {"enabled": True, "auto_detect": True, "deadline_day": 5, "x_hour": "18:00", "remind_days": [3, 1, 0],
            "repeat_every": 2, "repeat_max": 3, "channels": ["telegram"], "roles": ["manager"], "contact_email": ""}
    assert client.put("/api/v1/doc-requests/settings", headers=a["hdr"], json={**base, "escalate_days": 31}).status_code == 422
    res = client.put("/api/v1/doc-requests/settings", headers=a["hdr"], json={**base, "escalate_days": 5})
    assert res.status_code == 200, res.text
    overview = client.get("/api/v1/doc-requests", headers=a["hdr"]).json()
    assert overview["settings"]["escalate_days"] == 5
    legacy = client.put("/api/v1/doc-requests/settings", headers=a["hdr"], json=base)   # старый клиент без поля
    assert legacy.status_code == 200
    assert client.get("/api/v1/doc-requests", headers=a["hdr"]).json()["settings"]["escalate_days"] == 3
    assert svc.settings_dict(svc.settings_of(SessionLocal(), "нет-такой"))["escalate_days"] == 3


@pytest.mark.usefixtures("channels")
def test_preview_shows_escalated_count(client, client_firm):
    cid = client_firm["a"]["id"]
    _dr._enable(cid, escalate_days=1, auto_detect=False)
    _age_receipt_request(cid, svc.now_msk(), 3)
    res = client.get(f"/api/v1/doc-requests/preview?period={PERIOD}", headers=client_firm["a"]["hdr"])
    assert res.status_code == 200, res.text
    boss = next(r for r in res.json()["recipients"] if r["recipient"].startswith("Руководитель Тестов"))
    assert boss["escalated"] == 1 and "не прислал(а) документ 3 дн." in boss["text"]
