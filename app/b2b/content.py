# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — содержимое закрытых страниц контура B2B (v1.70.45).
#
# Принцип: страница не может разойтись с сервисом. Состав методов
# берётся из роутера app/pool/router_api.py, лимиты тарифов — из его же
# констант, тексты отказов — дословно из кода. Вручную ведутся только
# пояснения к методам (и расхождение ловит тест).
#
# Раздел администратора (docs/API.md, §10) в клиентские страницы НЕ
# входит: это внутренняя поверхность сервиса.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

from pathlib import Path

from ..pool.router_api import CALLS_RETENTION_DAYS, KEY_PREFIX
from .models import TARIFF_TITLES, TARIFFS
from .pipeline import TARIFF_LIMITS

# Пометка владельца на внутренних поверхностях администратора
ADMIN_MARKER = "Это не видит никто"

CONTACT_EMAIL = "chek@ymaster.ru"
BASE_PATH = "/api/v1/pool-api"
ASSETS = Path(__file__).resolve().parent / "assets"
DIAGRAMS = ("api-access.svg", "api-request-path.svg", "api-data-boundary.svg")

# --------------------------------------------------------------------------
# Что нужно сделать компании или ИП (страница «Раздел API для бизнеса»)
# --------------------------------------------------------------------------
HUB_STEPS = (
    {
        "n": 1,
        "title": "Зарегистрируйтесь с корпоративной почтой",
        "text": "Почта на домене компании (не mail.ru, yandex.ru, gmail.com и "
                "подобных), наименование так, как записано в ЕГРЮЛ/ЕГРИП, ИНН "
                "и пароль не короче 8 символов. Согласие на обработку "
                "персональных данных фиксируется до отправки формы.",
    },
    {
        "n": 2,
        "title": "Подтвердите почту по ссылке из письма",
        "text": "Ссылка действует 24 часа и только один раз. Подтверждение "
                "корпоративного ящика доказывает, что заявку подал "
                "представитель организации, а не посторонний.",
    },
    {
        "n": 3,
        "title": "Пройдите проверку статуса компании",
        "text": "Сервис проверяет ИНН (контрольная сумма, 10 цифр = ООО, "
                "12 = ИП), запрашивает карточку ЕГРЮЛ/ЕГРИП: организация "
                "существует, статус действующий, наименование совпадает. "
                "Дополнительно — дубликаты и оценка риска.",
    },
    {
        "n": 4,
        "title": "Откройте раздел и изучите состав",
        "text": "После подтверждения статуса открываются страницы "
                "«Документация API» и «Тарифы и состав». Это то, что раньше "
                "было видно всем на главной странице, — теперь только "
                "подтверждённым компаниям.",
    },
    {
        "n": 5,
        "title": "Нажмите «Обсудить подключение» и отправьте заявку",
        "text": "Опишите задачу (не короче 20 символов), ожидаемый объём "
                "запросов и выберите тариф. Заявка уходит администратору "
                "сервиса вместе с результатами проверок.",
    },
    {
        "n": 6,
        "title": "Получите решение и ключ",
        "text": "По умолчанию работает частичная автоматизация: проверки "
                "выполняет сервис, а одобряет заявку и выдаёт ключ "
                "администратор. Ключ показывается в кабинете один раз — "
                "в базе хранится только его хэш.",
    },
    {
        "n": 7,
        "title": "Узнайте стоимость",
        "text": "Цены не публикуются: стоимость зависит от лимитов и срока "
                "подписки и сообщается по запросу. Напишите задачу в заявке "
                f"или на {CONTACT_EMAIL} — пришлём условия и счёт.",
    },
)

REQUIREMENTS = (
    "Действующее юридическое лицо (ООО, АО) или индивидуальный "
    "предприниматель (ИП), зарегистрированные в РФ",
    "Корпоративная почта на домене компании — доступ к ящику у заявителя",
    "ИНН, совпадающий с данными ЕГРЮЛ/ЕГРИП, и наименование как в реестре",
    "Понятная задача: что считаете, как часто обращаетесь к API",
    "Согласие на обработку персональных данных (152-ФЗ)",
)

