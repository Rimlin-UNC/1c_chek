# -*- coding: utf-8 -*-
"""
Ямастер Чек — работа с настройками приложения (key-value). ООО «Ямастер», ymaster.ru
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AppSetting


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    if row is not None and row.value != "":
        return row.value
    return default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def auto_verify_enabled(db: Session) -> bool:
    """Автоматическая проверка чека в ФНС сразу после сканирования."""
    return get_setting(db, "auto_verify", "1") == "1"
