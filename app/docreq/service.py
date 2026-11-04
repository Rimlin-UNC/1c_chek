"""«Что нужно от клиента» (T-58, v1.70.97): недостающие документы, срок сдачи, напоминания, журнал.

Главная боль бухгалтера на аутсорсе — превращение в «службу сбора первички»: клиент уверен, что
«всё отправил», документы приходят в последний день, а напоминания вручную не работают (Клерк,
Контур — docs/primary_docs_design.md, шаг 2). Здесь:

* **Программа сама находит, чего не хватает за месяц:** поставщики, которые присылали документы
  два из трёх прошлых месяцев, а в этом — нет; чеки, не подтверждённые ФНС (адресно — тому, кто
  их сканировал); УПД с ошибками — нужен исправленный документ. Бухгалтер добавляет и свои запросы.
* **Пришедший документ закрывает запрос сам:** УПД от ожидаемого поставщика, подтверждённый чек,
  исправление документа.
* **Срок сдачи** — число следующего месяца (по умолчанию 5-е) и «час икс» (по умолчанию 18:00 МСК).
* **Напоминания** — за 3, 1 и 0 дней до срока и повторы после срока, по уже подключённым каналам
  (Telegram, MAX, почта). Каждое сообщение — в журнал с текстом: письменный след на случай
  «я всё отправил». Автонапоминания включает бухгалтер: без его решения программа клиентам не пишет.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

from sqlalchemy.orm import Session

from ..models import Company, Receipt, User, utcnow
from ..primary.models import PrimaryDocument
from .models import DocRequest, DocRequestEvent, DocRequestSettings

log = logging.getLogger("ymaster.docreq")
MSK = timezone(timedelta(hours=3))            # Москва без перехода на летнее время с 2014 года
REMIND_AT = time(10, 0)                       # плановые напоминания — в 10:00 по Москве
WINDOW = timedelta(hours=20)                  # не отправлено в срок (сервер стоял) — ещё 20 часов
KINDS = {"expected": "ожидается по истории", "receipt": "чек не подтверждён ФНС",
         "upd_fix": "нужен исправленный документ", "payment": "платёж без документов",   # v1.70.98: T-45
         "manual": "запрос бухгалтера"}
PERSONAL = ("receipt", "payment")             # личные запросы: чек сотрудника, покупка по его карте
STATUSES = ("open", "received", "cancelled")
CHANNELS = ("telegram", "max", "email")
ROLES = ("manager", "user")
MAX_ITEMS = 25                                # в одном сообщении — не больше 25 строк
DEFAULTS = {"enabled": False, "auto_detect": True, "deadline_day": 5, "x_hour": "18:00", "remind_days": "3,1,0",
            "repeat_every": 2, "repeat_max": 3, "channels": "telegram,max,email", "roles": "manager",
            "contact_email": ""}
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь")
MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря")
_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_HOUR = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_send_lock = threading.Lock()


class DocRequestError(ValueError):
    """Ошибка ввода — сообщение для пользователя (status — код ответа API)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ периоды и срок сдачи
def parse_period(period: str) -> tuple[int, int]:
    found = _PERIOD.match(period or "")
    if not found:
        raise DocRequestError("Период — месяц в виде ГГГГ-ММ, например 2026-10")
    return int(found[1]), int(found[2])


def shift_period(period: str, months: int) -> str:
    year, month = parse_period(period)
    index = year * 12 + month - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def period_start(period: str) -> date:
    year, month = parse_period(period)
    return date(year, month, 1)


def period_end(period: str) -> date:
    """Первый день следующего месяца (граница не входит в период)."""
    return period_start(shift_period(period, 1))


def period_title(period: str) -> str:
    year, month = parse_period(period)
    return f"{MONTHS[month - 1]} {year}"


def now_msk() -> datetime:
    return datetime.now(MSK)


def default_period(now: datetime | None = None) -> str:
    """Отчётный месяц по умолчанию — прошлый: в начале месяца бухгалтер закрывает предыдущий."""
    current = (now or now_msk()).astimezone(MSK)
    return shift_period(f"{current.year:04d}-{current.month:02d}", -1)


