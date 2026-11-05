"""Подключение к банку по API (v1.70.99, ADR-29): Точка Банк, OAuth 2.0 (основной способ) или JWT-ключ.

OAuth: в карточке компании (администратор) или на экране «Банк: выписка» (бухгалтер) выбирается банк и
сохраняются client_id и client_secret приложения из интернет-банка — либо используется приложение сервера
(TOCHKA_CLIENT_ID и TOCHKA_CLIENT_SECRET в .env). «Подключить» → программа создаёт разрешение на чтение
счетов и выписок → клиент входит в Точку и подтверждает → банк возвращает его на CALLBACK_PATH с кодом →
программа получает токены, находит компанию по ИНН и её рублёвые счета. Отметка запроса (state) —
одноразовая и живёт 15 минут: чужой или старый возврат не привяжет банк к компании.

Дальше выписка загружается сама раз в 6 часов (и по кнопке «Загрузить сейчас»): одна выписка на счёт и
месяц, дополняется при каждой загрузке; операции идут тем же путём, что и файл (классификатор,
сопоставление с чеками и УПД, запросы в «Что нужно от клиента»). Токен доступа живёт сутки, refresh_token —
30 дней и меняется при каждом продлении, поэтому регулярная загрузка сама держит подключение живым.

v1.70.102 (ADR-32): банков у компании может быть несколько — Точка, Т-Банк, Сбербанк, Альфа-Банк, Модульбанк
(реестр и способы доступа — app/bank/providers.py). Для Т-Банка, Модульбанка и Альфы — ключ API, который
владелец счёта выпускает сам; для Сбера — client_id, client_secret и refresh_token из личного кабинета
Sber API (токен доступа программа продлевает сама) и сертификат клиента для взаимного TLS. «Проверить и
подключить» — запрос в банк: ИНН (если банк его сообщает) сверяется с карточкой, счета запоминаются
зашифрованно. Дальше загрузка общая: выписка на счёт и месяц, тот же классификатор и та же сверка.
Путь Точки (разрешение, вход клиента в банк, выписка «заказать — забрать») остался как в 1.70.99.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Company, User, utcnow
from ..services import secretbox
from ..services.audit import log_action
from . import clientcert, http, parser, providers, sber, service, tochka
from .models import BankConnection, BankOperation, BankStatement

log = logging.getLogger("ymaster.bank.connect")

BANKS = dict(providers.TITLES)        # v1.70.102: Точка, Т-Банк, Сбербанк, Альфа-Банк, Модульбанк
METHODS = {"oauth": "OAuth 2.0", "jwt": "JWT-ключ", "token": "Ключ API", "refresh": "OAuth 2.0 — токены из кабинета"}
STATUS_TITLES = {"none": "не подключён", "new": "не подключён", "awaiting": "ждём подтверждения в банке",
                 "active": "подключён", "reauth": "нужно подключить заново", "error": "ошибка подключения"}
CALLBACK_PATH = "/api/v1/bank/tochka/callback"
STATE_TTL = timedelta(minutes=15)
SYNC_EVERY = timedelta(hours=6)
ERROR_RETRY = timedelta(minutes=30)
PENDING_RETRY = timedelta(minutes=3)
OVERLAP_DAYS = 3                      # поздние проводки прошлых дней: каждый раз берём с запасом
MAX_BACK_DAYS = 366
MAX_PER_TICK = 3
_CLIENT_ID = re.compile(r"^[\w.\-:@]{3,200}$")
_JWT = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*$")
_KEY = re.compile(r"^[\x21-\x7e]{8,4000}$")       # ключ API и refresh_token: печатные латинские символы без пробелов
_sync_lock = threading.Lock()


class ConnectError(ValueError):
    """Ошибка для пользователя (status — код ответа API)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def today_msk() -> date:
    return (utcnow() + timedelta(hours=3)).date()


def default_start(today: date) -> date:
    """Первая загрузка — с 1-го числа прошлого месяца: текущий отчётный период целиком."""
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def server_app() -> tuple[str, str]:
    """Приложение Точки владельца сервера (.env) — компании подключаются без своих client_id и secret."""
    return os.environ.get("TOCHKA_CLIENT_ID", "").strip(), os.environ.get("TOCHKA_CLIENT_SECRET", "").strip()


def redirect_uri(db: Session) -> str:
    """Адрес возврата: его же нужно указать в приложении в интернет-банке (берётся из адреса сайта)."""
    from ..pool.mailer import base_url
    return base_url(db) + CALLBACK_PATH


def connection_of(db: Session, company_id: str, bank: str = "tochka") -> BankConnection | None:
    return (db.query(BankConnection)
            .filter(BankConnection.company_id == company_id, BankConnection.bank == bank).first())


def connections_of(db: Session, company_id: str) -> list[BankConnection]:
    """Все банки компании по API — в порядке подключения (v1.70.102)."""
    rows = db.query(BankConnection).filter(BankConnection.company_id == company_id).all()
    return sorted(rows, key=lambda r: (r.created_at or datetime.min, list(BANKS).index(r.bank) if r.bank in BANKS else 99))


def bank_of(db: Session, company_id: str, bank: str | None) -> str:
    """Банк запроса: явно указанный; иначе — первый подключённый у компании; иначе — Точка."""
    if bank:
        return bank
    rows = connections_of(db, company_id)
    return rows[0].bank if rows else "tochka"


