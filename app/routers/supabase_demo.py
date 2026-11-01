"""Публичная конфигурация отдельного Supabase демо-контура.

Рабочую БД приложения не заменяет. Возвращает только publishable key.
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from ..auth import require_accountant
from ..config import settings
from ..models import User

router = APIRouter(prefix="/api/v1/supabase-demo", tags=["supabase-demo"])


def public_config() -> dict:
    if not settings.SUPABASE_DEMO_ENABLED:
        return {"enabled": False, "mode": "read_only_demo"}
    url = settings.SUPABASE_URL.rstrip("/")
    key = settings.SUPABASE_PUBLISHABLE_KEY.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    valid_url = (parsed.scheme == "https" and host
                 and (host.endswith(".supabase.co") or host.endswith(".supabase.in")
                      or host in {"localhost", "127.0.0.1"}))
    valid_key = key.startswith("sb_publishable_") and len(key) >= 20
    if not valid_url or not valid_key:
        return {"enabled": False, "mode": "read_only_demo",
                "error": "Supabase демо включён, но URL или publishable key заданы неверно"}
    return {"enabled": True, "mode": "read_only_demo", "url": url,
            "publishable_key": key, "rpc": "chek_demo_snapshot"}


@router.get("/config", summary="Supabase демо: публичная конфигурация")
def config(_user: Annotated[User, Depends(require_accountant)]) -> dict:
    return public_config()
