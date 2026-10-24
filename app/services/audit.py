# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru

Журнал аудита: фиксация всех значимых действий пользователей.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError

from ..database import SessionLocal
from ..models import AuditLog, User


# v1.70.38: сколько раз перечитать max(id) при столкновении. Таблица
# audit_log объявлена как «id INTEGER NOT NULL, PRIMARY KEY (id)» — при
# отдельной строке PRIMARY KEY SQLite НЕ делает колонку алиасом rowid и
# сам id не выдаёт, поэтому номер считается вручную. Под нагрузкой два
# потока успевали прочитать один и тот же max → UNIQUE constraint failed →
# запись журнала ТЕРЯЛАСЬ (и именно в момент нагрузки, когда журнал нужнее
# всего). Повтор с перечитанным id устраняет потерю без смены схемы.
MAX_ID_ATTEMPTS = 8

# v1.70.38: в пределах одного процесса номер выдаётся под замком — потоки
# больше не читают один и тот же max(id) одновременно. Замок держится только
# на время короткой вставки (SQLite всё равно сериализует запись), зато
# повторы остаются нужны лишь для нескольких процессов/воркеров.
_ID_LOCK = threading.Lock()


def log_action(user: User | None, action: str, entity_type: str = "",
               entity_id: str | None = None,
               details: dict[str, Any] | None = None) -> bool:
    """Запись в журнал аудита (отдельная сессия — переживает запрос).

    v1.18.0 (исправление): раньше запись молча падала — в SQLite «голый»
    BigInteger PK не автоинкрементируется (NOT NULL id). Теперь id задаётся
    явно (max+1) — работает и с уже созданной таблицей старой схемы; сбой
    больше не тихий (пишется в лог приложения).

    v1.70.38: возвращает True/False — записано или нет, а столкновение id
    в параллельных потоках повторяется (до MAX_ID_ATTEMPTS раз).
    """
    payload = json.dumps(details or {}, ensure_ascii=False)
    err: Exception | None = None
    # v1.70.38: номер выдаётся под замком процесса — потоки больше не читают
    # один и тот же max(id) одновременно (SQLite всё равно сериализует запись,
    # поэтому замок не снижает пропускную способность).
    with _ID_LOCK:
        for attempt in range(MAX_ID_ATTEMPTS):
            db = SessionLocal()
            try:
                next_id = (db.query(
                    func.coalesce(func.max(AuditLog.id), 0)).scalar() or 0) + 1
                db.add(AuditLog(
                    id=next_id,
                    user_id=user.id if user else None,
                    username=user.username if user else "system",
                    company_id=getattr(user, "company_id", None),   # v1.11.0
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    details=payload,
                ))
                db.commit()
                return True
            except (IntegrityError, OperationalError) as e:
                # IntegrityError — столкновение id (другой процесс вставил тот
                # же номер), OperationalError — «database is locked».
                # И то и другое лечится повтором с перечитанным id; пауза с
                # дрожанием, чтобы повторы не шли за одним номером гуськом.
                db.rollback()
                err = e
                time.sleep(0.002 + random.random() * 0.006)
            except Exception as e:                               # noqa: BLE001
                db.rollback()
                err = e
                break
            finally:
                db.close()
    # журнал не должен ломать запрос пользователя, но и молчать нельзя
    logging.getLogger("ymaster").warning(
        "Аудит не записан (%s): %s", action, err)
    return False
