# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Настройки: маппинг реквизитов, параметры ФНС и 1С, справочники полей.
"""
from __future__ import annotations

import secrets

from fastapi import (APIRouter, Depends, HTTPException, Query,
                     Request, status)
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_accountant, require_admin, verify_password
from ..config import settings as cfg
from ..database import get_db
from ..models import AppSetting, Company, Receipt, User
from ..schemas import (AppSettingsPatch, ExternalSettingsPatch, PolicyPatch,
                       ExternalTestRequest, FnsSettingsPatch, MappingSave,
                       OnecSettingsPatch)
from ..services import appsettings, exporter, secretbox
from sqlalchemy import func
from ..security import limiter
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/settings", tags=["Настройки"])


# --------------------------------------------------------------------------
#  Маппинг реквизитов (доступен всем пользователям на чтение)
# --------------------------------------------------------------------------
@router.get("/mapping", summary="Настройки маппинга чек → 1С")
def get_mapping(db: Session = Depends(get_db), user: User = Depends(require_accountant)):
    from ..models import MappingSetting
    rows = (db.query(MappingSetting)
            .order_by(MappingSetting.position, MappingSetting.source_field).all())
    return {"items": [m.to_dict() for m in rows]}


@router.put("/mapping", summary="Сохранить настройки маппинга")
def save_mapping(body: MappingSave, user: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    from ..models import MappingSetting
    existing = {m.id: m for m in db.query(MappingSetting).all()}
    seen: set[str] = set()
    for i, item in enumerate(body.items):
        obj = existing.get(item.id) if item.id else None
        if obj is None:
            obj = MappingSetting()
            db.add(obj)
        obj.source_field = item.source_field
        obj.target_object = item.target_object
        obj.target_field = item.target_field
        obj.transform = item.transform
        obj.transform_param = item.transform_param
        obj.is_active = item.is_active
        obj.position = i
        seen.add(obj.id)
    # Удаляем записи, которых больше нет в присланном списке
    for mid, obj in existing.items():
        if mid not in seen:
            db.delete(obj)
    db.commit()
    log_action(user, "mapping_updated", details={"count": len(body.items)})
    rows = (db.query(MappingSetting)
            .order_by(MappingSetting.position, MappingSetting.source_field).all())
    return {"items": [m.to_dict() for m in rows]}


# --------------------------------------------------------------------------
#  Справочники для конструктора маппинга
# --------------------------------------------------------------------------
@router.get("/mapping/catalog", summary="Справочник полей и документов 1С")
def mapping_catalog(user: User = Depends(get_current_user)):
    return {
        "source_fields": exporter.SOURCE_FIELDS,
        "target_objects": exporter.TARGET_OBJECTS,
        "transforms": exporter.TRANSFORMS,
    }


# --------------------------------------------------------------------------
#  Настройки ФНС (только админ)
# --------------------------------------------------------------------------
# v1.16.0: шифрование секретов живёт в appsettings (единый источник) —
# здесь только тонкие обёртки для совместимости.


def _get_setting(db: Session, key: str, default: str = "") -> str:
    return appsettings.get_setting(db, key, default)


def _set_setting(db: Session, key: str, value: str) -> None:
    appsettings.set_setting(db, key, value)


def _mask(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 6:
        return "•••"
    return token[:3] + "•" * max(6, len(token) - 6) + token[-3:]


@router.get("/fns", summary="Настройки проверки ФНС")
def get_fns(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return {
        "provider": _get_setting(db, "fns_provider", cfg.FNS_PROVIDER),
        "api_base": _get_setting(db, "fns_api_base", cfg.FNS_API_BASE),
        "master_token_masked": _mask(_get_setting(db, "fns_master_token", cfg.FNS_MASTER_TOKEN)),
        "has_master_token": bool(_get_setting(db, "fns_master_token", cfg.FNS_MASTER_TOKEN)),
        "client_app_id": _get_setting(db, "fns_client_app_id", cfg.FNS_CLIENT_APP_ID),
        "cache_ttl_days": cfg.FNS_CACHE_TTL_DAYS,
    }


@router.put("/fns", summary="Сохранить настройки ФНС")
def put_fns(body: FnsSettingsPatch, db: Session = Depends(get_db),
            user: User = Depends(require_admin)):
    _set_setting(db, "fns_provider", body.provider)
    if body.api_base:
        _set_setting(db, "fns_api_base", body.api_base.strip())
    if body.master_token is not None and "•" not in body.master_token:
        _set_setting(db, "fns_master_token", body.master_token.strip())
    if body.client_app_id:
        _set_setting(db, "fns_client_app_id", body.client_app_id.strip())
    log_action(user, "fns_settings_updated", details={"provider": body.provider})
    return {"ok": True, "message": "Настройки ФНС сохранены"}


# --------------------------------------------------------------------------
#  Настройки 1С: токен Push-доступа
# --------------------------------------------------------------------------
@router.get("/onec", summary="Настройки интеграции с 1С")
def get_onec(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    token = _get_setting(db, "onec_api_token", cfg.ONEC_API_TOKEN)
    return {
        "api_token_masked": _mask(token),
        "has_token": bool(token),
        "batch_size": cfg.ONEC_BATCH_SIZE,
        "pull_url_hint": "/onec/v1/receipts/pull",
    }


@router.put("/onec", summary="Обновить токен 1С")
def put_onec(body: OnecSettingsPatch, db: Session = Depends(get_db),
             user: User = Depends(require_admin)):
    token = _get_setting(db, "onec_api_token", cfg.ONEC_API_TOKEN)
    if body.regen_token:
        token = secrets.token_urlsafe(32)
        _set_setting(db, "onec_api_token", token)
    elif body.api_token and "•" not in body.api_token:
        token = body.api_token.strip()
        _set_setting(db, "onec_api_token", token)
    log_action(user, "onec_token_updated")
    return {"ok": True, "message": "Токен 1С обновлён"}


@router.get("/onec/reveal", summary="Показать токен 1С полностью (админ)")
def reveal_onec(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return {"api_token": _get_setting(db, "onec_api_token", cfg.ONEC_API_TOKEN)}


# --------------------------------------------------------------------------
#  Общие настройки приложения (админ)
# --------------------------------------------------------------------------
@router.get("/app", summary="Общие настройки приложения (админ/бухгалтер)")
def get_app_settings(db: Session = Depends(get_db),
                     user: User = Depends(require_accountant)):
    # v1.8.0: бухгалтеру нужен срок авансового отчёта для контроля просрочки
    try:
        deadline = int(appsettings.get_setting(db, "advance_deadline_days", "10") or 10)
    except ValueError:
        deadline = 10
    return {
        "auto_verify": appsettings.auto_verify_enabled(db),
        "advance_deadline_days": deadline,
    }


@router.put("/app", summary="Сохранить общие настройки")
def put_app_settings(body: AppSettingsPatch, db: Session = Depends(get_db),
                     user: User = Depends(require_admin)):
    if body.auto_verify is not None:
        appsettings.set_setting(db, "auto_verify", "1" if body.auto_verify else "0")
    if body.advance_deadline_days is not None:
        appsettings.set_setting(db, "advance_deadline_days", str(body.advance_deadline_days))
    log_action(user, "app_settings_updated", details={
        "auto_verify": body.auto_verify,
        "advance_deadline_days": body.advance_deadline_days})
    return {"ok": True, "message": "Настройки сохранены"}


# --------------------------------------------------------------------------
#  v1.62.0: Политика расходов — лимиты ₽/чек, ₽/мес на сотрудника,
#  запрещённые статьи расходов (на каждую компанию). Мягкий комплаенс:
#  чек с нарушением НЕ блокируется — пометка policy_flags + чип в списке
#  + счётчик в сводке. Чтение — бухгалтер+, изменение — администратор.
# --------------------------------------------------------------------------
def _default_company(db: Session) -> Company | None:
    return (db.query(Company).filter(Company.is_active == True)  # noqa: E712
            .order_by(Company.created_at).first())


@router.get("/policy", summary="Политика расходов компании (бухгалтер+)")
def get_policy(company_id: str | None = Query(None, max_length=36),
               db: Session = Depends(get_db),
               user: User = Depends(require_accountant)):
    comp = None
    if user.role == "admin":
        if company_id:
            comp = db.get(Company, company_id)
            if not comp:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "Компания не найдена")
        else:
            comp = _default_company(db)
    else:
        comp = db.get(Company, user.company_id) if user.company_id else None
    flagged = 0
    if comp:
        flagged = (db.query(func.count(Receipt.id))
                   .filter(Receipt.company_id == comp.id,
                           Receipt.policy_flags != "").scalar()) or 0
    from ..services import policy as policy_service
    return {
        "company_id": comp.id if comp else None,
        "company_name": comp.name if comp else None,
        "limit_per_check": comp.limit_per_check if comp else None,
        "limit_per_month": comp.limit_per_month if comp else None,
        "forbidden_categories": (policy_service.parse_categories(
            comp.forbidden_categories) if comp else []),
        "flagged": int(flagged),     # чеков с нарушениями политики сейчас
    }


@router.put("/policy", summary="Сохранить политику расходов (администратор)")
def put_policy(body: PolicyPatch, db: Session = Depends(get_db),
               user: User = Depends(require_admin)):
    if body.company_id:
        comp = db.get(Company, body.company_id)
        if not comp:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "Компания не найдена")
    else:
        comp = _default_company(db)
        if not comp:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "Нет активных компаний")
    if body.limit_per_check is not None:
        comp.limit_per_check = body.limit_per_check or None    # 0 = снять
    if body.limit_per_month is not None:
        comp.limit_per_month = body.limit_per_month or None    # 0 = снять
    if body.forbidden_categories is not None:
        cats = [c.strip()[:100] for c in body.forbidden_categories
                if c and c.strip()][:50]
        comp.forbidden_categories = ", ".join(cats)
    from ..services import policy as policy_service
    flagged = policy_service.refresh_company(db, comp.id)
    db.commit()
    log_action(user, "policy_updated", "company", comp.id, {
        "limit_per_check": comp.limit_per_check,
        "limit_per_month": comp.limit_per_month,
        "forbidden_categories": comp.forbidden_categories,
        "flagged": flagged})
    return {"ok": True, "message": "Политика расходов сохранена",
            "flagged": int(flagged), "company_id": comp.id}


# --------------------------------------------------------------------------
#  v1.2.0: Источники данных о чеке (ФНС API / proverkacheka.com / свой сервис)
# --------------------------------------------------------------------------
@router.get("/external", summary="Настройки источников данных чека (админ)")
def get_external(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from ..services.external import engine
    return {
        "fns_master_token_masked": _mask(_get_setting(db, "fns_master_token", "")),
        "has_fns_master_token": bool(_get_setting(db, "fns_master_token", "")),
        "proverkacheka_token_masked": _mask(_get_setting(db, "proverkacheka_token", "")),
        "has_proverkacheka_token": bool(_get_setting(db, "proverkacheka_token", "")),
        "external_order": _get_setting(db, "external_order",
                                       "proverkacheka"),
        "external_auto": _get_setting(db, "external_auto", "1") == "1",
        "engine": engine.status(),
    }


@router.put("/external", summary="Сохранить источники данных чека (админ)")
def put_external(body: ExternalSettingsPatch, db: Session = Depends(get_db),
                 user: User = Depends(require_admin)):
    if body.fns_master_token is not None and "•" not in body.fns_master_token:
        _set_setting(db, "fns_master_token", body.fns_master_token.strip())
    if body.proverkacheka_token is not None and "•" not in body.proverkacheka_token:
        _set_setting(db, "proverkacheka_token", body.proverkacheka_token.strip())
    # v1.55.0/v1.57.0: выведенные источники (свои шлюзы, Приложение ФНС,
    # Честный Знак, ОФД-ру) игнорируются — соответствующих полей в схеме
    # больше нет; строки в БД не трогаем (миграции только добавляющие)
    if body.external_order is not None:
        allowed = {"fns_api", "proverkacheka"}
        items = [x.strip() for x in body.external_order.split(",") if x.strip() in allowed]
        _set_setting(db, "external_order",
                     ",".join(items or ["proverkacheka"]))
    if body.external_auto is not None:
        _set_setting(db, "external_auto", "1" if body.external_auto else "0")
    log_action(user, "external_settings_updated", details={"auto": body.external_auto})
    return {"ok": True, "message": "Источники данных сохранены"}


@router.post("/external/test", summary="Проверить источник контрольным чеком (админ)")
def test_external(body: ExternalTestRequest, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    """Синхронный тест: движок сам выдержит паузу 2–7 с и вернёт ответ источника."""
    from ..services.external import engine
    qr = body.qrraw or ("t=20260927T1529&s=2150.00&fn=7381440700130934"
                        "&i=33079&fp=3673437411&n=1")
    from ..services.qr import parse_qr, QRParseError
    try:
        parsed = parse_qr(qr)
    except QRParseError as e:
        return {"ok": False, "message": f"Строка QR не распознана: {e}"}
    res = engine.fetch(db, qr, parsed.fn, parsed.fd, parsed.fp,
                       parsed.total_sum, parsed.date_time)
    ok, message = res.ok, res.message
    # v1.7.0: mock-источник всегда «доступен» — чек не находится в ФНС это его
    # нормальный режим, а не ошибка (раньше тест показывал сбой без причины)
    if res.source == "mock" and not res.found:
        ok = True
        message = ("Мок-источник работает корректно: тестовый чек не найден "
                   "в ФНС (эмуляция) — это ожидаемое поведение")
    return {
        "ok": ok,
        "source": res.source,
        "found": res.found,
        "items_count": len(res.items),
        "message": message,
        "engine": engine.status(),
    }


@router.get("/checko", summary="Настройки Checko.ru — карточки компаний по ИНН (админ)")
def get_checko(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    key = _get_setting(db, "checko_api_key", "")
    return {"has_key": bool(key), "key_masked": _mask(key),
            "encrypted": key.startswith("enc:") or not key,
            "hint": "Ключ из личного кабинета checko.ru → вкладка API. "
                    "Бесплатный тариф: 100 запросов в день, техлимит 32 запроса/сек.",
            "storage": "ключ хранится зашифрованным (Fernet/AES + HMAC), "
                       "показывается только вам по паролю"}


@router.put("/checko", summary="Сохранить API-ключ Checko.ru (админ)")
def put_checko(body: dict, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    key = ((body or {}).get("api_key") or "").strip()
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Укажите API-ключ Checko")
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9._\-]{12,120}", key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Ключ выглядит неправильно (12–120 символов без "
                            "пробелов). Скопируйте его из личного кабинета "
                            "checko.ru → API целиком")
    _set_setting(db, "checko_api_key", key)      # уйдёт в БД зашифрованным
    log_action(admin, "checko_key_saved", details={"tail": key[-4:]})
    return {"ok": True, "key_masked": _mask(key),
            "message": "Ключ Checko сохранён (зашифрован) — карточки компаний "
                       "заполняются из ЕГРЮЛ/ЕГРИП по ИНН"}


@router.post("/checko/test",
             summary="Проверить ключ Checko живым запросом (админ, 1 запрос/день)")
def test_checko_key(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import checko
    key = _get_setting(db, "checko_api_key", "")
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Сначала сохраните API-ключ Checko")
    try:
        result = checko.test_key(key)
    except checko.CheckoError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    log_action(admin, "checko_key_tested",
               details={"meta": result.get("meta", {})})
    return result


class CheckoRevealBody(dict):
    """body {password} — подтверждение паролем администратора."""


@router.post("/checko/reveal",
             summary="Показать ключ Checko (подтверждение паролем, аудит)")
def reveal_checko(body: dict, request: Request,
                  db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    # защита от подбора пароля: 5 попыток / 5 минут с одного IP
    ip = request.client.host if request.client else "?"
    if not limiter.allow(f"checko-reveal:{ip}", limit=5, window_s=300):
        log_action(admin, "checko_key_reveal_blocked", details={"ip": ip})
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Слишком много попыток — повторите через несколько минут")
    password = (body or {}).get("password") or ""
    if not verify_password(password, admin.password_hash):
        log_action(admin, "checko_key_reveal_failed", details={"ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный пароль")
    key = _get_setting(db, "checko_api_key", "")
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ключ Checko ещё не задан")
    log_action(admin, "checko_key_revealed", details={"ip": ip})
    return {"api_key": key,
            "warning": "Ключ показан один раз и записан в журнал аудита"}


# --------------------------------------------------------------------------
#  v1.16.0: Telegram-бот (токен — секрет, шифруется; worker с hot-restart)
# --------------------------------------------------------------------------
@router.get("/max", summary="Настройки ассистента MAX (админ)")
def get_max_settings(db: Session = Depends(get_db),
                     admin: User = Depends(require_admin)):
    from ..services import max_bot as mx
    from ..services.appsettings import get_setting as _g
    s = mx.bot_settings(db)
    return {**s, "token_masked": _mask(_g(db, mx.SETT_TOKEN, "")),
            "worker_running": mx.worker_is_alive(),
            "bound_users": db.query(User).filter(
                User.max_user_id.isnot(None)).count(),
            "hint": "Токен бота MAX — на платформе business.max.ru "
                    "(Чат-боты → ⋮ → Настройки). Токен хранится зашифрованным. "
                    "Сервер Минцифры — прокси не нужен. Сотрудники подключаются "
                    "сами: Настройки → MAX — личное → код → /start КОД."}


@router.put("/max", summary="Сохранить настройки ассистента MAX (админ)")
def put_max_settings(body: dict, db: Session = Depends(get_db),
                     admin: User = Depends(require_admin)):
    import re as _re
    from ..services import max_bot as mx
    from ..services.appsettings import set_setting as _s
    body = body or {}
    if "channel" in body:
        ch = (body.get("channel") or "").strip()
        if ch not in ("telegram", "max", "both"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Канал: telegram, max или both")
        _s(db, mx.SETT_CHANNEL, ch)
    token = (body.get("bot_token") or "").strip()
    if token and token != "-":
        # формат токена MAX не опубликован — мягкая проверка + живой get_me
        if not _re.fullmatch(r"[A-Za-z0-9_\-]{20,}", token):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Токен MAX — строка с платформы business.max.ru "
                                "(Чат-боты → ⋮ → Настройки → токен)")
        _s(db, mx.SETT_TOKEN, token)                 # уйдёт зашифрованным
        try:
            mx.get_me(token)
        except mx.MaxError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
        mx.refresh_username(db)
    elif token == "-":
        _s(db, mx.SETT_TOKEN, "")
    if "enabled" in body:
        _s(db, mx.SETT, "1" if body["enabled"] else "0")
    if "reminder_time" in body:
        tme = (body.get("reminder_time") or "").strip()
        if tme:
            m = _re.fullmatch(r"(\d{2}):(\d{2})", tme)
            if not m or not (0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "Время напоминаний — в формате ЧЧ:ММ (например 18:00)")
            _s(db, mx.SETT_TIME, tme)
    if "reminder_text" in body:
        _s(db, mx.SETT_TEXT, (body.get("reminder_text") or "").strip()[:500])
    if "digest_enabled" in body:
        _s(db, mx.SETT_DIGEST, "1" if body["digest_enabled"] else "0")
    started = mx.restart_worker()
    log_action(admin, "max_settings_saved",
               details={"enabled": bool(body.get("enabled", False)),
                        "channel": mx.get_channel(db), "worker": started})
    return {"ok": True, "worker_running": started,
            "message": ("MAX-бот запущен — напоминания и дайджест по расписанию"
                        if started else
                        "Сохранено. Бот включится, когда зададите токен и включите тумблер")}


@router.post("/max/test", summary="Тестовое сообщение себе в MAX (админ)")
def test_max(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import max_bot as mx
    if not admin.max_user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Сначала привяжите MAX: блок «MAX — личное» ниже")
    try:
        mx.send_message(mx.get_token(db), admin.max_user_id,
                        "🧪 Тест: уведомления «Ямастер Чек» работают!")
    except mx.MaxError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    return {"ok": True, "message": "Отправлено — проверьте MAX"}


@router.get("/telegram", summary="Настройки Telegram-бота (админ)")
def get_telegram(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import telegram_bot as tg
    s = tg.bot_settings(db)
    from ..services.appsettings import get_setting as _g
    key = _mask(_g(db, tg.SETT_TOKEN, ""))
    proxy_masked = _mask(_g(db, tg.SETT_PROXY, ""))
    from ..services import max_bot as _mx
    return {**s, "token_masked": key,
            "proxy_masked": proxy_masked,
            "channel": _mx.get_channel(db),   # v1.66.0: канал ассистентов
            "bound_users": db.query(User).filter(
                User.telegram_chat_id.isnot(None)).count(),
            "hint": "Токен от @BotFather хранится зашифрованным. Если сервер "
                    "не видит api.telegram.org (частая блокировка в РФ) — "
                    "укажите прокси. Сотрудники подключаются сами: "
                    "Настройки → Telegram → код → /start КОД."}


@router.put("/telegram", summary="Сохранить настройки Telegram-бота (админ)")
def put_telegram(body: dict, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    import re as _re
    from ..services import telegram_bot as tg
    from ..services.appsettings import set_setting as _s
    # v1.29.0: прокси сохраняем ДО проверки токена — проверка пойдёт через него.
    # Пусто = не менять; "-" = убрать прокси.
    if "proxy" in (body or {}):
        proxy = ((body.get("proxy")) or "").strip()
        if proxy == "-":
            _s(db, tg.SETT_PROXY, "")
        elif proxy:
            if not proxy.lower().startswith(tg.PROXY_SCHEMES):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "Прокси должен начинаться с socks5:// или http:// "
                                    "(например socks5://логин:пароль@хост:порт)")
            _s(db, tg.SETT_PROXY, proxy)             # уйдёт зашифрованным (секрет)
    token = ((body or {}).get("bot_token") or "").strip()
    if token:
        if not _re.fullmatch(r"\d{6,}:[A-Za-z0-9_\-]{30,}", token):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Токен бота неверный — скопируйте его целиком "
                                "из @BotFather (вид: 123456789:AA…)")
        _s(db, tg.SETT_TOKEN, token)                 # уйдёт зашифрованным
        try:
            tg.get_me(token, proxy=tg.get_proxy(db))
        except tg.TelegramError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
        tg.refresh_username(db)                      # кэш username бота
    if "enabled" in (body or {}):
        _s(db, tg.SETT, "1" if body["enabled"] else "0")
    if "channel" in (body or {}):            # v1.66.0: канал ассистентов
        from ..services.max_bot import SETT_CHANNEL as _ASSIST_CH
        ch = (body.get("channel") or "").strip()
        if ch not in ("telegram", "max", "both"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Канал: telegram, max или both")
        _s(db, _ASSIST_CH, ch)
    if "reminder_time" in (body or {}):
        tme = (body["reminder_time"] or "").strip()
        if tme:
            m = _re.fullmatch(r"(\d{2}):(\d{2})", tme)
            if not m or not (0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "Время напоминаний — в формате ЧЧ:ММ (например 18:00)")
            _s(db, tg.SETT_TIME, tme)
    if "reminder_text" in (body or {}):
        _s(db, tg.SETT_TEXT, (body["reminder_text"] or "").strip()[:500])
    started = tg.restart_worker()
    log_action(admin, "telegram_settings_saved",
               details={"enabled": bool((body or {}).get("enabled", False)),
                        "worker": started})
    return {"ok": True, "worker_running": started,
            "message": ("Бот запущен — напоминания по расписанию"
                        if started else
                        "Сохранено. Бот включится, когда зададите токен и включите тумблер")}


@router.post("/telegram/test", summary="Тестовое сообщение себе в Telegram (админ)")
def test_telegram(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import telegram_bot as tg
    if not admin.telegram_chat_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Сначала привяжите свой чат: блок «Telegram — личное» ниже")
    try:
        tg.send_message(tg.get_token(db), admin.telegram_chat_id,
                        "🧪 Тест: уведомления «Ямастер Чек» работают!")
    except tg.TelegramError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    return {"ok": True, "message": "Отправлено — проверьте Telegram"}


@router.post("/telegram/diagnose", summary="Диагностика доступа к Telegram (админ)")
def diagnose_telegram(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import telegram_bot as tg
    return tg.diagnose(db)


@router.post("/telegram/refresh-bot", summary="Обновить username бота (админ)")
def refresh_bot(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import telegram_bot as tg
    username = tg.refresh_username(db)
    if not username:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Не удалось получить данные бота — проверьте токен")
    return {"ok": True, "bot_username": username}


@router.get("/app/summary", summary="Краткие настройки для всех ролей")
def app_summary(db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Информативные блоки настроек (v1.17.0): срок авансовых отчётов,
    авто-проверка ФНС — видно бухгалтеру и сотруднику (только чтение)."""
    from ..services import appsettings
    return {
        "advance_deadline_days": int(
            appsettings.get_setting(db, "advance_deadline_days", "10") or 10),
        "auto_verify": appsettings.get_setting(db, "auto_verify", "1") == "1",
        "version": cfg.APP_VERSION,
    }