def human_moment(moment: datetime) -> str:
    moment = moment.astimezone(MSK)
    return f"{moment.day} {MONTHS_GEN[moment.month - 1]} {moment.year}, {moment:%H:%M}"


# ------------------------------------------------------------------ настройки компании
def settings_of(db: Session, company_id: str) -> DocRequestSettings:
    """Настройки компании; строки нет — значения по умолчанию (объект в базу не добавляется)."""
    row = db.get(DocRequestSettings, company_id)
    if row is not None:
        return row
    return DocRequestSettings(company_id=company_id, **DEFAULTS)


def _days(s: DocRequestSettings) -> list[int]:
    out = sorted({int(x) for x in (s.remind_days or "").split(",") if x.strip().isdigit()}, reverse=True)
    return [d for d in out if 0 <= d <= 10][:5]


def _list(value: str, allowed: tuple[str, ...]) -> list[str]:
    return [x for x in allowed if x in {v.strip() for v in (value or "").split(",")}]


def settings_dict(s: DocRequestSettings) -> dict:
    return {"company_id": s.company_id, "enabled": bool(s.enabled), "auto_detect": bool(s.auto_detect),
            "deadline_day": s.deadline_day, "x_hour": s.x_hour, "remind_days": _days(s),
            "repeat_every": s.repeat_every, "repeat_max": s.repeat_max, "channels": _list(s.channels, CHANNELS),
            "roles": _list(s.roles, ROLES), "contact_email": s.contact_email or "",
            "updated_at": s.updated_at.isoformat() if s.updated_at else None}


def save_settings(db: Session, company_id: str, data: dict, user: User) -> DocRequestSettings:
    """Проверить и сохранить настройки компании (всё или ничего)."""
    day = int(data.get("deadline_day", 5))
    if not 1 <= day <= 28:
        raise DocRequestError("Срок сдачи — число месяца от 1 до 28")
    hour = str(data.get("x_hour", "18:00")).strip()
    if not _HOUR.match(hour):
        raise DocRequestError("«Час икс» — время в виде ЧЧ:ММ, например 18:00")
    days = data.get("remind_days", [3, 1, 0])
    if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 10 for d in days) or len(days) > 5:
        raise DocRequestError("Напоминать до срока можно за 0–10 дней, не больше пяти раз")
    every, times = int(data.get("repeat_every", 2)), int(data.get("repeat_max", 3))
    if not 1 <= every <= 14 or not 0 <= times <= 10 or every * times > 30:
        raise DocRequestError("После срока — повтор раз в 1–14 дней, не больше 10 раз и не дольше 30 дней")
    channels, roles = data.get("channels", []), data.get("roles", [])
    if not set(channels) <= set(CHANNELS) or not set(roles) <= set(ROLES):
        raise DocRequestError("Неизвестный канал или роль получателя")
    email = str(data.get("contact_email", "") or "").strip()
    if email and (len(email) > 200 or not _EMAIL.match(email)):
        raise DocRequestError("Почта для запросов указана с ошибкой")
    row = db.get(DocRequestSettings, company_id) or DocRequestSettings(company_id=company_id)
    row.enabled = bool(data.get("enabled", False))
    row.auto_detect = bool(data.get("auto_detect", True))
    row.deadline_day, row.x_hour = day, hour
    row.remind_days = ",".join(str(d) for d in sorted(set(days), reverse=True))
    row.repeat_every, row.repeat_max = every, times
    row.channels = ",".join(c for c in CHANNELS if c in channels)
    row.roles = ",".join(r for r in ROLES if r in roles)
    row.contact_email = email
    row.updated_at = utcnow()
    row.updated_by = user.id
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------------ расписание напоминаний
def deadline(period: str, s: DocRequestSettings) -> datetime:
    """Срок сдачи документов за период: число следующего месяца и «час икс», по Москве."""
    nxt = period_start(shift_period(period, 1))
    hour, minute = (int(x) for x in s.x_hour.split(":"))
    return datetime(nxt.year, nxt.month, min(s.deadline_day, 28), hour, minute, tzinfo=MSK)