def _accounts(conn: BankConnection) -> list[dict]:
    try:
        data = json.loads(secretbox.unseal(conn.accounts_enc) or "[]")
    except ValueError:
        return []
    return [a for a in data if isinstance(a, dict) and a.get("id")]


def _pending(conn: BankConnection) -> list[dict]:
    try:
        data = json.loads(conn.pending_json or "[]")
    except ValueError:
        return []
    return [p for p in data if isinstance(p, dict)]


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _cert(conn: BankConnection) -> dict:
    if not conn.cert_enc:
        return {}
    return clientcert.info(secretbox.unseal(conn.cert_enc) or "")


def _brief(conn: BankConnection) -> dict:
    """Строка списка «Банки компании»: состояние без секретов."""
    awaiting = bool(conn.oauth_state and conn.oauth_state_until and conn.oauth_state_until > utcnow())
    status = conn.status if conn.status in STATUS_TITLES else "new"
    if status == "awaiting" and not awaiting:
        status = "new"
    return {"bank": conn.bank, "bank_title": BANKS.get(conn.bank, conn.bank), "method": conn.method, "status": status,
            "status_title": STATUS_TITLES[status], "accounts": [a.get("tail", "") for a in _accounts(conn)],
            "synced_until": _iso(conn.synced_until), "last_sync_at": _iso(conn.last_sync_at),
            "last_error": conn.last_error or "", "pending": len(_pending(conn))}


def connection_dict(db: Session, company: Company, conn: BankConnection | None, bank: str = "tochka") -> dict:
    """Состояние для интерфейса: секретов нет — только признаки «задан» и последние 4 цифры счетов.
    v1.70.102: banks — реестр банков с подсказками, connections — все банки компании, file_banks — банки
    первой десятки без открытого API (для них — файл выписки)."""
    sid, ssecret = server_app()
    bank = conn.bank if conn is not None else (bank if bank in BANKS else "tochka")
    prov = providers.PROVIDERS[bank]
    base = {"banks": [providers.meta(k) for k in BANKS],
            "methods": [{"id": k, "title": v} for k, v in METHODS.items()],
            "file_banks": providers.FILE_BANKS,
            "connections": [_brief(c) for c in connections_of(db, company.id)],
            "server_app": bool(sid and ssecret), "redirect_uri": redirect_uri(db), "company_inn": company.inn or "",
            "how": prov["how"], "docs": prov["docs"], "cert_policy": prov["cert"]}
    if conn is None:
        return {**base, "bank": bank, "bank_title": BANKS[bank], "method": prov["methods"][0], "status": "none",
                "status_title": STATUS_TITLES["none"], "client_id": "", "secret_set": False, "jwt_set": False,
                "token_set": False, "refresh_set": False, "cert_set": False, "cert": {},
                "accounts": [], "start_date": default_start(today_msk()).isoformat(), "synced_until": None,
                "last_sync_at": None, "last_ok_at": None, "last_error": "", "last_result": {}, "pending": 0,
                "awaiting": False, "consent_expires_at": None}
    try:
        result = json.loads(conn.last_result_json or "{}")
    except ValueError:
        result = {}
    awaiting = bool(conn.oauth_state and conn.oauth_state_until and conn.oauth_state_until > utcnow())
    status = conn.status if conn.status in STATUS_TITLES else "new"
    if status == "awaiting" and not awaiting:        # вход в банк начат и брошен — снова «не подключён»
        status = "new"
    return {**base, "bank": conn.bank, "bank_title": BANKS.get(conn.bank, conn.bank), "method": conn.method,
            "status": status, "status_title": STATUS_TITLES[status], "client_id": conn.client_id,
            "secret_set": bool(conn.client_secret_enc), "jwt_set": bool(conn.jwt_enc),
            "token_set": bool(conn.jwt_enc) and conn.method == "token", "refresh_set": bool(conn.refresh_enc),
            "cert_set": bool(conn.cert_enc), "cert": _cert(conn),
            "accounts": [{"tail": a.get("tail", ""), "currency": a.get("currency", "RUB"),
                          "loaded": (a.get("currency") or "RUB") == "RUB"} for a in _accounts(conn)],
            "start_date": _iso(conn.start_date) or default_start(today_msk()).isoformat(),
            "synced_until": _iso(conn.synced_until), "last_sync_at": _iso(conn.last_sync_at),
            "last_ok_at": _iso(conn.last_ok_at), "last_error": conn.last_error, "last_result": result,
            "pending": len(_pending(conn)), "awaiting": awaiting, "consent_expires_at": _iso(conn.consent_expires_at)}


def _reset_access(conn: BankConnection) -> None:
    """Сменились способ или данные приложения — старые токены и разрешение больше не годятся."""
    conn.access_enc = conn.refresh_enc = conn.accounts_enc = ""
    conn.token_expires_at = conn.consent_expires_at = None
    conn.consent_id = conn.customer_code = conn.oauth_state = ""
    conn.oauth_state_until = None
    conn.pending_json = "[]"
    conn.status = "new"


def _start_date(data: dict) -> date | None:
    start = data.get("start_date")
    today = today_msk()
    if start is not None:
        if not isinstance(start, date):
            raise ConnectError("Дата начала загрузки — в формате ГГГГ-ММ-ДД")
        if start > today or start < today - timedelta(days=MAX_BACK_DAYS):
            raise ConnectError("Загружать операции можно не раньше чем за год и не позже сегодняшнего дня")
    return start


