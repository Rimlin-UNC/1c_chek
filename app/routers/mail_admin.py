# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — Почтовый центр: админ-API (v1.44.0).
# Единый ящик отправителя (например chek@chek.ymaster.ru), пароль
# (хранится зашифрованным), тексты писем (приветствие/подпись),
# правила бота рассылок и журнал отправок.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User
from ..pool.models import MailLog, MailRule
from ..services import appsettings, mail_center
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/mail-admin", tags=["Почта"])
public_router = APIRouter(prefix="/api/v1/public/mail", tags=["Почта"])


def _mask(e: str) -> str:
    """Адрес с маскированием середины: мар***@mail.ru."""
    local, _, dom = (e or "").partition("@")
    if not dom:
        return "***"
    return f"{local[:3]}***@{dom}"


# --------------------------------------------------------------------------
#  Конфигурация: SMTP + идентичность + шаблоны
# --------------------------------------------------------------------------
@router.get("/config", summary="Почтовый центр: конфигурация (админ)")
def get_config(db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    from ..pool import mailer
    g = appsettings.get_setting
    ident = mail_center.identity(db)
    month_edge = None
    today = dt.date.today()
    month_edge = dt.datetime(today.year, today.month, 1)
    sent_month = (db.query(MailLog)
                    .filter(MailLog.status == "sent",
                            MailLog.created_at >= month_edge).count())
    err_month = (db.query(MailLog)
                   .filter(MailLog.status == "error",
                           MailLog.created_at >= month_edge).count())
    return {
        # SMTP (те же ключи, что использует кабинет пула)
        "host": g(db, mailer.SET_SMTP_HOST), "port": g(db, mailer.SET_SMTP_PORT, "587"),
        "user": g(db, mailer.SET_SMTP_USER), "has_password": bool(g(db, "smtp_pass")),
        "sender": g(db, mailer.SET_SMTP_FROM) or "chek@chek.ymaster.ru",
        "tls": g(db, mailer.SET_SMTP_TLS, "1") == "1",
        "base_url": g(db, mailer.SET_BASE_URL),
        "configured": mailer.smtp_configured(db),
        # идентичность и тексты
        "from_name": ident["from_name"], "greeting": ident["greeting"],
        "signature": ident["signature"], "bot_on": ident["bot_on"],
        # статистика
        "suppressed": len(mail_center._suppressed(db)),
        "sent_month": sent_month, "errors_month": err_month,
    }


class ConfigBody(BaseModel):
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None          # "" — не менять
    sender: str | None = None
    tls: bool | None = None
    base_url: str | None = None
    from_name: str = Field(default="", max_length=80)
    greeting: str = Field(default="", max_length=400)
    signature: str = Field(default="", max_length=600)
    bot_on: bool | None = None


@router.put("/config", summary="Почтовый центр: сохранить конфигурацию (админ)")
def put_config(body: ConfigBody, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    from ..pool import mailer
    b = body
    if b.host is not None:
        appsettings.set_setting(db, mailer.SET_SMTP_HOST, b.host.strip())
    if b.port is not None:
        appsettings.set_setting(db, mailer.SET_SMTP_PORT, str(int(b.port)))
    if b.user is not None:
        appsettings.set_setting(db, mailer.SET_SMTP_USER, b.user.strip())
    if b.password:                       # пусто — не менять
        appsettings.set_setting(db, "smtp_pass", b.password)
    if b.sender is not None:
        appsettings.set_setting(db, mailer.SET_SMTP_FROM, b.sender.strip())
    if b.tls is not None:
        appsettings.set_setting(db, mailer.SET_SMTP_TLS, "1" if b.tls else "0")
    if b.base_url is not None:
        appsettings.set_setting(db, mailer.SET_BASE_URL, b.base_url.strip())
    if b.from_name.strip():
        appsettings.set_setting(db, mail_center.SET_FROM_NAME,
                                b.from_name.strip())
    if b.greeting.strip():
        appsettings.set_setting(db, mail_center.SET_GREETING, b.greeting.strip())
    if b.signature.strip():
        appsettings.set_setting(db, mail_center.SET_SIGNATURE,
                                b.signature.strip())
    if b.bot_on is not None:
        appsettings.set_setting(db, mail_center.SET_BOT_ON,
                                "1" if b.bot_on else "0")
    db.commit()                          # аудит отдельной сессией — заранее
    log_action(admin, "mail_center_saved",
               details={"smtp": bool((b.host or "").strip()),
                        "bot": b.bot_on})
    return {"ok": True, "message": "Настройки почты сохранены"}


@router.post("/test", summary="Почтовый центр: тестовое письмо (админ)")
def test_mail(body: dict, db: Session = Depends(get_db),
              admin: User = Depends(require_admin)):
    to = ((body or {}).get("email") or "").strip()
    if "@" not in to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Укажите корректный адрес")
    ident = mail_center.identity(db)
    body_text = ("Это тестовое письмо Почтового центра. Если вы его читаете — "
                 "SMTP, шаблоны и подпись настроены верно.\n\n"
                 "Дальше система сможет отправлять подтверждения e-mail, "
                 "ссылки входа и автоматические рассылки бота.")
    html = mail_center.wrap_html("Тестовое письмо",
                                 mail_center.render(ident["greeting"],
                                                    {"name": to.split("@")[0]}),
                                 body_text, ident["signature"])
    from ..pool.mailer import send_mail
    ok, err = send_mail(db, to, "Ямастер Чек: тестовое письмо", body_text,
                        html=html, from_name=ident["from_name"])
    log_action(admin, "mail_center_test", details={"ok": ok})
    return {"ok": ok,
            "message": (f"Письмо отправлено на {to}" if ok
                        else f"Не отправлено: {err}")}




# --------------------------------------------------------------------------
#  Правила бота рассылок
# --------------------------------------------------------------------------
@router.get("/rules", summary="Почтовый центр: правила рассылок (админ)")
def list_rules(db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    rows = (db.query(MailRule)
              .order_by(MailRule.created_at.desc()).all())
    return {"items": [r.to_dict() for r in rows]}


class RuleBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    trigger: str = Field(default="schedule", pattern="^(schedule|condition)$")
    schedule: str = Field(default="daily", max_length=8)
    hh_mm: str = Field(default="09:00", max_length=5)
    condition_key: str = Field(default="pool_inactive", max_length=24)
    cond_days: int = Field(default=14, ge=1, le=365)
    recipients: str = Field(default="", max_length=2000)
    subject: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=2, max_length=4000)
    enabled: bool = True


@router.post("/rules", summary="Почтовый центр: создать правило (админ)")
def create_rule(body: RuleBody, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    if body.condition_key not in mail_center.AUDIENCES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Неизвестная аудитория")
    if body.trigger == "schedule" and body.schedule not in (
            "daily", *mail_center.DAY_KEYS):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "День недели: daily, mon…sun")
    if body.trigger == "custom_schedule":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Триггер?")
    r = MailRule(name=body.name.strip(), enabled=body.enabled,
                 trigger=body.trigger, schedule=body.schedule,
                 hh_mm=body.hh_mm.strip()[:5],
                 condition_key=body.condition_key,
                 cond_days=int(body.cond_days),
                 recipients=body.recipients.strip()[:2000],
                 subject=body.subject.strip(), body=body.body.strip())
    db.add(r)
    db.commit()                          # аудит отдельной сессией — заранее
    log_action(admin, "mail_rule_created", details={"name": r.name,
                                                    "trigger": r.trigger})
    return {"ok": True, "id": r.id, "message": f"Правило «{r.name}» создано"}


@router.patch("/rules/{rid}", summary="Почтовый центр: изменить правило (админ)")
def patch_rule(rid: str, body: dict, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    r = db.get(MailRule, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не найдено")
    if "enabled" in (body or {}):
        r.enabled = bool(body["enabled"])
    for f in ("name", "subject", "body", "recipients", "hh_mm", "schedule"):
        if f in (body or {}) and isinstance(body[f], str) and body[f].strip():
            setattr(r, f, body[f].strip()[:2000 if f == "body" else 200])
    if "cond_days" in (body or {}):
        try:
            r.cond_days = max(1, min(365, int(body["cond_days"])))
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Дней — целое число")
    db.commit()
    log_action(admin, "mail_rule_updated", details={"name": r.name})
    return {"ok": True, "message": "Правило обновлено"}


@router.delete("/rules/{rid}",
               summary="Почтовый центр: удалить правило (админ)")
def delete_rule(rid: str, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    r = db.get(MailRule, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не найдено")
    name = r.name
    db.delete(r)
    db.commit()                          # аудит отдельной сессией — заранее
    log_action(admin, "mail_rule_deleted", details={"name": name})
    return {"ok": True, "message": f"Правило «{name}» удалено"}


@router.post("/rules/{rid}/run",
             summary="Почтовый центр: запустить правило сейчас (админ)")
def run_rule_now(rid: str, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    r = db.get(MailRule, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не найдено")
    res = mail_center.run_rule(db, r)
    log_action(admin, "mail_rule_ran", details={"name": r.name, **res})
    return {"ok": True, "message": (f"Готово: отправлено {res['sent']}, "
                                    f"пропущено {res['skipped']}, "
                                    f"ошибок {res['errors']}"), **res}


# --------------------------------------------------------------------------
#  Журнал отправок и отписки
# --------------------------------------------------------------------------
@router.get("/log", summary="Почтовый центр: журнал отправок (админ)")
def mail_log(limit: int = Query(50, ge=1, le=200),
             db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    rows = (db.query(MailLog)
              .order_by(MailLog.created_at.desc()).limit(limit).all())
    return {"items": [r.to_dict() for r in rows],
            "masked": [_mask(r.to_email) for r in rows]}


@router.delete("/log", summary="Почтовый центр: очистить журнал (админ)")
def clear_log(db: Session = Depends(get_db),
              admin: User = Depends(require_admin)):
    n = mail_center.purge_log(db, days=0)
    log_action(admin, "mail_log_cleared", details={"count": n})
    return {"ok": True, "message": f"Удалено записей: {n}"}


@router.get("/suppressed", summary="Почтовый центр: отписавшиеся (админ)")
def suppressed(db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    return {"items": sorted(mail_center._suppressed(db))}


@router.post("/suppressed/remove",
             summary="Почтовый центр: вернуть адрес в рассылки (админ)")
def suppressed_remove(body: dict, db: Session = Depends(get_db),
                      admin: User = Depends(require_admin)):
    e = ((body or {}).get("email") or "").strip().lower()
    ok = mail_center.unsuppress(db, e)
    return {"ok": ok, "message": ("Адрес возвращён в рассылки" if ok
                                  else "Адреса не было в списке")}


# --------------------------------------------------------------------------
#  Публичная отписка в один клик (без входа)
# --------------------------------------------------------------------------
@public_router.get("/unsub", summary="Отписаться от рассылок (ссылка из письма)",
                   include_in_schema=False)
def public_unsub(e: str = "", s: str = "",
                 db: Session = Depends(get_db)):
    e = (e or "").strip().lower()
    if not e or s != mail_center.unsub_sign(e):
        return HTMLResponse(
            "<meta charset='utf-8'><body style='font-family:sans-serif;"
            "padding:40px;text-align:center'><h2>Ссылка недействительна</h2>"
            "<p>Скопируйте ссылку из письма полностью.</p></body>", 400)
    mail_center.suppress(db, e)
    return HTMLResponse(
        "<meta charset='utf-8'><body style='font-family:sans-serif;"
        "padding:40px;text-align:center;background:#f4f6f9'>"
        "<div style='max-width:460px;margin:40px auto;background:#fff;"
        "border-radius:14px;padding:28px;border:1px solid #e8ebf0'>"
        "<h2 style='margin-top:0'>\u2713 Вы отписаны</h2>"
        f"<p style='color:#5a6170'>Адрес <b>{_mask(e)}</b> исключён из "
        "автоматических рассылок «Ямастер Чек».</p>"
        "<p style='color:#5a6170;font-size:13px'>Письма о безопасности "
        "(подтверждение e-mail, вход по ссылке) по-прежнему будут доходить."
        "</p></div></body>")
