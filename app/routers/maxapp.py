# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — API мини-приложения MAX (v1.69.0).
# Контур Чек-Пула (PoolUser/PoolReceipt/баллы) — движок приёма не
# продублирован: precheck_ingest + фоновый ingest_parsed(source="max").
# Вход: initData (подпись MAX по доке) → PoolUser(tg_user_id = MAX-id)
# + пул-JWT (issue_pool_token, как в кабинете; 30 дней).
# Бонус: если логин сотрудника ядра совпал с username MAX — привязываем
# ассистента MAX (users.max_user_id): напоминания и дайджест сами.
# ======================================================================
from __future__ import annotations

import logging
import threading
from urllib.parse import unquote

from fastapi import (APIRouter, Depends, File, HTTPException,
                     Query, UploadFile, status)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..config import settings
from ..auth import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_USER
from ..models import Company, Invite, User
from ..pool import batch as pool_batch
from ..pool import ingest
from ..pool import referral as pool_referral
from ..pool.accounts import issue_pool_token, require_pool_user
from ..services import consent, maxapp

log = logging.getLogger("ymaster.maxapp")

router = APIRouter(prefix="/api/v1/maxapp", tags=["Мини-приложение MAX"])


class SessionBody(BaseModel):
    init_data: str = Field(min_length=16, max_length=8192)


class ConsentBody(BaseModel):
    """Согласие 152-ФЗ принимается только по проверенной сессии MAX:
    initData подписан платформой, поэтому мы уверены, кто именно согласился."""
    init_data: str = Field(min_length=16, max_length=8192)


class ScanBody(BaseModel):
    qr: str = Field(min_length=10, max_length=4000)
    form_ms: int = Field(default=0, ge=0, le=3_600_000)


class BatchKey(BaseModel):
    """Ключ чека пачки — та же тройка, что в UNIQUE-индексе пула."""
    fn: str = Field(default="", max_length=20)
    fd: str = Field(default="", max_length=10)
    fp: str = Field(default="", max_length=20)


class BatchStatusBody(BaseModel):
    """Запрос статусов пачки: не больше KEYS_MAX ключей за раз."""
    keys: list[BatchKey] = Field(default_factory=list,
                                 max_length=pool_batch.KEYS_MAX)


def _start_param(init_data: str) -> str:
    for chunk in init_data.split("&"):
        if chunk.startswith("start_param="):
            return unquote(chunk.split("=", 1)[1])[:512]
    return ""


def _bind_assistant(db: Session, max_username: str, max_id: str) -> None:
    """Если логин сотрудника ядра совпал с username MAX — привязать
    канал уведомлений (v1.69.0). Иначе — ничего (код из настроек)."""
    from ..services import max_bot as mx
    if not (max_username and mx.get_token(db)):
        return
    user = (db.query(User)
            .filter(User.username == max_username.strip().lower(),
                    User.is_active.is_(True)).first())
    if user is not None and not (user.max_user_id or "").strip():
        user.max_user_id = max_id[:32]
        db.commit()
        log.info("maxapp: ассистент привязан (%s)", user.username)


def _me_dict(db: Session, user) -> dict:
    code = (pool_referral.get_or_create_code(db, user)
            if not user.is_blocked else "")
    prof = consent.profile_dict(user)
    return {
        "max_id": user.tg_user_id or "",
        "name": prof["first_name"] and (
            " ".join(x for x in (prof["first_name"], prof["last_name"]) if x)
        ) or (user.tg_username or ""),
        "points": int(user.points or 0),
        "blocked": bool(user.is_blocked),
        "trust_level": int(getattr(user, "trust_level", 0) or 0),
        "ref_code": code,
        # v1.70.37: профиль из MAX — пустой, пока не дано согласие (152-ФЗ)
        **prof,
        "reg_source": user.reg_source or "",
        "consent": consent.consent_state(user),
    }


def _last_receipts(db: Session, user, limit: int = 20) -> list:
    from ..pool.models import PoolReceipt
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.pool_user_id == user.id)
            .order_by(PoolReceipt.created_at.desc())
            .limit(limit).all())
    return [{"id": r.id, "fn": r.fn, "fd": r.fd, "fp": r.fp,
             "sum": float(r.total_sum or 0), "status": r.status,
             "created_at": r.created_at.strftime("%d.%m %H:%M")
             if r.created_at else ""} for r in rows]