def save(db: Session, company: Company, data: dict, user: User) -> BankConnection:
    """Сохранить банк, способ и данные доступа. Пустые client_secret и JWT — «не менять»."""
    bank = data.get("bank") or "tochka"
    if bank not in BANKS:
        raise ConnectError("Этот банк пока не подключается по API — загрузите его выписку файлом в формате 1С")
    if bank != "tochka":
        return _save_other(db, company, bank, data, user)
    method = data.get("method") or "oauth"
    if method not in providers.PROVIDERS["tochka"]["methods"]:
        raise ConnectError("Способ подключения — OAuth 2.0 или JWT-ключ")
    client_id = (data.get("client_id") or "").strip()
    secret = (data.get("client_secret") or "").strip()
    jwt = "".join((data.get("jwt") or "").split())
    if client_id and not _CLIENT_ID.match(client_id):
        raise ConnectError("client_id — латиница, цифры и знаки . _ - : @, до 200 символов")
    if secret and (len(secret) > 500 or any(ch.isspace() for ch in secret)):
        raise ConnectError("client_secret — одна строка без пробелов, до 500 символов")
    if jwt and (len(jwt) > 8000 or not _JWT.match(jwt)):
        raise ConnectError("JWT-ключ — строка из трёх частей через точку, как её выдаёт интернет-банк")
    start = _start_date(data)
    today = today_msk()
    conn = connection_of(db, company.id, bank)
    if conn is None:
        conn = BankConnection(company_id=company.id, bank=bank, method=method, created_by=user.id, pending_json="[]",
                              status="new", start_date=start or default_start(today))
        db.add(conn)
    changed = method != conn.method
    conn.method = method
    if method == "oauth":
        if client_id != (conn.client_id or ""):
            conn.client_id, changed = client_id, True
            if not client_id:
                conn.client_secret_enc = ""
        if secret:
            conn.client_secret_enc, changed = secretbox.seal(secret), True
        if client_id and not conn.client_secret_enc:
            raise ConnectError("Укажите client_secret приложения — он выдаётся вместе с client_id")
        if not client_id and not all(server_app()):
            raise ConnectError("Укажите client_id и client_secret приложения из интернет-банка Точки: «Интеграции "
                               "и API» → «Подключить OAuth». Или администратор сервера задаёт общее приложение "
                               "в .env (TOCHKA_CLIENT_ID, TOCHKA_CLIENT_SECRET)")
    elif jwt:
        conn.jwt_enc, changed = secretbox.seal(jwt), True
    elif not conn.jwt_enc:
        raise ConnectError("Вставьте JWT-ключ из интернет-банка: «Интеграции и API» → «Создать JWT-ключ»")
    if changed and conn.id:
        _reset_access(conn)
    if start is not None and start != conn.start_date:
        conn.start_date = start
        conn.synced_until = None                     # с новой даты — загрузить заново (дубли отсеются)
    conn.updated_by, conn.updated_at = user.id, utcnow()
    db.commit()
    return conn


def _reset_derived(conn: BankConnection) -> None:
    """Ключи сменились — токен доступа и счета получим заново; введённые ключи и сертификат остаются."""
    conn.access_enc = conn.accounts_enc = ""
    conn.token_expires_at = None
    conn.pending_json = "[]"
    conn.status = "new"


