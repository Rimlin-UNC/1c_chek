# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — мини-приложение MAX (v1.69.0).
# Валидация initData по документации dev.max.ru/docs/webapps/validation:
#   1. initData → пары key=value (из #WebAppData или «сырые» из Bridge)
#   2. hash встречается ровно один раз; исходное значение сохраняем
#   3. URL-декодирование значений
#   4. сортировка по ключу a→z; строка "k=v\nk=v" (без hash)
#   5. secret = HMAC-SHA256(key="WebAppData", msg=bot_token)
#   6. hex(HMAC-SHA256(key=secret, msg=launch_params)) == hash
# Дополнительно: auth_date не старше 24 часов (рекомендация доки — 1 ч,
# у нас окно шире: mini-app пересоздают сессию при каждом запуске).
# Токен бота — секрет max_bot_token (настройки, шифруется); в коде
# никаких токенов нет.
# ======================================================================
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse

log = logging.getLogger("ymaster.maxapp")

MAX_INITDATA_LEN = 8192          # initData ~ до 2 КБ; 8 КБ с запасом
AUTH_TTL = 24 * 3600             # сек: максимум возраста auth_date
REQUIRED_KEYS = ("auth_date", "hash", "user")


def parse_pairs(init_data: str) -> list[tuple[str, str]]:
    """Пары key=value из initData.

    Bridge отдаёт window.WebApp.initData как URL-закодированную строку
    «key=urlencoded-value» (это же значение живёт в #WebAppData=…).
    Поддерживаем оба вида: закодированный (есть %XX) и «сырой» JSON.
    Значение hash не декодируем — сравниваем как есть.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in (init_data or "").split("&"):
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        if k in seen or not k:
            return []
        seen.add(k)
        if k != "hash" and ("%" in v or "+" in v):
            v = urllib.parse.unquote(v)
        out.append((k, v))
    return out


def validate_init_data(init_data: str, bot_token: str) -> tuple[dict | None, str | None]:
    """→ (user_dict, None) при успехе; (None, код) при отказе.

    Коды: not_configured | format | bad_hash | expired. Единый ответ
    «bad_hash» на любые проблемы подписи (без деталей — анти-оракул)."""
    if not bot_token:
        return None, "not_configured"
    if not init_data or len(init_data) > MAX_INITDATA_LEN:
        return None, "format"
    pairs = parse_pairs(init_data)
    kv = dict(pairs)
    if not pairs or sum(1 for k, _ in pairs if k == "hash") != 1:
        return None, "format"
    for k in REQUIRED_KEYS:
        if k not in kv:
            return None, "format"
    try:
        auth_date = int(kv["auth_date"])
    except ValueError:
        return None, "format"
    if abs(time.time() - auth_date) > AUTH_TTL:
        return None, "expired"
    provided_hash = kv["hash"]
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    def _calc(extra_decode: bool) -> str:
        # доковский формат: значения закодированы один раз; страховка на
        # случай двойного энкода клиента/платформы — вторая попытка
        launch = "\n".join(
            f"{k}={(urllib.parse.unquote(v) if extra_decode else v)}"
            for k, v in sorted(pairs) if k != "hash")
        return hmac.new(secret, launch.encode(), hashlib.sha256).hexdigest()

    ok_plain = hmac.compare_digest(_calc(False), provided_hash)
    ok_extra = (not ok_plain and
                hmac.compare_digest(_calc(True), provided_hash))
    if not (ok_plain or ok_extra):
        return None, "bad_hash"
    try:
        user = json_loads(kv["user"] if ok_plain
                          else urllib.parse.unquote(kv["user"]))
        assert isinstance(user, dict) and str(user.get("id", "")).strip()
    except Exception:                                    # noqa: BLE001
        return None, "format"
    return user, None


def json_loads(raw: str):
    import json
    return json.loads(raw)


def bot_token(db) -> str:
    from . import appsettings
    return appsettings.get_setting(db, "max_bot_token", "") or ""


def ref_deep_link(db, ref_code: str) -> str:
    """Диплинк приглашения: открывает мини-приложение бота с кодом
    (ник бота — фактический, из настроек, v1.70.1)."""
    from . import max_bot
    return f"https://max.ru/{max_bot.bot_username(db)}?startapp=ref_{ref_code}"


def parse_start_param(sp: str) -> dict:
    """start_param → {'ref': 'YM-XXXXXX'} (только безопасный формат)."""
    out: dict = {}
    sp = (sp or "").strip()
    if sp.startswith("ref_") and 4 < len(sp) <= 16:
        code = sp[4:]
        if all(c.isalnum() or c == "-" for c in code):
            out["ref"] = code.upper()
    return out