@router.post("/session", summary="Вход/регистрация через MAX (initData)")
def session(body: SessionBody, db: Session = Depends(get_db)):
    user_raw, err = maxapp.validate_init_data(body.init_data,
                                              maxapp.bot_token(db))
    if err == "not_configured":
        from fastapi import HTTPException, status
        # v1.70.38: было 503 «Service Unavailable». Это неправда дважды:
        # повторить позже не поможет (пока владелец не впишет токен бота MAX,
        # состояние не изменится), а любой мониторинг и фаззинг по схеме
        # считают 5xx отказом сервера. 409 — «конфликт с текущим состоянием
        # сервера»: в проекте этот код уже используется для случаев, когда
        # действие невозможно из-за состояния сервера («обновление уже
        # выполняется»), а не из-за ошибки в запросе.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Мини-приложение ещё не настроено")
    if err in ("format", "bad_hash"):
        from fastapi import HTTPException
        from ..services import max_bot as _mb
        tech = _mb.tech_username(db)
        log.info("maxapp session отказ (%s), токен-бот: %s",
                 err, tech or "неизвестен")
        # v1.70.36: причина и обходной путь в одном тексте. Вход на сайт
        # через MAX устроен как device-code (код подтверждается либо
        # мини-приложением, либо СООБЩЕНИЕМ боту), поэтому подпись initData
        # для входа на сайт не обязательна — человек не должен оставаться
        # без входа, если подпись по какой-то причине не сходится.
        bypass = ("Войти на сайт можно и без мини-приложения: кнопка "
                  "«Войти через MAX» на chek.ymaster.ru — подтвердите вход "
                  "кодом сообщением боту, подпись для этого не нужна")
        if err == "format":
            detail = ("MAX прислал initData в нераспознанном виде — "
                      "обновите приложение MAX и откройте мини-приложение "
                      "заново. " + bypass)
        else:
            detail = ((f"Подпись MAX не совпала с сохранённым токеном бота "
                       f"«{tech}». initData подписывается тем ботом, из чата "
                       "которого открыто приложение: если это другой ваш "
                       "бот — вставьте его токен в Настройках → Ассистент MAX "
                       "и откройте приложение заново. "
                       if tech else
                       "Подпись MAX не сошлась — проверьте токен бота "
                       "(Настройки → Ассистент MAX) и откройте приложение "
                       "из чата бота ещё раз. ") + bypass)
        raise HTTPException(401, detail)
    if err == "expired":
        from fastapi import HTTPException
        raise HTTPException(401, "Сессия устарела — откройте приложение "
                                 "заново")
    assert user_raw is not None
    max_id = str(user_raw["id"])
    log.info("maxapp session ok: max_id=%s", max_id)
    username = (user_raw.get("username") or "").strip()

    existing = (db.query(_PoolUserModel)
                .filter(_PoolUserModel.tg_user_id == max_id).first())
    is_new = existing is None
    # v1.70.37 (152-ФЗ): до согласия в карточку НЕ пишутся имя, фамилия, ник
    # и фото из MAX — сохраняется только идентификатор, без которого невозможно
    # начислять баллы (п. 5 ч. 1 ст. 6 152-ФЗ — исполнение условий сервиса).
    user = ingest.get_or_create_user(db, tg_user_id=max_id, tg_username="")
    if not (user.reg_source or ""):
        user.reg_source = "max"          # факт канала регистрации
    if user.consent_at:                  # согласие уже есть — профиль свежий
        consent.fill_profile(user, user_raw)
    if not user.is_blocked:
        ref = maxapp.parse_start_param(_start_param(body.init_data))
        if ref.get("ref"):
            pool_referral.attribute(db, user, ref["ref"], request=None)
    db.commit()

    _bind_assistant(db, username, max_id)
    me = _me_dict(db, user)
    resp = {"access_token": issue_pool_token(user.id),
            "token_type": "bearer", "is_new": is_new, "me": me}
    # v1.70.37: первый вход (или смена редакции согласия) — клиент обязан
    # показать текст и получить явное «согласен» ДО заполнения карточки.
    if not me["consent"]["given"] or me["consent"]["stale"]:
        resp["consent_doc"] = consent.consent_payload(db)
    return resp


@router.get("/me", summary="Профиль мини-приложения (баллы, чеки)")
def me(db: Session = Depends(get_db), user=Depends(require_pool_user)):
    d = _me_dict(db, user)
    d["receipts"] = _last_receipts(db, user)
    return d


