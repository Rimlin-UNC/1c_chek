"""Обезличенные коды участников для чеков, уже закреплённых за компанией.

Код не заменяет права доступа: исходные ID и контакты не возвращаются.
Один участник получает разные коды в разных компаниях. Новые таблицы и
копирование фискальных данных не требуются.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from ..config import settings

UNKNOWN_PARTICIPANT = "unknown"
_DOMAIN = b"ymaster-pool-company-participant-v1:"


def participant_key(company_id: str, pool_user_id: str | None) -> str:
    """Необратимый код пары компания + участник; пустая связь — unknown.

    SECRET_KEY используется только как ключ HMAC и не попадает в ответ.
    После смены ключа коды изменятся; сохранённых сопоставлений в БД нет.
    Непробельные части идентификаторов сохраняются без нормализации.
    """
    if (not isinstance(company_id, str) or not company_id.strip()
            or not isinstance(pool_user_id, str) or not pool_user_id.strip()):
        return UNKNOWN_PARTICIPANT
    payload = json.dumps([company_id, pool_user_id], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), _DOMAIN + payload,
                    hashlib.sha256).hexdigest()[:32]


def participant_label(key: str) -> str:
    """Короткая подпись; точное значение фильтра — полный ключ, не подпись."""
    if key == UNKNOWN_PARTICIPANT:
        return "Без участника"
    return "Участник " + key[:12].upper()
