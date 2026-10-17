# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Журнал аудита: фиксация всех значимых действий пользователей.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func

from ..database import SessionLocal
from ..models import AuditLog, User


def log_action(user: User | None, action: str, entity_type: str = "",
               entity_id: str | None = None, details: dict[str, Any] | None = None) -> None:
    """Запись в журнал аудита (отдельная сессия — переживает запрос).

    v1.18.0 (исправление): раньше запись молча падала — в SQLite «голый»
    BigInteger PK не автоинкрементируется (NOT NULL id). Теперь id задаётся
    явно (max+1) — работает и с уже созданной таблицей старой схемы; сбой
    больше не тихий (пишется в лог приложения).
    """
    db = SessionLocal()
    try:
        next_id = (db.query(func.coalesce(func.max(AuditLog.id), 0)).scalar() or 0) + 1
        db.add(AuditLog(
            id=next_id,
            user_id=user.id if user else None,
            username=user.username if user else "system",
            company_id=getattr(user, "company_id", None),   # v1.11.0
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details or {}, ensure_ascii=False),
        ))
        db.commit()
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        # журнал не должен ломать запрос пользователя, но и молчать нельзя
        logging.getLogger("ymaster").warning(
            "Аудит не записан (%s): %s", action, e)
    finally:
        db.close()
