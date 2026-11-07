"""Первичка: фото и PDF документов от сотрудников через ботов Telegram и MAX (T-64, v1.70.105, ADR-35).

Сотрудник на объекте снимает бумажный УПД или накладную и отправляет боту, к которому привязал свой профиль
(«Настройки» → Telegram или MAX → код). Файл попадает в ту же очередь, что и вложения из почтового ящика (T-63,
ADR-34): распознавание на своём сервере по одному файлу за раз → черновик в «Ждут сверки» у бухгалтера компании
сотрудника → бухгалтер сверяет и сохраняет. Ни одно поле не попадает в реестр без человека.

Правила:
- принимаем только от привязанных и активных пользователей, у которых есть компания: чужие люди в бот документы
  не положат, а документ всегда попадает в компанию сотрудника и виден только её бухгалтерам;
- только PDF и картинки JPG, PNG, TIFF (по сигнатуре файла, не по имени), не больше 20 МБ (предел скачивания
  у Telegram Bot API) и не меньше 15 КБ для картинок;
- тот же файл повторно не принимается (сравнение по SHA-256 в компании — и с письмами тоже);
- не больше 30 документов от одного сотрудника за сутки — распознавание тяжёлое, сервер небольшой;
- ссылки на файлы MAX скачиваются только по https и только с публичных адресов (защита от SSRF).
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from ..models import Company, User, utcnow
from . import mailbox
from .models import PrimaryMailFile, PrimaryMailItem

CHANNELS = {"mail": "почта", "telegram": "Telegram", "max": "MAX"}
BOT_CHANNELS = ("telegram", "max")
MAX_BYTES = 20 * 1024 * 1024
DAILY_LIMIT = 30
TIMEOUT = 60
MAX_REDIRECTS = 3
BIND_HINT = {"telegram": "«Настройки» → Telegram → код → отправьте боту /start КОД",
             "max": "«Настройки» → MAX → код → отправьте боту /start КОД"}


class BotFileError(ValueError):
    """Файл из мессенджера не скачался: текст — для ответа сотруднику в боте."""


def sender_label(user: User) -> str:
    return (user.full_name or user.username or "сотрудник")[:200]


def not_linked_text(channel: str) -> str:
    return ("📄 Фото документов (УПД, накладные) принимаются от сотрудников компаний, привязавших бота: "
            + BIND_HINT.get(channel, BIND_HINT["telegram"]) + ".")


def file_name_for(channel: str, name: str | None, data: bytes) -> str:
    """Имя для журнала: своё имя файла, если есть, иначе «telegram-фото.jpg» по типу содержимого."""
    if name:
        return name.strip()[:255]
    ext = {"application/pdf": ".pdf", "image/png": ".png", "image/tiff": ".tif"}.get(mailbox.media_type(data), ".jpg")
    return f"{channel}-фото{ext}"


def accept(db: Session, user: User, data: bytes, file_name: str, caption: str, channel: str,
           ref: str = "") -> dict:
    """Файл от сотрудника → очередь распознавания. Возвращает {ok, result, message[, item_id]}.

    message — готовый ответ сотруднику в боте; result: queued | duplicate | no_company | type | small | big | limit.
    """
    if channel not in BOT_CHANNELS:
        raise ValueError("неизвестный канал")
    company = db.get(Company, user.company_id) if user.company_id else None
    if company is None or not company.is_active:
        return {"ok": False, "result": "no_company",
                "message": "Документы принимаются от сотрудников компаний: попросите администратора добавить "
                           "вас в компанию в программе."}
    kind = mailbox.attachment_kind(file_name, data)
    if kind not in ("pdf", "image"):
        return {"ok": False, "result": "type",
                "message": "🤔 Принимаются фото документа (JPG, PNG, TIFF) и PDF. Снимите УПД или накладную "
                           "целиком и пришлите сюда."}
    if len(data) > MAX_BYTES:
        return {"ok": False, "result": "big", "message": "📦 Файл больше 20 МБ — пришлите фото или PDF поменьше."}
    if kind == "image" and len(data) < mailbox.MIN_IMAGE_BYTES:
        return {"ok": False, "result": "small",
                "message": "🔍 Фото слишком маленькое — снимите документ целиком и крупнее, при хорошем свете."}
    sha = hashlib.sha256(data).hexdigest()
    if mailbox._seen(db, company.id, sha):
        return {"ok": True, "result": "duplicate", "message": "ℹ️ Этот файл уже получен — бухгалтер его видит."}
    day_ago = utcnow() - timedelta(days=1)
    today = db.query(PrimaryMailItem.id).filter(PrimaryMailItem.user_id == user.id,
                                                PrimaryMailItem.created_at >= day_ago).count()
    if today >= DAILY_LIMIT:
        return {"ok": False, "result": "limit",
                "message": f"🚦 За сутки от вас принято уже {DAILY_LIMIT} документов — остальные пришлите завтра "
                           "или передайте бухгалтеру."}
    item = PrimaryMailItem(company_id=company.id, channel=channel, user_id=user.id, sender=sender_label(user),
                           subject=(caption or "").strip()[:300], message_id=ref[:255], received_at=utcnow(),
                           file_name=file_name[:255], file_sha256=sha, size=len(data),
                           kind="pdf" if kind == "pdf" else "scan", result="queued")
    db.add(item)
    db.flush()
    db.add(PrimaryMailFile(item_id=item.id, media_type=mailbox.media_type(data), data=data))
    db.commit()
    name = company.short_name or company.name
    return {"ok": True, "result": "queued", "item_id": item.id, "company": name,
            "message": f"📄 Документ получен — бухгалтер «{name}» сверит черновик в «Первичке». "
                       "Многостраничный документ лучше присылать одним PDF."}


# ------------------------------------------------------------------ скачивание файла MAX по ссылке
def _public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BotFileError("⚠️ Не удалось получить файл — пришлите ещё раз.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise BotFileError("⚠️ Ссылка на файл ведёт во внутреннюю сеть — файл не принят.")


def download_public(url: str, limit: int = MAX_BYTES) -> bytes:
    """Скачать файл по https-ссылке мессенджера: только публичные адреса, до трёх переадресаций, не больше limit."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url or "")
        if parts.scheme != "https" or not parts.hostname:
            raise BotFileError("⚠️ Не удалось получить файл — пришлите ещё раз.")
        _public(parts.hostname)
        try:
            with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=False) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise BotFileError("⚠️ Не удалось получить файл — пришлите ещё раз.")
                size = int(resp.headers.get("content-length") or 0)
                if size > limit:
                    raise BotFileError("📦 Файл больше 20 МБ — пришлите фото или PDF поменьше.")
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise BotFileError("📦 Файл больше 20 МБ — пришлите фото или PDF поменьше.")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise BotFileError("⚠️ Не удалось получить файл — пришлите ещё раз.") from exc
    raise BotFileError("⚠️ Не удалось получить файл — пришлите ещё раз.")
