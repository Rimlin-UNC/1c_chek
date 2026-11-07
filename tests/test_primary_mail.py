"""Первичка из почтового ящика компании (T-63, v1.70.104, ADR-34).

Почтовый сервер имитирует поддельный клиент FakeImap с поведением imaplib: EXAMINE (select только на чтение),
UID SEARCH, UID FETCH с RFC822.SIZE и BODY.PEEK[]. В сеть тесты не ходят; письма, ИНН, адреса и документы
синтетические (ADR-14). Проверяется: только чтение, XML и ZIP — в реестр, PDF — черновик на сверку, печатная форма
уже пришедшего XML — дубль, повторный файл узнаётся по хешу, фильтр отправителей, смена UIDVALIDITY, пределы,
ошибки входа, изоляция компаний, подтверждение и отказ, срок хранения файла, защита от SSRF.
"""
from __future__ import annotations

import imaplib
import json
import socket
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import utcnow
from app.primary import mailbox, service
from app.primary.models import PrimaryDocument, PrimaryMailbox, PrimaryMailFile, PrimaryMailItem
from app.services import secretbox
from tests.primary_samples import SELLER_INN, upd_xml
from tests.test_primary_upd import firms  # noqa: F401 — фикстура компаний и бухгалтеров
from tests.test_v1140 import _mk_user

PDF = Path("tests/fixtures/primary/upd_print_text.pdf").read_bytes()     # УТ-215 от 14.10.2026, продавец 5001007329
SUPPLIER = "docs@stroybaza-synthetic.ru"


class FakeImap:
    """Почтовый сервер в памяти: UID → письмо. Запоминает команды, чтобы проверить «только чтение»."""

    def __init__(self, messages: dict[int, bytes] | None = None, uidvalidity: str = "77", fail_login: bool = False):
        self.messages = dict(messages or {})
        self.uidvalidity = uidvalidity
        self.fail_login = fail_login
        self.calls: list[tuple] = []

    def login(self, user, password):
        self.calls.append(("LOGIN", user))
        if self.fail_login:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def select(self, mailbox_name, readonly=False):
        self.calls.append(("SELECT", mailbox_name, readonly))
        if mailbox_name not in ('"INBOX"', '"&BBQEPgQ6BEMEPAQ1BD0EQgRL-"'):
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.uidvalidity.encode()]

    def uid(self, command, *args):
        self.calls.append(("UID", command, *[a for a in args if a is not None]))
        crit = [a for a in args if a is not None]
        if command == "SEARCH":
            if crit[0] == "UID":
                low = int(crit[1].split(":")[0])
                found = [u for u in sorted(self.messages) if u >= low]
                if self.messages and max(self.messages) not in found:      # «N:*» всегда отдаёт последнее письмо
                    found.append(max(self.messages))
            else:
                assert crit[0] == "SINCE" and len(crit[1].split("-")) == 3
                found = sorted(self.messages)
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            uid, what = int(crit[0]), crit[1]
            raw = self.messages[uid]
            if what == "(RFC822.SIZE)":
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            assert what == "(BODY.PEEK[])", "письмо забирается только через PEEK — остаётся непрочитанным"
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(f"команда {command} изменила бы ящик")

    def logout(self):
        self.calls.append(("LOGOUT",))


def letter(*attachments: tuple[str, bytes], sender: str = SUPPLIER, subject: str = "Документы по заказу") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"СтройБаза <{sender}>"
    msg["To"] = "buh@client-synthetic.ru"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 04 Nov 2026 10:15:00 +0300"
    msg["Message-ID"] = f"<{uuid4().hex}@synthetic>"
    msg.set_content("Добрый день! Во вложении документы.")
    for name, data in attachments:
        main, sub = ("application", "octet-stream")
        if name.endswith(".pdf"):
            main, sub = "application", "pdf"
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


def xml_for(firm, **kw) -> bytes:
    return upd_xml(buyer=("ООО «Покупатель»", firm["inn"], "770401001"), number=kw.pop("number", f"М-{uuid4().hex[:6]}"),
                   **kw)


