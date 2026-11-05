"""Первичка: фото и PDF документов от сотрудников через ботов Telegram и MAX (T-64, v1.70.105, ADR-35).

В сеть тесты не ходят: ответы ботов и скачивание файлов подменены, документы и люди синтетические (ADR-14).
Проверяется: файл привязанного сотрудника попадает в очередь его компании и после распознавания — в «Ждут сверки»
у бухгалтера этой компании (чужой бухгалтер его не видит); подтверждённый документ помнит канал; непривязанным,
групповым чатам, чужим форматам, мелким фото, повторам и сверх суточного предела — честный ответ без записи;
скачивание из Telegram идёт через getFile и мост, ссылки MAX — только https и только публичные адреса.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.database import SessionLocal
from app.models import User
from app.primary import bots, mailbox
from app.primary.models import PrimaryMailFile, PrimaryMailItem
from app.services import max_bot, telegram_bot
from tests.test_primary_upd import firms  # noqa: F401 — фикстура компаний и бухгалтеров
from tests.test_v1140 import _mk_user

PDF = Path("tests/fixtures/primary/upd_print_text.pdf").read_bytes()     # УТ-215 от 14.10.2026, продавец 5001007329


def photo(size: int = 40_000) -> bytes:
    """Синтетическое «фото»: сигнатура JPEG и случайные байты — у каждого вызова свой хеш."""
    return b"\xff\xd8\xff\xe0" + os.urandom(size)


def employee(client, admin_hdr, company_id: str, **link) -> str:
    """Сотрудник компании с привязанным ботом: возвращает id пользователя."""
    name = f"bot_{uuid4().hex[:10]}"
    hdr = _mk_user(client, admin_hdr, company_id, name, role="user")
    uid = client.get("/api/v1/auth/me", headers=hdr).json()["id"]
    with SessionLocal() as db:
        user = db.get(User, uid)
        for key, value in link.items():
            setattr(user, key, value)
        db.commit()
    return uid


@pytest.fixture
def tg(monkeypatch):
    """Telegram без сети: ответы бота копятся в sent, файлы берутся из files по file_id."""
    state = {"sent": [], "files": {}}
    monkeypatch.setattr(telegram_bot, "get_token", lambda db: "123:synthetic")
    monkeypatch.setattr(telegram_bot, "send_message",
                        lambda token, chat_id, text, proxy="", base="": state["sent"].append((str(chat_id), text)))

    def fake_download(_token, file_id, **_kw):
        if file_id not in state["files"]:
            raise telegram_bot.TelegramError("нет файла")
        return state["files"][file_id]

    monkeypatch.setattr(telegram_bot, "download_file", fake_download)
    return state


@pytest.fixture
def mx(monkeypatch):
    """MAX без сети: ответы — в sent, ссылки на вложения — в files."""
    state = {"sent": [], "files": {}}
    monkeypatch.setattr(max_bot, "get_token", lambda db: "max-synthetic")
    monkeypatch.setattr(max_bot, "send_message", lambda token, user_id, text: state["sent"].append((user_id, text)))

    def fake_download(url, **_kw):
        if url not in state["files"]:
            raise bots.BotFileError("⚠️ Не удалось получить файл — пришлите ещё раз.")
        return state["files"][url]

    monkeypatch.setattr(bots, "download_public", fake_download)
    return state


def tg_update(chat_id: str, chat_type: str = "private", **msg) -> dict:
    return {"update_id": 1, "message": {"message_id": 7, "chat": {"id": int(chat_id), "type": chat_type},
                                        "from": {"id": int(chat_id)}, **msg}}


def items(company_id: str) -> list[PrimaryMailItem]:
    with SessionLocal() as db:
        rows = db.query(PrimaryMailItem).filter(PrimaryMailItem.company_id == company_id) \
            .order_by(PrimaryMailItem.created_at).all()
        db.expunge_all()
        return rows


# ------------------------------------------------------------------ Telegram
def test_telegram_pdf_from_employee_to_review_and_registry(client, firms, tg):  # noqa: F811
    admin, a, b = firms
    chat = str(700_000_000 + int(uuid4().int % 99_999_999))
    uid = employee(client, admin, a["id"], telegram_chat_id=chat)
    tg["files"]["doc-1"] = PDF
    upd = tg_update(chat, caption="УПД от СтройБазы, объект на Лесной",
                    document={"file_id": "doc-1", "file_name": "УПД УТ-215.pdf", "mime_type": "application/pdf",
                              "file_size": len(PDF)})
    with SessionLocal() as db:
        telegram_bot.handle_update(db, upd)
    assert tg["sent"][-1][0] == chat and tg["sent"][-1][1].startswith("📄 Документ получен — бухгалтер «")
    [item] = items(a["id"])
    assert (item.channel, item.user_id, item.kind, item.result) == ("telegram", uid, "pdf", "queued")
    assert item.subject == "УПД от СтройБазы, объект на Лесной" and item.file_name == "УПД УТ-215.pdf"
    assert item.message_id == f"telegram:{chat}:7" and item.mailbox_id == ""
    with SessionLocal() as db:
        assert db.get(PrimaryMailFile, item.id).media_type == "application/pdf"
        assert mailbox.recognize_queued(db, company_id=a["id"])["draft"] == 1
    inbox = client.get("/api/v1/primary/inbox", headers=a["hdr"]).json()
    [draft] = inbox["drafts"]
    assert draft["channel"] == "telegram" and draft["draft"]["number"] == "УТ-215" and draft["sender"].startswith("Bot_")
    assert client.get("/api/v1/primary/inbox", headers=b["hdr"]).json()["drafts"] == []     # чужая компания не видит
    rec = client.get(f"/api/v1/primary/inbox/{draft['id']}", headers=a["hdr"]).json()
    assert rec["channel"] == "telegram" and rec["subject"] == "УПД от СтройБазы, объект на Лесной"
    saved = client.post(f"/api/v1/primary/inbox/{draft['id']}/confirm", headers=a["hdr"], json={"draft": rec["draft"]})
    assert saved.status_code == 200 and saved.json()["result"] == "created", saved.text
    doc = client.get(f"/api/v1/primary/{saved.json()['document']['id']}", headers=a["hdr"]).json()
    assert (doc["source"], doc["channel"], doc["doc_number"]) == ("pdf", "telegram", "УТ-215")
    # тот же файл ещё раз — узнан по хешу, новой записи нет
    with SessionLocal() as db:
        telegram_bot.handle_update(db, upd)
    assert tg["sent"][-1][1] == "ℹ️ Этот файл уже получен — бухгалтер его видит." and len(items(a["id"])) == 1


def test_telegram_photo_takes_largest_size_and_names_file(client, firms, tg):  # noqa: F811
    admin, a, _b = firms
    chat = str(710_000_000 + int(uuid4().int % 9_999_999))
    employee(client, admin, a["id"], telegram_chat_id=chat)
    tg["files"]["big"] = photo()
    sizes = [{"file_id": "small", "file_size": 900, "width": 90}, {"file_id": "big", "file_size": 40_004, "width": 1280}]
    with SessionLocal() as db:
        telegram_bot.handle_update(db, tg_update(chat, photo=sizes))
    [item] = items(a["id"])
    assert (item.kind, item.file_name, item.subject) == ("scan", "telegram-фото.jpg", "")
    assert tg["sent"][-1][1].startswith("📄 Документ получен")


def test_telegram_refusals_without_records(client, firms, tg):  # noqa: F811
    admin, a, _b = firms
    chat = str(720_000_000 + int(uuid4().int % 9_999_999))
    stranger = str(730_000_000 + int(uuid4().int % 9_999_999))
    employee(client, admin, a["id"], telegram_chat_id=chat)
    tg["files"].update({"word": b"PK\x03\x04" + os.urandom(30_000), "tiny": photo(2_000)})
    cases = [
        (stranger, {"document": {"file_id": "word", "file_name": "u.pdf", "mime_type": "application/pdf"}},
         "привязавших бота: «Настройки» → Telegram → код"),
        (chat, {"document": {"file_id": "word", "file_name": "Счёт.docx", "mime_type": "application/msword"}},
         "Принимаются фото документа"),
        (chat, {"document": {"file_id": "word", "file_name": "Счёт.docx"}}, "Принимаются фото документа"),
        (chat, {"photo": [{"file_id": "tiny", "file_size": 2_004}]}, "Фото слишком маленькое"),
        (chat, {"photo": [{"file_id": "huge", "file_size": 21 * 1024 * 1024}]}, "Файл больше 20 МБ"),
        (chat, {"photo": [{"file_id": "gone", "file_size": 50_000}]}, "Не удалось получить файл"),
    ]
    for who, msg, expected in cases:
        with SessionLocal() as db:
            telegram_bot.handle_update(db, tg_update(who, **msg))
        assert expected in tg["sent"][-1][1], (msg, tg["sent"][-1])
    sent = len(tg["sent"])
    with SessionLocal() as db:                       # групповой чат — молчим и ничего не принимаем
        telegram_bot.handle_update(db, tg_update(chat, chat_type="group", photo=[{"file_id": "word"}]))
    assert len(tg["sent"]) == sent and items(a["id"]) == []


def test_daily_limit_and_user_without_company(client, firms, tg, monkeypatch):  # noqa: F811
    admin, a, _b = firms
    chat = str(740_000_000 + int(uuid4().int % 9_999_999))
    employee(client, admin, a["id"], telegram_chat_id=chat)
    monkeypatch.setattr(bots, "DAILY_LIMIT", 2)
    for n in range(3):
        tg["files"][f"p{n}"] = photo()
        with SessionLocal() as db:
            telegram_bot.handle_update(db, tg_update(chat, photo=[{"file_id": f"p{n}"}]))
    assert "За сутки от вас принято уже 2 документов" in tg["sent"][-1][1] and len(items(a["id"])) == 2
    nobody = SimpleNamespace(id=uuid4().hex, company_id=None, full_name="", username="solo")
    with SessionLocal() as db:
        res = bots.accept(db, nobody, photo(), "x.jpg", "", "telegram")
        assert res["result"] == "no_company" and not res["ok"]
        with pytest.raises(ValueError):
            bots.accept(db, nobody, photo(), "x.jpg", "", "mail")


def test_telegram_download_goes_through_getfile_and_bridge(monkeypatch):
    calls = {}

    def fake_api(_token, method, payload=None, base="", **_kw):
        calls["api"] = (method, payload, base)
        return {"file_path": "documents/file_1.pdf", "file_size": 10}

    class Resp:
        status_code = 200

        def __init__(self, url, body):
            self.url, self.body = url, body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            yield from (self.body[i:i + 4] for i in range(0, len(self.body), 4))

    body = {"data": b"%PDF-1.7 synthetic"}
    monkeypatch.setattr(telegram_bot, "api", fake_api)

    def fake_stream(_method, url, proxy=None, **_kw):
        calls["url"], calls["proxy"] = url, proxy
        return Resp(url, body["data"])

    monkeypatch.setattr(telegram_bot.httpx, "stream", fake_stream)
    data = telegram_bot.download_file("123:tok", "doc-1", proxy="socks5://p:1080", base="http://bridge:8081/")
    assert data == b"%PDF-1.7 synthetic" and calls["api"] == ("getFile", {"file_id": "doc-1"}, "http://bridge:8081/")
    assert calls["url"] == "http://bridge:8081/file/bot123:tok/documents/file_1.pdf"
    assert calls["proxy"] == "socks5://p:1080"
    telegram_bot.download_file("123:tok", "doc-1")
    assert calls["url"] == "https://api.telegram.org/file/bot123:tok/documents/file_1.pdf" and calls["proxy"] is None
    body["data"] = b"x" * 50
    with pytest.raises(telegram_bot.TelegramError, match="20 МБ"):
        telegram_bot.download_file("123:tok", "doc-1", limit=40)


# ------------------------------------------------------------------ MAX
def test_max_album_from_employee_one_answer(client, firms, mx):  # noqa: F811
    admin, a, _b = firms
    max_id = str(800_000 + int(uuid4().int % 99_999))
    employee(client, admin, a["id"], max_user_id=max_id)
    mx["files"].update({"https://i.max-cdn.example/1": photo(), "https://f.max-cdn.example/2": PDF})
    upd = {"update_type": "message_created", "message": {
        "sender": {"user_id": int(max_id), "name": "Сотрудник"}, "recipient": {"chat_type": "dialog"},
        "body": {"mid": "mid.1", "text": "накладные с объекта", "attachments": [
            {"type": "image", "payload": {"photo_id": 1, "token": "t", "url": "https://i.max-cdn.example/1"}},
            {"type": "file", "payload": {"url": "https://f.max-cdn.example/2", "token": "t"},
             "filename": "УПД.pdf", "size": len(PDF)},
            {"type": "sticker", "payload": {"url": "https://i.max-cdn.example/3"}}]}}}
    with SessionLocal() as db:
        max_bot.handle_update(db, upd)
    rows = items(a["id"])
    assert sorted((r.channel, r.kind, r.file_name) for r in rows) == [("max", "pdf", "УПД.pdf"),
                                                                       ("max", "scan", "max-фото.jpg")]
    assert all(r.subject == "накладные с объекта" and r.message_id == f"max:{max_id}:mid.1" for r in rows)
    assert len(mx["sent"]) == 1 and mx["sent"][0][1].startswith("📄 Принято документов: 2 из 2 — бухгалтер «")
    # повтор того же альбома: обе записи узнаны, итог честный
    with SessionLocal() as db:
        max_bot.handle_update(db, upd)
    assert mx["sent"][-1][1].startswith("📄 Принято документов: 0 из 2.") and "уже получен" in mx["sent"][-1][1]
    assert len(items(a["id"])) == 2


def test_max_stranger_group_and_bind_code_still_work(firms, mx):  # noqa: F811
    _admin, a, _b = firms
    stranger = str(900_000 + int(uuid4().int % 99_999))
    image = {"type": "image", "payload": {"url": "https://i.max-cdn.example/9"}}
    with SessionLocal() as db:
        max_bot.handle_update(db, {"message": {"sender": {"user_id": int(stranger)},
                                               "body": {"attachments": [image]}}})
    assert "«Настройки» → MAX → код" in mx["sent"][-1][1]
    sent = len(mx["sent"])
    with SessionLocal() as db:                       # групповой чат — молчим
        max_bot.handle_update(db, {"message": {"sender": {"user_id": int(stranger)}, "recipient": {"chat_type": "chat"},
                                               "body": {"attachments": [image]}}})
    assert len(mx["sent"]) == sent and items(a["id"]) == []
    with SessionLocal() as db:                       # текст без вложений — прежняя ветка (код привязки)
        max_bot.handle_update(db, {"message": {"sender": {"user_id": int(stranger)}, "body": {"text": "/start 000000"}}})
    assert "Код не найден или устарел" in mx["sent"][-1][1]


def test_download_public_allows_only_https_public_hosts(monkeypatch):
    real = socket.getaddrinfo
    hosts = {"cdn.example": "93.184.216.34", "evil.example": "10.0.0.5", "loop.example": "127.0.0.1"}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in hosts:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (hosts[host], port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    routes = {"https://cdn.example/ok": (200, {"content-length": "8"}, b"%PDF-1.4"),
              "https://cdn.example/go": (302, {"location": "https://evil.example/x"}, b""),
              "https://cdn.example/rel": (302, {"location": "/ok"}, b""),
              "https://cdn.example/big": (200, {"content-length": str(bots.MAX_BYTES + 1)}, b""),
              "https://cdn.example/404": (404, {}, b"")}

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, content = routes[str(request.url)]
        return httpx.Response(status, headers=headers, content=content)

    mock = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(bots.httpx, "stream", lambda method, url, **kw: mock.stream(method, url, follow_redirects=False))
    assert bots.download_public("https://cdn.example/ok") == b"%PDF-1.4"
    assert bots.download_public("https://cdn.example/rel") == b"%PDF-1.4"          # относительная переадресация
    for url, expected in (("http://cdn.example/ok", "Не удалось получить файл"),
                          ("https://loop.example/x", "внутреннюю сеть"),
                          ("https://cdn.example/go", "внутреннюю сеть"),          # переадресация во внутреннюю сеть
                          ("https://cdn.example/big", "больше 20 МБ"),
                          ("https://cdn.example/404", "Не удалось получить файл"),
                          ("", "Не удалось получить файл")):
        with pytest.raises(bots.BotFileError, match=expected):
            bots.download_public(url)