def schedule(period: str, s: DocRequestSettings) -> list[tuple[str, datetime]]:
    """Плановые напоминания: [(метка, когда)] — D-3, D-1, D0 до срока и D+2, D+4 … после."""
    due = deadline(period, s)
    out = []
    for days in _days(s):
        if days == 0:      # в день срока — утром, но не позже чем за час до «часа икс»
            at = min(datetime.combine(due.date(), REMIND_AT, MSK), due - timedelta(hours=1))
            out.append(("D0", at))
        else:
            out.append((f"D-{days}", datetime.combine(due.date() - timedelta(days=days), REMIND_AT, MSK)))
    for k in range(1, max(0, s.repeat_max) + 1):
        days = k * max(1, s.repeat_every)
        out.append((f"D+{days}", datetime.combine(due.date() + timedelta(days=days), REMIND_AT, MSK)))
    return sorted(out, key=lambda item: item[1])


def due_slot(period: str, s: DocRequestSettings, now: datetime, sent: set[str]) -> str | None:
    """Наступившее и ещё не отправленное напоминание. Пропущенные из-за простоя сервера старые
    напоминания пачкой не догоняются: берётся только самое свежее в пределах окна."""
    due = [slot for slot, at in schedule(period, s) if at <= now < at + WINDOW]
    if not due or due[-1] in sent:
        return None
    return due[-1]


# ------------------------------------------------------------------ запросы
def _money(value: float | None) -> str:
    if value is None:
        return ""
    whole, frac = f"{value:,.2f}".split(".")
    return f"{whole.replace(',', ' ')},{frac} ₽"


def _event(db: Session, req: DocRequest | None, kind: str, *, company_id: str = "", period: str = "",
           actor_id: str = "", text: str = "", slot: str = "", channel: str = "", recipient: str = "",
           ok: bool = True) -> DocRequestEvent:
    event = DocRequestEvent(company_id=req.company_id if req else company_id, period=req.period if req else period,
                            request_id=req.id if req else None, kind=kind, slot=slot, channel=channel,
                            recipient=recipient[:200], actor_id=actor_id or "", ok=ok, text=text)
    db.add(event)
    return event


def _new(db: Session, company_id: str, period: str, kind: str, title: str, *, actor_id: str = "",
         counterparty_name: str = "", counterparty_inn: str = "", amount: float | None = None, ref_type: str = "",
         ref_id: str = "", assignee_id: str | None = None) -> DocRequest:
    req = DocRequest(company_id=company_id, period=period, kind=kind, title=title[:500],
                     counterparty_name=counterparty_name[:500], counterparty_inn=counterparty_inn[:12],
                     amount=amount, ref_type=ref_type, ref_id=ref_id[:64], assignee_id=assignee_id,
                     created_by=actor_id or "")
    db.add(req)
    db.flush()
    _event(db, req, "created", actor_id=actor_id, text=f"{KINDS[kind]}: {req.title}")
    return req


def _first_error(doc: PrimaryDocument) -> str:
    try:
        issues = json.loads(doc.issues_json or "[]")
    except ValueError:
        return ""
    return next((i.get("message", "") for i in issues if isinstance(i, dict) and i.get("level") == "error"), "")


def _doc_label(doc: PrimaryDocument) -> str:
    day = doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else "без даты"
    return f"{doc.kind} № {doc.doc_number or 'без номера'} от {day}"