@pytest.fixture
def imap(monkeypatch):
    fake = FakeImap()

    def fake_open(box, password):
        assert password == "app-password-synthetic" or password == "wrong", "пароль расшифровывается перед входом"
        if fake.fail_login:
            raise mailbox.MailboxError("Почтовый сервер не принял логин или пароль", 424)
        fake.login(box.username, password)
        return fake

    monkeypatch.setattr(mailbox, "_open", fake_open)
    return fake


def connect(client, hdr, company_id=None, **kw) -> dict:
    body = {"host": "imap.yandex.ru", "port": 993, "security": "ssl", "username": "docs@client-synthetic.ru",
            "password": "app-password-synthetic", "folder": "INBOX", "senders": "", "start_date": None, **kw}
    if company_id:
        body["company_id"] = company_id
    res = client.put("/api/v1/primary/mailbox", headers=hdr, json=body)
    assert res.status_code == 200, res.text
    res = client.post("/api/v1/primary/mailbox/check", headers=hdr, json={"company_id": company_id} if company_id else {})
    assert res.status_code == 200, res.text
    return res.json()


def sync(client, hdr) -> dict:
    res = client.post("/api/v1/primary/mailbox/sync", headers=hdr, json={})
    assert res.status_code == 200, res.text
    return res.json()["result"]


# ------------------------------------------------------------------ чистые функции
def test_folder_names_dates_and_senders():
    assert mailbox.imap_utf7("INBOX") == "INBOX" and mailbox.imap_utf7("Входящие") == "&BBIERQQ+BDQETwRJBDgENQ-"
    assert mailbox.imap_utf7("A&B") == "A&-B"
    assert mailbox.imap_date(date(2026, 3, 5)) == "05-Mar-2026"       # не strftime: месяц не зависит от локали
    assert mailbox.parse_senders(" A@B.ru; @c.ru d.ru,a@b.ru ") == ["a@b.ru", "@c.ru", "@d.ru"]
    assert mailbox.parse_senders("") == []
    with pytest.raises(mailbox.MailboxError):
        mailbox.parse_senders("не адрес")
    allowed = ["@c.ru", "x@y.ru"]
    assert mailbox.sender_allowed("buh@c.ru", allowed) and mailbox.sender_allowed("a@sub.c.ru", allowed)
    assert not mailbox.sender_allowed("a@evilc.ru", allowed) and not mailbox.sender_allowed("", allowed)
    assert mailbox.sender_allowed("X@Y.RU", allowed) and mailbox.sender_allowed("кто-угодно@где.рф", [])
    assert mailbox.clean_host(" IMAP.Yandex.RU. ") == "imap.yandex.ru"
    for bad in ("localhost", "a b.ru", "imap.yandex.ru:993", "", None, "-x.ru"):
        assert mailbox.clean_host(bad) == "", bad


def test_attachment_kinds_and_message_parsing():
    xml = upd_xml()
    assert mailbox.attachment_kind("upd.xml", xml) == "xml"
    assert mailbox.attachment_kind("без расширения", b"\xef\xbb\xbf<?xml version='1.0'?><a/>") == "xml"
    assert mailbox.attachment_kind("pack.zip", b"PK\x03\x04...") == "zip"
    assert mailbox.attachment_kind("Счёт.docx", b"PK\x03\x04...") == ""                # Word — тоже ZIP, но не наш
    assert mailbox.attachment_kind("upd.pdf", PDF) == "pdf"
    assert mailbox.attachment_kind("photo.jpg", b"\xff\xd8\xff" + b"0" * 10) == "image"
    assert mailbox.attachment_kind("upd.xml.sig", b"0\x82\x05") == ""
    assert mailbox.media_type(PDF) == "application/pdf" and mailbox.media_type(b"\x89PNG....") == "image/png"
    inner = EmailMessage()
    inner["From"] = "x@y.ru"
    inner["Subject"] = "переслано"
    inner.set_content("текст")
    inner.add_attachment(xml, maintype="application", subtype="xml", filename="УПД № 15.xml")
    outer = EmailMessage()
    outer["From"] = "Бухгалтер <Buh@Client.RU>"
    outer["Subject"] = "Fwd: документы"
    outer["Date"] = "Wed, 04 Nov 2026 10:15:00 +0300"
    outer.set_content("пересылаю")
    outer.add_related(b"\x89PNG" + b"0" * 30000, maintype="image", subtype="png", cid="<logo>",
                      filename="logo.png", disposition="inline")
    outer.add_attachment(inner)
    parsed = mailbox.parse_message(outer.as_bytes())
    assert parsed["sender"] == "buh@client.ru" and parsed["subject"] == "Fwd: документы"
    assert parsed["received_at"].isoformat() == "2026-11-04T07:15:00"
    assert [n for n, _ in parsed["attachments"]] == ["УПД № 15.xml"]      # логотип из текста письма пропущен
    assert parsed["attachments"][0][1] == xml


