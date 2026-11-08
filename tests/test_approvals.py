"""Согласование авансовых отчётов (T-46, v1.70.112, ADR-24, ADR-42): маршрут, права, пороги, 72 часа, журнал.

Маршрут сотрудник → руководитель → бухгалтер проверяется через API (права и коды ответов) и через сервис с
подменённым временем (срок 72 часа, пороги). Каналы Telegram и MAX подменены — наружу ничего не уходит.
Файл входит в прогон на PostgreSQL (qa/pg_local.py). Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.approvals import service as svc
from app.approvals.models import AdvanceApproval, AdvanceApprovalEvent, ApprovalSettings
from app.auth import create_access_token, hash_password
from app.docreq import service as docreq
from app.models import Company, Receipt, User

T0 = datetime(2026, 11, 2, 9, 0)            # наивное UTC, как в базе
PASSWORD_HASH = hash_password("parol123")


def _user(db, company_id: str | None, role: str, name: str, **kw) -> User:
    user = User(username=f"ap_{role}_{uuid4().hex[:10]}", full_name=name, password_hash=PASSWORD_HASH, role=role,
                company_id=company_id, **kw)
    db.add(user)
    db.flush()
    return user


def _hdr(user: User) -> dict:
    return {"Authorization": "Bearer " + create_access_token(user)}


def _receipt(db, company_id: str, assignee: str, day: int, total: float, *, fns: str = "valid",
             personal: float = 0, operation: int = 1, flags: str = "") -> Receipt:
    fn, fd = f"99{uuid4().int % 10**14:014d}", str(uuid4().int % 10**6)
    r = Receipt(qr_data=f"t=202610{day:02d}T1200&s={total:.2f}&fn={fn}&i={fd}&fp=1&n=1", fn=fn, fd=fd, fp="1",
                receipt_date=datetime(2026, 10, day, 12, 0), total_sum=total, operation=operation,
                company_id=company_id, assignee=assignee, assignee_lc=assignee.casefold()[:200],
                merchant_name=f"Стройбаза {day}", merchant_inn="4703148343", fns_status=fns, status="verified",
                personal_sum=personal, policy_flags=flags, category="Материалы",
                # дата добавления — в прошлом: чеки этих тестов не попадают в «последние сутки» чужих проверок
                # (дайджест MAX в tests/test_v1660.py берёт 12 сотрудников с наибольшей суммой за сутки)
                created_at=datetime(2026, 10, day, 12, 30))
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def team(client, store):
    """Две компании. В A: сотрудник (два своих чека за октябрь, один с личной покупкой, и чужой чек), руководитель
    с Telegram и MAX, бухгалтер; в B — свои сотрудник, руководитель и бухгалтер. Согласование включено только в A."""
    tag = uuid4().hex[:6]
    out = {"store": store}
    with store.session() as db:
        # администратор в программе один (tests/test_roles.py проверяет это на общей базе): в SQLite — прежний
        # «admin», второго не заводим; в одноразовой схеме PostgreSQL — свой, как в pg_backend
        if store.name == "SQLite":
            admin = db.query(User).filter(User.username == "admin", User.role == "admin").one()
        else:
            admin = _user(db, None, "admin", "Администратор Тестов")
        out["admin"] = _hdr(admin)
        for letter in "AB":
            company = Company(name=f"Согласование {letter} {tag}")
            db.add(company)
            db.flush()
            emp = _user(db, company.id, "user", f"Сотрудник {letter} {tag}", telegram_chat_id=f"70{tag}{letter}")
            boss = _user(db, company.id, "manager", f"Руководитель {letter} {tag}",
                         telegram_chat_id=f"71{tag}{letter}", max_user_id=f"81{tag}{letter}")
            acc = _user(db, company.id, "accountant", f"Бухгалтер {letter} {tag}")
            out[letter] = {"id": company.id, "emp": _hdr(emp), "boss": _hdr(boss), "acc": _hdr(acc),
                           "emp_id": emp.id, "boss_id": boss.id, "acc_id": acc.id, "emp_name": emp.full_name,
                           "boss_tg": boss.telegram_chat_id, "boss_max": boss.max_user_id,
                           "emp_tg": emp.telegram_chat_id}
        a = out["A"]
        _receipt(db, a["id"], a["emp_name"], 5, 1200.0)
        _receipt(db, a["id"], a["emp_name"], 7, 800.0, personal=100.0)
        _receipt(db, a["id"], "Чужой Сотрудник", 8, 5000.0)
        db.add(ApprovalSettings(company_id=a["id"], enabled=True))
        db.commit()
    out["emp_id"], out["emp_name"], out["boss_id"] = out["A"]["emp_id"], out["A"]["emp_name"], out["A"]["boss_id"]
    company_ids = [out["A"]["id"], out["B"]["id"]]
    yield out
    client.cookies.clear()
    # v1.71.0: чеки согласования оставались в общей тестовой базе и мешали
    # фильтрам выпуска 1.70.72 (диапазон 5000–6000). Фикстура убирает только
    # свои компании, пользователей, настройки, отчёты и чеки — чужие тесты не трогает.
    with store.session() as db:
        for model in (AdvanceApprovalEvent, AdvanceApproval, ApprovalSettings, Receipt, User):
            db.query(model).filter(model.company_id.in_(company_ids)).delete(
                synchronize_session=False)
        db.query(Company).filter(Company.id.in_(company_ids)).delete(
            synchronize_session=False)
        db.commit()


def _create(client, hdr, **kw):
    body = {"date_from": "2026-10-01", "date_to": "2026-10-31", **kw}
    return client.post("/api/v1/approvals", headers=hdr, json=body)


def _decide(client, hdr, d, decision="approve", comment=""):
    return client.post(f"/api/v1/approvals/{d['id']}/decision", headers=hdr,
                       json={"decision": decision, "doc_hash": d["doc_hash"], "confirm": True, "comment": comment})


# ------------------------------------------------------------------ маршрут
def test_full_route_with_pep_journal(client, team):
    a = team["A"]
    res = _create(client, a["emp"], purpose="Материалы для объекта")
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["status"] == "draft" and d["stage"] == "employee" and d["receipts_count"] == 2
    assert d["total"] == 1900.0                                          # 1200 + (800 − 100 личных)
    assert re.fullmatch(r"АО-\d{4}-\d{2}-\d{2,}", d["number"]) and d["can"]["sign"] and not d["can"]["decide"]
    assert d["integrity"]["content"] and d["integrity"]["journal"]["ok"]
    # подпись без подтверждения или с чужим хешем не проходит
    assert client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["emp"],
                       json={"doc_hash": d["doc_hash"], "confirm": False}).status_code == 422
    bad = client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["emp"],
                      json={"doc_hash": "0" * 64, "confirm": True})
    assert bad.status_code == 409 and "изменился" in bad.json()["detail"]
    d = client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["emp"],
                    json={"doc_hash": d["doc_hash"], "confirm": True}).json()
    assert d["status"] == "pending" and d["stage"] == "manager" and d["minutes_left"] in (72 * 60 - 1, 72 * 60)
    d = _decide(client, a["boss"], d).json()
    assert d["stage"] == "accountant"
    d = _decide(client, a["acc"], d, comment="Проверено").json()
    assert d["status"] == "approved" and d["stage"] == ""
    actions = [(e["stage"], e["action"], e["pep"]) for e in d["events"]]
    assert actions == [("employee", "created", False), ("employee", "signed", True), ("manager", "approved", True),
                       ("accountant", "approved", True), ("", "completed", False)]
    signed = d["events"][1]
    assert signed["actor_name"] == team["emp_name"] and signed["doc_hash"] == d["doc_hash"] and signed["ip"]
    assert d["integrity"]["journal"] == {"ok": True, "broken_at": 0}


def test_rights_and_isolation(client, team):
    a, b = team["A"], team["B"]
    d = _create(client, a["emp"]).json()
    # чужая компания не видит отчёт и не решает по нему
    for hdr in (b["emp"], b["boss"], b["acc"]):
        assert client.get(f"/api/v1/approvals/{d['id']}", headers=hdr).status_code == 404
        assert _decide(client, hdr, d).status_code == 404
    # подписывает только сотрудник; решать на этапе сотрудника некому
    assert client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["boss"],
                       json={"doc_hash": d["doc_hash"], "confirm": True}).status_code == 403
    assert _decide(client, a["boss"], d).status_code == 409
    d = client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["emp"],
                    json={"doc_hash": d["doc_hash"], "confirm": True}).json()
    # этап руководителя: бухгалтер и сам сотрудник не решают
    assert _decide(client, a["acc"], d).status_code == 403
    assert _decide(client, a["emp"], d).status_code == 403
    d = _decide(client, a["boss"], d).json()
    assert _decide(client, a["boss"], d).status_code == 403                # этап бухгалтера — не руководителю
    # сотрудник не собирает отчёт за другого; настройки меняет только администратор
    assert _create(client, a["emp"], employee_id=team["boss_id"]).status_code == 403
    body = {"company_id": a["id"], "enabled": False, "timeout_hours": 72, "manager_limit": 0, "accountant_limit": 0,
            "channels": []}
    for hdr in (a["emp"], a["boss"], a["acc"]):
        assert client.put("/api/v1/approvals/settings", headers=hdr, json=body).status_code == 403
    assert client.put("/api/v1/approvals/settings", headers=team["admin"], json=body).status_code == 200
    # выключено — новый отчёт не создать, но начатые доводятся до конца
    assert _create(client, a["emp"]).status_code == 409
    assert _decide(client, a["acc"], d).json()["status"] == "approved"
    # списки: «Ждут меня» у бухгалтера B пуст, «Все» у руководителя A — с отчётом
    assert client.get("/api/v1/approvals?box=todo", headers=b["acc"]).json()["items"] == []
    assert [x["id"] for x in client.get("/api/v1/approvals?box=all", headers=a["boss"]).json()["items"]] == [d["id"]]


def test_disabled_by_default(client, team):
    b = team["B"]
    res = client.get("/api/v1/approvals/settings", headers=b["acc"]).json()
    assert res["settings"]["enabled"] is False and res["settings"]["timeout_hours"] == 72 and res["can_edit"] is False
    assert _create(client, b["emp"]).status_code == 409
    me = client.get("/api/v1/auth/me", headers=team["A"]["emp"]).json()
    assert me["approvals_enabled"] is True
    assert client.get("/api/v1/auth/me", headers=b["emp"]).json()["approvals_enabled"] is False


def test_reject_needs_reason_and_closes(client, team):
    a = team["A"]
    d = _create(client, a["emp"]).json()
    d = client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["emp"],
                    json={"doc_hash": d["doc_hash"], "confirm": True}).json()
    assert _decide(client, a["boss"], d, "reject").status_code == 422
    d = _decide(client, a["boss"], d, "reject", "Нет чека на доставку").json()
    assert d["status"] == "rejected" and d["can"]["resubmit"] is False      # руководитель не автор
    assert _decide(client, a["acc"], d).status_code == 409
    again = client.get(f"/api/v1/approvals/{d['id']}", headers=a["emp"]).json()
    assert again["can"]["resubmit"] is True
    second = _create(client, a["emp"], previous_id=d["id"]).json()
    assert second["number"] != d["number"] and second["status"] == "draft"


def test_cancel_rules(client, team):
    a = team["A"]
    d = _create(client, a["emp"]).json()
    assert client.post(f"/api/v1/approvals/{d['id']}/cancel", headers=a["boss"], json={}).status_code == 403
    d = client.post(f"/api/v1/approvals/{d['id']}/cancel", headers=a["emp"], json={"comment": "ошибся периодом"}).json()
    assert d["status"] == "cancelled" and d["events"][-1]["action"] == "cancelled" and d["events"][-1]["pep"]
    assert client.post(f"/api/v1/approvals/{d['id']}/cancel", headers=a["emp"], json={}).status_code == 409


# ------------------------------------------------------------------ сервис: время, пороги, пропуск этапа
def _svc_create(db, team, *, actor_id=None, employee_id=None):
    company = db.get(Company, team["A"]["id"])
    employee = db.get(User, employee_id or team["emp_id"])
    actor = db.get(User, actor_id or employee.id)
    return svc.create(db, actor, company, employee, "2026-10-01", "2026-10-31", now=T0)


def test_timeout_72h_is_separate_event(team):
    with team["store"].session() as db:
        ap = _svc_create(db, team)
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        assert ap.stage == "manager" and ap.deadline_at == T0 + timedelta(hours=72)
        cid = team["A"]["id"]
        assert svc.expire(db, T0 + timedelta(hours=71, minutes=59), cid) == 0
        assert svc.expire(db, T0 + timedelta(hours=72), cid) == 1
        db.refresh(ap)
        assert ap.stage == "accountant" and ap.deadline_at == T0 + timedelta(hours=144)
        assert svc.expire(db, T0 + timedelta(hours=144), cid) == 1
        db.refresh(ap)
        assert ap.status == "approved"
        events = svc.events_of(db, ap.id)
        auto = [e for e in events if e.action == "auto_approved"]
        assert [(e.stage, e.actor_id) for e in auto] == [("manager", ""), ("accountant", "")]
        assert all("72 ч" in e.comment for e in auto) and svc.verify(events)["ok"]


def test_thresholds(team):
    with team["store"].session() as db:
        s = db.get(ApprovalSettings, team["A"]["id"])
        s.manager_limit, s.accountant_limit = 2000.0, 2000.0
        db.commit()
        ap = _svc_create(db, team)
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        db.refresh(ap)
        actions = [(e.stage, e.action) for e in svc.events_of(db, ap.id)]
        assert ("manager", "threshold_approved") in actions and ("accountant", "threshold_approved") in actions
        assert ap.status == "approved"
        # чек не подтверждён ФНС — порог бухгалтера не срабатывает, порог руководителя — да
        _receipt(db, team["A"]["id"], team["emp_name"], 9, 50.0, fns="not_found")
        db.commit()
        ap2 = _svc_create(db, team)
        svc.sign(db, ap2, db.get(User, team["emp_id"]), ap2.doc_hash, now=T0)
        db.refresh(ap2)
        assert ap2.status == "pending" and ap2.stage == "accountant"
        # выше порога руководителя — решает руководитель
        s.manager_limit = 100.0
        db.commit()
        ap3 = _svc_create(db, team)
        svc.sign(db, ap3, db.get(User, team["emp_id"]), ap3.doc_hash, now=T0)
        db.refresh(ap3)
        assert ap3.stage == "manager"


def test_manager_own_report_skips_manager_stage(client, team):
    a = team["A"]
    with team["store"].session() as db:
        boss = db.get(User, team["boss_id"])
        _receipt(db, a["id"], boss.full_name, 10, 300.0)
        db.commit()
    d = _create(client, a["boss"]).json()
    d = client.post(f"/api/v1/approvals/{d['id']}/sign", headers=a["boss"],
                    json={"doc_hash": d["doc_hash"], "confirm": True}).json()
    assert d["stage"] == "accountant"
    assert [e["action"] for e in d["events"]][-1] == "skipped"
    assert "нет руководителя" in d["events"][-1]["comment"]


def test_concurrent_decisions_second_gets_conflict(team):
    with team["store"].session() as db:
        ap = _svc_create(db, team)
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        ap_id, doc_hash = ap.id, ap.doc_hash
    first, second = team["store"].session(), team["store"].session()
    try:
        a1, a2 = first.get(AdvanceApproval, ap_id), second.get(AdvanceApproval, ap_id)
        boss1, boss2 = first.get(User, team["boss_id"]), second.get(User, team["boss_id"])
        svc.decide(first, a1, boss1, "approve", doc_hash)
        from sqlalchemy.orm.exc import StaleDataError
        with pytest.raises(StaleDataError):
            svc.decide(second, a2, boss2, "approve", doc_hash)
        second.rollback()
    finally:
        first.close()
        second.close()
    with team["store"].session() as db:
        approved = [e for e in svc.events_of(db, ap_id) if e.action == "approved"]
        assert len(approved) == 1


def test_notifications_once_per_stage(team, monkeypatch):
    sent = []
    monkeypatch.setitem(svc.SENDERS, "telegram", lambda db, target, text, subject: sent.append(("tg", target, text))
                        or (True, ""))
    monkeypatch.setitem(svc.SENDERS, "max", lambda db, target, text, subject: sent.append(("max", target, text))
                        or (True, ""))
    monkeypatch.setattr(docreq, "channel_status", lambda db: {"telegram": {"ready": True, "hint": ""},
                                                              "max": {"ready": True, "hint": ""},
                                                              "email": {"ready": False, "hint": ""}})
    with team["store"].session() as db:
        ap = _svc_create(db, team)
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        svc.notify(db, T0, team["A"]["id"])
        a = team["A"]
        assert sorted((ch, target) for ch, target, _ in sent) == [("max", a["boss_max"]), ("tg", a["boss_tg"])]
        assert "ждёт вашего согласования" in sent[0][2].lower() and ap.number in sent[0][2]
        svc.notify(db, T0, team["A"]["id"])
        assert len(sent) == 2                                              # повтор не уходит
        notified = [e for e in svc.events_of(db, ap.id) if e.action == "notified"]
        assert len(notified) == 1 and "Telegram" in notified[0].comment and "MAX" in notified[0].comment
        db.refresh(ap)
        svc.decide(db, ap, db.get(User, team["boss_id"]), "reject", ap.doc_hash, "Нет акта")
        sent.clear()
        svc.notify(db, T0, team["A"]["id"])
        assert [(ch, target) for ch, target, _ in sent] == [("tg", a["emp_tg"])] and "Нет акта" in sent[0][2]


def test_journal_tamper_is_detected(team):
    with team["store"].session() as db:
        ap = _svc_create(db, team)
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        assert svc.verify(svc.events_of(db, ap.id))["ok"]
        signed = db.query(AdvanceApprovalEvent).filter_by(approval_id=ap.id, action="signed").one()
        signed.actor_name = "Подменённое Имя"
        signed.comment = "правка задним числом"
        db.commit()
        assert svc.verify(svc.events_of(db, ap.id)) == {"ok": False, "broken_at": signed.seq}


def test_changes_after_snapshot_block_accountant_threshold(team):
    with team["store"].session() as db:
        s = db.get(ApprovalSettings, team["A"]["id"])
        s.accountant_limit = 100000.0
        db.commit()
        ap = _svc_create(db, team)
        item = svc.snapshot_of(ap)["items"][0]
        db.get(Receipt, item["id"]).total_sum = 1500.0
        db.commit()
        assert [c["kind"] for c in svc.changes(db, ap)] == ["changed"]
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        db.refresh(ap)
        assert ap.stage == "manager"
        svc.decide(db, ap, db.get(User, team["boss_id"]), "approve", ap.doc_hash)
        db.refresh(ap)
        assert ap.status == "pending" and ap.stage == "accountant"       # правка после формирования — не автомат


def test_number_shares_ao1_counter(client, team):
    a = team["A"]
    d = _create(client, a["emp"]).json()
    seq = int(d["number"].rsplit("-", 1)[1])
    res = client.post("/api/v1/receipts/advance-report/number", headers=a["acc"],
                      json={"company_id": a["id"], "report_date": d["number"][3:10] + "-15",
                            "request_id": str(uuid4())})
    assert res.status_code == 200, res.text
    assert res.json()["sequence"] == seq + 1


def test_tick_and_decision_in_same_second_do_not_fork_journal(team, monkeypatch):
    """Руководитель открыл отчёт, тик в эту секунду записал уведомление — решение по устаревшей версии получает
    конфликт (в API — 409), журнал остаётся одной цепочкой; повторное решение проходит."""
    monkeypatch.setitem(svc.SENDERS, "telegram", lambda *_a: (True, ""))
    monkeypatch.setitem(svc.SENDERS, "max", lambda *_a: (True, ""))
    monkeypatch.setattr(docreq, "channel_status", lambda _db: {"telegram": {"ready": True, "hint": ""},
                                                               "max": {"ready": True, "hint": ""},
                                                               "email": {"ready": False, "hint": ""}})
    with team["store"].session() as db:
        ap = _svc_create(db, team)
        svc.sign(db, ap, db.get(User, team["emp_id"]), ap.doc_hash, now=T0)
        ap_id, doc_hash = ap.id, ap.doc_hash
    boss_db, tick_db = team["store"].session(), team["store"].session()
    try:
        stale = boss_db.get(AdvanceApproval, ap_id)
        boss = boss_db.get(User, team["boss_id"])
        assert svc.notify(tick_db, T0, team["A"]["id"]) == 1
        with pytest.raises(StaleDataError):
            svc.decide(boss_db, stale, boss, "approve", doc_hash)
        boss_db.rollback()
        fresh = boss_db.get(AdvanceApproval, ap_id)
        boss_db.refresh(fresh)
        svc.decide(boss_db, fresh, boss, "approve", doc_hash)
    finally:
        boss_db.close()
        tick_db.close()
    with team["store"].session() as db:
        events = svc.events_of(db, ap_id)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert [e.action for e in events][-2:] == ["notified", "approved"] and svc.verify(events)["ok"]


def test_journal_number_is_unique(team):
    with team["store"].session() as db:
        ap = _svc_create(db, team)
        db.add(AdvanceApprovalEvent(approval_id=ap.id, company_id=ap.company_id, seq=1, action="created"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