def detect(db: Session, company_id: str, period: str, actor_id: str = "") -> dict:
    """Найти недостающее за период. Повторный поиск дублей не создаёт, отменённое не возвращает."""
    start, end = period_start(period), period_end(period)
    known = {(r.kind, r.ref_type, r.ref_id) for r in
             db.query(DocRequest).filter(DocRequest.company_id == company_id, DocRequest.period == period)}
    created = 0

    def add(kind: str, title: str, ref_type: str, ref_id: str, **extra) -> None:
        nonlocal created
        if (kind, ref_type, ref_id) in known:
            return
        known.add((kind, ref_type, ref_id))
        _new(db, company_id, period, kind, title, actor_id=actor_id, ref_type=ref_type, ref_id=ref_id, **extra)
        created += 1

    # 1. Регулярные поставщики: документы в двух из трёх прошлых месяцев, а в этом месяце — нет
    history = (db.query(PrimaryDocument)
               .filter(PrimaryDocument.company_id == company_id, PrimaryDocument.seller_inn != "",
                       PrimaryDocument.doc_date >= period_start(shift_period(period, -3)),
                       PrimaryDocument.doc_date < end).all())
    sellers: dict[str, dict] = {}
    for doc in history:
        info = sellers.setdefault(doc.seller_inn, {"months": set(), "now": False, "names": [], "sums": []})
        if doc.doc_date >= start:
            info["now"] = True
            continue
        info["months"].add(doc.doc_date.strftime("%Y-%m"))
        info["names"].append(doc.seller_name)
        if doc.total_with_vat:
            info["sums"].append(doc.total_with_vat)
    for inn, info in sorted(sellers.items()):
        if len(info["months"]) >= 2 and not info["now"]:
            name = next((n for n in reversed(info["names"]) if n), f"ИНН {inn}")
            add("expected", f"Документ от {name} за {period_title(period)} (присылает каждый месяц)", "seller", inn,
                counterparty_name=name, counterparty_inn=inn,
                amount=round(median(info["sums"]), 2) if info["sums"] else None)
    # 2. Чеки, не подтверждённые ФНС, — напоминание тому, кто сканировал
    receipts = (db.query(Receipt)
                .filter(Receipt.company_id == company_id, Receipt.fns_status.in_(("invalid", "not_found")),
                        Receipt.receipt_date >= datetime.combine(start, time.min),
                        Receipt.receipt_date < datetime.combine(end, time.min))
                .order_by(Receipt.receipt_date).all())
    for rec in receipts:
        why = "не найден в ФНС" if rec.fns_status == "not_found" else "ФНС считает недействительным"
        add("receipt", f"Чек на {_money(rec.total_sum)} от {rec.receipt_date:%d.%m.%Y} {why} — нужен бумажный "
            f"чек или его фото", "receipt", rec.id, amount=rec.total_sum, assignee_id=rec.created_by,
            counterparty_name=getattr(rec, "merchant_name", "") or "")
    # 3. УПД и счета-фактуры с ошибками проверки — нужен исправленный документ
    broken = (db.query(PrimaryDocument)
              .filter(PrimaryDocument.company_id == company_id, PrimaryDocument.status == "errors",
                      PrimaryDocument.doc_date >= start, PrimaryDocument.doc_date < end)
              .order_by(PrimaryDocument.doc_date).all())
    for doc in broken:
        error = _first_error(doc)
        add("upd_fix", f"Исправленный документ вместо {_doc_label(doc)} от {doc.seller_name or 'продавца'}"
            + (f": {error}" if error else ""), "primary", doc.id, counterparty_name=doc.seller_name,
            counterparty_inn=doc.seller_inn, amount=doc.total_with_vat)
    # 4. v1.70.98 (T-45): платежи из банковской выписки без документов — оплата поставщику ждёт УПД, накладную
    #    или акт, покупка по карте — чек; карта сотрудника указана в настройках — запрос ему лично
    from ..bank import service as bank
    for op in bank.open_operations(db, company_id, start, end):
        add("payment", bank.request_title(op), "bank_op", op.id, counterparty_name=op.counterparty_name,
            counterparty_inn=op.counterparty_inn, amount=op.amount,
            assignee_id=bank.card_owner_id(db, company_id, op.card_tail) or None)
    db.commit()
    return {"created": created, "closed": refresh(db, company_id, period)}