def _save_other(db: Session, company: Company, bank: str, data: dict, user: User) -> BankConnection:
    """Т-Банк, Модульбанк, Альфа — ключ API; Сбер — client_id, client_secret, refresh_token. Сертификат клиента
    (PEM или .p12) — там, где банк его требует или выдал. Пустые поля секретов — «не менять»."""
    prov = providers.PROVIDERS[bank]
    title = prov["title"]
    method = data.get("method") or prov["methods"][0]
    if method not in prov["methods"]:
        method = prov["methods"][0]                  # у банка один способ — переключатель «OAuth/JWT» не про него
    start = _start_date(data)
    token = "".join((data.get("token") or "").split())
    client_id = (data.get("client_id") or "").strip()
    secret = (data.get("client_secret") or "").strip()
    refresh_token = "".join((data.get("refresh_token") or "").split())
    if token and not _KEY.match(token):
        raise ConnectError("Ключ API — одна строка латиницей без пробелов, от 8 до 4000 символов")
    if refresh_token and not _KEY.match(refresh_token):
        raise ConnectError("refresh_token — одна строка латиницей без пробелов, от 8 до 4000 символов")
    if client_id and not _CLIENT_ID.match(client_id):
        raise ConnectError("client_id — латиница, цифры и знаки . _ - : @, до 200 символов")
    if secret and (len(secret) > 500 or any(ch.isspace() for ch in secret)):
        raise ConnectError("client_secret — одна строка без пробелов, до 500 символов")
    pem = ""
    if data.get("cert") or data.get("cert_p12"):
        try:
            pem = clientcert.normalize(data.get("cert") or "", data.get("cert_p12") or "",
                                       data.get("cert_password") or "")
        except clientcert.CertError as exc:
            raise ConnectError(str(exc)) from exc
    conn = connection_of(db, company.id, bank)
    if conn is None:
        conn = BankConnection(company_id=company.id, bank=bank, method=method, created_by=user.id, pending_json="[]",
                              status="new", start_date=start or default_start(today_msk()))
        db.add(conn)
    changed = method != conn.method
    conn.method = method
    if method == "token":
        if token:
            conn.jwt_enc, changed = secretbox.seal(token), True
        elif not conn.jwt_enc:
            raise ConnectError(f"Вставьте ключ API банка «{title}». {prov['how']}")
    else:                                            # refresh — Сбер
        if client_id != (conn.client_id or ""):
            conn.client_id, changed = client_id, True
        if not conn.client_id:
            raise ConnectError(f"Укажите client_id интеграции. {prov['how']}")
        if secret:
            conn.client_secret_enc, changed = secretbox.seal(secret), True
        elif not conn.client_secret_enc:
            raise ConnectError("Укажите client_secret — он выдаётся вместе с client_id")
        if refresh_token:
            conn.refresh_enc, changed = secretbox.seal(refresh_token), True
            conn.access_enc, conn.token_expires_at = "", None
        elif not conn.refresh_enc:
            raise ConnectError("Вставьте refresh_token из личного кабинета банка — по нему программа сама "
                               "получает и продлевает доступ")
    if pem:
        conn.cert_enc, changed = secretbox.seal(pem), True
    elif data.get("cert_remove") and prov["cert"] != "required" and conn.cert_enc:
        conn.cert_enc, changed = "", True
    if prov["cert"] == "required" and not conn.cert_enc:
        raise ConnectError(f"Загрузите сертификат клиента для API банка «{title}» (PEM с закрытым ключом или "
                           ".p12/.pfx с паролем): без него банк не пустит к выпискам")
    if changed and conn.id:
        _reset_derived(conn)
    if start is not None and start != conn.start_date:
        conn.start_date = start
        conn.synced_until = None
    conn.updated_by, conn.updated_at = user.id, utcnow()
    db.commit()
    return conn


def remove(db: Session, company: Company, bank: str = "tochka") -> bool:
    """Отключить: строка с секретами и токенами удаляется; загруженные выписки и операции остаются."""
    conn = connection_of(db, company.id, bank)
    if conn is None:
        return False
    db.delete(conn)
    db.commit()
    return True


# ------------------------------------------------------------------ OAuth 2.0
def _app(conn: BankConnection) -> tuple[str, str]:
    if conn.client_id:
        return conn.client_id, secretbox.unseal(conn.client_secret_enc)
    return server_app()


def start_oauth(db: Session, company: Company, user: User) -> str:
    """Создать разрешение и вернуть адрес входа в Точку."""
    conn = connection_of(db, company.id)
    if conn is None or conn.method != "oauth":
        raise ConnectError("Сначала сохраните подключение со способом OAuth 2.0", 409)
    client_id, secret = _app(conn)
    if not client_id or not secret:
        raise ConnectError("Не заданы client_id и client_secret приложения Точки", 422)
    try:
        consent = tochka.create_consent(tochka.app_token(client_id, secret),
                                        utcnow() + timedelta(days=tochka.CONSENT_DAYS))
    except tochka.TochkaError as exc:
        conn.last_error = str(exc)[:300]
        db.commit()
        raise ConnectError(str(exc), 502) from exc
    conn.consent_id = consent["consent_id"]
    conn.consent_expires_at = utcnow() + timedelta(days=tochka.CONSENT_DAYS)
    conn.oauth_state = secrets.token_urlsafe(32)[:43]
    conn.oauth_state_until = utcnow() + STATE_TTL
    conn.oauth_user = user.id
    if conn.status != "active":
        conn.status = "awaiting"
    conn.last_error = ""
    db.commit()
    return tochka.authorize_url(client_id, redirect_uri(db), conn.oauth_state, conn.consent_id)


def _save_tokens(conn: BankConnection, data: dict) -> None:
    conn.access_enc = secretbox.seal(str(data["access_token"]))
    if data.get("refresh_token"):                     # при продлении банк выдаёт новый refresh_token
        conn.refresh_enc = secretbox.seal(str(data["refresh_token"]))
    try:
        seconds = int(data.get("expires_in") or 86400)
    except (TypeError, ValueError):
        seconds = 86400
    conn.token_expires_at = utcnow() + timedelta(seconds=max(60, min(seconds, 86400 * 7)))


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _bind(conn: BankConnection, company: Company, access: str) -> None:
    """Найти компанию в банке по ИНН и запомнить её счета (идентификаторы — зашифрованно)."""
    found = tochka.customers(access)
    business = [c for c in found if c.get("customerType") in (None, "", "Business")] or found
    inn = _digits(company.inn)
    if inn:
        match = [c for c in business if _digits(c.get("taxCode")) == inn]
        if not match:
            names = ", ".join(f"{c.get('shortName') or c.get('fullName') or '—'} (ИНН {c.get('taxCode') or '—'})"
                              for c in business[:5]) or "нет компаний"
            raise ConnectError(f"В этом доступе Точки нет компании с ИНН {inn}. Доступны: {names}. "
                               "Войдите в банк под нужной компанией", 409)
        customer = match[0]
    elif len(business) == 1:
        customer = business[0]
    else:
        raise ConnectError("У компании в программе не указан ИНН, а в банке несколько компаний — укажите ИНН "
                           "в карточке компании", 409)
    conn.customer_code = str(customer.get("customerCode") or "")[:40]
    accounts = []
    for acc in tochka.accounts(access):
        if conn.customer_code and acc.get("customerCode") not in (None, "", conn.customer_code):
            continue
        if acc.get("status") not in (None, "", "Enabled"):
            continue
        number = tochka.account_number(str(acc.get("accountId") or ""))
        if len(number) < 4:
            continue
        accounts.append({"id": str(acc["accountId"]), "tail": number[-4:], "currency": acc.get("currency") or "RUB"})
    if not accounts:
        raise ConnectError("В банке у компании нет открытых счетов, доступных по этому разрешению", 409)
    conn.accounts_enc = secretbox.seal(json.dumps(accounts, ensure_ascii=False))


