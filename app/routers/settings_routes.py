# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Настройки: маппинг реквизитов, параметры ФНС и 1С, справочники полей.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_accountant, require_admin
from ..config import settings as cfg
from ..database import get_db
from ..models import AppSetting, User
from ..schemas import (AppSettingsPatch, ExternalSettingsPatch,
                       ExternalTestRequest, FnsSettingsPatch, MappingSave,
                       OnecSettingsPatch)
from ..services import appsettings, exporter
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
def _get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row and row.value != "" else default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


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
@router.get("/app", summary="Общие настройки приложения")
def get_app_settings(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return {
        "auto_verify": appsettings.auto_verify_enabled(db),
    }


@router.put("/app", summary="Сохранить общие настройки")
def put_app_settings(body: AppSettingsPatch, db: Session = Depends(get_db),
                     user: User = Depends(require_admin)):
    if body.auto_verify is not None:
        appsettings.set_setting(db, "auto_verify", "1" if body.auto_verify else "0")
    log_action(user, "app_settings_updated", details={"auto_verify": body.auto_verify})
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
        "external_custom_url": _get_setting(db, "external_custom_url", ""),
        "external_custom_urls": [u for u in urls if isinstance(u, dict)],
        "external_order": _get_setting(db, "external_order",
                                       "fns_api,ofd_ru,proverkacheka,custom"),
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
    return {
        "ok": res.ok,
        "source": res.source,
        "found": res.found,
        "items_count": len(res.items),
        "message": res.message,
        "engine": engine.status(),
    }