def test_private_addresses_are_refused(monkeypatch):
    monkeypatch.delenv("YM_MAIL_PRIVATE_HOSTS", raising=False)

    def resolver(ip):
        return lambda host, port, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver("10.0.0.5"))
    with pytest.raises(mailbox.MailboxError, match="внутренней сети"):
        mailbox.resolve_public("mail.corp.local", 993)
    monkeypatch.setattr(socket, "getaddrinfo", resolver("127.0.0.1"))
    with pytest.raises(mailbox.MailboxError):
        mailbox.resolve_public("imap.example.ru", 993)
    monkeypatch.setattr(socket, "getaddrinfo", resolver("77.88.21.125"))
    assert mailbox.resolve_public("imap.yandex.ru", 993) == "77.88.21.125"
    monkeypatch.setenv("YM_MAIL_PRIVATE_HOSTS", "1")                 # свой сервер в локальной сети — явно разрешён
    monkeypatch.setattr(socket, "getaddrinfo", resolver("10.0.0.5"))
    assert mailbox.resolve_public("mail.corp.local", 993) == "10.0.0.5"


# ------------------------------------------------------------------ подключение и права
def test_save_check_state_and_password_never_returned(client, firms, imap):  # noqa: F811
    _admin, a, _b = firms
    state = client.get("/api/v1/primary/mailbox", headers=a["hdr"]).json()
    assert state["configured"] is False and state["status_title"] == "не подключён"
    res = client.put("/api/v1/primary/mailbox", headers=a["hdr"], json={"host": "imap.yandex.ru", "username": "x@y.ru"})
    assert res.status_code == 400 and "пароль приложения" in res.json()["detail"]
    res = client.put("/api/v1/primary/mailbox", headers=a["hdr"], json={"host": "imap.yandex.ru", "port": 25,
                                                                        "username": "x@y.ru", "password": "p"})
    assert res.status_code == 400 and "993" in res.json()["detail"]
    out = connect(client, a["hdr"])
    box = out["mailbox"]
    assert out["result"] == {"ok": True, "messages_in_folder": 0}
    assert box["status"] == "active" and box["password_set"] is True and "password" not in box
    assert "app-password-synthetic" not in json.dumps(box)
    assert ("SELECT", '"INBOX"', True) in imap.calls                 # папка открыта только на чтение
    with SessionLocal() as db:
        row = db.query(PrimaryMailbox).filter(PrimaryMailbox.company_id == a["id"]).one()
        assert secretbox.is_sealed(row.password_enc) and "app-password" not in row.password_enc
        assert row.start_date == mailbox.today_msk() - timedelta(days=30)
    # без пароля — сохранённый остаётся; другая папка — номера писем сначала
    res = client.put("/api/v1/primary/mailbox", headers=a["hdr"], json={
        "host": "imap.yandex.ru", "username": "docs@client-synthetic.ru", "folder": "Документы", "senders": "@ok.ru"})
    assert res.status_code == 200 and res.json()["password_set"] and res.json()["status"] == "new"
    assert res.json()["senders"] == "@ok.ru"
    assert client.post("/api/v1/primary/mailbox/check", headers=a["hdr"], json={}).status_code == 200
    assert ("SELECT", '"&BBQEPgQ6BEMEPAQ1BD0EQgRL-"', True) in imap.calls