def finish_oauth(db: Session, state: str, code: str, error: str = "") -> tuple[BankConnection | None, str]:
    """Возврат из банка. (подключение, текст ошибки или пусто). Отметка state одноразовая."""
    state = (state or "").strip()
    if len(state) < 20:
        return None, "Банк вернул ответ без отметки запроса — начните подключение заново"
    conn = db.query(BankConnection).filter(BankConnection.oauth_state == state[:64]).first()
    if conn is None or not hmac.compare_digest(conn.oauth_state, state):
        return None, "Ссылка подключения устарела или уже использована — начните подключение заново"
    expired = not conn.oauth_state_until or conn.oauth_state_until < utcnow()
    conn.oauth_state, conn.oauth_state_until = "", None
    if expired:
        conn.status = "active" if conn.access_enc else "new"
        db.commit()
        return conn, "Подтверждение заняло больше 15 минут — начните подключение заново"
    if error or not code:
        conn.status = "active" if conn.access_enc else "new"
        conn.last_error = "Доступ в банке не подтверждён"
        db.commit()
        return conn, conn.last_error
    company = db.get(Company, conn.company_id)
    if company is None:
        db.commit()
        return conn, "Компания не найдена"
    client_id, secret = _app(conn)
    try:
        data = tochka.exchange_code(client_id, secret, code.strip()[:500], redirect_uri(db))
        _save_tokens(conn, data)
        _bind(conn, company, str(data["access_token"]))
    except (tochka.TochkaError, ConnectError) as exc:
        conn.status = "error"
        conn.last_error = str(exc)[:300]
        db.commit()
        return conn, str(exc)
    conn.status, conn.last_error, conn.updated_at = "active", "", utcnow()
    conn.pending_json = "[]"
    db.commit()
    who = db.get(User, conn.oauth_user) if conn.oauth_user else None
    log_action(who, "bank_connected", details={"company": company.name, "bank": BANKS[conn.bank],
                                               "method": METHODS["oauth"], "accounts": len(_accounts(conn))})
    return conn, ""


def connect_jwt(db: Session, company: Company, user: User) -> BankConnection:
    """JWT-ключ: проверить его запросом в банк и запомнить счета компании."""
    conn = connection_of(db, company.id)
    if conn is None or conn.method != "jwt" or not conn.jwt_enc:
        raise ConnectError("Сначала сохраните JWT-ключ", 409)
    try:
        _bind(conn, company, secretbox.unseal(conn.jwt_enc))
    except tochka.TochkaError as exc:
        conn.status, conn.last_error = ("reauth" if exc.kind in ("auth", "forbidden") else "error"), str(exc)[:300]
        db.commit()
        raise ConnectError(str(exc), 502) from exc
    conn.status, conn.last_error, conn.updated_at, conn.updated_by = "active", "", utcnow(), user.id
    db.commit()
    log_action(user, "bank_connected", details={"company": company.name, "bank": BANKS[conn.bank],
                                                "method": METHODS["jwt"], "accounts": len(_accounts(conn))})
    return conn


def _credentials(db: Session, conn: BankConnection) -> tuple[str, str]:
    """Ключ для запросов к банку (не Точка) и сертификат клиента. Сбер: токен доступа живёт 60 минут —
    продлеваем по refresh_token и сразу сохраняем новую пару (старый refresh_token банк держит 2 часа)."""
    cert = secretbox.unseal(conn.cert_enc) if conn.cert_enc else ""
    if conn.cert_enc and not cert:
        raise http.BankApiError("Сертификат клиента не читается — загрузите его заново", "auth")
    if conn.method == "token":
        key = secretbox.unseal(conn.jwt_enc)
        if not key:
            raise http.BankApiError("Ключ API не задан или не читается — вставьте его заново", "auth")
        return key, cert
    access = secretbox.unseal(conn.access_enc)
    if access and conn.token_expires_at and conn.token_expires_at > utcnow() + timedelta(minutes=5):
        return access, cert
    refresh_token = secretbox.unseal(conn.refresh_enc)
    if not refresh_token or not conn.client_id:
        raise http.BankApiError("Нет действующего доступа к банку — вставьте новый refresh_token", "auth")
    data = sber.refresh(conn.client_id, secretbox.unseal(conn.client_secret_enc), refresh_token, cert)
    _save_tokens(conn, data)
    db.commit()
    return str(data["access_token"]), cert


