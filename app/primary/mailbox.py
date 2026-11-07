"""Первичка из почтового ящика компании (T-63, v1.70.104, ADR-34).

Бухгалтер подключает ящик компании по IMAP (Яндекс 360, Mail.ru и VK WorkSpace, Рамблер, свой сервер) — в
карточке компании или на экране «Первичка». Программа раз в 15 минут читает новые письма ТОЛЬКО на чтение:
папка открывается командой EXAMINE (select с readonly), письма забираются через BODY.PEEK — они остаются
непрочитанными, ничего не удаляется и не перемещается. Вложения идут в тот же конвейер, что и ручная загрузка:

- XML и ZIP от операторов ЭДО — сразу в реестр (проверки, дубли, схема ФНС — service.accept);
- PDF, сканы и фото — черновиками на сверку: распознавание на своём сервере (service.recognize_file), бухгалтер
  сверяет и подтверждает; само вложение хранится только до его решения и не дольше 30 дней;
- печатная форма того же документа, что уже пришёл в XML, черновиком не становится — это дубль;
- подписи (.sig, .p7s), Word, Excel и картинки из подписи письма пропускаются без записи.

Защита: пароль — только зашифрованным (secretbox) и наружу не отдаётся; вход только по TLS (993 — SSL, 143 —
STARTTLS) с проверкой сертификата; адрес сервера во внутренней сети запрещён (защита от SSRF), если
администратор сервера не разрешил это явно переменной YM_MAIL_PRIVATE_HOSTS=1 (свой почтовый сервер в локальной
сети); соединение идёт на тот IP, который прошёл проверку (подмена DNS между проверкой и входом не поможет).
Пределы: 30 писем и 80 МБ за проверку, письмо до 40 МБ, 20 вложений из письма. Обработанные письма помнятся
по UIDVALIDITY и UID (как в paperless-ngx); уже разобранное вложение узнаётся по хешу файла.
"""
from __future__ import annotations

import base64
import contextlib
import email
import email.policy
import hashlib
import imaplib
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from ..auth import ROLE_ACCOUNTANT, ROLE_ADMIN
from ..models import Company, User, utcnow
from ..services import secretbox
from . import service
from .models import PrimaryMailbox, PrimaryMailFile, PrimaryMailItem

CHECK_EVERY = timedelta(minutes=15)
ERROR_RETRY = timedelta(hours=1)
MAX_PER_TICK = 3                     # ящиков за один фоновый тик
MAX_MESSAGES = 30                    # писем за одну проверку — остальные в следующий раз
MAX_MESSAGE_BYTES = 40 * 1024 * 1024
MAX_CHECK_BYTES = 80 * 1024 * 1024
MAX_ATTACHMENTS = 20
MIN_IMAGE_BYTES = 15_000             # картинки меньше — логотипы и подписи в теле письма
RECOGNIZE_PER_TICK = 3               # распознавание тяжёлое: не больше трёх вложений за тик
DRAFT_DAYS = 30
LOG_DAYS = 365
DEFAULT_DAYS = 30                    # первая проверка — письма за последние 30 дней
MAX_START_DAYS = 365
TIMEOUT = 30
PORTS = (993, 143)
SECURITY = {"ssl": "SSL/TLS (порт 993)", "starttls": "STARTTLS (порт 143)"}
STATUSES = {"new": "сохранён, не проверен", "active": "подключён", "error": "ошибка подключения"}
RESULTS = {"created": "в реестре", "duplicate": "уже был в реестре", "rejected": "не принят", "queued": "распознаётся",
           "draft": "ждёт сверки", "confirmed": "сверен и сохранён", "dismissed": "не нужен", "expired": "срок вышел",
           "skipped": "не УПД"}
MAX_SENDERS = 50
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
HOST_RE = re.compile(r"^(?=.{4,200}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
ADDRESS_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
DOMAIN_RE = re.compile(r"^@?[a-z0-9.\-]+\.[a-z]{2,}$")
MEDIA = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".png": "image/png", ".tif": "image/tiff"}

_lock = threading.Lock()
_purged = {"day": ""}