def test_companies_are_isolated_and_admin_must_choose(client, firms, imap):  # noqa: F811
    admin, a, b = firms
    connect(client, a["hdr"])
    assert client.get(f"/api/v1/primary/mailbox?company_id={a['id']}", headers=b["hdr"]).status_code == 404
    assert client.get("/api/v1/primary/mailbox", headers=admin).status_code == 400
    assert client.get(f"/api/v1/primary/mailbox?company_id={a['id']}", headers=admin).json()["configured"] is True
    imap.messages = {5: letter(("upd.pdf", PDF))}
    sync(client, a["hdr"])
    item = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()["drafts"][0]
    for path in (f"/api/v1/primary/inbox/{item['id']}", f"/api/v1/primary/inbox/{item['id']}/file"):
        assert client.get(path, headers=b["hdr"]).status_code == 404, path
    assert client.post(f"/api/v1/primary/inbox/{item['id']}/dismiss", headers=b["hdr"], json={}).status_code == 404
    assert client.get("/api/v1/primary/inbox", headers=b["hdr"]).json()["drafts"] == []
    seen = client.get(f"/api/v1/primary/inbox?company_id={a['id']}", headers=admin).json()["drafts"]
    assert [d["id"] for d in seen] == [item["id"]] and seen[0]["company"]
    user = _mk_user(client, admin, a["id"], f"mail_u_{uuid4().hex[:8]}")
    assert client.get("/api/v1/primary/inbox", headers=user).status_code == 403          # сотруднику — нет


def test_wrong_password_and_folder_are_explained(client, firms, imap):  # noqa: F811
    _admin, a, _b = firms
    client.put("/api/v1/primary/mailbox", headers=a["hdr"], json={
        "host": "imap.mail.ru", "username": "docs@client-synthetic.ru", "password": "wrong", "folder": "Нет такой"})
    res = client.post("/api/v1/primary/mailbox/check", headers=a["hdr"], json={})
    assert res.status_code == 424 and "Нет такой" in res.json()["detail"]
    imap.fail_login = True
    res = client.post("/api/v1/primary/mailbox/check", headers=a["hdr"], json={})
    assert res.status_code == 424 and "логин или пароль" in res.json()["detail"]
    state = client.get("/api/v1/primary/mailbox", headers=a["hdr"]).json()
    assert state["status"] == "error" and "логин или пароль" in state["last_error"]
    with SessionLocal() as db:
        box = db.query(PrimaryMailbox).filter(PrimaryMailbox.company_id == a["id"]).one()
        assert mailbox.due(box, box.last_check_at + timedelta(minutes=59)) is False      # после ошибки — через час
        assert mailbox.due(box, box.last_check_at + timedelta(minutes=61)) is True
        box.status = "active"
        assert mailbox.due(box, box.last_check_at + timedelta(minutes=16)) is True       # обычно — раз в 15 минут
        box.status = "new"
        assert mailbox.due(box, box.last_check_at + timedelta(days=1)) is False          # не проверен — не трогаем