# ==========================================================================
#  Согласие на обработку персональных данных из MAX (152-ФЗ, v1.70.37)
# ==========================================================================

@router.get("/consent", summary="Текст согласия на обработку данных из MAX")
def consent_doc(db: Session = Depends(get_db)):
    """Публичная редакция согласия: оператор, цели, перечень данных, срок,
    порядок отзыва и права субъекта (ч. 4 ст. 9 152-ФЗ). Реквизиты оператора
    подставляются из настроек (Настройки → «Согласие 152-ФЗ»)."""
    return consent.consent_payload(db)


@router.post("/consent",
             summary="Дать согласие и заполнить карточку данными из MAX")
def consent_give(body: ConsentBody, db: Session = Depends(get_db)):
    """Принимает согласие только по подписанной платформой initData:
    иначе за участника мог бы согласиться кто угодно. После проверки
    подписи фиксируются редакция согласия, момент и канал, и лишь затем
    карточка заполняется именем, фамилией, ником, фото и языком."""
    user_raw, err = maxapp.validate_init_data(body.init_data,
                                              maxapp.bot_token(db))
    if err is not None or user_raw is None:
        raise HTTPException(401, "Согласие не принято: сессию MAX не удалось "
                                 "проверить — закройте и откройте приложение "
                                 "заново, затем повторите")
    max_id = str(user_raw["id"])
    user = ingest.get_or_create_user(db, tg_user_id=max_id, tg_username="")
    if not (user.reg_source or ""):
        user.reg_source = "max"
    res = consent.record_consent(db, user, consent.SOURCE_MAXAPP, user_raw)
    res["me"] = _me_dict(db, user)
    log.info("maxapp consent ok: max_id=%s версия=%s", max_id,
             consent.CONSENT_VERSION)
    return res


@router.post("/consent/revoke",
             summary="Отозвать согласие: профиль очищается (ст. 21 152-ФЗ)")
def consent_revoke(db: Session = Depends(get_db),
                   user=Depends(require_pool_user)):
    """Отзыв согласия: имя, фамилия, ник, фото и язык удаляются из карточки,
    баллы и история чеков сохраняются обезличенными — обязательства перед
    участником не теряются."""
    res = consent.revoke_consent(db, user)
    res["me"] = _me_dict(db, user)
    log.info("maxapp consent revoked: участник %s", user.id)
    return res


def _launch_ingest(user, qr_raw: str, parsed, fast: bool, db=None) -> dict:
    """Общий финал /scan и /scan-image: precheck пройден, уходим в фон.

    v1.70.40: сразу возвращаем ФН/ФД/ФП (клиент строит по ним ключ пачки и
    опрашивает статус) и прозрачность суточного лимита — сколько сдано
    сегодня, потолок и остаток. Донор видит приближение к лимиту заранее,
    а не узнаёт о нём из отказа.
    """
    res = {"result": "pending", "message": "Чек принят — проверяем",
           "accepted": True, "fn": parsed.fn, "fd": parsed.fd,
           "fp": parsed.fp, "points": 0,
           "user_points": int(user.points or 0),
           "session_cap": pool_batch.SESSION_CAP}
    if db is not None:
        try:
            res.update(ingest.limit_info(db, user))
        except Exception as e:                            # noqa: BLE001
            # счётчик лимита не должен ломать приём чека, но и молча
            # глотать сбой нельзя: причина уходит в журнал (S110)
            log.warning("maxapp limit_info %s: %s", user.id, e)
    user_id = user.id

    def _bg():
        from ..database import SessionLocal
        s = SessionLocal()
        try:
            u = s.get(_PoolUserModel, user_id)
            if u is not None:
                ingest.ingest_parsed(s, qr_raw, parsed, "max", u, fast=fast)
                s.commit()
        except Exception as e:                           # noqa: BLE001
            log.warning("maxapp ingest %s: %s", user_id, e)
        finally:
            s.close()

    threading.Thread(target=_bg, daemon=True,
                     name="ymaster-maxapp-ingest").start()
    return res


@router.post("/scan", summary="Сдача чека из мини-приложения (нативный QR)")
def scan(body: ScanBody, db: Session = Depends(get_db),
         user=Depends(require_pool_user)):
    parsed, pre_err = ingest.precheck_ingest(db, body.qr, user)
    if pre_err is not None:
        return {**pre_err, "accepted": False}
    fast = (body.form_ms or 0) < 1200        # антифрод: слишком быстрое окно
    return _launch_ingest(user, body.qr, parsed, fast, db)