def add_manual(db: Session, company_id: str, period: str, data: dict, user: User) -> DocRequest:
    title = " ".join(str(data.get("title") or "").split())
    if len(title) < 3:
        raise DocRequestError("Опишите, какой документ нужен")
    amount = data.get("amount")
    if amount is not None and (not isinstance(amount, (int, float)) or not 0 <= amount < 1e13):
        raise DocRequestError("Сумма указана с ошибкой")
    req = _new(db, company_id, period, "manual", title, actor_id=user.id,
               counterparty_name=" ".join(str(data.get("counterparty_name") or "").split()),
               counterparty_inn="".join(ch for ch in str(data.get("counterparty_inn") or "") if ch.isdigit())[:12],
               amount=float(amount) if amount is not None else None)
    db.commit()
    return req


def set_status(db: Session, req: DocRequest, status: str, user: User | None, note: str = "") -> DocRequest:
    """Получено, отменено или снова открыто — с записью в журнал."""
    if status not in STATUSES:
        raise DocRequestError("Неизвестный статус")
    if req.status == status:
        return req
    req.status = status
    req.closed_at = None if status == "open" else utcnow()
    req.closed_by = "" if status == "open" or user is None else user.id
    req.close_note = "" if status == "open" else note[:500]
    kind = {"open": "reopened", "received": "received", "cancelled": "cancelled"}[status]
    _event(db, req, kind, actor_id=user.id if user else "", text=note or req.title)
    db.commit()
    return req


def _satisfied(db: Session, req: DocRequest) -> str:
    """Почему запрос можно закрыть сам (пусто — ещё нет)."""
    if req.kind == "expected" and req.ref_type == "seller":
        doc = (db.query(PrimaryDocument)
               .filter(PrimaryDocument.company_id == req.company_id, PrimaryDocument.seller_inn == req.ref_id,
                       PrimaryDocument.doc_date >= period_start(req.period),
                       PrimaryDocument.doc_date < period_end(req.period))
               .order_by(PrimaryDocument.created_at).first())
        return f"получен {_doc_label(doc)}" if doc else ""
    if req.kind == "receipt":
        rec = db.get(Receipt, req.ref_id)
        if rec is None:
            return "чек удалён из реестра"
        return "чек подтверждён ФНС" if rec.fns_status == "valid" else ""
    if req.kind == "upd_fix":
        doc = db.get(PrimaryDocument, req.ref_id)
        if doc is None:
            return "документ удалён из реестра"
        fixed = (db.query(PrimaryDocument)
                 .filter(PrimaryDocument.company_id == req.company_id, PrimaryDocument.seller_inn == doc.seller_inn,
                         PrimaryDocument.doc_number == doc.doc_number, PrimaryDocument.correction_number != "",
                         PrimaryDocument.status != "errors")
                 .order_by(PrimaryDocument.created_at.desc()).first())
        if fixed:
            when = f" от {fixed.correction_date:%d.%m.%Y}" if fixed.correction_date else ""
            return f"получено исправление № {fixed.correction_number}{when}"
        return "ошибок в документе больше нет" if doc.status != "errors" else ""
    if req.kind == "payment" and req.ref_type == "bank_op":      # v1.70.98: пришёл чек или документ
        from ..bank import service as bank
        return bank.satisfied(db, req.ref_id)
    return ""


def refresh(db: Session, company_id: str, period: str) -> int:
    """Закрыть запросы, документы по которым уже пришли. Возвращает число закрытых."""
    closed = 0
    for req in db.query(DocRequest).filter(DocRequest.company_id == company_id, DocRequest.period == period,
                                           DocRequest.status == "open").all():
        note = _satisfied(db, req)
        if note:
            set_status(db, req, "received", None, f"Закрыт сам: {note}")
            closed += 1
    return closed


def brief(req: DocRequest, users: dict[str, User] | None = None) -> dict:
    who = (users or {}).get(req.assignee_id or "")
    return {"id": req.id, "company_id": req.company_id, "period": req.period, "kind": req.kind,
            "kind_title": KINDS.get(req.kind, req.kind), "title": req.title,
            "counterparty_name": req.counterparty_name, "counterparty_inn": req.counterparty_inn,
            "amount": req.amount, "ref_type": req.ref_type, "ref_id": req.ref_id,
            "assignee": (who.full_name or who.username) if who else "", "status": req.status,
            "auto": not req.created_by, "created_at": req.created_at.isoformat() if req.created_at else None,
            "closed_at": req.closed_at.isoformat() if req.closed_at else None, "close_note": req.close_note}


