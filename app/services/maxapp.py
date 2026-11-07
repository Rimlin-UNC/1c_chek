# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — мини-приложение MAX (v1.69.0).
# Валидация initData по документации dev.max.ru/docs/webapps/validation:
#   1. initData → пары key=value (из #WebAppData или «сырые» из Bridge)
#   2. hash встречается ровно один раз; исходное значение сохраняем
#   3. parse_qsl: раскрытие %XX И «+»→пробел (v1.70.36: раньше «+» не
#      раскрывался — подпись не сходилась ни на одном стандартном initData)
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
    """Пары key=value из initData — как их видит официальный алгоритм
    (parse_qsl): раскрываются И %XX, И «+» как пробел.

    v1.70.36 — исправление причины «Подпись MAX не совпала»: прежний разбор
    декодировал только %XX (urllib.parse.unquote), а «+» оставлял как есть.
    Платформа кодирует пробелы знаком «+» (urlencode), а в user всегда есть
    пробелы (разделители JSON), поэтому строка подписи собиралась с «+»
    вместо пробелов и HMAC не сходился НИ НА ОДНОМ стандартном initData:
    вход и регистрация через MAX были невозможны, хотя токен и бот верные.
    Эталон: aiogram-схема в maxo/utils/webapp.py и реализация официального
    Go-клиента — обе читают пары через parse_qsl.
    Значение hash после decode остаётся шестнадцатеричным — сравнение корректно.
    """
    return urllib.parse.parse_qsl(init_data or "", keep_blank_values=True)


def validate_init_data(init_data: str, bot_token: str) -> tuple[dict | None, str | None]:
    """→ (user_dict, None) при успехе; (None, код) при отказе.

    Коды: not_configured | format | bad_hash | expired. Единый ответ
    «bad_hash» на любые проблемы подписи (без деталей — анти-оракул)."""
    if not bot_token:
        return None, "not_configured"
    if not init_data or len(init_data) > MAX_INITDATA_LEN:
        return None, "format"
    user, err = _verify(parse_pairs(init_data), bot_token)
    if user is None and err in ("format", "bad_hash"):
        # Страховка официального Go-клиента: initData могла прийти
        # закодированной ЦЕЛИКОМ ещё раз (веб-клиент MAX, #WebAppData из
        # location.hash) — тогда пары читаются после одного unquote строки.
        user2, err2 = _verify(parse_pairs(urllib.parse.unquote(init_data)),
                              bot_token)
        if user2 is not None:
            return user2, None
        if err2 in ("expired", "bad_hash"):
            err = err2          # конкретнее, чем «format» первой попытки
    return user, err


def _verify(pairs: list[tuple[str, str]],
            bot_token: str) -> tuple[dict | None, str | None]:
    """(user, None) при совпадении подписи; (None, код) иначе.

    Коды те же, что у validate_init_data: format | expired | bad_hash —
    роутер по ним выбирает текст ответа, поэтому терять их нельзя.
    Строка подписи собирается из РАСКРЫТЫХ значений (как в эталоне), ключи
    сортируются a→z, hash исключается. Дубликаты ключей схлопываются как в
    эталонном dict(parse_qsl(...)) — последнее значение побеждает.
    """
    kv: dict[str, str] = dict(pairs)
    if not pairs or "hash" not in kv:
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
    launch = "\n".join(f"{k}={v}" for k, v in sorted(kv.items())
                        if k != "hash")
    calc = hmac.new(secret, launch.encode(errors="surrogatepass"),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, provided_hash):
        return None, "bad_hash"
    try:
        user = json_loads(kv["user"])
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
