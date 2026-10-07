# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — публичные маршруты «Чек-Пула» (v1.31.0, Этап 2).
# Приём чеков на сайте chek.ymaster.ru БЕЗ регистрации: гость получает
# подписанную анонимную cookie pool_vid; чеки копятся до регистрации
# (слияние с аккаунтом — Этап 3).
#
# Быстрый ответ ≤ 5 с: лёгкие проверки синхронно (оферта, honeypot,
# лимиты, дедуп), проверка источников — в фоновом потоке; страница
# опрашивает статус (live). Антифрод-минимум: honeypot, тайминг формы,
# rate-limit на IP, суточный лимит из ядра пула.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
import re
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from . import antifraud, engage, ingest, partners, referral

router = APIRouter(prefix="/api/v1/public/pool", tags=["pool-public"])

VID_COOKIE = "pool_vid"           # анонимная подписанная cookie гостя
VID_MAX_AGE = 60 * 60 * 24 * 365  # 1 год (152-ФЗ: техданные ≤ 12 мес)
WEB_IP_HOURLY_LIMIT = 20          # отправок с одного IP в час (nginx дублирует)
HONEYPOT_MS = 800                 # быстрее — сигнал «быстрая отправка»

# Скользящее окно IP-лимитера (в процессе; в бою дублируется nginx)
_IP_HITS: dict[str, deque] = {}


# --- cookie гостя: выдача/проверка подписи --------------------------------
def _sign(vid: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), vid.encode(),
                    hashlib.sha256).hexdigest()[:16]


def _get_or_issue_vid(request: Request, response: Response) -> str:
    raw = (request.cookies.get(VID_COOKIE) or "").strip()
    if raw and "." in raw:
        vid, sig = raw.rsplit(".", 1)
        if hmac.compare_digest(_sign(vid), sig):
            return vid
    vid = uuid.uuid4().hex
    response.set_cookie(VID_COOKIE, f"{vid}.{_sign(vid)}",
                        max_age=VID_MAX_AGE, httponly=True,
                        samesite="lax", path="/")
    return vid


def _user_by_vid(db: Session, vid: str):
    from .models import PoolUser
    if not vid:
        return None
    user = db.query(PoolUser).filter(PoolUser.vid == vid).first()
    if user is None:
        user = PoolUser(vid=vid)
        db.add(user)
        db.flush()
    return user


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(f"pool:{ip}".encode()).hexdigest()[:16]


def _client_ip(request: Request) -> str:
    return antifraud.request_ip(request)


# --- IP-лимитер ------------------------------------------------------------
def _ip_allow(ip: str) -> bool:
    hits = _IP_HITS.setdefault(ip, deque())
    now = time.monotonic()
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= WEB_IP_HOURLY_LIMIT:
        return False
    hits.append(now)
    return True


# --- схемы -----------------------------------------------------------------
class CheckBody(BaseModel):
    qr_text: str = ""
    offerta: bool = False
    hp: str = ""                # honeypot: человек это поле не видит и не заполняет
    form_ms: int = 0            # время заполнения формы (антифрод-минимум)
    employee_email: str = Field("", max_length=200)   # v1.37.0: сценарий C


def _receipt_public(r) -> dict:
    return {
        "id": r.id, "status": r.status, "message": r.status_message,
        "fn": r.fn, "total_sum": r.total_sum,
        "merchant_name": r.merchant_name,
        "receipt_date": r.receipt_date.isoformat() if r.receipt_date else None,
        "points": r.points_awarded,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# --- эндпоинты --------------------------------------------------------------
@router.get("/info", summary="Чек-Пул: публичная информация для формы")
def pool_info(db: Session = Depends(get_db)):
    ov = ingest.overview(db)
    return {
        "enabled": ingest.pool_enabled(db),
        "offerta": ingest.OFFERTA_SHORT,
        "offerta_version": ingest.OFFERTA_VERSION,
        "daily_limit": ingest.DAILY_LIMIT,
        "points_per_receipt": ingest.POINTS_PER_RECEIPT,
        "receipts_total": ov["receipts_total"],
        "verified": ov["verified"],
    }


def _request_pool_account(db: Session, request: Request):
    """Авторизованный участник кабинета (JWT typ=pool) или None. Гость с
    неприсоединённой историей приливается к аккаунту на месте (v1.32.0)."""
    from . import accounts
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    account = accounts.pool_user_from_token(db, auth[7:].strip())
    if account is not None:
        accounts.merge_guest_into_account(
            db, account, accounts.vid_from_cookie(request))
    return account


@router.get("/my", summary="Чек-Пул: баланс и последние чеки (гость или кабинет)")
def pool_my(request: Request, response: Response,
            db: Session = Depends(get_db)):
    from ..models import utcnow
    from .models import PoolReceipt

    vid = _get_or_issue_vid(request, response)
    user = _request_pool_account(db, request) or _user_by_vid(db, vid)
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.pool_user_id == user.id)
            .order_by(PoolReceipt.created_at.desc()).limit(10).all())
    today = (db.query(PoolReceipt)
             .filter(PoolReceipt.pool_user_id == user.id,
                     PoolReceipt.created_at >= day_start).count())
    return {
        "points": user.points, "today": today,
        "daily_limit": ingest.DAILY_LIMIT,
        "receipts": [_receipt_public(r) for r in rows],
    }


