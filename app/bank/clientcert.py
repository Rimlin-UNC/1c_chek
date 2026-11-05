"""Сертификат клиента для API банка (v1.70.102, ADR-32): PEM или PKCS#12 (.p12, .pfx) → один PEM.

Сбер и Альфа-Банк пускают к API только по взаимному TLS: у компании есть сертификат, выпущенный банком,
и закрытый ключ к нему. Бухгалтер вставляет PEM (сертификат и ключ) или загружает файл .p12/.pfx с паролем.
Здесь он проверяется (ключ подходит к сертификату, срок не истёк) и приводится к одному PEM без пароля —
дальше он хранится только зашифрованным (services.secretbox), как остальные ключи банка. В интерфейс
уходят лишь срок действия и короткий отпечаток — ни ключа, ни владельца сертификата.

Ключи ГОСТ стандартная криптография Python не читает — для них вернётся понятная ошибка.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import UTC, date, datetime

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

MAX_SIZE = 64 * 1024
_KEY_PEM = re.compile(r"-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----.+?-----END (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----",
                      re.S)


class CertError(ValueError):
    """Сертификат не подходит — текст для пользователя."""


def _public(obj) -> bytes:
    return obj.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def _pack(cert: x509.Certificate, chain: list[x509.Certificate], key) -> str:
    if _public(cert) != _public(key):
        raise CertError("Закрытый ключ не подходит к сертификату — нужен ключ, с которым выпускали сертификат")
    until = cert.not_valid_after_utc.date()
    if until < datetime.now(UTC).date():
        raise CertError(f"Срок сертификата истёк {until:%d.%m.%Y} — выпустите новый в интернет-банке")
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    pem += "".join(c.public_bytes(serialization.Encoding.PEM).decode("ascii") for c in chain if c != cert)
    pem += key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")
    return pem


def normalize(text: str = "", p12_b64: str = "", password: str = "") -> str:
    """PEM (сертификат + ключ) или PKCS#12 в base64 → проверенный PEM без пароля."""
    secret = password.encode() if password else None
    if p12_b64:
        try:
            raw = base64.b64decode("".join(p12_b64.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CertError("Файл сертификата повреждён — загрузите .p12 или .pfx заново") from exc
        if len(raw) > MAX_SIZE:
            raise CertError("Файл сертификата слишком большой — нужен .p12/.pfx до 64 КБ")
        try:
            key, cert, extra = pkcs12.load_key_and_certificates(raw, secret)
        except (ValueError, TypeError) as exc:
            raise CertError("Не удалось открыть .p12/.pfx: неверный пароль или формат (ключи ГОСТ не "
                            "поддерживаются — нужен сертификат RSA или ECDSA)") from exc
        if key is None or cert is None:
            raise CertError("В файле нет сертификата или закрытого ключа — выгрузите их вместе")
        return _pack(cert, list(extra or []), key)
    text = (text or "").strip()
    if not text:
        raise CertError("Вставьте сертификат и закрытый ключ в формате PEM или загрузите файл .p12/.pfx")
    if len(text) > MAX_SIZE:
        raise CertError("Сертификат слишком большой — нужен PEM до 64 КБ")
    try:
        certs = x509.load_pem_x509_certificates(text.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CertError("Не найден сертификат: нужен блок «-----BEGIN CERTIFICATE-----»") from exc
    match = _KEY_PEM.search(text)
    if not match:
        raise CertError("Не найден закрытый ключ: вставьте вместе с сертификатом блок «-----BEGIN PRIVATE KEY-----»")
    try:
        key = serialization.load_pem_private_key(match.group(0).encode("ascii"), secret)
    except TypeError as exc:
        raise CertError("Ключ защищён паролем — укажите пароль ключа") from exc
    except ValueError as exc:
        raise CertError("Закрытый ключ не читается: неверный пароль или формат (ключи ГОСТ не поддерживаются)") from exc
    return _pack(certs[0], certs[1:], key)


def info(pem: str) -> dict:
    """Для интерфейса: срок действия и короткий отпечаток (без владельца и ключа)."""
    try:
        cert = x509.load_pem_x509_certificates(pem.encode("ascii"))[0]
    except (ValueError, UnicodeEncodeError, IndexError):
        return {}
    until: date = cert.not_valid_after_utc.date()
    der = cert.public_bytes(serialization.Encoding.DER)
    return {"until": until.isoformat(), "fingerprint": hashlib.sha256(der).hexdigest()[:16].upper()}
