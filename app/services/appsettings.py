# -*- coding: utf-8 -*-
"""
Ямастер Чек — работа с настройками приложения (key-value). ООО «Ямастер», ymaster.ru

v1.16.0: значения из _SECRET_KEYS шифруются при записи (services.secretbox)
и расшифровываются при чтении — в БД секреты лежат только в виде «enc:v1:…».
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AppSetting

# Эти ключи — секреты: в БД хранятся зашифрованными
SECRET_KEYS = frozenset({
    "checko_api_key", "fns_master_token", "proverkacheka_token",
    "ofd_ru_token", "onec_api_token", "telegram_bot_token",
})


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    val = row.value if row is not None and row.value != "" else default
    if key in SECRET_KEYS and val:
        from . import secretbox
        return secretbox.unseal(val)     # легаси (открытые) читаются как есть
    return val


def set_setting(db: Session, key: str, value: str) -> None:
    if key in SECRET_KEYS and value:
        from . import secretbox
        value = secretbox.seal(value)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def auto_verify_enabled(db: Session) -> bool:
    """Автоматическая проверка чека в ФНС сразу после сканирования."""
    return get_setting(db, "auto_verify", "1") == "1"