def event_dict(event: DocRequestEvent, users: dict[str, User] | None = None) -> dict:
    actor = (users or {}).get(event.actor_id or "")
    return {"id": event.id, "request_id": event.request_id, "at": event.at.isoformat() if event.at else None,
            "kind": event.kind, "slot": event.slot, "channel": event.channel, "recipient": event.recipient,
            "actor": (actor.full_name or actor.username) if actor else ("программа" if not event.actor_id else ""),
            "ok": event.ok, "text": event.text}


# ------------------------------------------------------------------ каналы
def channel_status(db: Session) -> dict[str, dict]:
    """Какие каналы подключены в программе (настраивает администратор)."""
    from ..pool import mailer
    from ..services import appsettings, max_bot, telegram_bot
    tg = appsettings.get_setting(db, telegram_bot.SETT, "0") == "1" and bool(telegram_bot.get_token(db))
    mx = appsettings.get_setting(db, max_bot.SETT, "0") == "1" and bool(max_bot.get_token(db))
    email = mailer.smtp_configured(db)
    return {"telegram": {"ready": tg, "hint": "" if tg else "бот Telegram не включён в настройках"},
            "max": {"ready": mx, "hint": "" if mx else "бот MAX не включён в настройках"},
            "email": {"ready": email, "hint": "" if email else "почта (SMTP) не настроена"}}


def _send_telegram(db: Session, target: str, text: str, _subject: str) -> tuple[bool, str]:
    from ..services import telegram_bot
    try:
        telegram_bot.send_message(telegram_bot.get_token(db), target, text, proxy=telegram_bot.get_proxy(db),
                                  base=telegram_bot.get_api_base(db))
        return True, ""
    except Exception as exc:                                     # noqa: BLE001
        return False, exc.__class__.__name__


def _send_max(db: Session, target: str, text: str, _subject: str) -> tuple[bool, str]:
    from ..services import max_bot
    try:
        max_bot.send_message(max_bot.get_token(db), target, text)
        return True, ""
    except Exception as exc:                                     # noqa: BLE001
        return False, exc.__class__.__name__


def _send_email(db: Session, target: str, text: str, subject: str) -> tuple[bool, str]:
    from ..pool import mailer
    return mailer.send_mail(db, target, subject, text, from_name="Ямастер Чек")


SENDERS = {"telegram": _send_telegram, "max": _send_max, "email": _send_email}


# ------------------------------------------------------------------ напоминания
def message(company: Company, period: str, due: datetime, items: list[DocRequest], now: datetime) -> str:
    """Текст напоминания: что нужно, к какому сроку; после срока — без упрёков, но с датой."""
    name = company.short_name or company.name
    lines = [f"{name}: для закрытия месяца ({period_title(period)}) бухгалтеру нужны документы."]
    if now < due:
        lines.append(f"Срок сдачи — {human_moment(due)} (МСК).")
    else:
        lines.append(f"Срок сдачи прошёл ({human_moment(due)}, МСК) — пришлите, пожалуйста, как можно скорее.")
    for i, req in enumerate(items[:MAX_ITEMS], 1):
        about = f" — обычно около {_money(req.amount)}" if req.kind == "expected" and req.amount else ""
        lines.append(f"{i}. {req.title}{about}")
    if len(items) > MAX_ITEMS:
        lines.append(f"…и ещё {len(items) - MAX_ITEMS}: полный список — у бухгалтера.")
    lines.append("Документы можно прислать бухгалтеру фото, сканом или файлом из ЭДО; чеки — через сканер "
                 "в программе. Это автоматическое напоминание «Ямастер Чек».")
    return "\n".join(lines)