@router.post("/check", summary="Чек-Пул: приём чека с сайта (быстрый ответ)")
def pool_check(body: CheckBody, request: Request, response: Response,
               db: Session = Depends(get_db)):
    from .models import PoolConsent

    vid = _get_or_issue_vid(request, response)
    ip = _client_ip(request)

    # honeypot: боты заполняют скрытое поле — тихо «соглашаемся» и выбрасываем
    if (body.hp or "").strip():
        return {"ok": True, "accepted": False, "result": "ok",
                "message": "Спасибо!", "points": 0}

    if not ingest.pool_enabled(db):
        return {"ok": False, "error": "disabled",
                "message": "Приём чеков сейчас выключен — загляните позже"}

    if not _ip_allow(ip):
        raise HTTPException(429, "Слишком много отправок с этого адреса — попробуйте позже")

    qr_raw = (body.qr_text or "").strip()
    if not body.offerta:
        return {"ok": False, "error": "offerta_required",
                "message": "Нужно согласие с офертой — отметьте галочку"}
    if not qr_raw:
        return {"ok": False, "error": "bad_qr",
                "message": "Вставьте строку QR или сфотографируйте код"}

    # факт согласия с офертой — до приёма чека (152-ФЗ)
    _account = _request_pool_account(db, request)
    consent = PoolConsent(
        vid=vid, offerta_version=ingest.OFFERTA_VERSION,
        ip_hash=_hash_ip(ip),
        user_agent_hash=hashlib.sha256(
            (request.headers.get("user-agent") or "").encode()
        ).hexdigest()[:16],
        user_id=(_account.id if _account is not None else None),
        form_ms=max(0, int(body.form_ms or 0)))
    _vh = antifraud.visitor_hash(request)
    if _vh:
        consent.visitor_hash = _vh          # v1.34.0: отпечаток (хэш)
    db.add(consent)

    user = _account or _user_by_vid(db, vid)
    parsed, err = ingest.precheck_ingest(db, qr_raw, user)
    db.commit()
    if err:                                  # bad_qr / duplicate / flood / disabled
        return {"ok": True, "accepted": False, **err}

    fast = bool(body.form_ms) and body.form_ms < HONEYPOT_MS

    # v1.37.0 (сценарий C): e-mail сотрудника компании — в аккаунт пула.
    # Когда он будет подтверждён в кабинете, новые чеки будут уходить
    # компании сотрудника, минуя общий пул.
    emp = (body.employee_email or "").strip().lower()
    if emp and "@" in emp and not (user.email or "").strip():
        from .models import PoolUser as _PU
        exists = db.query(_PU).filter(_PU.email == emp).first()
        if exists is None or exists.id == user.id:
            user.email = emp

    # проверка источников — в фоне: ответ форме отдаём сразу (≤ 5 с)
    user_id = user.id
    db_params = None                          # сессия потока создаст сама

    _client_ip_str = ip
    _form_ms = max(0, int(body.form_ms or 0))

    def _bg():
        from ..database import SessionLocal
        from . import antifraud
        from .models import PoolUser
        s = SessionLocal()
        try:
            u = s.get(PoolUser, user_id)
            if u is not None:
                res = ingest.ingest_parsed(s, qr_raw, parsed, "web", u,
                                           fast=fast)
                # v1.34.0: сигналы поведения/правил после приёма чека
                if isinstance(res, dict) and res.get("receipt_id"):
                    antifraud.after_receipt(s, u, res["receipt_id"], None,
                                            form_ms=_form_ms)
                s.commit()          # сигналы антифрода не должны пропасть
        except Exception:                                 # noqa: BLE001
            pass
        finally:
            s.close()

    threading.Thread(target=_bg, name="pool-web-ingest", daemon=True).start()
    return {"ok": True, "accepted": True, "fn": parsed.fn, "fd": parsed.fd,
            "fp": parsed.fp, "message": "Чек принят — проверяем",
            "points": 0, "user_points": user.points}


