# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — кабинет компании/ИП для доступа к платному API (v1.70.45).
#
# Маршруты /api/v1/b2b/*. Контур отдельный: токен ядра и токен Чек-Пула
# сюда не проходят (проверка typ в JWT — первое действие).
#
# Доступ к содержимому раздела — только после подтверждения статуса
# компании (зависимость require_verified): защита на сервере, а не только
# в интерфейсе. Схемы API отдаются текстом через закрытый маршрут —
# публичного адреса у файлов больше нет.
#
# Антифрод как в публичной форме пула: поле-ловушка, минимальное время
# заполнения формы, ограничение регистраций по IP.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..pool import antifraud
from ..pool.ingest import OFFERTA_VERSION
from ..pool.router_api import CALLS_RETENTION_DAYS
from ..services.companies_util import normalize_inn
from . import auth, content, letters, pipeline, verify
from .models import (
    ACTOR_COMPANY,
    PURPOSE_RESET,
    PURPOSE_VERIFY,
    ST_BLOCKED,
    ST_EMAIL_PENDING,
    ST_VERIFIED,
    B2bApplication,
    B2bCompany,
    _utcnow,
    log_event,
    refresh_search_key,
)

log = logging.getLogger("ymaster.b2b.public")

router = APIRouter(prefix="/api/v1/b2b", tags=["b2b"])

HONEYPOT_MS = 800                 # быстрее — сигнал «быстрая отправка»
TASK_MIN = 20                     # заявка: описание задачи не короче
NAME_MIN = 2                      # наименование компании
CLOSED_TEXT = ("Подключение компаний к API пока закрыто. Напишите на "
               f"{content.CONTACT_EMAIL} — ответим и обсудим условия")


# --------------------------------------------------------------------------
# Тела запросов
# --------------------------------------------------------------------------
class RegisterBody(BaseModel):
    email: str = ""
    password: str = ""
    company_name: str = ""
    inn: str = ""
    contact_name: str = ""
    phone: str = ""
    tariff: str = ""
    task: str = ""
    consent: bool = False
    hp: str = ""                  # поле-ловушка: человек его не видит
    form_ms: int = 0              # время заполнения формы, мс


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


class TokenBody(BaseModel):
    token: str = ""


class ResetConfirmBody(BaseModel):
    token: str = ""
    password: str = ""


class ApplicationBody(BaseModel):
    tariff: str = ""
    task: str = ""
    expected_volume: str = ""


class ResetBody(BaseModel):
    email: str = ""


# --------------------------------------------------------------------------
# Служебное
# --------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    return antifraud.request_ip(request)


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(f"b2b:{ip}".encode()).hexdigest()[:16]


def _hash_ua(request: Request) -> str:
    return hashlib.sha256(
        (request.headers.get("user-agent") or "").encode()).hexdigest()[:16]


def _enabled_or_403(db: Session) -> dict:
    cfg = pipeline.get_config(db)
    if not cfg["enabled"]:
        raise HTTPException(403, CLOSED_TEXT)
    return cfg


def _by_email(db: Session, email: str):
    return (db.query(B2bCompany)
            .filter(B2bCompany.email == (email or "").strip().lower()).first())


def _applications(db: Session, company_id: str) -> list[dict]:
    rows = (db.query(B2bApplication)
            .filter(B2bApplication.company_id == company_id)
            .order_by(B2bApplication.created_at.desc()).limit(20).all())
    return [r.to_dict() for r in rows]