def _bind_other(db: Session, conn: BankConnection, company: Company) -> None:
    """Проверить ключ запросом в банк: ИНН (если банк его сообщает) — как в карточке; счета — зашифрованно."""
    key, cert = _credentials(db, conn)
    org = providers.organization(conn.bank, key, cert)
    title = BANKS[conn.bank]
    inn = _digits(company.inn)
    if org.get("inn") and inn and org["inn"] != inn:
        raise ConnectError(f"Ключ банка «{title}» выдан компании с ИНН {org['inn']}, а в карточке — ИНН {inn}. "
                           "Выпустите ключ под нужной компанией", 409)
    found = [a for a in org.get("accounts") or [] if isinstance(a, dict) and a.get("id")]
    companies = org.get("companies") or []
    if len(companies) > 1:                           # Модульбанк: один токен — несколько компаний владельца
        mine = [a for a in found if inn and a.get("inn") == inn]
        if not mine:
            raise ConnectError(f"В доступе банка «{title}» несколько компаний, и по ИНН {inn or '—'} нужная не "
                               "найдена — выпустите токен только для этой компании", 409)
        found = mine
    accounts = [{"id": str(a["id"]), "number": a.get("number") or "", "tail": a.get("tail") or "",
                 "currency": a.get("currency") or "RUB"} for a in found]
    if not accounts:
        raise ConnectError(f"В банке «{title}» у компании нет открытых счетов, доступных по этому ключу", 409)
    conn.accounts_enc = secretbox.seal(json.dumps(accounts, ensure_ascii=False))


def connect_key(db: Session, company: Company, user: User, bank: str = "tochka") -> BankConnection:
    """«Проверить и подключить»: Точка — JWT-ключ (как в 1.70.99); остальные банки — их ключи и сертификат."""
    if bank == "tochka":
        return connect_jwt(db, company, user)
    conn = connection_of(db, company.id, bank)
    if conn is None:
        raise ConnectError("Сначала сохраните ключи банка", 409)
    try:
        _bind_other(db, conn, company)
    except http.BankApiError as exc:
        conn.status, conn.last_error = (("reauth" if exc.kind in ("auth", "forbidden") else "error"),
                                        str(exc)[:300])
        db.commit()
        raise ConnectError(str(exc), 502) from exc
    except ConnectError as exc:
        conn.status, conn.last_error = "error", str(exc)[:300]
        db.commit()
        raise
    conn.status, conn.last_error, conn.updated_at, conn.updated_by = "active", "", utcnow(), user.id
    conn.pending_json = "[]"
    db.commit()
    log_action(user, "bank_connected", details={"company": company.name, "bank": BANKS[conn.bank],
                                                "method": METHODS[conn.method], "accounts": len(_accounts(conn))})
    return conn


def _access(db: Session, conn: BankConnection) -> str:
    """Действующий токен: JWT-ключ как есть; OAuth — при необходимости продлить (новый refresh_token)."""
    if conn.method == "jwt":
        key = secretbox.unseal(conn.jwt_enc)
        if not key:
            raise tochka.TochkaError("JWT-ключ не задан или не читается — вставьте его заново", "auth")
        return key
    access = secretbox.unseal(conn.access_enc)
    if access and conn.token_expires_at and conn.token_expires_at > utcnow() + timedelta(minutes=5):
        return access
    refresh_token = secretbox.unseal(conn.refresh_enc)
    if not refresh_token:
        raise tochka.TochkaError("Нет действующего доступа к банку — подключите банк заново", "auth")
    client_id, secret = _app(conn)
    data = tochka.refresh(client_id, secret, refresh_token)
    _save_tokens(conn, data)
    db.commit()
    return str(data["access_token"])


# ------------------------------------------------------------------ загрузка выписок
def months(start: date, end: date) -> list[tuple[date, date]]:
    """Отрезки по календарным месяцам: выписка из API — одна на счёт и месяц."""
    out = []
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(start, cur), min(end, nxt - timedelta(days=1))))
        cur = nxt
    return out


def window(conn: BankConnection, today: date) -> tuple[date, date]:
    """Что загружать: в первый раз — с даты начала; дальше — с начала месяца, где последний день загрузки
    минус 3 дня (поздние проводки), чтобы итоги месячной выписки были полными."""
    base = conn.start_date or default_start(today)
    start = max(base, (conn.synced_until - timedelta(days=OVERLAP_DAYS)).replace(day=1)) if conn.synced_until else base
    return max(start, today - timedelta(days=MAX_BACK_DAYS)), today


def _statement_key(company_id: str, account_id: str, month: str, bank: str = "tochka") -> str:
    return hashlib.sha256(f"{bank}|{company_id}|{account_id}|{month}".encode()).hexdigest()


def _account_key(account_id: str) -> str:
    """Короткая метка счёта для очереди ожидания: без номера счёта, но различает счета с одинаковым хвостом."""
    return hashlib.sha256(account_id.encode()).hexdigest()[:12]


def _classify_doc(doc: dict, own_account: str, own_inn: str) -> dict | None:
    op = parser.classify(doc, [own_account], own_inn)
    if op and doc.get("_card") and op["kind"] == "transfer":
        op["kind"] = "card"                          # вид документа «Банковские карты» — покупка по карте
    return op


def _classify(txn: dict, own_account: str, own_inn: str) -> dict | None:
    doc = tochka.transaction_doc(txn, own_account)
    if doc is None:
        return None
    return _classify_doc(doc, own_account, own_inn)