@router.get("/receipt/{receipt_id}", summary="Чек-Пул: статус своего чека")
def pool_receipt(receipt_id: str, request: Request, response: Response,
                 db: Session = Depends(get_db)):
    from .models import PoolReceipt

    vid = _get_or_issue_vid(request, response)
    r = (db.query(PoolReceipt)
         .filter(PoolReceipt.id == receipt_id,
                 PoolReceipt.pool_user_id == _user_by_vid(db, vid).id).first())
    if r is None:
        raise HTTPException(404, "Чек не найден")
    return _receipt_public(r)


@router.get("/ref/{code}", summary="Чек-Пул: информация о коде приглашения")
def ref_info(code: str, db: Session = Depends(get_db)):
    """Для лендинга /r/КОД: жив ли код (без персональных данных)."""
    if not ingest.pool_enabled(db):
        return {"valid": False, "label": ""}
    owner = referral.owner_of_code(db, code)
    if owner is None:
        return {"valid": False, "label": ""}
    email = (owner.email or "").strip()
    if email:
        name, _, domain = email.partition("@")
        label = (name[:1] + "***@" + domain) if len(name) > 1 else email
    else:
        label = "участник Чек-Пула"
    return {"valid": True, "label": label}


@router.get("/leaderboard", summary="Чек-Пул: лидерборд месяца (публично)")
def public_leaderboard(city: str = Query("", max_length=128),
                       db: Session = Depends(get_db)):
    """Топ-10 месяца + стендинг регионов. Только маски и числа (152-ФЗ)."""
    if not ingest.pool_enabled(db):
        return {"month": "", "entries": [], "regions": [],
                "participants": 0, "me": None}
    data = engage.leaderboard(db, user=None)
    want = (city or "").strip().lower()
    if want:
        data["entries"] = [e for e in data["entries"]
                           if e["city"].lower() == want]
    return data


# --------------------------------------------------------------------------
# v1.38.0: партнёрская программа — список и QR «на кассе» (кэшбэк-канал)
# --------------------------------------------------------------------------
@router.get("/partners", summary="Чек-Пул: партнёры и кэшбэк")
def partners_list():
    return {"items": partners.list_all(),
            "cap": partners.CASHBACK_CAP,
            "note": "Сдайте чек партнёра — получите кэшбэк баллами "
                    "(процент от суммы чека, до 100 баллов за чек)"}


@router.get("/partners/qr.svg", summary="Чек-Пул: QR «на кассе» (SVG)")
def partner_qr(code: str = Query("", max_length=32),
               db: Session = Depends(get_db)):
    p = partners.find_by_code(code)
    if p is None:
        raise HTTPException(404, "Партнёр не найден")
    from . import mailer
    link = f"{mailer.base_url(db)}/#/pub"
    try:
        svg = partners.qr_svg(link)
    except ValueError:
        raise HTTPException(422, "Не удалось построить QR")
    from fastapi import Response
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------------
# v1.54.0: главная страница — быстрый захват контакта и письмо с чеком
# --------------------------------------------------------------------------
_LEAD_HITS: dict[str, deque] = {}
_MAIL_HITS: dict[str, deque] = {}
_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _limited(hits: dict, ip: str, per_hour: int) -> bool:
    q = hits.setdefault(ip, deque())
    now = time.monotonic()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= per_hour:
        return False
    q.append(now)
    return True


class LeadBody(BaseModel):
    email: str = Field("", max_length=200)
    consent: bool = False
    hp: str = ""                    # honeypot: человек поле не видит
    form_ms: int = 0                # слишком быстрая отправка — бот


