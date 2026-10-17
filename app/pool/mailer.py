# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — письма «Чек-Пула» (v1.32.0, Этап 3): подтверждение e-mail
# и magic link (вход по одноразовой ссылке). SMTP настраивает администратор
# в Настройках → «🧩 Чек-Пул»; пароль SMTP хранится зашифрованным
# (appsettings SECRET_KEYS). Пока SMTP не настроен, кабинет полностью
# работает по паролю — письма лишь дополнительный удобный путь.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from ..services import appsettings

SET_SMTP_HOST = "smtp_host"
SET_SMTP_PORT = "smtp_port"
SET_SMTP_USER = "smtp_user"
SET_SMTP_PASS = "smtp_pass"        # секрет (шифруется)
SET_SMTP_FROM = "smtp_from"
SET_SMTP_TLS = "smtp_tls"
SET_BASE_URL = "public_base_url"   # для ссылок в письмах


def smtp_configured(db: Session) -> bool:
    return bool(appsettings.get_setting(db, SET_SMTP_HOST, "").strip())


def smtp_config(db: Session) -> dict | None:
    """Настройки SMTP или None, если не задан хост."""
    host = appsettings.get_setting(db, SET_SMTP_HOST, "").strip()
    if not host:
        return None
    try:
        port = int(appsettings.get_setting(db, SET_SMTP_PORT, "587") or 587)
    except ValueError:
        port = 587
    return {
        "host": host, "port": port,
        "user": appsettings.get_setting(db, SET_SMTP_USER, "").strip(),
        "password": appsettings.get_setting(db, SET_SMTP_PASS, ""),
        "sender": (appsettings.get_setting(db, SET_SMTP_FROM, "").strip()
                   or "chek@ymaster.ru"),
        "tls": appsettings.get_setting(db, SET_SMTP_TLS, "1") == "1",
    }


def base_url(db: Session) -> str:
    """Адрес сайта для ссылок в письмах (задаёт администратор)."""
    return (appsettings.get_setting(db, SET_BASE_URL, "").strip()
            or "https://chek.ymaster.ru").rstrip("/")


def send_mail(db: Session, to_email: str, subject: str, body: str,
              html: str | None = None, from_name: str = "") -> tuple[bool, str]:
    """Отправка письма. Возвращает (ok, сообщение об ошибке). Никогда не
    поднимает исключений — сбой почты не должен ломать приём чеков.
    v1.44.0: html (многочастёвое письмо с красивой версией) и имя отправителя."""
    cfg = smtp_config(db)
    if cfg is None:
        return False, "SMTP не настроен"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        sender = cfg["sender"]
        if from_name:
            sender = f"{from_name} <{cfg['sender']}>"
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        # v1.44.2: порт 465 = SMTP_SSL — защита с первого байта (так работает
        # почта Timeweb: smtp.timeweb.ru:465, ящик chek@ymaster.ru).
        # Остальные порты — как в v1.44.0 (STARTTLS по флагу tls).
        if cfg["port"] == 465:
            conn = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=12)
            tls_needed = False            # соединение уже защищённое
        else:
            conn = smtplib.SMTP(cfg["host"], cfg["port"], timeout=12)
            tls_needed = bool(cfg["tls"])
        with conn as smtp:
            if tls_needed:
                smtp.starttls()
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        return True, ""
    except Exception as e:                                # noqa: BLE001
        return False, f"{e.__class__.__name__}"


def send_verify_email(db: Session, to_email: str, link: str) -> tuple[bool, str]:
    return send_mail(
        db, to_email, "Ямастер Чек-Пул: подтвердите e-mail",
        "Здравствуйте!\n\nПодтвердите e-mail для кабинета «Ямастер Чек-Пул» "
        f"по ссылке (действительна 24 часа):\n{link}\n\n"
        "Если это были не вы — просто не открывайте ссылку.\n\n"
        "ООО «Ямастер» · https://ymaster.ru")


def send_magic_link(db: Session, to_email: str, link: str) -> tuple[bool, str]:
    return send_mail(
        db, to_email, "Ямастер Чек-Пул: вход по ссылке",
        "Здравствуйте!\n\nВойдите в кабинет «Ямастер Чек-Пул» по одноразовой "
        f"ссылке (действительна 15 минут):\n{link}\n\n"
        "Если это были не вы — просто не открывайте ссылку.\n\n"
        "ООО «Ямастер» · https://ymaster.ru")