class MailboxError(ValueError):
    """Понятное сообщение для бухгалтера; status — код ответа API (424 — ошибка почтового сервера)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def today_msk() -> date:
    return (utcnow() + timedelta(hours=3)).date()


def private_hosts_allowed() -> bool:
    return os.environ.get("YM_MAIL_PRIVATE_HOSTS", "") == "1"


# ------------------------------------------------------------------ проверка полей
def clean_host(value: str | None) -> str:
    host = (value or "").strip().lower().rstrip(".")
    return host if HOST_RE.match(host) else ""


def parse_senders(text: str | None) -> list[str]:
    """«a@b.ru, @c.ru d.ru» → ['a@b.ru', '@c.ru', '@d.ru']; пусто — письма принимаются от всех."""
    out: list[str] = []
    for raw in re.split(r"[\s,;]+", (text or "").strip().lower()):
        if not raw:
            continue
        if ADDRESS_RE.match(raw):
            value = raw
        elif DOMAIN_RE.match(raw):
            value = raw if raw.startswith("@") else "@" + raw
        else:
            raise MailboxError(f"Не похоже на адрес или домен: «{raw[:60]}»")
        if value not in out:
            out.append(value)
    if len(out) > MAX_SENDERS:
        raise MailboxError(f"Не больше {MAX_SENDERS} адресов и доменов")
    return out


def sender_allowed(address: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    address = (address or "").lower()
    if not address or "@" not in address:
        return False
    if address in allowed:
        return True
    domain = address.rsplit("@", 1)[1]
    return any(a.startswith("@") and (domain == a[1:] or domain.endswith("." + a[1:])) for a in allowed)


def imap_utf7(text: str) -> str:
    """Имя папки в «изменённой UTF-7» (RFC 3501, 5.1.3): «Документы» → «&BBQEPgQ6BEMEPAQ1BD0EQgRL-»."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b64 + "-")
            buf.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"     # не strftime: %b зависит от локали


