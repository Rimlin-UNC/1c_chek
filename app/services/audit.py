# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Журнал аудита: фиксация всех значимых действий пользователей.
"""
from __future__ import annotations

import json
from typing import Any

from ..database import SessionLocal
from ..models import AuditLog, User


def log_action(user: User | None, action: str, entity_type: str = "",
               entity_id: str | None = None, details: dict[str, Any] | None = None) -> None:
    """Запись в журнал аудита (отдельная сессия — переживает запрос)."""
    db = SessionLocal()
    try:
        db.add(AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "system",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details or {}, ensure_ascii=False),
        ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