NOT_ACCEPTED = (
    "Бесплатные почтовые домены (mail.ru, yandex.ru, gmail.com и подобные) — "
    "такой ящик не подтверждает принадлежность к компании",
    "Недействующие, ликвидированные и банкротные организации",
    "ИНН с ошибкой в контрольной сумме или чужой ИНН",
    "Запрос сырых чеков, их позиций и данных участников пула",
)

# --------------------------------------------------------------------------
# Граница данных: что отдаём и чего не отдаём ни на каком тарифе
# --------------------------------------------------------------------------
BOUNDARY_GIVE = (
    "Сводка пула: чеки, участники, средний чек, покрытие гео-данными",
    "Топы регионов, отраслей и продавцов за период от 1 до 365 дней",
    "Счётчик и агрегаты под фильтрами: даты, регион, город, отрасль, ИНН, "
    "вилка суммы",
    "Спецификация OpenAPI и интерактивная документация",
)

BOUNDARY_NEVER = (
    "Сырые чеки и их позиции — ни на каком тарифе",
    "Персональные данные участников пула и их история",
    "Идентификация покупателя: кто, где и когда оплатил",
    "Данные, позволяющие связать чек с конкретным человеком",
)

AGGREGATE_FLAG = ("Каждый ответ помечен aggregated: true — это агрегат, "
                  "а не набор чеков")

