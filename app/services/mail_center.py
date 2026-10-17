# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — Почтовый центр (v1.44.0): единый отправитель
# chek@chek.ymaster.ru, шаблоны писем (приветствие/подпись) и «бот
# рассылок» — автоматические письма по расписанию (день недели + время)
# или по условию (e-mail не подтверждён N дней, участник не сдавал
# чеки N дней), плюс адресные рассылки по списку.
# Передовые практики: HTML+текст в одном письме, отписка в один клик
# (152-ФЗ/Google-Yahoo-2024 требование для массовых писем), суточный
# антиспам-лимит на адрес, журнал с чисткой 90 дней, тёмная пауза
# правила после ошибки SMTP, маскирование адреса в результатах.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import smtplib
import time

from sqlalchemy.orm import Session

from ..services import appsettings
from ..pool.mailer import base_url, send_mail, smtp_configured

# --- настройки идентичности и шаблонов (app_settings) -----------------------
SET_FROM_NAME = "mail_from_name"       # имя отправителя
SET_GREETING = "mail_greeting"         # приветствие, {name}
SET_SIGNATURE = "mail_signature"       # подпись в конце письма
SET_BOT_ON = "mail_bot_enabled"        # «1» — бот рассылок включён
SET_SUPPRESSED = "mail_suppressed"     # JSON-список отписавшихся
LOG_RETENTION_DAYS = 90                # 152-ФЗ: журнал отправок
PER_ADDR_HOURS = 24                    # антиспам: не чаще 1 письма в сутки
                                       # на адрес в рамках правил бота

DEFAULT_FROM_NAME = "Чек-Пул Ямастер"
DEFAULT_GREETING = "Здравствуйте, {name}!"
DEFAULT_SIGNATURE = ("С уважением,\nООО «Ямастер»\nhttps://ymaster.ru · "
                     "info@ymaster.ru")

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_TITLES = {"daily": "ежедневно", "mon": "понедельник", "tue": "вторник",
              "wed": "среда", "thu": "четверг", "fri": "пятница",
              "sat": "суббота", "sun": "воскресенье"}
AUDIENCES = {
    "pool_unverified": "Участники без подтверждённого e-mail",
    "pool_inactive": "Участники, не сдававшие чеки",
    "pool_all": "Все участники с подтверждённым e-mail",
    "custom": "Список адресов (например, руководителю)",
}


# --------------------------------------------------------------------------
#  Конфиг идентичности
# --------------------------------------------------------------------------
def identity(db: Session) -> dict:
    g = appsettings.get_setting
    return {
        "from_name": g(db, SET_FROM_NAME, "") or DEFAULT_FROM_NAME,
        "greeting": g(db, SET_GREETING, "") or DEFAULT_GREETING,
        "signature": g(db, SET_SIGNATURE, "") or DEFAULT_SIGNATURE,
        "bot_on": g(db, SET_BOT_ON, "0") == "1",
    }


def render(text: str, vars_: dict) -> str:
    """Подстановки {name} {days} {points} {count} {link} {stats} —
    неизвестные плейсхолдеры остаются как есть (легко заметить опечатку)."""
    out = text or ""
    for k, v in (vars_ or {}).items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _esc(s: str) -> str:
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# --------------------------------------------------------------------------
#  HTML-упаковка (фирменный стиль: светлая тема, оранжевый дозировано)
# --------------------------------------------------------------------------
def wrap_html(title: str, greeting: str, body: str, signature: str,
              unsub_link: str = "") -> str:
    body_html = "".join(
        f"<p style='margin:0 0 12px'>{_esc(line)}</p>"
        for line in (body or "").splitlines() if line.strip())
    unsub_html = (f"<p style='margin:14px 0 0;font-size:12px;color:#8a8f98'>"
                  f"<a href='{unsub_link}' style='color:#8a8f98'>Отписаться от "
                  f"рассылок</a></p>" if unsub_link else "")
    return f"""<!doctype html><html lang="ru"><body style="margin:0;padding:0;background:#f4f6f9;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif">
<div style="max-width:560px;margin:0 auto;padding:24px 16px">
  <div style="background:#ffffff;border-radius:14px;padding:28px 26px;border:1px solid #e8ebf0">
    <div style="font-size:17px;font-weight:700;color:#20263b;margin-bottom:14px">\U0001F9EE Ямастер Чек</div>
    <div style="font-size:15px;color:#20263b;margin-bottom:12px">{_esc(greeting)}</div>
    {body_html}
    <div style="font-size:13px;color:#5a6170;white-space:pre-line;margin-top:18px">{_esc(signature)}</div>
    {unsub_html}
  </div>
  <div style="text-align:center;font-size:11px;color:#a3a9b4;padding-top:12px">
    {_esc(title)} · письмо отправлено системой «Ямастер Чек»
  </div>
</div>
</body></html>"""