# ------------------------------------------------------------------ соединение
def resolve_public(host: str, port: int) -> str:
    """IP почтового сервера; адреса внутренней сети — отказ (SSRF), если сервер не разрешил их явно."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise MailboxError(f"Почтовый сервер «{host}» не найден — проверьте адрес", 424) from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise MailboxError(f"Почтовый сервер «{host}» не найден — проверьте адрес", 424)
    if not private_hosts_allowed() and any(not ip.is_global for ip in addresses):
        raise MailboxError("Адрес почтового сервера — во внутренней сети. Такой сервер подключается, только если "
                           "администратор сервера программы разрешил это (YM_MAIL_PRIVATE_HOSTS=1)")
    return str(addresses[0])


class _Pinned(imaplib.IMAP4):
    """IMAP4 на заранее проверенный IP: имя сервера — для сертификата (STARTTLS), сокет — на этот IP."""

    def __init__(self, host: str, port: int, ip: str, timeout: float):
        self._ip = ip
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout):
        return socket.create_connection((self._ip, self.port), timeout)


class _PinnedSSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, ip: str, context: ssl.SSLContext, timeout: float):
        self._ip = ip
        super().__init__(host, port, ssl_context=context, timeout=timeout)

    def _create_socket(self, timeout):
        sock = socket.create_connection((self._ip, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def _open(box: PrimaryMailbox, password: str):
    """Вход в ящик. Тесты подменяют эту функцию поддельным клиентом (без сети)."""
    ip = resolve_public(box.host, box.port)
    context = ssl.create_default_context()
    try:
        if box.security == "starttls":
            client = _Pinned(box.host, box.port, ip, TIMEOUT)
            client.starttls(context)
        else:
            client = _PinnedSSL(box.host, box.port, ip, context, TIMEOUT)
    except ssl.SSLError as exc:
        raise MailboxError("Сертификат почтового сервера не прошёл проверку — подключение небезопасно", 424) from exc
    except (OSError, imaplib.IMAP4.error) as exc:
        raise MailboxError(f"Почтовый сервер «{box.host}» не отвечает на порту {box.port}", 424) from exc
    try:
        client.login(box.username, password)
    except UnicodeEncodeError as exc:
        _bye(client)
        raise MailboxError("Логин и пароль — латиницей: русские буквы протокол IMAP не принимает") from exc
    except imaplib.IMAP4.error as exc:
        _bye(client)
        raise MailboxError("Почтовый сервер не принял логин или пароль. Для Яндекса и Mail.ru нужен пароль "
                           "приложения, а в настройках ящика — разрешён доступ по IMAP", 424) from exc
    return client


def _bye(client) -> None:
    with contextlib.suppress(Exception):                  # соединение уже могло оборваться — выход не важен
        client.logout()


def _select(client, folder: str) -> tuple[int, str]:
    """Открыть папку только на чтение → (писем в папке, UIDVALIDITY)."""
    typ, data = client.select(_quote(imap_utf7(folder or "INBOX")), readonly=True)
    if typ != "OK":
        raise MailboxError(f"Папка «{folder}» не найдена в ящике", 424)
    raw = data[0] if data else b""
    text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw or "")
    total = int(text) if text.isdigit() else 0
    _typ, uv = client.response("UIDVALIDITY")
    value = uv[0] if uv and uv[0] else b""
    return total, (value.decode("ascii", "replace") if isinstance(value, bytes) else str(value))[:20]


def _search(client, box: PrimaryMailbox, since: date) -> list[int]:
    fresh = not box.last_uid
    if fresh:
        typ, data = client.uid("SEARCH", None, "SINCE", imap_date(since))
    else:
        typ, data = client.uid("SEARCH", None, "UID", f"{box.last_uid + 1}:*")
    if typ != "OK":
        raise MailboxError("Почтовый сервер не выполнил поиск писем", 424)
    raw = data[0] if data and data[0] else b""
    uids = sorted({int(x) for x in raw.split() if x.isdigit()})
    return uids if fresh else [u for u in uids if u > box.last_uid]   # «N:*» всегда отдаёт последнее письмо


def _size(client, uid: int) -> int:
    typ, data = client.uid("FETCH", str(uid), "(RFC822.SIZE)")
    if typ != "OK" or not data or data[0] is None:
        return -1
    head = data[0][0] if isinstance(data[0], tuple) else data[0]
    found = re.search(rb"RFC822\.SIZE (\d+)", head or b"")
    return int(found.group(1)) if found else -1


def _fetch(client, uid: int) -> bytes:
    typ, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")      # PEEK — письмо остаётся непрочитанным
    if typ != "OK":
        return b""
    for part in data or []:
        if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
            return part[1]
    return b""


# ------------------------------------------------------------------ разбор письма
def parse_message(raw: bytes) -> dict:
    """Письмо → отправитель, тема, дата, Message-ID и вложения [(имя, байты)] — и из пересланных писем тоже."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    def header(name: str, limit: int) -> str:
        try:
            return " ".join(str(msg.get(name, "") or "").split())[:limit]
        except Exception:                                 # noqa: BLE001 — битый заголовок не должен ронять разбор
            return ""

    received = None
    try:
        when = parsedate_to_datetime(header("Date", 100))
        received = (when.astimezone(timezone.utc) if when.tzinfo else when).replace(tzinfo=None)
    except (TypeError, ValueError, IndexError):
        received = None
    attachments: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            name = part.get_filename() or ""
        except Exception:                                 # noqa: BLE001
            name = ""
        if not name:
            continue
        lowered = name.lower()
        inline_picture = (part.get_content_disposition() != "attachment" and part.get("Content-ID")
                          and not lowered.endswith((".xml", ".zip", ".pdf")))
        if inline_picture:                               # картинка внутри текста письма: логотип, подпись
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:                                 # noqa: BLE001
            data = b""
        if data:
            attachments.append((name.replace("/", "_").replace("\\", "_")[:200], data))
        if len(attachments) >= MAX_ATTACHMENTS:
            break
    return {"sender": parseaddr(header("From", 300))[1].lower()[:200], "subject": header("Subject", 300),
            "message_id": header("Message-ID", 255), "received_at": received, "attachments": attachments}


def attachment_kind(name: str, data: bytes) -> str:
    """xml | zip | pdf | image | '' (не наш формат) — по сигнатуре, а для XML ещё и по имени."""
    lowered = name.lower()
    if data.startswith(b"PK"):
        return "zip" if lowered.endswith(".zip") else ""          # .docx и .xlsx — тоже ZIP, но не наши
    if data.startswith(b"%PDF"):
        return "pdf"
    if any(data.startswith(magic) for magic in service.IMAGE_TYPES):
        return "image"
    head = data[:200].lstrip(b"\xef\xbb\xbf \r\n\t")
    if lowered.endswith(".xml") or head.startswith(b"<?xml"):
        return "xml" if head.startswith(b"<") else ""
    return ""