@router.post("/scan-image",
             summary="Сдача чека фотографией QR-кода (фолбэк сканера)")
async def scan_image(file: UploadFile = File(...),
                     db: Session = Depends(get_db),
                     user=Depends(require_pool_user)):
    """Если нативный сканер MAX недоступен — распознаём QR на фото
    сервером (тот же конвейер, что на сайте: services.imaging)."""
    raw = await file.read()
    if len(raw) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Файл больше {settings.MAX_UPLOAD_MB} МБ")
    from ..services import imaging
    from ..services.imaging import MultipleReceiptsError
    try:
        result = imaging.decode_qr_image(raw)
    except MultipleReceiptsError:
        return {"accepted": False, "result": "multi",
                "message": "На фото несколько разных чеков — "
                           "сдайте по одному",
                "points": 0, "user_points": int(user.points or 0)}
    except (ingest.QRParseError, ValueError) as e:      # noqa: BLE001
        return {"accepted": False, "result": "bad_qr",
                "message": f"Не похоже на чек: {e}",
                "points": 0, "user_points": int(user.points or 0)}
    parsed = result.parsed
    if parsed is None:
        return {"accepted": False, "result": "bad_qr",
                "message": "QR чека на фото не найден — снимите ближе "
                           "и ровнее",
                "points": 0, "user_points": int(user.points or 0)}
    qr_raw = parsed.qr_data or ""
    _, pre_err = ingest.precheck_ingest(db, qr_raw, user)
    if pre_err is not None:
        return {**pre_err, "accepted": False}
    return _launch_ingest(user, qr_raw, parsed, fast=False, db=db)


@router.post("/batch-status",
             summary="Пачка: статусы сданных чеков и суточный лимит")
def batch_status(body: BatchStatusBody, db: Session = Depends(get_db),
                 user=Depends(require_pool_user)):
    """Что стало с чеками пачки: verified / pending / rejected.

    Приём асинхронный (ответ «чек принят — проверяем» уходит сразу), поэтому
    панель сессии опрашивает этот эндпоинт одним запросом на всю пачку и
    перекрашивает счётчики: синий «на модерации» → оранжевый «принят» или
    красный «не принят». Чужие чеки не отдаются: выборка по своему участнику.
    """
    res = pool_batch.statuses(db, user, [k.model_dump() for k in body.keys])
    res["counts"] = pool_batch.count_by_status(res["items"])
    res["user_points"] = int(user.points or 0)
    return res


@router.get("/summary", summary="Сводка: баллы, чеки, код приглашения")
def summary(db: Session = Depends(get_db), user=Depends(require_pool_user)):
    d = _me_dict(db, user)
    d["receipts"] = _last_receipts(db, user)
    d["ref_link"] = (maxapp.ref_deep_link(db, d["ref_code"])
                     if d["ref_code"] else "")
    d["app_version"] = settings.APP_VERSION
    return d


from ..pool.models import PoolUser as _PoolUserModel  # noqa: E402


# ==========================================================================
#  v1.70.0: вход/регистрация на сайте через MAX + приглашения
# ==========================================================================
class ConfirmLoginBody(BaseModel):
    code: str = Field(min_length=16, max_length=160)
    # v1.70.37: initData — необязательна. Нужна, чтобы взять username MAX
    # напрямую из подписанного платформой payload для ВХОДА НА САЙТ: в
    # карточке участника имени до согласия (152-ФЗ) нет.
    init_data: str = Field(default="", max_length=8192)


class InviteTokenBody(BaseModel):
    token: str = Field(min_length=8, max_length=64)
    init_data: str = Field(default="", max_length=8192)   # см. выше


def _max_identity(db: Session, user, init_data: str) -> tuple[str, str]:
    """(username, display) для учётной записи САЙТА по данным MAX.

    v1.70.37: карточка участника до согласия пустая, поэтому имя берём из
    проверенной initData — той же подписи платформы, которой доверяет вход.
    initData чужого участника не принимается: идентификатор сверяется.
    """
    nick = (user.tg_username or "").strip()
    if nick:
        return nick, nick
    raw = None
    if (init_data or "").strip():
        candidate, err = maxapp.validate_init_data(init_data,
                                                   maxapp.bot_token(db))
        if err is None and candidate is not None and \
                str(candidate.get("id")) == str(user.tg_user_id or ""):
            raw = candidate
    if raw is None:
        return "", ""
    uname = str(raw.get("username") or "").strip()
    full = " ".join(x for x in (str(raw.get("first_name") or "").strip(),
                               str(raw.get("last_name") or "").strip()) if x)
    return (uname or full), (full or uname)