# ------------------------------------------------------------------ приём писем
def test_xml_to_registry_pdf_to_draft_confirm_and_dedupe(client, firms, imap):  # noqa: F811
    _admin, a, _b = firms
    connect(client, a["hdr"])
    number = f"М-{uuid4().hex[:6]}"
    imap.messages = {11: letter(("upd.xml", xml_for(a, number=number)), ("upd.xml.sig", b"0\x82\x05signature"),
                                ("Акт и УПД.pdf", PDF))}
    res = sync(client, a["hdr"])
    assert (res["messages"], res["created"], res["drafts"], res["rejected"]) == (1, 1, 1, 0)
    assert not any(c[0] == "UID" and c[1] not in ("SEARCH", "FETCH") for c in imap.calls)
    listed = client.get("/api/v1/primary", headers=a["hdr"], params={"q": number}).json()["items"]
    assert len(listed) == 1 and listed[0]["channel"] == "mail" and listed[0]["source"] == "xml"
    inbox = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()
    draft = inbox["drafts"][0]
    assert draft["result"] == "draft" and draft["sender"] == SUPPLIER and draft["draft"]["number"] == "УТ-215"
    assert [i["result"] for i in inbox["recent"]] == ["created"] and inbox["week"]["created"] == 1
    rec = client.get(f"/api/v1/primary/inbox/{draft['id']}", headers=a["hdr"]).json()
    assert rec["media_type"] == "application/pdf" and rec["draft"]["seller"]["inn"] == "5001007329"
    assert "token" not in rec and rec["file_url"].endswith("/file")
    file = client.get(rec["file_url"], headers=a["hdr"])
    assert file.status_code == 200 and file.content == PDF and file.headers["x-content-type-options"] == "nosniff"
    assert file.headers["content-disposition"].startswith("inline;")
    saved = client.post(f"/api/v1/primary/inbox/{draft['id']}/confirm", headers=a["hdr"], json={"draft": rec["draft"]})
    assert saved.status_code == 200 and saved.json()["result"] == "created", saved.text
    doc = client.get(f"/api/v1/primary/{saved.json()['document']['id']}", headers=a["hdr"]).json()
    assert (doc["source"], doc["channel"], doc["doc_number"]) == ("pdf", "mail", "УТ-215")
    assert client.get(rec["file_url"], headers=a["hdr"]).status_code == 404          # файл удалён после решения
    again = client.post(f"/api/v1/primary/inbox/{draft['id']}/confirm", headers=a["hdr"], json={"draft": rec["draft"]})
    assert again.status_code == 409
    # нового нет: «N:*» вернул последнее письмо — оно не разбирается повторно
    assert sync(client, a["hdr"])["messages"] == 0
    # тот же файл в новом письме — узнан по хешу, записи не прибавилось
    imap.messages[12] = letter(("копия.pdf", PDF))
    res = sync(client, a["hdr"])
    assert (res["messages"], res["seen"], res["drafts"]) == (1, 1, 0)


def test_print_form_of_xml_document_is_duplicate(client, firms, imap):  # noqa: F811
    _admin, a, _b = firms
    connect(client, a["hdr"])
    same = upd_xml(number="УТ-215", day="14.10.2026", seller=("ООО «СтройБаза»", "5001007329", "500101001"),
                   buyer=("ООО «Покупатель»", a["inn"], "770401001"))
    imap.messages = {3: letter(("ON_NSCHFDOPPR.xml", same), ("УПД УТ-215.pdf", PDF))}
    res = sync(client, a["hdr"])
    assert (res["created"], res["drafts"], res["duplicates"]) == (1, 0, 1)
    inbox = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()
    assert inbox["drafts"] == [] and sorted(i["result"] for i in inbox["recent"]) == ["created", "duplicate"]
    with SessionLocal() as db:
        assert db.query(PrimaryMailFile).join(PrimaryMailItem, PrimaryMailItem.id == PrimaryMailFile.item_id) \
            .filter(PrimaryMailItem.company_id == a["id"]).count() == 0                  # файл дубля не хранится


def test_zip_senders_filter_and_rejected(client, firms, imap):  # noqa: F811
    import io
    import zipfile
    _admin, a, _b = firms
    connect(client, a["hdr"], senders="@stroybaza-synthetic.ru")
    pack = io.BytesIO()
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("ON_NSCHFDOPPR_1.xml", xml_for(a))
        z.writestr("ON_NSCHFDOPPR_2.xml", xml_for(a))
        z.writestr("print.pdf", b"%PDF-1.4 not taken from zip")
    imap.messages = {1: letter(("edo.zip", pack.getvalue())),
                     2: letter(("upd.xml", xml_for(a)), sender="spam@evil-synthetic.ru"),
                     3: letter(("не УПД.xml", b"<?xml version='1.0'?><Invoice/>"))}
    res = sync(client, a["hdr"])
    assert (res["messages"], res["created"], res["foreign"], res["rejected"]) == (2, 2, 1, 1)
    recent = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()["recent"]
    assert sorted(i["file_name"] for i in recent if i["result"] == "created") == [
        "edo.zip/ON_NSCHFDOPPR_1.xml", "edo.zip/ON_NSCHFDOPPR_2.xml"]
    assert not any(i["sender"].startswith("spam@") for i in recent)                  # чужие письма не записываются