def media_type(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return MEDIA[".pdf"]
    for magic, suffix in service.IMAGE_TYPES.items():
        if data.startswith(magic):
            return MEDIA[suffix]
    return "application/octet-stream"


# ------------------------------------------------------------------ состояние и настройки
def mailbox_of(db: Session, company_id: str) -> PrimaryMailbox | None:
    return db.query(PrimaryMailbox).filter(PrimaryMailbox.company_id == company_id).first()


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _result(box: PrimaryMailbox) -> dict:
    try:
        value = json.loads(box.last_result_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def state(db: Session, company: Company, box: PrimaryMailbox | None) -> dict:
    """Состояние для карточки: без пароля — только признак «задан»."""
    drafts = (db.query(PrimaryMailItem).filter(PrimaryMailItem.company_id == company.id,
                                               PrimaryMailItem.result.in_(("queued", "draft"))).count())
    base = {"company_id": company.id, "company": company.short_name or company.name, "configured": box is not None,
            "drafts": drafts, "check_every_min": int(CHECK_EVERY.total_seconds() // 60),
            "private_hosts": private_hosts_allowed()}
    if box is None:
        return {**base, "host": "", "port": 993, "security": "ssl", "username": "", "password_set": False,
                "folder": "INBOX", "senders": "", "start_date": None, "status": "", "status_title": "не подключён",
                "last_check_at": None, "last_ok_at": None, "last_error": "", "last_result": {}}
    return {**base, "host": box.host, "port": box.port, "security": box.security, "username": box.username,
            "password_set": bool(box.password_enc), "folder": box.folder or "INBOX",
            "senders": ", ".join(parse_senders_safe(box.senders)), "start_date": _iso(box.start_date),
            "status": box.status, "status_title": STATUSES.get(box.status, box.status),
            "last_check_at": _iso(box.last_check_at), "last_ok_at": _iso(box.last_ok_at),
            "last_error": box.last_error or "", "last_result": _result(box)}


def parse_senders_safe(text: str | None) -> list[str]:
    try:
        return parse_senders(text)
    except MailboxError:
        return []


def save(db: Session, company: Company, data: dict, user: User) -> PrimaryMailbox:
    host = clean_host(data.get("host"))
    if not host:
        raise MailboxError("Укажите адрес IMAP-сервера, например imap.yandex.ru")
    security = data.get("security") if data.get("security") in SECURITY else "ssl"
    try:
        port = int(data.get("port") or (993 if security == "ssl" else 143))
    except (TypeError, ValueError) as exc:
        raise MailboxError("Порт — число: 993 для SSL или 143 для STARTTLS") from exc
    if port not in PORTS and not (private_hosts_allowed() and 1 <= port <= 65535):
        raise MailboxError("Порт IMAP — 993 (SSL) или 143 (STARTTLS)")
    username = " ".join(str(data.get("username") or "").split())[:200]
    if not username:
        raise MailboxError("Укажите логин — обычно это адрес ящика")
    folder = " ".join(str(data.get("folder") or "INBOX").split())[:200] or "INBOX"
    senders = parse_senders(data.get("senders"))
    start = data.get("start_date")
    today = today_msk()
    if isinstance(start, str):
        try:
            start = date.fromisoformat(start) if start else None
        except ValueError as exc:
            raise MailboxError("Дата начала — в формате ГГГГ-ММ-ДД") from exc
    if start is not None and not today - timedelta(days=MAX_START_DAYS) <= start <= today:
        raise MailboxError("Дата начала — не позже сегодняшней и не раньше чем год назад")
    box = mailbox_of(db, company.id)
    password = str(data.get("password") or "")
    if box is None:
        if not password:
            raise MailboxError("Укажите пароль ящика (для Яндекса и Mail.ru — пароль приложения)")
        box = PrimaryMailbox(company_id=company.id, created_by=user.id)
        db.add(box)
    moved = (box.host, box.port, box.security, box.username, box.folder) != (host, port, security, username, folder)
    if moved or password:
        box.status = "new"
        box.last_error = ""
    if moved:                                          # другой ящик или папка — другие номера писем
        box.uidvalidity, box.last_uid = "", 0
    if start is not None and start != box.start_date and not moved:
        box.uidvalidity, box.last_uid = "", 0          # новая дата начала — заново с неё (дубли узнаются по хешу)
    box.host, box.port, box.security, box.username, box.folder = host, port, security, username, folder
    if password:
        if len(password) > 500:
            raise MailboxError("Пароль длиннее 500 символов")
        box.password_enc = secretbox.seal(password)
    box.senders = ", ".join(senders)
    box.start_date = start or box.start_date or today - timedelta(days=DEFAULT_DAYS)
    box.updated_by, box.updated_at = user.id, utcnow()
    db.commit()
    return box


def remove(db: Session, company: Company) -> bool:
    """Отключить ящик: забыть адрес и пароль. Пришедшие черновики остаются — их можно досверить."""
    box = mailbox_of(db, company.id)
    if box is None:
        return False
    db.delete(box)
    db.commit()
    return True


# ------------------------------------------------------------------ проверка ящика
def _robot(box: PrimaryMailbox):
    """Фоновый приём работает от имени того, кто подключил ящик, но строго в компании ящика."""
    return SimpleNamespace(id=box.created_by or "", role=ROLE_ACCOUNTANT, company_id=box.company_id,
                           username="почта")


def _seen(db: Session, company_id: str, sha: str) -> bool:
    return db.query(PrimaryMailItem.id).filter(PrimaryMailItem.company_id == company_id,
                                               PrimaryMailItem.file_sha256 == sha).first() is not None


def _take(db: Session, box: PrimaryMailbox, robot, meta: dict, uid: int, name: str, data: bytes,
          counters: Counter) -> None:
    kind = attachment_kind(name, data)
    if not kind or (kind == "image" and len(data) < MIN_IMAGE_BYTES):
        counters["other"] += 1
        return
    if len(data) > service.MAX_ZIP_BYTES:
        counters["too_big"] += 1
        return
    sha = hashlib.sha256(data).hexdigest()
    if _seen(db, box.company_id, sha):
        counters["seen"] += 1
        return
    common = {"company_id": box.company_id, "mailbox_id": box.id, "message_uid": uid,
              "message_id": meta["message_id"], "sender": meta["sender"], "subject": meta["subject"],
              "received_at": meta["received_at"], "file_sha256": sha, "size": len(data)}
    if kind in ("xml", "zip"):
        for label, body, reason in service.unpack(name, data):
            if body is None:
                res = {"result": "rejected", "message": reason}
            else:
                res = service.accept(db, robot, body, label, box.company_id, channel="mail")
            counters[res["result"]] += 1
            db.add(PrimaryMailItem(**common, file_name=label[:255], kind="xml", result=res["result"],
                                   message=str(res.get("message") or "")[:300],
                                   document_id=(res.get("document") or {}).get("id", "")))
        db.commit()
        return
    item = PrimaryMailItem(**common, file_name=name[:255], kind="pdf" if kind == "pdf" else "scan", result="queued")
    db.add(item)
    db.flush()
    db.add(PrimaryMailFile(item_id=item.id, media_type=media_type(data), data=data))
    db.commit()
    counters["queued"] += 1


def _process(db: Session, box: PrimaryMailbox, uid: int, raw: bytes, allowed: list[str], counters: Counter) -> None:
    meta = parse_message(raw)
    if not sender_allowed(meta["sender"], allowed):
        counters["foreign"] += 1
        return
    counters["messages"] += 1
    robot = _robot(box)
    for name, data in meta["attachments"]:
        _take(db, box, robot, meta, uid, name, data, counters)


def _fail(db: Session, box: PrimaryMailbox, exc: MailboxError) -> None:
    box.status = "error"
    box.last_error = str(exc)[:300]
    db.commit()


def test_connection(db: Session, box: PrimaryMailbox) -> dict:
    """«Проверить и подключить»: вход, папка на чтение — без загрузки писем."""
    password = secretbox.unseal(box.password_enc)
    if not password:
        raise MailboxError("Пароль не задан или не расшифровывается — введите его заново")
    box.last_check_at = utcnow()
    client = None
    try:
        client = _open(box, password)
        total, _uidvalidity = _select(client, box.folder)
    except MailboxError as exc:
        _fail(db, box, exc)
        raise
    except (imaplib.IMAP4.error, OSError) as exc:
        err = MailboxError(f"Почтовый сервер прервал соединение: {str(exc)[:120]}", 424)
        _fail(db, box, err)
        raise err from exc
    finally:
        if client is not None:
            _bye(client)
    box.status, box.last_error = "active", ""
    db.commit()
    return {"ok": True, "messages_in_folder": total}


def check(db: Session, box: PrimaryMailbox) -> dict:
    """Забрать новые письма ящика и разобрать вложения; итог — в last_result_json."""
    password = secretbox.unseal(box.password_enc)
    if not password:
        err = MailboxError("Пароль не задан или не расшифровывается — введите его заново")
        _fail(db, box, err)
        raise err
    now = utcnow()
    box.last_check_at = now
    counters: Counter = Counter()
    left = 0
    client = None
    try:
        client = _open(box, password)
        _total, uidvalidity = _select(client, box.folder)
        if uidvalidity != box.uidvalidity:              # ящик пересоздан или первая проверка — заново с даты
            box.uidvalidity, box.last_uid = uidvalidity, 0
        uids = _search(client, box, box.start_date or today_msk() - timedelta(days=DEFAULT_DAYS))
        allowed = parse_senders_safe(box.senders)
        loaded = 0
        for n, uid in enumerate(uids):
            if n >= MAX_MESSAGES:
                left = len(uids) - n
                break
            size = _size(client, uid)
            if size > MAX_MESSAGE_BYTES:
                counters["too_big"] += 1
            else:
                if loaded and loaded + max(size, 0) > MAX_CHECK_BYTES:
                    left = len(uids) - n
                    break
                raw = _fetch(client, uid)
                loaded += len(raw)
                if raw:
                    _process(db, box, uid, raw, allowed, counters)
            box.last_uid = uid
            db.commit()
    except MailboxError as exc:
        _fail(db, box, exc)
        raise
    except (imaplib.IMAP4.error, OSError) as exc:
        err = MailboxError(f"Почтовый сервер прервал соединение: {str(exc)[:120]}", 424)
        _fail(db, box, err)
        raise err from exc
    finally:
        if client is not None:
            _bye(client)
    recognized = recognize_queued(db, company_id=box.company_id)
    result = {"ok": True, "messages": counters["messages"], "created": counters["created"],
              "duplicates": counters["duplicate"] + recognized["duplicate"], "rejected": counters["rejected"],
              "drafts": recognized["draft"], "queued": max(0, counters["queued"] - sum(recognized.values())),
              "skipped": recognized["skipped"], "seen": counters["seen"], "foreign": counters["foreign"],
              "too_big": counters["too_big"], "left": left}
    box.status, box.last_error, box.last_ok_at = "active", "", now
    box.last_result_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    return result


def check_now(db: Session, company: Company) -> dict:
    box = mailbox_of(db, company.id)
    if box is None:
        raise MailboxError("Ящик не подключён", 404)
    if not _lock.acquire(blocking=False):
        raise MailboxError("Почта уже проверяется — повторите через минуту", 429)
    try:
        return check(db, box)
    finally:
        _lock.release()


def due(box: PrimaryMailbox, now: datetime) -> bool:
    if box.status not in ("active", "error"):
        return False
    if box.last_check_at is None:
        return True
    return now - box.last_check_at >= (ERROR_RETRY if box.status == "error" else CHECK_EVERY)


def check_due(db: Session, now: datetime | None = None) -> list[dict]:
    """Фоновый тик: ящики, которым пора (не больше трёх), распознавание очереди и раз в сутки — чистка."""
    now = now or utcnow()
    if not _lock.acquire(blocking=False):
        return []
    out: list[dict] = []
    try:
        for box in db.query(PrimaryMailbox).filter(PrimaryMailbox.status.in_(("active", "error"))).all():
            if len(out) >= MAX_PER_TICK:
                break
            if not due(box, now):
                continue
            company = db.get(Company, box.company_id)
            if company is None or not company.is_active:
                continue
            try:
                out.append({"company_id": company.id, **check(db, box)})
            except MailboxError as exc:
                out.append({"company_id": company.id, "ok": False, "error": str(exc)})
        recognize_queued(db)
        day = (now + timedelta(hours=3)).date().isoformat()
        if _purged["day"] != day:
            _purged["day"] = day
            purge(db, now)
    finally:
        _lock.release()
    return out


# ------------------------------------------------------------------ черновики: распознавание, сверка, отказ
def _empty_rec(item: PrimaryMailItem) -> dict:
    return {"source": item.kind, "file_name": item.file_name, "file_sha256": item.file_sha256,
            "company_id": item.company_id, "found": [], "uncertain": [], "issues": [], "duplicate": None,
            "hints": {}, "ocr": None,
            "draft": {"function": "СЧФДОП", "number": "", "date": None, "correction_number": "",
                      "correction_date": None, "currency": "643", "seller": {"name": "", "inn": "", "kpp": ""},
                      "buyer": {"name": "", "inn": "", "kpp": ""},
                      "totals": {"without_vat": None, "vat": None, "with_vat": None}, "lines": []}}


def _drop_file(db: Session, item_id: str) -> None:
    blob = db.get(PrimaryMailFile, item_id)
    if blob is not None:
        db.delete(blob)


def recognize_queued(db: Session, limit: int = RECOGNIZE_PER_TICK, company_id: str | None = None) -> Counter:
    """Распознать вложения из очереди: черновик на сверку, дубль (уже есть в реестре) или «не УПД»."""
    done: Counter = Counter()
    query = db.query(PrimaryMailItem).filter(PrimaryMailItem.result == "queued")
    if company_id:
        query = query.filter(PrimaryMailItem.company_id == company_id)
    for item in query.order_by(PrimaryMailItem.created_at).limit(limit).all():
        blob = db.get(PrimaryMailFile, item.id)
        if blob is None:
            item.result, item.message = "expired", "Файл вложения не сохранился"
            db.commit()
            continue
        box = mailbox_of(db, item.company_id)
        robot = _robot(box) if box else SimpleNamespace(id="", role=ROLE_ACCOUNTANT, company_id=item.company_id,
                                                         username="почта")
        try:
            rec = service.recognize_file(db, robot, blob.data, item.file_name, item.company_id)
        except service.RecognizeError as exc:
            if exc.status == 429:                       # распознавание занято — в следующий тик
                break
            if exc.status == 409:                       # нет языковых моделей — бухгалтер заполнит по файлу
                item.result, item.message = "draft", str(exc)[:300]
                item.draft_json = json.dumps(_empty_rec(item), ensure_ascii=False)
                done["draft"] += 1
            else:                                       # не УПД (счёт на оплату, договор) или не читается
                item.result, item.message = "skipped", str(exc)[:300]
                _drop_file(db, item.id)
                done["skipped"] += 1
            db.commit()
            continue
        rec.pop("token", None)
        rec.pop("preview", None)
        item.kind = rec.get("source") if rec.get("source") in ("pdf", "scan") else item.kind
        if rec.get("duplicate"):                        # печатная форма документа, который уже пришёл в XML
            item.result, item.message = "duplicate", "Этот документ уже есть в реестре"
            item.document_id = rec["duplicate"].get("id", "")
            _drop_file(db, item.id)
            done["duplicate"] += 1
        else:
            item.result = "draft"
            item.draft_json = json.dumps(rec, ensure_ascii=False, default=str)
            done["draft"] += 1
        db.commit()
    return done


def scope(query, user: User, company_id: str | None = None):
    if user.role == ROLE_ADMIN:
        return query.filter(PrimaryMailItem.company_id == company_id) if company_id else query
    return query.filter(PrimaryMailItem.company_id == (user.company_id or ""))


def visible(user: User, item: PrimaryMailItem) -> bool:
    return user.role == ROLE_ADMIN or item.company_id == user.company_id


def _draft_summary(item: PrimaryMailItem) -> dict:
    try:
        draft = (json.loads(item.draft_json or "{}") or {}).get("draft") or {}
    except ValueError:
        draft = {}
    totals = draft.get("totals") or {}
    return {"number": draft.get("number") or "", "date": draft.get("date"),
            "seller": (draft.get("seller") or {}).get("name") or "", "total": totals.get("with_vat")}


def brief(item: PrimaryMailItem, names: dict[str, str] | None = None) -> dict:
    return {"id": item.id, "company_id": item.company_id, "company": (names or {}).get(item.company_id, ""),
            "received_at": _iso(item.received_at), "created_at": _iso(item.created_at), "sender": item.sender,
            "subject": item.subject, "file_name": item.file_name, "kind": item.kind, "result": item.result,
            "result_title": RESULTS.get(item.result, item.result), "message": item.message,
            "document_id": item.document_id, "size": item.size, "channel": item.channel or "mail",
            "draft": _draft_summary(item) if item.result == "draft" else None}


def inbox(db: Session, user: User, company_id: str | None, limit: int = 50) -> dict:
    query = scope(db.query(PrimaryMailItem), user, company_id)
    drafts = query.filter(PrimaryMailItem.result.in_(("queued", "draft"))) \
        .order_by(PrimaryMailItem.created_at).limit(200).all()
    recent = query.filter(PrimaryMailItem.result.notin_(("queued", "draft"))) \
        .order_by(PrimaryMailItem.created_at.desc()).limit(limit).all()
    ids = {i.company_id for i in drafts + recent}
    names = {c.id: (c.short_name or c.name) for c in db.query(Company).filter(Company.id.in_(ids)).all()} if ids else {}
    week = utcnow() - timedelta(days=7)
    counts = Counter(r for (r,) in query.filter(PrimaryMailItem.created_at >= week)
                     .with_entities(PrimaryMailItem.result).all())
    return {"drafts": [brief(i, names) for i in drafts], "recent": [brief(i, names) for i in recent],
            "week": {k: int(v) for k, v in counts.items()}}


def item_for_review(db: Session, item: PrimaryMailItem) -> dict:
    if item.result not in ("draft", "queued"):
        raise MailboxError("Этот черновик уже разобран", 409)
    try:
        rec = json.loads(item.draft_json) if item.draft_json else _empty_rec(item)
    except ValueError:
        rec = _empty_rec(item)
    blob = db.get(PrimaryMailFile, item.id)
    rec.update({"item_id": item.id, "source": item.kind, "company_id": item.company_id, "sender": item.sender,
                "channel": item.channel or "mail",
                "subject": item.subject, "received_at": _iso(item.received_at), "message": item.message,
                "queued": item.result == "queued",
                "media_type": blob.media_type if blob else "", "file_url": f"/api/v1/primary/inbox/{item.id}/file"
                if blob else ""})
    return rec


def confirm_item(db: Session, user: User, item: PrimaryMailItem, draft: dict, company_id: str | None) -> dict:
    """Сверенный бухгалтером черновик из почты или бота → документ реестра (подпись черновика выдаёт сервер сам)."""
    if item.result != "draft":
        raise MailboxError("Этот черновик уже разобран", 409)
    source = item.kind if item.kind in ("pdf", "scan") else "pdf"
    payload = {"source": source, "token": service.sign(user.id, item.file_sha256, source),
               "file_name": item.file_name, "file_sha256": item.file_sha256,
               "company_id": company_id or item.company_id, "draft": draft}
    try:
        res = service.confirm(db, user, payload, channel=item.channel or "mail")
    except service.RecognizeError as exc:
        raise MailboxError(str(exc), exc.status) from exc
    item.result = "confirmed" if res["result"] == "created" else "duplicate"
    item.document_id = (res.get("document") or {}).get("id", "")
    item.decided_by, item.decided_at = user.id, utcnow()
    _drop_file(db, item.id)
    db.commit()
    return res


def dismiss_item(db: Session, user: User, item: PrimaryMailItem) -> None:
    if item.result not in ("draft", "queued"):
        raise MailboxError("Этот черновик уже разобран", 409)
    item.result = "dismissed"
    item.decided_by, item.decided_at = user.id, utcnow()
    _drop_file(db, item.id)
    db.commit()


def purge(db: Session, now: datetime | None = None) -> dict:
    """Черновики старше 30 дней — без файла («срок вышел»); журнал вложений — 365 дней."""
    now = now or utcnow()
    stale = db.query(PrimaryMailItem).filter(PrimaryMailItem.result.in_(("queued", "draft")),
                                             PrimaryMailItem.created_at < now - timedelta(days=DRAFT_DAYS)).all()
    for item in stale:
        item.result, item.message = "expired", f"Черновик не сверили за {DRAFT_DAYS} дней — файл удалён"
        _drop_file(db, item.id)
    old = db.query(PrimaryMailItem).filter(PrimaryMailItem.created_at < now - timedelta(days=LOG_DAYS))
    removed = 0
    for item in old.all():
        _drop_file(db, item.id)
        db.delete(item)
        removed += 1
    db.commit()
    return {"expired": len(stale), "removed": removed}