def key_info(db: Session, company: B2bCompany) -> dict:
    """Ключ компании: префикс, лимиты, расход. Полный ключ не храним."""
    from ..pool.models import PoolApiCall, PoolApiKey
    rec = (db.query(B2bApplication)
           .filter(B2bApplication.company_id == company.id,
                   B2bApplication.key_id != "")
           .order_by(B2bApplication.created_at.desc()).first())
    if rec is None or not rec.key_id:
        return {"issued": False}
    key = db.get(PoolApiKey, rec.key_id)
    if key is None:
        return {"issued": False}
    now = _utcnow()
    hour_ago = now - timedelta(hours=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used_hour = (db.query(PoolApiCall)
                 .filter(PoolApiCall.key_id == key.id,
                         PoolApiCall.created_at >= hour_ago).count())
    used_month = (db.query(PoolApiCall)
                  .filter(PoolApiCall.key_id == key.id,
                          PoolApiCall.created_at >= month_start).count())
    return {
        "issued": True,
        "active": bool(key.active),
        "prefix": key.prefix,
        "name": key.name,
        "rate_per_hour": key.rate_per_hour,
        "monthly_quota": key.monthly_quota,
        "used_hour": used_hour,
        "used_month": used_month,
        "last_used_at": (key.last_used_at.strftime("%d.%m.%Y %H:%M")
                         if key.last_used_at else "—"),
        "application_id": rec.id,
        "tariff": rec.tariff,
    }


def _events(db: Session, company_id: str, limit: int = 20) -> list[dict]:
    from .models import B2bEvent
    rows = (db.query(B2bEvent)
            .filter(B2bEvent.company_id == company_id)
            .order_by(B2bEvent.created_at.desc()).limit(limit).all())
    return [r.to_dict() for r in rows]


def _state(db: Session, company: B2bCompany) -> dict:
    """Полное состояние кабинета (для /me и после каждого шага)."""
    cfg = pipeline.get_config(db)
    return {
        "company": company.to_dict(),
        "status": company.status,
        "pages": [name for name in content.PAGES
                  if company.status == ST_VERIFIED],
        "checks": verify.latest_checks(db, company.id),
        "applications": _applications(db, company.id),
        "key": key_info(db, company),
        "events": _events(db, company.id),
        "auto_mode_title": pipeline.MODE_TITLES[cfg["auto_mode"]],
        "contact": content.CONTACT_EMAIL,
    }


# --------------------------------------------------------------------------
# Открытая часть: состояние контура и требования
# --------------------------------------------------------------------------
@router.get("/status", summary="B2B: открыто ли подключение компаний")
def contour_status(db: Session = Depends(get_db)):
    cfg = pipeline.get_config(db)
    return {
        "ok": True,
        "enabled": cfg["enabled"],
        "closed_text": "" if cfg["enabled"] else CLOSED_TEXT,
        "requirements": list(content.REQUIREMENTS),
        "not_accepted": list(content.NOT_ACCEPTED),
        "steps": [{"n": s["n"], "title": s["title"], "text": s["text"]}
                  for s in content.HUB_STEPS],
        "tariffs": content.tariffs(),
        "contact": content.CONTACT_EMAIL,
    }


# --------------------------------------------------------------------------
# Регистрация и подтверждение корпоративной почты
# --------------------------------------------------------------------------
@router.post("/register", summary="B2B: регистрация компании")
def register(body: RegisterBody, request: Request,
             db: Session = Depends(get_db)):
    """Регистрация кабинета компании.

    Порядок проверок: ловушка для ботов → включён ли контур → лимит IP →
    поля формы → согласие → дубликат почты. Согласие фиксируется до создания
    записи (152-ФЗ).
    """
    # бот заполнил скрытое поле или отправил форму мгновенно — тихо «принимаем»
    if (body.hp or "").strip() or (body.form_ms and body.form_ms < HONEYPOT_MS):
        return {"ok": True, "registered": False, "message": "Заявка принята"}

    cfg = _enabled_or_403(db)
    ip = _client_ip(request)
    if auth.ip_blocked(ip):
        raise HTTPException(429, "Слишком много попыток регистрации с этого "
                                 "адреса — попробуйте позже")
    auth.note_ip(ip)

    email = (body.email or "").strip().lower()
    if not verify.check_email_format(email)["ok"]:
        raise HTTPException(422, "Укажите корпоративный e-mail: имя@доменкомпании.ru")
    if verify.is_free_domain(email) and not cfg["allow_free_domains"]:
        raise HTTPException(422, "Нужна почта компании на своём домене — "
                                 "бесплатный ящик не подтверждает статус компании")
    password_error = auth.password_error(body.password)
    if password_error:
        raise HTTPException(422, password_error)
    if len((body.company_name or "").strip()) < NAME_MIN:
        raise HTTPException(422, "Укажите наименование компании или ФИО "
                                 "предпринимателя")
    if not verify.check_inn(body.inn)["ok"]:
        raise HTTPException(422, "ИНН не проходит проверку: нужно 10 цифр "
                                 "(ООО) или 12 цифр (ИП) с верной контрольной "
                                 "суммой")
    if not body.consent:
        raise HTTPException(422, "Нужно согласие на обработку персональных "
                                 "данных — отметьте галочку")
    if _by_email(db, email) is not None:
        raise HTTPException(409, "Такая почта уже зарегистрирована — войдите "
                                 "или восстановите доступ")

    company = B2bCompany(
        email=email,
        password_hash=auth.hash_secret(body.password),
        company_name=(body.company_name or "").strip()[:300],
        inn=normalize_inn(body.inn),
        contact_name=(body.contact_name or "").strip()[:120],
        phone=(body.phone or "").strip()[:40],
        status=ST_EMAIL_PENDING,
        consent_version=OFFERTA_VERSION,
        consent_at=_utcnow(),
        ip_hash=_hash_ip(ip),
        user_agent_hash=_hash_ua(request),
    )
    db.add(company)
    refresh_search_key(company)
    db.flush()
    log_event(db, company.id, ACTOR_COMPANY, "registered",
              f"Регистрация с IP-хэшем {company.ip_hash}, тариф "
              f"{(body.tariff or '').strip()[:20] or 'не выбран'}")
    db.commit()

    token = auth.issue_one_time(db, company, PURPOSE_VERIFY)
    sent = letters.send_verify(db, company, token)
    return {
        "ok": True,
        "registered": True,
        "company": company.to_dict(),
        "letter_sent": sent,
        "message": ("Мы отправили письмо для подтверждения корпоративной почты. "
                    "Перейдите по ссылке из письма — она действует 24 часа"
                    if sent else
                    "Запись создана, но письмо не отправлено: почта сервиса не "
                    "настроена. Нажмите «Выслать письмо повторно» позже или "
                    f"напишите на {content.CONTACT_EMAIL}"),
    }


@router.post("/verify-email", summary="B2B: подтвердить корпоративную почту")
def verify_email(body: TokenBody, db: Session = Depends(get_db)):
    """Переход по ссылке из письма: почта подтверждена → проверки статуса."""
    auth.purge_expired(db)
    raw = (body.token or "").strip()
    if not raw:
        raise HTTPException(422, "Не передан токен из письма")
    company = auth.consume_one_time(db, raw, PURPOSE_VERIFY)
    if company is None:
        raise HTTPException(400, "Ссылка недействительна или уже использована — "
                                 "запросите письмо повторно")
    company.email_verified_at = _utcnow()
    log_event(db, company.id, ACTOR_COMPANY, "email_confirmed",
              "Корпоративная почта подтверждена по ссылке из письма")
    db.commit()

    result = pipeline.after_email_confirmed(db, company)
    payload = {
        "ok": True,
        "company": company.to_dict(),
        "status": company.status,
        "checks": result.get("checks") or [],
        "message": result.get("message", ""),
    }
    if company.status == ST_VERIFIED:
        # ссылка доказала владение ящиком — сразу открываем кабинет
        payload["token"] = auth.issue_b2b_token(company.id)
        payload["pages"] = list(content.PAGES)
    return payload


@router.post("/resend-verify", summary="B2B: выслать письмо повторно")
def resend_verify(request: Request, db: Session = Depends(get_db),
                  company: B2bCompany = Depends(auth.require_company)):
    ip = _client_ip(request)
    if auth.ip_blocked(ip):
        raise HTTPException(429, "Слишком много запросов — попробуйте позже")
    auth.note_ip(ip)
    if company.email_verified_at is not None:
        return {"ok": True, "sent": False,
                "message": "Почта уже подтверждена"}
    token = auth.issue_one_time(db, company, PURPOSE_VERIFY)
    sent = letters.send_verify(db, company, token)
    log_event(db, company.id, ACTOR_COMPANY, "verify_resent",
              "Письмо подтверждения выслано повторно")
    db.commit()
    return {"ok": True, "sent": sent,
            "message": "Письмо отправлено повторно" if sent
            else "Почта сервиса не настроена — письмо не отправлено"}


# --------------------------------------------------------------------------
# Вход и восстановление доступа
# --------------------------------------------------------------------------
@router.post("/login", summary="B2B: вход в кабинет компании")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    email = (body.email or "").strip().lower()
    key = f"{_client_ip(request)}|{email}"
    if auth.login_blocked(key) or auth.login_blocked(_client_ip(request)):
        raise HTTPException(429, "Слишком много попыток входа — попробуйте "
                                 "через 15 минут")
    company = _by_email(db, email)
    if company is None or not auth.check_secret(body.password,
                                                company.password_hash):
        auth.note_failure(key)
        raise HTTPException(401, "Неверная почта или пароль")
    if company.status == ST_BLOCKED:
        auth.note_failure(key)
        raise HTTPException(403, "Доступ закрыт администратором сервиса")
    auth.note_success(key)
    company.last_login_at = _utcnow()
    log_event(db, company.id, ACTOR_COMPANY, "login", "Вход в кабинет")
    db.commit()
    return {"ok": True, "token": auth.issue_b2b_token(company.id),
            **_state(db, company)}


@router.post("/reset-request", summary="B2B: запросить ссылку для входа")
def reset_request_mail(body: ResetBody, request: Request,
                       db: Session = Depends(get_db)):
    """Ссылка для входа на корпоративную почту.

    Ответ всегда одинаковый — не раскрываем, зарегистрирована ли такая почта
    в контуре (защита от перебора адресов).
    """
    ip = _client_ip(request)
    if auth.ip_blocked(ip):
        raise HTTPException(429, "Слишком много запросов — попробуйте позже")
    auth.note_ip(ip)
    _enabled_or_403(db)
    company = _by_email(db, (body.email or "").strip().lower())
    if company is not None:
        token = auth.issue_one_time(db, company, PURPOSE_RESET)
        letters.send_reset(db, company, token)
        log_event(db, company.id, ACTOR_COMPANY, "reset_requested",
                  "Запрошена ссылка для входа")
        db.commit()
    return {"ok": True,
            "message": "Если такая почта зарегистрирована, мы отправили ссылку "
                       "для входа — она действует 1 час"}


@router.post("/reset-confirm", summary="B2B: установить новый пароль")
def reset_confirm(body: ResetConfirmBody, db: Session = Depends(get_db)):
    company = auth.consume_one_time(db, (body.token or "").strip(),
                                    PURPOSE_RESET)
    if company is None:
        raise HTTPException(400, "Ссылка недействительна или истекла — "
                                 "запросите новую")
    error = auth.password_error(body.password)
    if error:
        raise HTTPException(422, error)
    company.password_hash = auth.hash_secret(body.password)
    log_event(db, company.id, ACTOR_COMPANY, "password_reset",
              "Пароль обновлён по ссылке из письма")
    db.commit()
    return {"ok": True, "token": auth.issue_b2b_token(company.id),
            "message": "Пароль обновлён — вход выполнен"}


# --------------------------------------------------------------------------
# Кабинет
# --------------------------------------------------------------------------
@router.get("/me", summary="B2B: состояние кабинета компании")
def me(db: Session = Depends(get_db),
       company: B2bCompany = Depends(auth.require_company)):
    return {"ok": True, **_state(db, company)}


@router.post("/applications", summary="B2B: заявка «Обсудить подключение»")
def submit_application(body: ApplicationBody,
                       db: Session = Depends(get_db),
                       company: B2bCompany = Depends(auth.require_verified)):
    """Заявка на подключение: тариф, задача, ожидаемый объём.

    В режиме полной автоматизации ключ выдаётся сразу; в частичной (по
    умолчанию) заявка встаёт в очередь к администратору.
    """
    task = (body.task or "").strip()
    if len(task) < TASK_MIN:
        raise HTTPException(422, f"Опишите задачу подробнее — не короче "
                                 f"{TASK_MIN} символов")
    result = pipeline.submit_application(
        db, company, body.tariff, task,
        (body.expected_volume or "").strip())
    if not result.get("ok"):
        raise HTTPException(422, result.get("error") or "Заявка не принята")
    # Состояние кабинета — каноническое, поэтому поля заявки названы своими
    # именами: «status» и «key» в _state означают статус компании и сведения
    # о выданном ключе, а не результат этой заявки. Раньше _state перезаписывал
    # их, и одноразовый ключ до компании не доходил.
    payload = _state(db, company)
    payload["ok"] = True
    payload["application"] = result["application"]
    payload["application_status"] = result["status"]
    payload["message"] = result["message"]
    payload["issued_key"] = result.get("key", "")
    if result.get("key"):
        payload["key_note"] = ("Ключ показывается один раз — скопируйте его "
                               "сейчас: в базе хранится только хэш")
    return payload


# --------------------------------------------------------------------------
# Закрытое содержимое раздела (только подтверждённые компании)
# --------------------------------------------------------------------------
@router.get("/content", summary="B2B: страницы раздела (после подтверждения)")
def gated_content(section: str = "hub",
                  company: B2bCompany = Depends(auth.require_verified)):
    """Параметр назван `section`, а не `page`: в этом API `page` — номер
    страницы списка с потолком PAGE_MAX (замок test_v17038 против падения 500
    на огромном номере), а здесь передаётся имя раздела."""
    payload = content.page(section, company)
    if payload is None:
        raise HTTPException(404, "Страница не найдена")
    return {"ok": True, **payload}


@router.get("/content/diagram/{name}",
            summary="B2B: схема раздела (после подтверждения)")
def gated_diagram(name: str,
                  company: B2bCompany = Depends(auth.require_verified)):
    """Схемы API: файлы лежат вне app/static, отдаются только через гейт."""
    diagram_name, svg = content.diagram(name)
    if not diagram_name:
        raise HTTPException(404, "Схема не найдена")
    return {"ok": True, "name": diagram_name, "svg": svg,
            "note": "Схема доступна только подтверждённым компаниям"}


@router.get("/usage", summary="B2B: расход лимитов ключа")
def usage(db: Session = Depends(get_db),
          company: B2bCompany = Depends(auth.require_verified)):
    info = key_info(db, company)
    if not info.get("issued"):
        return {"ok": True, "issued": False,
                "message": "Ключ ещё не выдан: отправьте заявку "
                           "«Обсудить подключение»"}
    return {"ok": True, **info,
            "retention_days": CALLS_RETENTION_DAYS,
            "note": "Журнал вызовов хранит только путь запроса; часы и месяцы "
                    "считаются в UTC"}