def _apply(db: Session, company: Company, account: dict, lo: date, hi: date, st: dict, user_id: str) -> dict:
    """Транзакции одной выписки Точки → документы → операции и месячная выписка в программе."""
    own_account = tochka.account_number(account["id"])
    docs = [doc for txn in st.get("Transaction") or [] if isinstance(txn, dict)
            and (doc := tochka.transaction_doc(txn, own_account)) is not None]
    return _apply_docs(db, company, "tochka", account, own_account, lo, hi, docs, user_id)


def _apply_docs(db: Session, company: Company, bank: str, account: dict, own_account: str, lo: date, hi: date,
                docs: list[dict], user_id: str) -> dict:
    """Документы выписки банка (поля формата 1С) → операции и месячная выписка (одна на счёт и месяц)."""
    own_inn = _digits(company.inn)
    ops = [op for doc in docs if (op := _classify_doc(doc, own_account, own_inn))]
    month = f"{lo.year:04d}-{lo.month:02d}"
    key = _statement_key(company.id, account["id"], month, bank)
    row = (db.query(BankStatement)
           .filter(BankStatement.company_id == company.id, BankStatement.file_sha256 == key).first())
    if row is None:
        row = BankStatement(company_id=company.id, file_sha256=key, source=bank, account_tail=account["tail"],
                            date_from=lo, date_to=hi, uploaded_by=user_id)
        db.add(row)
        db.flush()
    row.date_from = min(row.date_from or lo, lo)
    row.date_to = max(row.date_to or hi, hi)
    row.ops_total = len(ops)
    row.summary_json = json.dumps(dict(Counter(op["kind"] for op in ops)), ensure_ascii=False)
    saved, duplicates, _ = service.store_ops(db, company.id, row.id, ops)
    db.flush()
    row.ops_saved = db.query(BankOperation.id).filter(BankOperation.statement_id == row.id).count()
    row.duplicates = duplicates
    row.ops_skipped = max(0, row.ops_total - row.ops_saved - duplicates)
    return {"operations": len(ops), "saved": saved, "duplicates": duplicates}


def _doc_day(doc: dict) -> date | None:
    for field in ("ДатаСписано", "ДатаПоступило", "Дата"):
        try:
            return date.fromisoformat(str(doc.get(field) or "")[:10])
        except ValueError:
            continue
    return None


def _sync_failed(db: Session, conn: BankConnection, company: Company, exc: Exception) -> dict:
    db.rollback()
    conn = db.get(BankConnection, conn.id)
    conn.last_sync_at = utcnow()
    if isinstance(exc, http.BankApiError):
        conn.last_error = str(exc)[:300]
        if exc.kind in ("auth", "forbidden"):
            conn.status = "reauth"
        db.commit()
        log.warning("bank api %s %s: %s (%s)", conn.bank, company.id, exc.kind, exc.status)
        return {"ok": False, "error": str(exc), "kind": exc.kind}
    log.exception("bank api %s %s: сбой загрузки", conn.bank, company.id)
    conn.last_error = f"Внутренняя ошибка загрузки ({type(exc).__name__}) — повторим позже"
    db.commit()
    return {"ok": False, "error": conn.last_error, "kind": "internal"}


def _sync_other(db: Session, conn: BankConnection, company: Company, user: User | None) -> dict:
    """Т-Банк, Сбер, Альфа, Модульбанк: выписка за окно сразу (у Сбера и Альфы — по дням, не больше 45 дней
    за раз: дальше продолжим со следующей загрузкой), операции — по месяцам, как у Точки."""
    today = today_msk()
    lo_all, hi_all = window(conn, today)
    if providers.PROVIDERS[conn.bank]["daily"]:
        hi_all = min(hi_all, lo_all + timedelta(days=providers.DAILY_MAX_DAYS - 1))
    accounts = [a for a in _accounts(conn) if (a.get("currency") or "RUB") == "RUB"]
    saved_all: list[BankOperation] = []
    total = duplicates = 0
    conn.last_sync_at = utcnow()
    try:
        key, cert = _credentials(db, conn)
        for account in accounts:
            own = account.get("number") or _digits(account["id"])
            docs = providers.statement_docs(conn.bank, key, cert, account, lo_all, hi_all)
            for lo, hi in months(lo_all, hi_all):
                part = [d for d in docs if lo <= (_doc_day(d) or lo) <= hi]
                res = _apply_docs(db, company, conn.bank, account, own, lo, hi, part, user.id if user else "")
                total += res["operations"]
                duplicates += res["duplicates"]
                saved_all.extend(res["saved"])
    except Exception as exc:                             # noqa: BLE001 — фоновая загрузка не должна падать молча
        return _sync_failed(db, conn, company, exc)
    conn.pending_json = "[]"
    conn.synced_until = hi_all
    db.commit()
    matched = service.match(db, company.id, saved_all) if saved_all else 0
    requests = service.sync_requests(db, company.id, service.periods_of(saved_all)) if saved_all else 0
    service.purge(db, company.id)
    result = {"ok": True, "operations": total, "saved": len(saved_all), "duplicates": duplicates, "matched": matched,
              "open": len(saved_all) - matched, "requests": requests, "pending": 0, "accounts": len(accounts),
              "date_from": lo_all.isoformat(), "date_to": hi_all.isoformat()}
    conn = db.get(BankConnection, conn.id)
    conn.last_ok_at, conn.last_error = utcnow(), ""
    conn.last_result_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    log.info("bank api %s %s: %s ops, %s saved, %s matched", conn.bank, company.id, total, len(saved_all), matched)
    return result


