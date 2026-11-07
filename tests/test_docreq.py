"""«Что нужно от клиента» (T-58, v1.70.97): поиск недостающего, срок сдачи, напоминания, журнал.

Расписание проверяется на фиксированных датах; каналы (Telegram, MAX, почта) подменены — ничего не
уходит наружу. Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import random
from datetime import date, datetime
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.docreq import service as svc
from app.docreq.models import DocRequest, DocRequestEvent, DocRequestSettings
from app.models import Receipt, User
from app.primary.models import PrimaryDocument
from tests import test_primary_upd as _upd
from tests.primary_samples import inn10
from tests.test_v1140 import _mk_user

MSK = svc.MSK
PERIOD = "2026-10"
# фикстура компаний и бухгалтеров — общая с tests/test_primary_upd.py
firms = _upd.firms


def S(**kw) -> DocRequestSettings:
    return DocRequestSettings(company_id="c", **{**svc.DEFAULTS, **kw})


def at(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


# ------------------------------------------------------------------ срок и расписание
def test_deadline_and_schedule():
    s = S()
    assert svc.deadline(PERIOD, s) == at(5, 18)
    assert svc.deadline("2026-12", s) == datetime(2027, 1, 5, 18, 0, tzinfo=MSK)
    slots = svc.schedule(PERIOD, s)
    assert [slot for slot, _ in slots] == ["D-3", "D-1", "D0", "D+2", "D+4", "D+6"]
    assert dict(slots) == {"D-3": at(2, 10), "D-1": at(4, 10), "D0": at(5, 10), "D+2": at(7, 10),
                           "D+4": at(9, 10), "D+6": at(11, 10)}
    # «час икс» раньше 10:00 — в день срока напоминание за час до него
    assert dict(svc.schedule(PERIOD, S(x_hour="09:30")))["D0"] == at(5, 8, 30)
    assert [x for x, _ in svc.schedule(PERIOD, S(remind_days="7", repeat_max=0, deadline_day=10))] == ["D-7"]
    assert svc.period_title(PERIOD) == "октябрь 2026" and svc.human_moment(at(5, 18)) == "5 ноября 2026, 18:00"
    assert svc.default_period(at(4, 12)) == PERIOD and svc.shift_period("2026-01", -1) == "2025-12"
    with pytest.raises(svc.DocRequestError):
        svc.parse_period("2026-13")


def test_due_slot_window_and_no_burst_after_downtime():
    s = S()
    assert svc.due_slot(PERIOD, s, at(2, 9, 59), set()) is None
    assert svc.due_slot(PERIOD, s, at(2, 10, 0), set()) == "D-3"
    assert svc.due_slot(PERIOD, s, at(2, 10, 1), {"D-3"}) is None          # уже отправлено
    assert svc.due_slot(PERIOD, s, at(3, 5, 0), set()) == "D-3"            # окно 20 часов
    assert svc.due_slot(PERIOD, s, at(3, 7, 0), set()) is None             # окно прошло, D-1 ещё не время
    assert svc.due_slot(PERIOD, s, at(4, 15, 0), set()) == "D-1"           # после простоя — только свежее
    assert svc.due_slot(PERIOD, s, at(7, 10, 30), set()) == "D+2"
    assert svc.due_slot(PERIOD, s, at(20, 10, 30), set()) is None          # повторы закончились


# ------------------------------------------------------------------ данные компании
@pytest.fixture
def client_firm(client, firms):
    """Компания A: руководитель с Telegram и MAX, сотрудник с Telegram, его непроверенный чек,
    регулярный поставщик без документа за октябрь и УПД с ошибкой."""
    admin, a, b = firms
    tag = uuid4().hex[:8]
    _mk_user(client, admin, a["id"], f"boss_{tag}", role="manager")
    _mk_user(client, admin, a["id"], f"emp_{tag}", role="user")
    regular, broken = inn10(), inn10()
    with SessionLocal() as db:
        boss = db.query(User).filter_by(username=f"boss_{tag}").one()
        emp = db.query(User).filter_by(username=f"emp_{tag}").one()
        boss.telegram_chat_id, boss.max_user_id, boss.full_name = "1001", "2001", "Руководитель Тестов"
        emp.telegram_chat_id, emp.full_name = "1002", "Сотрудник Тестов"
        rec = Receipt(qr_data="t=synthetic", fn=str(random.randrange(10**15, 10**16)),
                      fd=str(random.randrange(10**6)), fp=str(random.randrange(10**9)), company_id=a["id"],
                      receipt_date=datetime(2026, 10, 10, 12, 0), total_sum=1250.0, status="failed",
                      fns_status="not_found", created_by=emp.id)
        db.add(rec)
        for month, total in ((7, 50000.0), (8, 52000.0), (9, 51000.0)):
            db.add(PrimaryDocument(company_id=a["id"], kind="УПД", function="СЧФДОП", doc_number=f"А-{month}",
                                   doc_date=date(2026, month, 28), seller_name="ООО «Арендодатель»",
                                   seller_inn=regular, total_with_vat=total, status="ok"))
        bad = PrimaryDocument(company_id=a["id"], kind="УПД", function="СЧФДОП", doc_number="С-15",
                              doc_date=date(2026, 10, 12), seller_name="ООО «Стройка»", seller_inn=broken,
                              total_with_vat=7930.0, status="errors",
                              issues_json='[{"level": "error", "code": "inn", "message": "ИНН продавца неверен"}]')
        db.add(bad)
        db.commit()
        ids = {"boss": boss.id, "emp": emp.id, "receipt": rec.id, "bad": bad.id}
    return {"admin": admin, "a": a, "b": b, "regular": regular, "broken": broken, "tag": tag, **ids}


def _requests(company_id: str, period: str = PERIOD) -> dict[str, DocRequest]:
    with SessionLocal() as db:
        rows = db.query(DocRequest).filter_by(company_id=company_id, period=period).all()
        db.expunge_all()
    return {r.kind: r for r in rows}


def _events(company_id: str, **filters) -> list[DocRequestEvent]:
    with SessionLocal() as db:
        rows = (db.query(DocRequestEvent).filter_by(company_id=company_id, **filters)
                .order_by(DocRequestEvent.at, DocRequestEvent.channel, DocRequestEvent.recipient).all())
        db.expunge_all()
    return rows


def test_detect_finds_recurring_supplier_unconfirmed_receipt_and_broken_upd(client_firm):
    cid = client_firm["a"]["id"]
    with SessionLocal() as db:
        assert svc.detect(db, cid, PERIOD) == {"created": 3, "closed": 0}
        assert svc.detect(db, cid, PERIOD) == {"created": 0, "closed": 0}       # без дублей
    reqs = _requests(cid)
    expected, receipt, fix = reqs["expected"], reqs["receipt"], reqs["upd_fix"]
    assert expected.counterparty_inn == client_firm["regular"] and expected.amount == 51000.0
    assert expected.title == "Документ от ООО «Арендодатель» за октябрь 2026 (присылает каждый месяц)"
    assert receipt.assignee_id == client_firm["emp"] and receipt.ref_id == client_firm["receipt"]
    assert receipt.title.startswith("Чек на 1 250,00 ₽ от 10.10.2026 не найден в ФНС")
    assert fix.ref_id == client_firm["bad"] and fix.title.endswith(": ИНН продавца неверен")
    assert all(not r.created_by for r in reqs.values())                          # нашла программа
    created = _events(cid, kind="created")
    assert len(created) == 3 and all(e.actor_id == "" for e in created)
    # отменённое бухгалтером повторный поиск не возвращает
    with SessionLocal() as db:
        svc.set_status(db, db.get(DocRequest, expected.id), "cancelled", None, "договор расторгнут")
        assert svc.detect(db, cid, PERIOD)["created"] == 0
    assert _requests(cid)["expected"].status == "cancelled"


def test_documents_that_arrive_close_requests_themselves(client_firm):
    cid = client_firm["a"]["id"]
    with SessionLocal() as db:
        svc.detect(db, cid, PERIOD)
        db.add(PrimaryDocument(company_id=cid, kind="УПД", function="СЧФДОП", doc_number="А-10",
                               doc_date=date(2026, 10, 31), seller_name="ООО «Арендодатель»",
                               seller_inn=client_firm["regular"], total_with_vat=50500.0, status="ok"))
        db.get(Receipt, client_firm["receipt"]).fns_status = "valid"
        db.add(PrimaryDocument(company_id=cid, kind="УПД", function="СЧФДОП", doc_number="С-15",
                               doc_date=date(2026, 10, 12), correction_number="1", correction_date=date(2026, 11, 2),
                               seller_name="ООО «Стройка»", seller_inn=client_firm["broken"], status="ok"))
        db.commit()
        assert svc.refresh(db, cid, PERIOD) == 3
    reqs = _requests(cid)
    assert {r.status for r in reqs.values()} == {"received"}
    assert reqs["expected"].close_note == "Закрыт сам: получен УПД № А-10 от 31.10.2026"
    assert reqs["receipt"].close_note == "Закрыт сам: чек подтверждён ФНС"
    assert reqs["upd_fix"].close_note == "Закрыт сам: получено исправление № 1 от 02.11.2026"
    assert len(_events(cid, kind="received")) == 3


def _enable(cid: str, **kw) -> None:
    with SessionLocal() as db:
        row = db.get(DocRequestSettings, cid) or DocRequestSettings(company_id=cid)
        for key, value in {**svc.DEFAULTS, "enabled": True, "contact_email": "buh@client.example", **kw}.items():
            setattr(row, key, value)
        db.add(row)
        db.commit()


@pytest.fixture
def channels(monkeypatch):
    """Все каналы «подключены», отправка — в список вместо сети."""
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(svc, "channel_status", lambda _db: {c: {"ready": True, "hint": ""} for c in svc.CHANNELS})
    for name in svc.CHANNELS:
        def fake(_db, target, text, _subject, name=name):
            sent.append((name, target, text))
            return True, ""
        monkeypatch.setitem(svc.SENDERS, name, fake)
    return sent


def test_scheduled_reminder_is_personal_common_logged_and_not_repeated(client_firm, channels):
    cid = client_firm["a"]["id"]
    _enable(cid)
    with SessionLocal() as db:
        reports = svc.tick(db, now=at(4, 10, 5), company_id=cid)
    assert reports == [{"company_id": cid, "period": PERIOD, "slot": "D-1", "sent": 4, "failed": 0, "note": "",
                        "warning": ""}]
    by_target = {(c, t): text for c, t, text in channels}
    assert set(by_target) == {("telegram", "1001"), ("max", "2001"), ("telegram", "1002"),
                              ("email", "buh@client.example")}
    boss_text, emp_text = by_target[("telegram", "1001")], by_target[("telegram", "1002")]
    assert "Срок сдачи — 5 ноября 2026, 18:00 (МСК)." in boss_text
    assert "ООО «Арендодатель»" in boss_text and "ИНН продавца неверен" in boss_text and "Чек на" not in boss_text
    assert "Чек на 1 250,00 ₽" in emp_text and "Арендодатель" not in emp_text      # личное — только автору чека
    assert "обычно около 51 000,00 ₽" in boss_text
    log = _events(cid, kind="reminder", slot="D-1")
    assert {(e.channel, e.ok) for e in log} == {("telegram", True), ("max", True), ("email", True)}
    assert any(e.recipient.startswith("Руководитель Тестов (boss_") and e.text == boss_text for e in log)
    # тот же тик ещё раз — ничего нового
    with SessionLocal() as db:
        assert svc.tick(db, now=at(4, 10, 6), company_id=cid) == []
    assert len(channels) == 4 and len(_events(cid, kind="reminder")) == 4
    # после срока — без упрёков, но с датой
    with SessionLocal() as db:
        svc.tick(db, now=at(7, 10, 1), company_id=cid)
    assert "Срок сдачи прошёл (5 ноября 2026, 18:00, МСК)" in channels[-1][2]


def test_nothing_open_means_no_reminder_but_journal_entry(client_firm, channels):
    cid = client_firm["a"]["id"]
    _enable(cid, auto_detect=False)
    with SessionLocal() as db:
        report = svc.tick(db, now=at(2, 10, 1), company_id=cid)[0]
    assert report["slot"] == "D-3" and report["sent"] == 0 and "Открытых запросов нет" in report["note"]
    assert channels == [] and _events(cid, kind="reminder")[0].text == "Открытых запросов нет — напоминание не нужно"


def test_failed_delivery_and_missing_channels_are_logged(client_firm, channels, monkeypatch):
    cid = client_firm["a"]["id"]
    _enable(cid, contact_email="", channels="telegram")
    monkeypatch.setitem(svc.SENDERS, "telegram", lambda *_: (False, "TimeoutError"))
    with SessionLocal() as db:
        svc.detect(db, cid, PERIOD)
        report = svc.send_reminders(db, cid, PERIOD, "manual", actor_id=client_firm["boss"], now=at(4, 12))
    assert report == {"sent": 0, "failed": 2, "note": "", "warning": ""}
    failed = _events(cid, kind="reminder", slot="manual")
    assert len(failed) == 2 and all(not e.ok and e.text.startswith("Не доставлено (TimeoutError).") for e in failed)
    # ни у кого нет каналов — так и записано
    with SessionLocal() as db:
        db.query(User).filter(User.company_id == cid).update({"telegram_chat_id": None, "max_user_id": None})
        db.commit()
        assert svc.send_reminders(db, cid, PERIOD, "manual", now=at(4, 12))["note"].startswith("Не отправлено")
    assert _events(cid, kind="reminder", slot="manual")[-1].text.startswith("Не отправлено: у получателей нет")


def test_common_requests_nobody_receives_are_flagged(client_firm, channels):
    """Общие запросы без получателя не теряются молча: предупреждение в предпросмотре и запись в журнале."""
    cid = client_firm["a"]["id"]
    _enable(cid, contact_email="")
    with SessionLocal() as db:
        db.query(User).filter(User.id == client_firm["boss"]).update({"telegram_chat_id": None, "max_user_id": None})
        db.commit()
        svc.detect(db, cid, PERIOD)
        pre = svc.preview(db, cid, PERIOD, now=at(4, 12))
        assert pre["warning"].startswith("Общие запросы (2) сейчас некому отправить")
        report = svc.send_reminders(db, cid, PERIOD, "manual", now=at(4, 12))
    assert report["sent"] == 1 and report["warning"] == pre["warning"]          # личный чек сотруднику ушёл
    assert channels == [("telegram", "1002", channels[0][2])]
    flagged = [e for e in _events(cid, kind="reminder", slot="manual") if not e.ok]
    assert len(flagged) == 1 and flagged[0].text == pre["warning"]
    # почта клиента задана — предупреждения нет
    _enable(cid, contact_email="buh@client.example")
    with SessionLocal() as db:
        assert svc.preview(db, cid, PERIOD, now=at(4, 12))["warning"] == ""


def test_tick_skips_disabled_and_archived_companies(client_firm, channels):
    cid = client_firm["a"]["id"]
    _enable(cid, enabled=False)
    with SessionLocal() as db:
        assert svc.tick(db, now=at(4, 10, 5), company_id=cid) == []
    assert channels == []


def test_discipline_share_on_time(client_firm):
    cid = client_firm["a"]["id"]
    with SessionLocal() as db:
        for closed in (datetime(2026, 11, 3, 9, 0), datetime(2026, 11, 9, 9, 0)):   # в срок и после срока
            db.add(DocRequest(company_id=cid, period=PERIOD, kind="manual", title="Акт", status="received",
                              closed_at=closed))
        db.commit()
        assert svc.discipline(db, cid, PERIOD) == {"received": 2, "on_time": 1, "share": 50}


# ------------------------------------------------------------------ API: изоляция, журнал, настройки
def test_api_isolation_between_companies(client, client_firm):
    a, b, admin = client_firm["a"], client_firm["b"], client_firm["admin"]
    base = "/api/v1/doc-requests"
    assert client.get(f"{base}?period={PERIOD}", headers=a["hdr"]).status_code == 200
    assert client.get(f"{base}?company_id={a['id']}&period={PERIOD}", headers=b["hdr"]).status_code == 404
    assert client.get(f"{base}?period={PERIOD}", headers=admin).status_code == 400          # выберите компанию
    detected = client.post(f"{base}/detect", headers=a["hdr"], json={"period": PERIOD})
    assert detected.status_code == 200 and detected.json()["created"] == 3
    rid = detected.json()["overview"]["items"][0]["id"]
    assert client.post(f"{base}/{rid}/status", headers=b["hdr"], json={"status": "cancelled"}).status_code == 404
    assert client.put(f"{base}/settings", headers=b["hdr"], json={"company_id": a["id"]}).status_code == 404
    assert client.get(f"{base}/journal?company_id={a['id']}", headers=b["hdr"]).status_code == 404
    own = client.get(f"{base}?period={PERIOD}", headers=b["hdr"]).json()
    assert own["items"] == [] and own["company"]["id"] == b["id"]
    as_admin = client.get(f"{base}?company_id={a['id']}&period={PERIOD}", headers=admin).json()
    assert len(as_admin["items"]) == 3 and as_admin["summary"]["open"] == 3


def test_api_manual_request_status_and_journal(client, client_firm):
    a = client_firm["a"]
    base = "/api/v1/doc-requests"
    res = client.post(base, headers=a["hdr"], json={"period": PERIOD, "title": "  Акт сверки  с ООО «Песок» ",
                                                    "counterparty_inn": "50 01007329", "amount": 1500})
    assert res.status_code == 200, res.text
    req = res.json()
    assert req["title"] == "Акт сверки с ООО «Песок»" and req["counterparty_inn"] == "5001007329"
    assert req["kind"] == "manual" and req["auto"] is False
    assert client.post(base, headers=a["hdr"], json={"period": PERIOD, "title": "x"}).status_code == 422
    done = client.post(f"{base}/{req['id']}/status", headers=a["hdr"], json={"status": "received", "note": "по почте"})
    assert done.json()["status"] == "received" and done.json()["close_note"] == "по почте"
    client.post(f"{base}/{req['id']}/status", headers=a["hdr"], json={"status": "open"})
    journal = client.get(f"{base}/journal?period={PERIOD}", headers=a["hdr"]).json()["items"]
    kinds = [e["kind"] for e in journal if e["request_id"] == req["id"]]
    assert kinds == ["reopened", "received", "created"]                    # новые сверху
    assert all(e["actor"] for e in journal if e["request_id"] == req["id"])


def test_api_settings_validation_and_overview(client, client_firm):
    a = client_firm["a"]
    base = "/api/v1/doc-requests"
    for bad in ({"deadline_day": 29}, {"x_hour": "25:00"}, {"repeat_every": 14, "repeat_max": 3},
                {"contact_email": "не почта"}, {"remind_days": [11]}, {"channels": ["sms"]}):
        assert client.put(f"{base}/settings", headers=a["hdr"], json=bad).status_code == 422, bad
    good = {"enabled": True, "auto_detect": False, "deadline_day": 7, "x_hour": "12:00", "remind_days": [0, 2],
            "repeat_every": 3, "repeat_max": 2, "channels": ["telegram", "email"], "roles": ["manager", "user"],
            "contact_email": "buh@client.example"}
    saved = client.put(f"{base}/settings", headers=a["hdr"], json=good)
    assert saved.status_code == 200, saved.text
    s = saved.json()["settings"]
    assert (s["deadline_day"], s["x_hour"], s["remind_days"], s["channels"], s["auto_detect"]) == \
        (7, "12:00", [2, 0], ["telegram", "email"], False)
    view = client.get(f"{base}?period={PERIOD}", headers=a["hdr"]).json()
    assert view["deadline_human"] == "7 ноября 2026, 12:00" and [x["slot"] for x in view["schedule"]] == \
        ["D-2", "D0", "D+3", "D+6"]
    assert set(view["channels"]) == {"telegram", "max", "email"}


def test_api_remind_now_and_preview(client, client_firm, channels):
    a = client_firm["a"]
    base = "/api/v1/doc-requests"
    client.post(f"{base}/detect", headers=a["hdr"], json={"period": PERIOD})
    preview = client.get(f"{base}/preview?period={PERIOD}", headers=a["hdr"]).json()
    who = {p["recipient"].split(" (")[0]: p for p in preview["recipients"]}
    assert who["Руководитель Тестов"]["items"] == 2 and who["Сотрудник Тестов"]["items"] == 1
    assert channels == []                                                   # предпросмотр ничего не отправляет
    report = client.post(f"{base}/remind", headers=a["hdr"], json={"period": PERIOD}).json()
    assert report["sent"] == 3 and report["failed"] == 0                    # почта компании не задана
    log = client.get(f"{base}/journal?period={PERIOD}", headers=a["hdr"]).json()["items"]
    assert {e["slot"] for e in log if e["kind"] == "reminder"} == {"manual"}


def test_audit_phrases():
    from app.services.audit_human import humanize_audit
    assert humanize_audit("docreq_settings_updated", {"company": "Ромашка", "enabled": True, "deadline_day": 5,
                                                      "x_hour": "18:00"}) == (
        "изменены срок сдачи и напоминания клиенту", "компания: „Ромашка“, срок: 5-е число 18:00, напоминания включены")
    assert humanize_audit("docreq_reminded", {"company": "Ромашка", "sent": 3, "failed": 1})[1] == \
        "компания: „Ромашка“, отправлено: 3, не доставлено: 1"