@router.post("/confirm-login",
             summary="Подтвердить вход на сайт (код из формы входа)")
def confirm_login(body: ConfirmLoginBody, db: Session = Depends(get_db),
                  user=Depends(require_pool_user)):
    """Вызывается мини-приложением, когда открыто по диплинку
    startapp=login_<код> с сайта: пул-JWT уже доказывает личность MAX."""
    from ..services import max_login
    uname, disp = _max_identity(db, user, body.init_data)
    ok = max_login.confirm(db, body.code, str(user.tg_user_id or ""),
                           username=uname, display=disp)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Код не найден, устарел или уже подтверждён")
    # v1.70.64: владелец — «при нажатии «подтвердить вход» перекидывало на
    # сайт»: мини-приложение открывает web_link, сайт по одноразовому
    # билету входит сам и предлагает вернуться в мини-приложение.
    ticket = max_login.make_webticket(db, str(user.tg_user_id or ""),
                                      username=uname, display=disp)
    # v1.70.65: без фрагмента — ссылка той же формы, что проверенная
    # кнопка профиля; фрагмент в chooser/WebAPK вёл себя непредсказуемо
    return {"status": "confirmed",
            "web_link": f"{max_login.SITE_URL}/?maxlogin={ticket}"}


def _invite_or_none(db: Session, token: str):
    inv = db.query(Invite).filter(
        Invite.token == (token or "").strip()).first()
    return inv if (inv is not None and inv.is_valid) else None


@router.get("/invite",
            summary="Информация о приглашении (для мини-приложения)")
def invite_preview(token: str = Query(default="", min_length=8,
                                      max_length=64),
                   db: Session = Depends(get_db)):
    inv = _invite_or_none(db, token)
    if inv is None:
        return {"valid": False,
                "message": "Приглашение недействительно или уже использовано"}
    comp = db.query(Company).filter(Company.id == inv.company_id).first() \
        if inv.company_id else None
    return {"valid": True, "role": inv.role,
            "company_name": comp.name if comp else "",
            "expires_at": inv.expires_at.strftime("%d.%m.%Y %H:%M")
            if inv.expires_at else ""}


@router.post("/invite/accept",
             summary="Принять приглашение: вход/компания + билет на сайт")
def invite_accept(body: InviteTokenBody, db: Session = Depends(get_db),
                  user=Depends(require_pool_user)):
    """Создаёт учётную запись (или присоединяет «личную» к компании)
    по приглашению и выдаёт одноразовый веб-билет — мини-приложение
    открывает сайт уже залогиненным."""
    from ..services import max_login
    inv = _invite_or_none(db, body.token)
    if inv is None or inv.role not in (ROLE_ACCOUNTANT, ROLE_MANAGER,
                                       ROLE_USER):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Приглашение недействительно или уже использовано")
    max_id = str(user.tg_user_id or "")
    existing = db.query(User).filter(User.max_user_id == max_id).first()
    comp_name = ""
    if inv.company_id:
        _c = db.query(Company).filter(Company.id == inv.company_id).first()
        comp_name = _c.name if _c else ""
    uname, disp = _max_identity(db, user, body.init_data)
    if existing is not None and existing.company_id:
        if inv.company_id and existing.company_id == inv.company_id:
            code = max_login.make_webticket(db, max_id, uname, disp)
            return {"status": "ok", "action": "already",
                    "username": existing.username,
                    "company_name": comp_name,
                    "web_link": f"{max_login.SITE_URL}/#/max-login/{code}"}
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Эта учётная запись уже привязана к другой "
                            "компании — войдите на сайт под своим логином")
    u = max_login.login_or_register(db, max_id, uname, disp, invite=inv)
    code = max_login.make_webticket(db, max_id, uname, disp)
    return {"status": "ok",
            "action": "upgraded" if existing is not None else "registered",
            "username": u.username, "company_name": comp_name,
            "web_link": f"{max_login.SITE_URL}/#/max-login/{code}"}