# --------------------------------------------------------------------------
# Методы: пояснения ведутся вручную, состав — из кода
# --------------------------------------------------------------------------
METHOD_NOTES = {
    "/v1/stats/overview": {
        "title": "Сводка пула",
        "text": "Общие объёмы: сколько чеков принято и верифицировано, сколько "
                "участников сдали чеки, какая доля чеков покрыта гео-данными, "
                "сколько чеков верифицировано за период и средний чек.",
        "params": ("days — глубина периода в днях, по умолчанию 30; "
                   "сервис ограничивает окно от 1 до 365 дней"),
        "fields": ("aggregated, receipts_total, receipts_verified, "
                   "participants, geo_coverage_pct, verified_in_period, "
                   "period_days, avg_receipt_sum"),
        "note": "period_days повторяет число из запроса как есть, а фактическое "
                "окно ограничено 365 днями",
        "example": "curl -H \"X-API-Key: KEY\" \"{base}/v1/stats/overview?days=30\"",
    },
    "/v1/stats/dynamics": {
        "title": "Динамика по периодам",
        "text": "Ряд по дням, неделям ISO или месяцам: число чеков, сумма и "
                "средний чек за период, индекс к первому непустому периоду "
                "(база = 100) и сравнение с предыдущим периодом той же длины. "
                "Отвечает на вопросы, на которые снимок за период ответить не "
                "может: растёт или падает, есть ли сезонность, как изменилось "
                "к прошлому периоду.",
        "params": ("days — глубина окна (1…365); bucket — day, week или month "
                   "(по умолчанию day, неверное значение даёт 422); "
                   "region_code и industry — необязательные фильтры, пустые "
                   "значения означают «без фильтра»"),
        "fields": ("aggregated, period_days, window_days, bucket, points "
                   "(label, receipts, sum, avg, index_sum, index_avg), "
                   "points_count, totals, previous, lfl_pct, base_note, "
                   "filters, receipts_excluded"),
        "note": "Ряд непрерывный: период без чеков присутствует с нулями, а не "
                "пропускается. window_days показывает фактическое окно после "
                "зажима 1…365, а period_days повторяет запрошенное значение. "
                "Индексы равны null, пока в ряду не появилось непустого "
                "периода; lfl_pct равен null, если в прошлом периоде чеков не "
                "было",
        "example": "curl -H \"X-API-Key: KEY\" \"{base}/v1/stats/dynamics?days=90&bucket=week\"",
    },
    "/v1/stats/regions": {
        "title": "Чеки по регионам",
        "text": "Топ регионов по количеству верифицированных чеков за период: "
                "код региона, название и число чеков.",
        "params": ("days — период (1…365); limit — сколько строк вернуть, "
                   "по умолчанию 12 и не больше 95 (число регионов РФ)"),
        "fields": "aggregated, period_days, items[{region_code, region, receipts}]",
        "note": "В подсчёт попадают только чеки с непустым кодом региона",
        "example": "curl -H \"X-API-Key: KEY\" \"{base}/v1/stats/regions?days=30&limit=12\"",
    },
    "/v1/stats/industries": {
        "title": "Чеки по отраслям",
        "text": "Топ отраслей: код отрасли, название, число чеков и средняя "
                "сумма чека в отрасли.",
        "params": ("days — период (1…365); limit — по умолчанию 18 и не "
                   "больше 50"),
        "fields": ("aggregated, period_days, "
                   "items[{industry, name, receipts, avg_sum}]"),
        "note": "Отрасль определяется по коду чека; пустые значения не учитываются",
        "example": "curl -H \"X-API-Key: KEY\" \"{base}/v1/stats/industries?days=90&limit=18\"",
    },
    "/v1/stats/merchants": {
        "title": "Топ продавцов",
        "text": "Продавцы по числу чеков: наименование, ИНН, количество чеков "
                "и средняя сумма. Это публичные реквизиты из чека, без самих "
                "чеков.",
        "params": ("days — период (1…365); limit — по умолчанию 10 и не "
                   "больше 50"),
        "fields": "aggregated, period_days, items[{merchant, inn, receipts, avg_sum}]",
        "note": "Группировка по паре «наименование + ИНН»: одно название у двух "
                "ИНН даёт две строки",
        "example": "curl -H \"X-API-Key: KEY\" \"{base}/v1/stats/merchants?days=30&limit=10\"",
    },
    "/v1/filters/count": {
        "title": "Сколько чеков под фильтрами",
        "text": "Счётчик верифицированных чеков под вашими условиями. Сами "
                "чеки не возвращаются — только количество.",
        "params": ("тело запроса JSON: date_from, date_to, region, city, "
                   "industry, inn, sum_min, sum_max"),
        "fields": "aggregated, count",
        "note": "Пустая выборка — не ошибка: приходит 200 и count = 0",
        "example": ("curl -X POST -H \"X-API-Key: KEY\" -H \"Content-Type: "
                    "application/json\" -d '{\"region\":\"77\",\"sum_min\":500}' "
                    "\"{base}/v1/filters/count\""),
    },
    "/v1/filters/aggregate": {
        "title": "Агрегаты под фильтрами",
        "text": "Те же условия, что и у счётчика, плюс суммы: общая, средняя, "
                "минимальная и максимальная, а также разбивка по отраслям "
                "(не больше 20 строк).",
        "params": ("тело запроса JSON: date_from, date_to, region, city, "
                   "industry, inn, sum_min, sum_max"),
        "fields": ("aggregated, count, sum, avg, min, max, "
                   "by_industry[{industry, name, receipts}]"),
        "note": ("При пустой выборке приходят нули и пустой by_industry; если "
                 "sum_min больше sum_max — выборка пуста, но это 200, не ошибка"),
        "example": ("curl -X POST -H \"X-API-Key: KEY\" -H \"Content-Type: "
                    "application/json\" -d '{\"industry\":\"47.11\","
                    "\"date_from\":\"2026-09-01\"}' "
                    "\"{base}/v1/filters/aggregate\""),
    },
}

# Порядок методов на странице (как в документе)
METHOD_ORDER = tuple(METHOD_NOTES)

# --------------------------------------------------------------------------
# Отказы: тексты дословно из кода (сверка тестом)
# --------------------------------------------------------------------------
ERRORS = (
    {"code": 403, "text": "API для внешних клиентов выключен — обратитесь к "
                          "администратору",
     "why": "Общий выключатель платного API (pool_api_enabled) не включён"},
    {"code": 401, "text": "Укажите ключ в заголовке X-API-Key",
     "why": "Заголовок не передан или пустой"},
    {"code": 401, "text": "Ключ недействителен или отозван",
     "why": "Ключа нет в реестре или он отозван администратором"},
    {"code": 429, "text": "Лимит тарифа: {rate} запросов/час",
     "why": "Превышен почасовой лимит ключа (скользящее окно 1 час)"},
    {"code": 429, "text": "Квота тарифа исчерпана: {quota} запросов/мес",
     "why": "Выбрана месячная квота (счёт с 00:00 первого числа, UTC)"},
    {"code": 403, "text": "Приём чеков выключен — агрегаты недоступны",
     "why": "Чек-Пул выключен администратором: данных для агрегатов нет"},
)