def test_new_uidvalidity_limits_and_resume(client, firms, imap, monkeypatch):  # noqa: F811
    _admin, a, _b = firms
    connect(client, a["hdr"])
    monkeypatch.setattr(mailbox, "MAX_MESSAGES", 2)
    imap.messages = {u: letter((f"upd{u}.xml", xml_for(a))) for u in (21, 22, 23)}
    first = sync(client, a["hdr"])
    assert (first["messages"], first["left"]) == (2, 1)
    second = sync(client, a["hdr"])
    assert (second["messages"], second["created"], second["left"]) == (1, 1, 0)
    imap.uidvalidity = "78"                       # ящик пересоздан: номера заново — письма узнаются по хешу файлов
    imap.messages = {1: imap.messages[21], 2: letter(("новый.xml", xml_for(a)))}
    third = sync(client, a["hdr"])
    assert (third["messages"], third["seen"], third["created"]) == (2, 1, 1)
    monkeypatch.setattr(mailbox, "MAX_MESSAGE_BYTES", 10)
    imap.messages[3] = letter(("большой.xml", xml_for(a)))
    assert sync(client, a["hdr"])["too_big"] == 1


def test_ocr_unavailable_not_upd_and_dismiss(client, firms, imap, monkeypatch):  # noqa: F811
    _admin, a, _b = firms
    connect(client, a["hdr"])
    photo = b"\xff\xd8\xff\xe0" + b"1" * 20000
    calls = []

    def fake_recognize(_db, user, _data, name, company_id):
        calls.append((user.role, user.company_id, company_id))
        if name.endswith(".jpg"):
            raise service.RecognizeError("Сканы не распознаются: не скачаны языковые модели", 409)
        raise service.RecognizeError("Это счёт на оплату, а не УПД или счёт-фактура", 422)

    monkeypatch.setattr(service, "recognize_file", fake_recognize)
    imap.messages = {7: letter(("фото УПД.jpg", photo), ("Счёт на оплату.pdf", PDF + b"%schet"),
                               ("logo.png", b"\x89PNG" + b"0" * 100))}
    res = sync(client, a["hdr"])
    assert (res["drafts"], res["skipped"]) == (1, 1)
    assert calls and all(c == ("accountant", a["id"], a["id"]) for c in calls)        # строго в компании ящика
    inbox = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()
    draft = inbox["drafts"][0]
    assert draft["kind"] == "scan" and draft["draft"]["number"] == ""
    assert [i["result"] for i in inbox["recent"]] == ["skipped"] and "счёт на оплату" in inbox["recent"][0]["message"]
    rec = client.get(f"/api/v1/primary/inbox/{draft['id']}", headers=a["hdr"]).json()
    assert "языковые модели" in rec["message"] and rec["media_type"] == "image/jpeg"
    assert client.post(f"/api/v1/primary/inbox/{draft['id']}/dismiss", headers=a["hdr"], json={}).status_code == 200
    assert client.post(f"/api/v1/primary/inbox/{draft['id']}/dismiss", headers=a["hdr"], json={}).status_code == 409
    assert client.get(f"/api/v1/primary/inbox/{draft['id']}/file", headers=a["hdr"]).status_code == 404


def test_busy_recognition_waits_for_next_tick(client, firms, imap, monkeypatch):  # noqa: F811
    _admin, a, _b = firms
    connect(client, a["hdr"])

    def busy(*_a, **_k):
        raise service.RecognizeError("Распознавание занято", 429)

    monkeypatch.setattr(service, "recognize_file", busy)
    imap.messages = {9: letter(("скан.pdf", PDF + b"%scan"))}
    res = sync(client, a["hdr"])
    assert (res["drafts"], res["queued"]) == (0, 1)
    drafts = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()["drafts"]
    assert [d["result"] for d in drafts] == ["queued"]
    monkeypatch.undo()
    with SessionLocal() as db:
        assert mailbox.recognize_queued(db, company_id=a["id"])["draft"] == 1