# --------------------------------------------------------------------------
#  Отписка (suppression list) — обязательна для массовых писем
# --------------------------------------------------------------------------
def _secret() -> str:
    from ..config import settings
    return settings.SECRET_KEY


def unsub_sign(email: str) -> str:
    return hmac.new(_secret().encode(), email.lower().encode(),
                    hashlib.sha256).hexdigest()[:16]


def unsub_link(db: Session, email: str) -> str:
    e = email.lower()
    return f"{base_url(db)}/api/v1/public/mail/unsub?e={e}&s={unsub_sign(e)}"


def _suppressed(db: Session) -> set:
    raw = appsettings.get_setting(db, SET_SUPPRESSED, "[]")
    try:
        return {x.lower() for x in (json.loads(raw) or [])}
    except Exception:                                  # noqa: BLE001
        return set()


def suppress(db: Session, email: str) -> None:
    cur = _suppressed(db)
    cur.add(email.lower())
    appsettings.set_setting(db, SET_SUPPRESSED, json.dumps(sorted(cur)))


def unsuppress(db: Session, email: str) -> bool:
    cur = _suppressed(db)
    if email.lower() in cur:
        cur.remove(email.lower())
        appsettings.set_setting(db, SET_SUPPRESSED, json.dumps(sorted(cur)))
        return True
    return False


# --------------------------------------------------------------------------
#  Аудитории правил
# --------------------------------------------------------------------------
def _pool_stats(db: Session) -> str:
    from ..pool.models import PoolReceipt, PoolUser
    week_ago = dt.datetime.utcnow() - dt.timedelta(days=7)
    receipts = (db.query(PoolReceipt)
                  .filter(PoolReceipt.created_at >= week_ago).count())
    users = db.query(PoolUser).count()
    return f"чеков за неделю: {receipts}, участников: {users}"


def evaluate_rule(db: Session, rule):
    """Аудитория правила → список (email, переменные-подстановки)."""
    from ..pool.models import PoolReceipt, PoolUser
    now = dt.datetime.utcnow()
    supp = _suppressed(db)
    out = []
    stats = _pool_stats(db)
    if rule.condition_key == "custom":
        for raw in (rule.recipients or "").replace(";", ",").split(","):
            e = raw.strip().lower()
            if "@" in e and e not in supp:
                out.append((e, {"name": "", "days": "", "points": "",
                                "count": "", "link": "", "stats": stats}))
        return out
    rows = db.query(PoolUser).filter(PoolUser.email != "").all()
    for u in rows:
        email = (u.email or "").strip().lower()
        if not email or email in supp:
            continue
        if u.is_blocked:
            continue
        last = (db.query(PoolReceipt.created_at)
                  .filter(PoolReceipt.pool_user_id == u.id)
                  .order_by(PoolReceipt.created_at.desc()).first())
        if rule.condition_key == "pool_unverified":
            if u.email_verified:
                continue
            days = (now - (u.created_at or now)).days
            if days < (rule.cond_days or 3):
                continue
            link = f"{base_url(db)}/#/my"
            out.append((email, {"name": email.split("@")[0], "days": days,
                                "points": int(u.points or 0), "count": 0,
                                "link": link, "stats": stats}))
        elif rule.condition_key == "pool_inactive":
            if not u.email_verified:
                continue
            if last is None:
                days = (now - (u.created_at or now)).days
            else:
                days = (now - last[0]).days
            if days < (rule.cond_days or 14):
                continue
            out.append((email, {"name": email.split("@")[0], "days": days,
                                "points": int(u.points or 0),
                                "count": (db.query(PoolReceipt)
                                          .filter(PoolReceipt.pool_user_id
                                                  == u.id).count()),
                                "link": f"{base_url(db)}/#/public",
                                "stats": stats}))
        else:                                        # pool_all
            if not u.email_verified:
                continue
            out.append((email, {"name": email.split("@")[0],
                                "points": int(u.points or 0),
                                "link": f"{base_url(db)}/#/my",
                                "stats": stats}))
    return out