STRICT_RULES = (
    "Даты (date_from, date_to) проверяются мягко: неверный формат просто не "
    "применяет фильтр, ответ 200",
    "Числа (sum_min, sum_max) и строки проверяются строго: неверный тип даёт "
    "422 с телом {\"detail\": \"<поле>: <сообщение>\"}",
    "Ограничения limit зависят от метода: 95 для регионов, 50 для отраслей и "
    "продавцов",
    f"Журнал вызовов хранит только путь запроса и очищается через "
    f"{CALLS_RETENTION_DAYS} дней",
)

# Заголовки остатка лимита: сервер сообщает остаток в каждом ответе, поэтому
# интеграция тормозит заранее, а не угадывает паузу после 429. Имена — те же,
# что ставит app/pool/router_api.rate_headers; соответствие закреплено тестом.
RATE_HEADERS = (
    {"name": "X-RateLimit-Limit",
     "text": "почасовой лимит ключа (скользящее окно 60 минут)"},
    {"name": "X-RateLimit-Remaining",
     "text": "сколько останется после этого обращения"},
    {"name": "X-RateLimit-Reset",
     "text": "Unix-время в секундах (UTC), когда в окне освободится место"},
    {"name": "X-RateLimit-Resource",
     "text": "какой ресурс исчерпывается: requests_per_hour"},
    {"name": "X-Quota-Limit", "text": "месячная квота ключа"},
    {"name": "X-Quota-Remaining",
     "text": "сколько осталось в текущем месяце по UTC"},
    {"name": "X-Quota-Reset",
     "text": "Unix-время в секундах (UTC) начала следующего месяца"},
    {"name": "Retry-After",
     "text": "только в ответе 429: сколько секунд ждать до повтора"},
)

RATE_HEADERS_NOTE = (
    "Окно часового лимита скользящее: X-RateLimit-Reset показывает момент, "
    "когда окно покинет самый старый вызов, а не границу календарного часа. "
    "Следите за X-RateLimit-Remaining и замедляйтесь, когда остаток мал; при "
    "429 ждите Retry-After секунд."
)

KEY_FORMAT = (f"Ключ начинается с {KEY_PREFIX} и целиком показывается один раз "
              "при выдаче; в базе хранится только SHA-256 хэш")


def methods() -> list[dict]:
    """Состав методов — из роутера сервиса (единый источник правды)."""
    from ..pool.router_api import router as api_router
    out: list[dict] = []
    for route in api_router.routes:
        route_methods = getattr(route, "methods", None)
        if not route_methods:
            continue
        path = getattr(route, "path", "")
        short = path.split(BASE_PATH, 1)[-1] if BASE_PATH in path else path
        note = METHOD_NOTES.get(short)
        if note is None:
            continue
        for verb in sorted(route_methods):
            out.append({
                "method": verb,
                "path": path,
                "short": short,
                "summary": getattr(route, "summary", "") or "",
                "title": note["title"],
                "text": note["text"],
                "params": note["params"],
                "fields": note["fields"],
                "note": note["note"],
                # Примеры содержат JSON-скобки, поэтому подстановка адреса —
                # простой заменой, а не str.format (иначе {«region»} → KeyError)
                "example": note["example"].replace("{base}", BASE_PATH),
            })
    order = {short: i for i, short in enumerate(METHOD_ORDER)}
    out.sort(key=lambda item: (order.get(item["short"], 99), item["method"]))
    return out


def tariffs() -> list[dict]:
    """Тарифы и лимиты: цифры из констант сервиса."""
    out = []
    for code in TARIFFS:
        rate, quota = TARIFF_LIMITS[code]
        out.append({
            "code": code,
            "title": TARIFF_TITLES[code],
            "rate_per_hour": rate,
            "monthly_quota": quota,
            "methods": len(METHOD_NOTES),
            "price": "по запросу",
        })
    return out


