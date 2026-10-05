# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Настройки: маппинг реквизитов, параметры ФНС и 1С, справочники полей.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_accountant, require_admin, verify_password
from ..config import settings as cfg
from ..database import get_db
from ..models import AppSetting, User
from ..schemas import (AppSettingsPatch, ExternalSettingsPatch,
                       ExternalTestRequest, FnsSettingsPatch, MappingSave,
                       OnecSettingsPatch)
from ..services import appsettings, exporter, secretbox
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
#  v1.2.0: Источники данных о чеке (ФНС API / proverkacheka.com / свой сервис)
# --------------------------------------------------------------------------
@router.get("/external", summary="Настройки источников данных чека (админ)")
def get_external(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from ..services.external import engine
    import json as _json
    try:
        urls = _json.loads(_get_setting(db, "external_custom_urls", "[]"))
    except ValueError:
        urls = []
    return {
        "fns_master_token_masked": _mask(_get_setting(db, "fns_master_token", "")),
        "has_fns_master_token": bool(_get_setting(db, "fns_master_token", "")),
        "proverkacheka_token_masked": _mask(_get_setting(db, "proverkacheka_token", "")),
        "has_proverkacheka_token": bool(_get_setting(db, "proverkacheka_token", "")),
        "ofd_ru_token_masked": _mask(_get_setting(db, "ofd_ru_token", "")),
        "has_ofd_ru_token": bool(_get_setting(db, "ofd_ru_token", "")),
        "fns_app_inn": _get_setting(db, "fns_app_inn", ""),                       # v1.27.0
        "has_fns_app_login": bool(_get_setting(db, "fns_app_inn", "")
                                  and _get_setting(db, "fns_app_password", "")),
        "fns_app_secret_masked": _mask(_get_setting(db, "fns_app_secret", "")),
        "has_fns_app_secret": bool(_get_setting(db, "fns_app_secret", "")),
        "external_custom_url": _get_setting(db, "external_custom_url", ""),
        "external_custom_urls": [u for u in urls if isinstance(u, dict)],
        "external_order": _get_setting(db, "external_order",
                                       "fns_api,fns_app,crpt,ofd_ru,custom,proverkacheka"),
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
    if body.ofd_ru_token is not None and "•" not in body.ofd_ru_token:
        _set_setting(db, "ofd_ru_token", body.ofd_ru_token.strip())
    # v1.27.0: «Приложение ФНС» (полный чек по ИНН+паролю ЛК)
    if body.fns_app_inn is not None:
        _set_setting(db, "fns_app_inn", body.fns_app_inn.strip())
    if body.fns_app_password is not None and "•" not in body.fns_app_password:
        _set_setting(db, "fns_app_password", body.fns_app_password.strip())
    if body.fns_app_secret is not None and "•" not in body.fns_app_secret:
        _set_setting(db, "fns_app_secret", body.fns_app_secret.strip())
    if body.external_custom_urls is not None:
        cleaned = [{"name": c.name.strip()[:60], "url": c.url.strip()[:500]}
                   for c in body.external_custom_urls]
        for c in cleaned:
            if not c["url"].lower().startswith(("http://", "https://")):
                raise HTTPException(400, f"URL источника «{c['name']}» должен начинаться с http(s)://")
        _set_setting(db, "external_custom_urls",
                     __import__("json").dumps(cleaned, ensure_ascii=False))
    if body.external_custom_url is not None:
        url = body.external_custom_url.strip()
        if url and not url.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "URL должен начинаться с http:// или https://")
        _set_setting(db, "external_custom_url", url)
    if body.external_order is not None:
        allowed = {"fns_api", "ofd_ru", "proverkacheka", "custom"}
        items = [x.strip() for x in body.external_order.split(",") if x.strip() in allowed]
        _set_setting(db, "external_order",
                     ",".join(items or ["fns_api", "ofd_ru", "proverkacheka", "custom"]))
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
@router.get("/telegram", summary="Настройки Telegram-бота (админ)")
def get_telegram(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import telegram_bot as tg
    s = tg.bot_settings(db)
    from ..services.appsettings import get_setting as _g
    key = _mask(_g(db, tg.SETT_TOKEN, ""))
    return {**s, "token_masked": key,
            "bound_users": db.query(User).filter(
                User.telegram_chat_id.isnot(None)).count(),
            "hint": "Токен от @BotFather хранится зашифрованным. Сотрудники "
                    "подключаются сами: Настройки → Telegram → код → /start КОД."}


@router.put("/telegram", summary="Сохранить настройки Telegram-бота (админ)")
def put_telegram(body: dict, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    import re as _re
    from ..services import telegram_bot as tg
    from ..services.appsettings import set_setting as _s
    token = ((body or {}).get("bot_token") or "").strip()
    if token:
        if not _re.fullmatch(r"\d{6,}:[A-Za-z0-9_\-]{30,}", token):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Токен бота неверный — скопируйте его целиком "
                                "из @BotFather (вид: 123456789:AA…)")
        _s(db, tg.SETT_TOKEN, token)                 # уйдёт зашифрованным
        try:
            tg.get_me(token)
        except tg.TelegramError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
        tg.refresh_username(db)                      # кэш username бота
    if "enabled" in (body or {}):
        _s(db, tg.SETT, "1" if body["enabled"] else "0")
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