@router.post("/leads", summary="Главная: заявка «разбор чека на e-mail»")
def landing_lead(body: LeadBody, request: Request,
                 db: Session = Depends(get_db)):
    """v1.54.0: e-mail с главной страницы. 152-ФЗ: только явное согласие,
    хранится сам адрес, источник и хэши (IP/UA ≤ 12 мес). Боты отсекаются
    honeypot и таймингом; на один IP — не более 3 заявок в час."""
    ip = _client_ip(request)
    if body.hp:                      # бот — отвечаем «успех», не сохраняя
        return {"ok": True, "message": "Заявка принята"}
    if body.form_ms and body.form_ms < 800:
        return {"ok": True, "message": "Заявка принята"}
    if not body.consent:
        raise HTTPException(422, "Отметьте согласие на обработку e-mail")
    email = body.email.strip().lower()
    if not _EMAIL_OK.match(email):
        raise HTTPException(422, "Проверьте адрес e-mail")
    if not _limited(_LEAD_HITS, ip, 3):
        raise HTTPException(429, "Слишком много заявок с этого адреса — попробуйте позже")
    from .models import PoolLead
    db.add(PoolLead(email=email, source="landing", ip_hash=_hash_ip(ip),
                    user_agent_hash=_hash_ip(
                        request.headers.get("user-agent", ""))))
    db.commit()
    return {"ok": True, "message": "Заявка принята — пришлём разбор чека на этот адрес"}


class MailBody(BaseModel):
    fn: str = Field("", max_length=20)
    email: str = Field("", max_length=200)
    hp: str = ""


@router.post("/receipt-email", summary="Прислать данные чека на e-mail")
def receipt_email(body: MailBody, request: Request,
                  db: Session = Depends(get_db)):
    """v1.54.0: письмо с разобранными данными отсканированного чека —
    гость получает результат на почту без регистрации. Не более 3 писем
    в час с одного IP; письма идут только через настроенный SMTP."""
    if body.hp:
        return {"ok": True, "message": "Письмо отправлено"}
    email = body.email.strip().lower()
    if not _EMAIL_OK.match(email):
        raise HTTPException(422, "Проверьте адрес e-mail")
    ip = _client_ip(request)
    if not _limited(_MAIL_HITS, ip, 3):
        raise HTTPException(429, "Слишком много запросов — попробуйте позже")
    import re as _re
    from .mailer import base_url, send_mail, smtp_configured
    from .models import PoolReceipt
    if not smtp_configured(db):
        raise HTTPException(503,
            "Отправка писем пока не настроена — данные чека видны на этой странице")
    fn = (body.fn or "").strip()
    r = (db.query(PoolReceipt).filter(PoolReceipt.fn == fn)
         .order_by(PoolReceipt.created_at.desc()).first())
    if r is None:
        raise HTTPException(404, "Чек не найден — отсканируйте его ещё раз")
    when = r.receipt_date or r.created_at
    status_ru = {"verified": "подтверждён", "pending": "на проверке",
                 "rejected": "не прошёл проверку"}.get(r.status, r.status)
    lines = ["Здравствуйте!", "",
             "Данные вашего чека из «Ямастер Чек-Пул»:", "",
             f"ФН {r.fn} · ФД {r.fd} · ФП {r.fp}",
             f"Магазин: {r.merchant_name or 'уточняется'}",
             f"Сумма: {r.total_sum:.2f} руб.",
             f"Дата: {when.strftime('%d.%m.%Y %H:%M') if when else '—'}",
             f"Статус: {status_ru}",
             f"Баллы: {r.points_awarded}", "",
             f"Кабинет участника: {base_url(db)}/#/my", "",
             "ООО «Ямастер» · ymaster.ru · info@ymaster.ru", "",
             "Письмо отправлено, потому что этот адрес указали при сканировании",
             "чека. Если это были не вы — просто не обращайте внимания."]
    ok, err = send_mail(db, email, "Ваш чек — Ямастер Чек-Пул",
                        chr(10).join(lines))
    if not ok:
        raise HTTPException(502, f"Письмо не ушло: {err}")
    return {"ok": True, "message": f"Данные чека отправлены на {email}"}