def test_purge_tick_and_remove(client, firms, imap):  # noqa: F811
    admin, a, b = firms
    connect(client, a["hdr"])
    imap.messages = {4: letter(("upd.pdf", PDF))}
    sync(client, a["hdr"])
    with SessionLocal() as db:
        item = db.query(PrimaryMailItem).filter(PrimaryMailItem.company_id == a["id"],
                                                PrimaryMailItem.result == "draft").one()
        item.created_at = utcnow() - timedelta(days=31)
        old = PrimaryMailItem(company_id=a["id"], file_name="старое.xml", result="created",
                              created_at=utcnow() - timedelta(days=400))
        db.add(old)
        db.commit()
        out = mailbox.purge(db)
        assert out["expired"] >= 1 and out["removed"] >= 1
        db.refresh(item)
        assert item.result == "expired" and db.get(PrimaryMailFile, item.id) is None
        # фоновый тик: проверяет только подключённые ящики, которым пора, и не трогает компанию в архиве
        box = db.query(PrimaryMailbox).filter(PrimaryMailbox.company_id == a["id"]).one()
        box.last_check_at = utcnow() - timedelta(minutes=20)
        db.commit()
    imap.messages[5] = letter(("upd.xml", xml_for(a)))
    with SessionLocal() as db:
        done = [r for r in mailbox.check_due(db) if r["company_id"] == a["id"]]
        assert done and done[0]["ok"] and done[0]["created"] == 1
        assert not [r for r in mailbox.check_due(db) if r["company_id"] == a["id"]]   # 15 минут ещё не прошли
    client.patch(f"/api/v1/companies/{a['id']}", headers=admin, json={"is_active": False})
    with SessionLocal() as db:
        box = db.query(PrimaryMailbox).filter(PrimaryMailbox.company_id == a["id"]).one()
        box.last_check_at = utcnow() - timedelta(hours=1)
        db.commit()
        assert not [r for r in mailbox.check_due(db) if r["company_id"] == a["id"]]
    client.patch(f"/api/v1/companies/{a['id']}", headers=admin, json={"is_active": True})
    res = client.delete("/api/v1/primary/mailbox", headers=a["hdr"])
    assert res.json() == {"removed": True}
    assert client.get("/api/v1/primary/mailbox", headers=a["hdr"]).json()["configured"] is False
    assert client.post("/api/v1/primary/mailbox/sync", headers=a["hdr"], json={}).status_code == 404


@pytest.mark.usefixtures("imap")
def test_lock_and_audit(client, firms):  # noqa: F811
    from app.services.audit_human import humanize_audit
    _admin, a, _b = firms
    connect(client, a["hdr"])
    assert mailbox._lock.acquire(blocking=False)
    try:
        res = client.post("/api/v1/primary/mailbox/sync", headers=a["hdr"], json={})
        assert res.status_code == 429 and "уже проверяется" in res.json()["detail"]
        with SessionLocal() as db:
            assert mailbox.check_due(db) == []
    finally:
        mailbox._lock.release()
    assert humanize_audit("primary_mailbox_saved", {"company": "Ромашка", "host": "imap.yandex.ru",
                                                    "folder": "INBOX"})[0] == "сохранён почтовый ящик для первички"
    assert humanize_audit("primary_mail_checked", {"company": "Ромашка", "messages": 2, "created": 1, "drafts": 1})[1] \
        == "компания: „Ромашка“, писем: 2, в реестр: 1, на сверку: 1, дублей: 0, не принято: 0"


def test_channel_column_and_documents_without_mail(client, firms):  # noqa: F811
    _admin, a, _b = firms
    res = client.post("/api/v1/primary/upload", headers=a["hdr"],
                      files=[("files", ("upd.xml", xml_for(a), "application/xml"))])
    assert res.status_code == 200 and res.json()["created"] == 1
    assert res.json()["results"][0]["document"]["channel"] == ""
    with SessionLocal() as db:
        assert db.query(PrimaryDocument).filter(PrimaryDocument.company_id == a["id"],
                                                PrimaryDocument.channel == "").count() >= 1
    assert SELLER_INN                                    # образец продавца — тот же, что у остальных тестов первички