def plan(db: Session, company_id: str, period: str, s: DocRequestSettings | None = None) -> list[dict]:
    """Кто что получит: личные запросы (чек сотрудника) — ему; общие — по ролям и на почту компании."""
    s = s or settings_of(db, company_id)
    items = (db.query(DocRequest)
             .filter(DocRequest.company_id == company_id, DocRequest.period == period, DocRequest.status == "open")
             .order_by(DocRequest.created_at).all())
    users = (db.query(User).filter(User.company_id == company_id, User.is_active.is_(True))
             .order_by(User.username).all())
    active = {u.id for u in users}
    personal: dict[str, list[DocRequest]] = {}
    common: list[DocRequest] = []
    for req in items:
        # чек сотрудника и покупка по его карте — ему лично; сотрудника в компании больше нет — руководителю
        if req.kind in PERSONAL and req.assignee_id in active:
            personal.setdefault(req.assignee_id, []).append(req)
        else:
            common.append(req)
    roles = _list(s.roles, ROLES)
    out = []
    for user in users:
        shared = common if user.role in roles else []
        mine = shared + personal.pop(user.id, [])
        if mine:
            out.append({"user": user, "items": mine, "common": len(shared)})
    channels = _list(s.channels, CHANNELS)
    if "email" in channels and s.contact_email and common:
        out.append({"email": s.contact_email, "items": common, "common": len(common)})
    return out


def _orphans(db: Session, company_id: str, period: str, entries: list[dict], channels: list[str],
             ready: dict[str, dict]) -> str:
    """Общие запросы, которые некому доставить: нет руководителя с каналом и нет почты клиента."""
    total = (db.query(DocRequest)
             .filter(DocRequest.company_id == company_id, DocRequest.period == period, DocRequest.status == "open")
             .count()) - sum(len(e["items"]) - e.get("common", 0) for e in entries)
    if total <= 0 or any(e.get("common") and _targets(e, channels, ready) for e in entries):
        return ""
    return (f"Общие запросы ({total}) сейчас некому отправить: у получателей нет привязанного Telegram или MAX, "
            "а почта клиента не задана. Укажите почту в «Срок и напоминания» или попросите руководителя "
            "привязать бота в своих настройках")


def _targets(entry: dict, channels: list[str], ready: dict[str, dict]) -> list[tuple[str, str, str]]:
    """[(канал, адрес, как показать в журнале)] для одного получателя."""
    if "email" in entry:
        return [("email", entry["email"], entry["email"])] if ready["email"]["ready"] else []
    user: User = entry["user"]
    label = f"{user.full_name or user.username} ({user.username})"
    out = []
    if "telegram" in channels and ready["telegram"]["ready"] and user.telegram_chat_id:
        out.append(("telegram", user.telegram_chat_id, label))
    if "max" in channels and ready["max"]["ready"] and user.max_user_id:
        out.append(("max", user.max_user_id, label))
    return out


def preview(db: Session, company_id: str, period: str, now: datetime | None = None) -> dict:
    """Кому и что уйдёт при напоминании сейчас — без отправки."""
    s = settings_of(db, company_id)
    company = db.get(Company, company_id)
    now = now or now_msk()
    ready = channel_status(db)
    channels = _list(s.channels, CHANNELS)
    out = []
    for entry in plan(db, company_id, period, s):
        targets = _targets(entry, channels, ready)
        who = entry.get("email") or f"{entry['user'].full_name or entry['user'].username} ({entry['user'].username})"
        out.append({"recipient": who, "items": len(entry["items"]), "channels": [t[0] for t in targets],
                    "text": message(company, period, deadline(period, s), entry["items"], now)})
    entries = plan(db, company_id, period, s)
    return {"recipients": out, "channels": ready, "warning": _orphans(db, company_id, period, entries, channels, ready)}


