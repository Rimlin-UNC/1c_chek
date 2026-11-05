"""Банки по API — реестр (v1.70.102, ADR-32): что нужно для подключения и как читать счета и выписку.

У компании может быть несколько банков (строка bank_connections на каждую пару «компания + банк»):
бухгалтер-аутсорсер держит в карточке клиента все его банки, выписки приходят сами раз в 6 часов и идут
одним путём — классификатор, сверка с чеками и УПД, «Что нужно от клиента».

Способы доступа (method):
  oauth   — Точка: вход клиента в банк и разрешение на чтение (app/bank/tochka.py, ADR-29);
  jwt     — Точка: JWT-ключ из интернет-банка;
  token   — ключ API, который владелец счёта выпускает сам (Т-Банк, Модульбанк, Альфа-Банк);
  refresh — OAuth 2.0 с парой токенов из личного кабинета банка (Сбер): программа сама продлевает доступ.
Сертификат клиента (cert): required — без него банк не пустит (Сбер), optional — если банк его выдал (Альфа).

Банки первой десятки без открытого API выписок — в FILE_BANKS: для них выписка загружается файлом
в формате 1С (он есть в интернет-банке любого банка), а состояние API описано честно — без догадок.
Адреса и поля — по открытым спецификациям банков (ссылки в docs); проверено на синтетике (ADR-14).
"""
from __future__ import annotations

from datetime import date

from . import alfa, modul, sber, tbank

PROVIDERS: dict[str, dict] = {
    "tochka": {
        "title": "Точка Банк", "methods": ["oauth", "jwt"], "cert": "", "daily": False,
        "how": "Интернет-банк Точки → «Интеграции и API» → «Подключить OAuth» (client_id и client_secret) "
               "или «Создать JWT-ключ».",
        "docs": "https://developers.tochka.com/docs/tochka-api/"},
    "tbank": {
        "title": "Т-Банк", "methods": ["token"], "cert": "", "daily": False,
        "how": "Т-Бизнес → «Интеграции» → «Выпуск токена»: доступ «Счета и выписки» — информация о счетах и "
               "об операциях компании. Токен привязан к ИНН компании; хватит прав только на чтение.",
        "docs": "https://developer.tbank.ru/docs/products/account-info"},
    "sber": {
        "title": "Сбербанк", "methods": ["refresh"], "cert": "required", "daily": True,
        "how": "Личный кабинет Sber API (developers.sber.ru) → интеграция своей организации: client_id, "
               "client_secret и refresh_token с операцией GET_STATEMENT_ACCOUNT. Сертификат для API "
               "выпускается в СберБизнесе — загрузите его с закрытым ключом (PEM или .p12).",
        "docs": "https://developers.sber.ru/docs/ru/sber-api/scenarios/rko/statements/overview"},
    "alfa": {
        "title": "Альфа-Банк", "methods": ["token"], "cert": "optional", "daily": True,
        "how": "Альфа-Бизнес Онлайн → Alfa API (заявка на alfa_api@alfabank.ru): ключ API или токен доступа. "
               "Если банк выдал сертификат по вашему запросу CSR — загрузите его вместе с ключом.",
        "docs": "https://github.com/alfacomdevelopment/alfa-api-sdk"},
    "modul": {
        "title": "Модульбанк", "methods": ["token"], "cert": "", "daily": False,
        "how": "Интернет-банк → «Услуги» → «Интеграция с банком» → «Подключение к API» → «Сгенерировать "
               "новый»: права account-info и operation-history.",
        "docs": "https://api.modulbank.ru"},
}

# Первая десятка без открытого API выписок для своей компании — выписка файлом в формате 1С.
FILE_BANKS: list[dict] = [
    {"title": "ВТБ", "note": "API — «Интеграционный Банк-Клиент» по договору (подписанные запросы ГОСТ)"},
    {"title": "Газпромбанк", "note": "открытого API выписок для своей компании нет"},
    {"title": "Райффайзенбанк", "note": "Raif API по заявке в интернет-банке; спецификация выписок — требует уточнения"},
    {"title": "Россельхозбанк", "note": "открытого API выписок для своей компании нет"},
    {"title": "ПСБ", "note": "PSB Corporate Open API — по договору"},
    {"title": "Совкомбанк", "note": "открытого API выписок для своей компании нет"},
    {"title": "МКБ", "note": "открытого API выписок для своей компании нет"},
    {"title": "Банк ДОМ.РФ", "note": "открытого API выписок для своей компании нет"},
]

TITLES = {k: v["title"] for k, v in PROVIDERS.items()}
DAILY_MAX_DAYS = 45                    # выписка по дням (Сбер, Альфа): за одну загрузку — не больше 45 дней


def meta(bank: str) -> dict:
    p = PROVIDERS[bank]
    return {"id": bank, "title": p["title"], "methods": list(p["methods"]), "cert": p["cert"], "how": p["how"],
            "docs": p["docs"]}


def organization(bank: str, key: str, cert_pem: str) -> dict:
    """{inn, accounts}: проверка доступа и счета компании (inn — пусто, если банк его не сообщает)."""
    if bank == "tbank":
        return {"inn": "", "accounts": tbank.accounts(key, cert_pem)}
    if bank == "sber":
        return sber.organization(key, cert_pem)
    if bank == "alfa":
        return alfa.organization(key, cert_pem)
    if bank == "modul":
        return modul.organization(key, cert_pem)
    raise KeyError(bank)


def statement_docs(bank: str, key: str, cert_pem: str, account: dict, lo: date, hi: date) -> list[dict]:
    """Документы выписки (поля формата обмена с 1С) по одному счёту за период."""
    if bank == "tbank":
        return tbank.statement_docs(key, account, lo, hi, cert_pem)
    if bank == "sber":
        return sber.statement_docs(key, account, lo, hi, cert_pem)
    if bank == "alfa":
        return alfa.statement_docs(key, account, lo, hi, cert_pem)
    if bank == "modul":
        return modul.statement_docs(key, account, lo, hi, cert_pem)
    raise KeyError(bank)