def diagram(name: str) -> tuple[str, str]:
    """Схема раздела. Возвращает (имя, текст SVG) или ("", "").

    Файлы лежат вне app/static: публичного адреса у них нет, отдаются только
    через закрытый маршрут подтверждённым компаниям.
    """
    if name not in DIAGRAMS:
        return "", ""
    path = ASSETS / name
    if not path.is_file():
        return "", ""
    return name, path.read_text(encoding="utf-8")


def hub(company=None) -> dict:
    """Страница «Раздел API для бизнеса»: что сделать компании или ИП."""
    return {
        "page": "hub",
        "title": "API для бизнеса — агрегаты Чек-Пула",
        "lead": "Обезличенные данные по кассовым чекам: объёмы, география, "
                "отрасли, средний чек, топ продавцов и агрегаты под вашими "
                "фильтрами. Подключение по подписке, тариф — это лимиты "
                "запросов.",
        "steps": list(HUB_STEPS),
        "requirements": list(REQUIREMENTS),
        "not_accepted": list(NOT_ACCEPTED),
        "boundary_give": list(BOUNDARY_GIVE),
        "boundary_never": list(BOUNDARY_NEVER),
        "aggregate_flag": AGGREGATE_FLAG,
        "diagrams": list(DIAGRAMS),
        "contact": CONTACT_EMAIL,
        "company": company.to_dict() if company is not None else None,
    }


def docs(company=None) -> dict:
    """Страница «Документация API»."""
    return {
        "page": "docs",
        "title": "Документация API",
        "base_url": BASE_PATH,
        "auth": "Заголовок X-API-Key в каждом запросе",
        "key_format": KEY_FORMAT,
        "methods": methods(),
        "errors": [dict(e) for e in ERRORS],
        "rate_headers": list(RATE_HEADERS),
        "rate_headers_note": RATE_HEADERS_NOTE,
        "strict_rules": list(STRICT_RULES),
        "openapi": ("/api/docs — Swagger UI, /redoc — ReDoc, "
                    "/openapi.json — спецификация"),
        "aggregate_flag": AGGREGATE_FLAG,
        "diagrams": list(DIAGRAMS),
        "contact": CONTACT_EMAIL,
        "company": company.to_dict() if company is not None else None,
    }


def tariff_page(company=None) -> dict:
    """Страница «Тарифы и состав»."""
    return {
        "page": "tariffs",
        "title": "Тарифы и состав",
        "lead": "Тариф — это лимиты ключа. Состав методов одинаков на любом "
                "тарифе: отличается число запросов в час и месячная квота.",
        "items": tariffs(),
        "included": list(BOUNDARY_GIVE),
        "never": list(BOUNDARY_NEVER),
        "price_note": "Стоимость не публикуется: она зависит от лимитов и "
                      "срока подписки и сообщается по запросу. Напишите задачу "
                      f"в заявке или на {CONTACT_EMAIL}.",
        "methods_count": len(METHOD_NOTES),
        "aggregate_flag": AGGREGATE_FLAG,
        "contact": CONTACT_EMAIL,
        "company": company.to_dict() if company is not None else None,
    }


def contact_page(company=None) -> dict:
    """Страница «Обсудить подключение»: подсказки к форме заявки."""
    return {
        "page": "contact",
        "title": "Обсудить подключение",
        "lead": "Опишите задачу — администратор сервиса увидит заявку вместе с "
                "результатами проверок и сообщит условия и стоимость.",
        "tariffs": [{"code": c, "title": TARIFF_TITLES[c]} for c in TARIFFS],
        "task_hint": "Что считаете, какие срезы нужны, как часто обращаетесь к "
                     "API (не короче 20 символов)",
        "volume_hint": "Например: до 5 000 запросов в месяц, ночной расчёт",
        "email": CONTACT_EMAIL,
        "company": company.to_dict() if company is not None else None,
    }


PAGES = {
    "hub": hub,
    "docs": docs,
    "tariffs": tariff_page,
    "contact": contact_page,
}


def page(name: str, company=None) -> dict | None:
    """Содержимое закрытой страницы по имени (None — если страницы нет)."""
    builder = PAGES.get((name or "").strip().lower())
    return builder(company) if builder else None