def send_reminders(db: Session, company_id: str, period: str, slot: str, actor_id: str = "",
                   now: datetime | None = None) -> dict:
    """Отправить напоминание по всем подключённым каналам и записать каждое сообщение в журнал."""
    with _send_lock:
        now = now or now_msk()
        s = settings_of(db, company_id)
        company = db.get(Company, company_id)
        refresh(db, company_id, period)
        entries = plan(db, company_id, period, s)
        if not entries:
            _event(db, None, "reminder", company_id=company_id, period=period, slot=slot, actor_id=actor_id,
                   text="Открытых запросов нет — напоминание не нужно")
            db.commit()
            return {"sent": 0, "failed": 0, "note": "Открытых запросов нет — напоминать не о чем", "warning": ""}
        ready = channel_status(db)
        channels = _list(s.channels, CHANNELS)
        due = deadline(period, s)
        sent = failed = 0
        for entry in entries:
            text = message(company, period, due, entry["items"], now)
            subject = f"Документы для бухгалтера: {period_title(period)}"
            for channel, address, label in _targets(entry, channels, ready):
                ok, error = SENDERS[channel](db, address, text, subject)
                _event(db, None, "reminder", company_id=company_id, period=period, slot=slot, channel=channel,
                       recipient=label, actor_id=actor_id, ok=ok,
                       text=text if ok else f"Не доставлено ({error}).\n{text}")
                sent, failed = sent + ok, failed + (not ok)
        if not sent and not failed:
            _event(db, None, "reminder", company_id=company_id, period=period, slot=slot, actor_id=actor_id, ok=False,
                   text="Не отправлено: у получателей нет подключённых каналов (Telegram, MAX) и не задана почта "
                        "компании — или эти каналы выключены в настройках")
        orphans = _orphans(db, company_id, period, entries, channels, ready)
        if orphans and (sent or failed):
            _event(db, None, "reminder", company_id=company_id, period=period, slot=slot, actor_id=actor_id,
                   ok=False, text=orphans)
        db.commit()
        note = "" if sent or failed else "Не отправлено: нет подключённых каналов у получателей"
        return {"sent": sent, "failed": failed, "note": note, "warning": orphans}


def sent_slots(db: Session, company_id: str, period: str) -> set[str]:
    rows = (db.query(DocRequestEvent.slot)
            .filter(DocRequestEvent.company_id == company_id, DocRequestEvent.period == period,
                    DocRequestEvent.kind == "reminder", DocRequestEvent.slot != "manual").distinct())
    return {slot for (slot,) in rows}


def tick(db: Session, now: datetime | None = None, company_id: str | None = None) -> list[dict]:
    """Раз в минуту: компаниям с включёнными напоминаниями — плановые напоминания. Смотрятся текущий и два
    прошлых месяца: напоминание «за 10 дней» при сроке 1-го числа приходится ещё на отчётный месяц, а
    повторы после срока (не дольше 30 дней) — на позапрошлый. company_id — только одна компания."""
    now = (now or now_msk()).astimezone(MSK)
    current = f"{now.year:04d}-{now.month:02d}"
    out = []
    query = db.query(DocRequestSettings).filter(DocRequestSettings.enabled.is_(True))
    if company_id:
        query = query.filter(DocRequestSettings.company_id == company_id)
    for s in query.all():
        company = db.get(Company, s.company_id)
        if company is None or not company.is_active:
            continue
        for period in (current, shift_period(current, -1), shift_period(current, -2)):
            slot = due_slot(period, s, now, sent_slots(db, s.company_id, period))
            if slot is None:
                continue
            if s.auto_detect:
                detect(db, s.company_id, period)
            report = send_reminders(db, s.company_id, period, slot, now=now)
            out.append({"company_id": s.company_id, "period": period, "slot": slot, **report})
            log.info("doc requests %s %s %s: %s", s.company_id, period, slot, report)
    return out


def discipline(db: Session, company_id: str, period: str) -> dict:
    """Дисциплина клиента за три месяца до периода включительно: доля документов, полученных в срок."""
    total = on_time = 0
    for back in range(3):
        p = shift_period(period, -back)
        due = deadline(p, settings_of(db, company_id)).astimezone(timezone.utc).replace(tzinfo=None)
        for req in db.query(DocRequest).filter(DocRequest.company_id == company_id, DocRequest.period == p,
                                               DocRequest.status == "received"):
            total += 1
            on_time += bool(req.closed_at and req.closed_at <= due)
    return {"received": total, "on_time": on_time, "share": round(on_time / total * 100) if total else None}