def _too_recent(db: Session, email: str) -> bool:
    """Антиспам: адрес получает письма правил не чаще раза в сутки."""
    from ..pool.models import MailLog
    edge = dt.datetime.utcnow() - dt.timedelta(hours=PER_ADDR_HOURS)
    return (db.query(MailLog)
              .filter(MailLog.to_email == email, MailLog.created_at >= edge,
                      MailLog.rule_id.isnot(None)).count() > 0)


def run_rule(db: Session, rule) -> dict:
    """Запуск правила: аудитория → письма (HTML+текст, с отпиской).
    Возвращает счётчики; результат пишется в журнал и last_result."""
    from ..pool.models import MailLog
    ident = identity(db)
    vars_list = evaluate_rule(db, rule)
    sent = skipped = errors = 0
    for email, vv in vars_list:
        if _too_recent(db, email):
            db.add(MailLog(rule_id=rule.id, to_email=email,
                           subject=rule.subject, status="skipped",
                           error="письмо этому адресу уже уходило за сутки"))
            skipped += 1
            continue
        name = vv.get("name") or email.split("@")[0]
        greet = render(ident["greeting"], {"name": name})
        body = render(rule.body or "", vv)
        # текстовая версия: приветствие + текст + подпись (как в HTML)
        full_text = f"{greet}\n\n{body}\n\n{ident['signature']}"
        html = wrap_html(rule.name or "Ямастер Чек", greet, body,
                         ident["signature"], unsub_link(db, email))
        ok, err = send_mail(db, email, rule.subject or rule.name, full_text,
                            html=html, from_name=ident["from_name"])
        db.add(MailLog(rule_id=rule.id, to_email=email,
                       subject=rule.subject or rule.name,
                       status="sent" if ok else "error", error=err[:190]))
        if ok:
            sent += 1
        else:
            errors += 1
    db.commit()                     # журнал фиксируем до записи результата
    rule.last_run_at = dt.datetime.utcnow()
    rule.last_result = (f"отправлено: {sent}, пропущено: {skipped}, "
                        f"ошибок: {errors}")
    db.commit()
    return {"sent": sent, "skipped": skipped, "errors": errors}


# --------------------------------------------------------------------------
#  Планировщик («бот»): тик раз в минуту из фонового цикла
# --------------------------------------------------------------------------
def tick(db: Session, now: dt.datetime | None = None) -> dict:
    """Проверить правила и выполнить те, что «должны» сработать сейчас.
    schedule: день недели + HH:MM (±1 минута, один раз в сутки);
    condition: проверка условия раз в сутки в заданное время."""
    if not smtp_configured(db) or not identity(db)["bot_on"]:
        return {"ran": []}
    now = now or dt.datetime.utcnow()
    wd = DAY_KEYS[now.weekday()]
    ran = []
    from ..pool.models import MailRule
    for rule in db.query(MailRule).filter(MailRule.enabled.is_(True)).all():
        if rule.trigger == "schedule":
            if rule.schedule != "daily" and rule.schedule != wd:
                continue
            try:
                hh, mm = (rule.hh_mm or "09:00").split(":")[:2]
                due_min = int(hh) * 60 + int(mm)
            except (ValueError, TypeError):
                continue
            now_min = now.hour * 60 + now.minute
            if abs(now_min - due_min) > 1:
                continue
            if rule.last_run_at and rule.last_run_at.date() == now.date():
                continue
        else:                                        # condition
            if rule.last_run_at and rule.last_run_at.date() == now.date():
                continue
            try:
                hh, mm = (rule.hh_mm or "09:00").split(":")[:2]
            except (ValueError, TypeError):
                hh, mm = "9", "0"
            now_min = now.hour * 60 + now.minute
            due_min = int(hh) * 60 + int(mm)
            if now_min < due_min:                    # окно с HH:MM до конца дня
                continue
        res = run_rule(db, rule)
        ran.append({"rule": rule.name, **res})
    return {"ran": ran}


# --------------------------------------------------------------------------
#  Обслуживание журнала (152-ФЗ)
# --------------------------------------------------------------------------
def purge_log(db: Session, days: int = LOG_RETENTION_DAYS) -> int:
    from ..pool.models import MailLog
    edge = dt.datetime.utcnow() - dt.timedelta(days=days)
    n = (db.query(MailLog)
           .filter(MailLog.created_at < edge)
           .delete(synchronize_session=False))
    db.commit()
    return int(n or 0)