def _sync(db: Session, conn: BankConnection, company: Company, user: User | None) -> dict:
    if conn.bank != "tochka":
        return _sync_other(db, conn, company, user)
    today = today_msk()
    lo_all, hi_all = window(conn, today)
    accounts = [a for a in _accounts(conn) if (a.get("currency") or "RUB") == "RUB"]
    pending_before = {(p.get("account"), p.get("month")): p.get("statement_id") for p in _pending(conn)}
    pending: list[dict] = []
    saved_all: list[BankOperation] = []
    total = duplicates = 0
    conn.last_sync_at = utcnow()
    try:
        access = _access(db, conn)
        for account in accounts:
            for lo, hi in months(lo_all, hi_all):
                month = f"{lo.year:04d}-{lo.month:02d}"
                sid = pending_before.get((_account_key(account["id"]), month))
                if not sid:
                    sid = str(tochka.init_statement(access, account["id"], lo, hi)["statementId"])
                st = tochka.wait_statement(access, account["id"], sid)
                if st is None:
                    pending.append({"account": _account_key(account["id"]), "month": month, "statement_id": sid})
                    continue
                res = _apply(db, company, account, lo, hi, st, user.id if user else "")
                total += res["operations"]
                duplicates += res["duplicates"]
                saved_all.extend(res["saved"])
    except tochka.TochkaError as exc:
        db.rollback()
        conn = db.get(BankConnection, conn.id)
        conn.last_sync_at = utcnow()
        conn.last_error = str(exc)[:300]
        if exc.kind in ("auth", "forbidden"):
            conn.status = "reauth"
        db.commit()
        log.warning("bank api %s: %s (%s)", company.id, exc.kind, exc.status)
        return {"ok": False, "error": str(exc), "kind": exc.kind}
    except Exception as exc:                             # noqa: BLE001 — фоновая загрузка не должна падать молча
        db.rollback()
        log.exception("bank api %s: сбой загрузки", company.id)
        conn = db.get(BankConnection, conn.id)
        conn.last_sync_at = utcnow()
        conn.last_error = f"Внутренняя ошибка загрузки ({type(exc).__name__}) — повторим позже"
        db.commit()
        return {"ok": False, "error": conn.last_error, "kind": "internal"}
    conn.pending_json = json.dumps(pending, ensure_ascii=False)
    if not pending:
        conn.synced_until = hi_all
    db.commit()
    matched = service.match(db, company.id, saved_all) if saved_all else 0
    requests = service.sync_requests(db, company.id, service.periods_of(saved_all)) if saved_all else 0
    service.purge(db, company.id)
    result = {"ok": True, "operations": total, "saved": len(saved_all), "duplicates": duplicates, "matched": matched,
              "open": len(saved_all) - matched, "requests": requests, "pending": len(pending),
              "accounts": len(accounts), "date_from": lo_all.isoformat(), "date_to": hi_all.isoformat()}
    conn = db.get(BankConnection, conn.id)
    conn.last_ok_at, conn.last_error = utcnow(), ""
    conn.last_result_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    log.info("bank api %s: %s ops, %s saved, %s matched, %s pending", company.id, total, len(saved_all), matched,
             len(pending))
    return result


def sync_now(db: Session, company: Company, user: User, bank: str = "tochka") -> dict:
    """Загрузить выписку по кнопке."""
    conn = connection_of(db, company.id, bank)
    if conn is None or conn.status not in ("active", "error"):
        raise ConnectError("Банк не подключён — сначала подключите его", 409)
    if not _sync_lock.acquire(timeout=60):
        raise ConnectError("Загрузка из банка уже идёт — подождите минуту", 409)
    try:
        result = _sync(db, conn, company, user)
    finally:
        _sync_lock.release()
    if result.get("ok"):
        log_action(user, "bank_synced", details={"company": company.name, "operations": result["operations"],
                                                 "saved": result["saved"], "matched": result["matched"],
                                                 "bank": BANKS.get(conn.bank, conn.bank)})
    return result


def due(conn: BankConnection, now: datetime) -> bool:
    if conn.status != "active":
        return False
    if conn.last_sync_at is None:
        return True
    passed = now - conn.last_sync_at
    if _pending(conn):
        return passed >= PENDING_RETRY
    if conn.last_error:
        return passed >= ERROR_RETRY
    return passed >= SYNC_EVERY


def sync_due(db: Session, now: datetime | None = None) -> list[dict]:
    """Фоновый тик: подключённые компании, которым пора загрузить выписку (не больше трёх за раз)."""
    now = now or utcnow()
    if not _sync_lock.acquire(blocking=False):
        return []
    out = []
    try:
        for conn in db.query(BankConnection).filter(BankConnection.status == "active").all():
            if len(out) >= MAX_PER_TICK:
                break
            if not due(conn, now):
                continue
            company = db.get(Company, conn.company_id)
            if company is None or not company.is_active:
                continue
            out.append({"company_id": company.id, "bank": conn.bank, **_sync(db, conn, company, None)})
    finally:
        _sync_lock.release()
    return out
