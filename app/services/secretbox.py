# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сейф для секретов (v1.15.0): шифрование API-ключей
# (Checko, ФНС, внешние источники) в базе данных.
#
# Угроза: файл БД (data/app.db) скомпрометирован (бэкап уплыл, диск
# снят) — ключи не должны читаться. Решение: Fernet (cryptography):
# AES-128-CBC + HMAC-SHA256, ключ выводится из SECRET_KEY сервера
# (SHA-256, отдельная доменная строка). Значения в БД хранятся с
# префиксом «enc:v1:»; старые (открытые) значения читаются как есть
# и перешифровываются при следующем сохранении — миграция бесшовная.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import base64
import hashlib

PREFIX = "enc:v1:"

try:                                    # cryptography — в requirements.txt
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_CRYPTO = True
except ImportError:                     # страховка для экзотических окружений
    Fernet = InvalidToken = None
    _HAVE_CRYPTO = False


def _fernet():
    """Fernet из доменно-разделённого SECRET_KEY (не совпадает с JWT-ключом)."""
    from app.config import settings
    digest = hashlib.sha256(
        b"ymaster-secretbox-v1:" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_sealed(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(plain: str) -> str:
    """Зашифровать секрет для хранения в БД. Без cryptography — как есть
    (система не должна падать, но в requirements.txt библиотека есть)."""
    plain = plain or ""
    if not _HAVE_CRYPTO:
        return plain
    return PREFIX + _fernet().encrypt(plain.encode()).decode()


def unseal(stored: str) -> str:
    """Расшифровать; не-зашифрованные (легаси) значения возвращаются как есть.
    Повреждённое значение (чужой SECRET_KEY, обрезка) → пустая строка:
    ключ «слетает» безопасно, админ просто вводит его заново."""
    stored = stored or ""
    if not is_sealed(stored):
        return stored
    if not _HAVE_CRYPTO:
        return ""
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except (InvalidToken, Exception):
        return ""
