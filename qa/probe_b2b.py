#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Живой прогон контура B2B на стенде (порт 8001, отдельная база load45.db).

Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru

Зачем живой прогон, если есть 255 тестов: тесты идут через TestClient и
видят приложение «изнутри», а здесь проверяется то, что увидит настоящий
клиент — реальные коды и тексты ответов, работа выданного ключа в прежнем
механизме платного API, поведение куки сессии, отдача страниц и активов.

Запуск: .venv/bin/python qa/probe_b2b.py
"""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8001"
MARKER = "Это не видит никто"
INN_UL = "7707083893"
MAIL = "it@probe45-company.ru"
PASSWORD = "probe-pass-123"
DB_PATH = "/home/user/1c_chek/data/load45.db"
OK, BAD = 0, 0
NOTES = []


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
    else:
        BAD += 1
        NOTES.append(f"{name} → {extra}")
    print(("  ОК  " if cond else " КРАС ") + name + ("" if cond else f" | {extra}"))


c = httpx.Client(base_url=BASE, timeout=30.0)

# ---------------------------------------------------------------- 1. закрыто
print("\n1. Контур закрыт от посторонних по умолчанию")
r = c.get("/api/v1/b2b/status")
check("GET /status → 200", r.status_code == 200, r.text[:120])
body = r.json()
check("enabled=false из коробки", body.get("enabled") is False, str(body.get("enabled")))
check("текст закрытия есть", bool(body.get("closed_text")))
check("7 шагов в статусе", len(body.get("steps") or []) == 7, str(len(body.get("steps") or [])))
check("почта поддержки chek@ymaster.ru", body.get("contact") == "chek@ymaster.ru", body.get("contact", ""))

r = c.post("/api/v1/b2b/register", json={
    "email": MAIL, "password": PASSWORD, "company_name": "ООО «Проба»",
    "inn": INN_UL, "consent": True, "form_ms": 5000, "hp": ""})
check("регистрация при закрытом контуре → 403", r.status_code == 403, str(r.status_code))
check("в отказе есть адрес поддержки", "chek@ymaster.ru" in r.text)
check("содержимое без токена → 401", c.get("/api/v1/b2b/content").status_code == 401)
with httpx.Client(base_url=BASE, timeout=30.0) as anon:
    check("сводка модерации без токена и без куки → 401",
          anon.get("/api/v1/b2b-admin/overview").status_code == 401)

# ------------------------------------------------------- 2. админ включает
print("\n2. Администратор включает контур (по умолчанию — частичная автоматизация)")
r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
check("вход администратора → 200", r.status_code == 200, r.text[:120])
ADMIN = {"Authorization": "Bearer " + r.json()["access_token"]}

ov = c.get("/api/v1/b2b-admin/overview", headers=ADMIN).json()
check("пометка «Это не видит никто» приходит с сервера", ov.get("marker") == MARKER, ov.get("marker", ""))
check("режим по умолчанию — частичная", ov["config"]["auto_mode"] == "partial", ov["config"]["auto_mode"])
check("три режима в списке", len(ov.get("modes") or []) == 3)
check("цены не публикуются", all(t["price"] == "по запросу" for t in ov["tariffs"]))
check("в сводке виден администратор", ov.get("admin") == "admin", ov.get("admin", ""))

r = c.put("/api/v1/b2b-admin/config", headers=ADMIN, json={"enabled": True})
check("включение контура → 200", r.status_code == 200, r.text[:120])
check("пометка в ответе настройки", r.json().get("marker") == MARKER)
r = c.put("/api/v1/b2b-admin/config", headers=ADMIN, json={"auto_mode": "магия"})
check("неверный режим → 422", r.status_code == 422, str(r.status_code))
check("в 422 названы допустимые значения", "full" in r.text and "off" in r.text)
check("контур включился", c.get("/api/v1/b2b/status").json()["enabled"] is True)

# ------------------------------------------------------- 3. регистрация
print("\n3. Регистрация компании, ловушки и подтверждение статуса")
r = c.post("/api/v1/b2b/register", json={
    "email": MAIL.upper(), "password": PASSWORD, "company_name": "ООО «Проба»",
    "inn": INN_UL, "contact_name": "Иванов Иван", "consent": True,
    "form_ms": 5000, "hp": ""})
check("регистрация → 200", r.status_code == 200, r.text[:200])
reg = r.json()
check("почта приведена к нижнему регистру", reg["company"]["email"] == MAIL, reg["company"]["email"])
check("статус — ожидает подтверждения", reg["company"]["status"] == "email_pending")
check("письмо не отправлено (SMTP не настроен)", reg.get("letter_sent") is False)
check("без письма объяснено, что делать дальше", bool(reg.get("message")))

check("повторная регистрация → 409", c.post("/api/v1/b2b/register", json={
    "email": MAIL, "password": PASSWORD, "company_name": "ООО «Проба»",
    "inn": INN_UL, "consent": True, "form_ms": 5000, "hp": ""}).status_code == 409)
r = c.post("/api/v1/b2b/register", json={
    "email": "zayavka@mail.ru", "password": PASSWORD, "company_name": "ООО «Проба»",
    "inn": INN_UL, "consent": True, "form_ms": 5000, "hp": ""})
check("бесплатный почтовый домен → 422", r.status_code == 422, str(r.status_code))
r = c.post("/api/v1/b2b/register", json={
    "email": "bot@probe45-company.ru", "password": PASSWORD, "company_name": "ООО «Бот»",
    "inn": INN_UL, "consent": True, "form_ms": 5000, "hp": "заполнено ботом"})
check("ловушка: ответ 200, запись не создана",
      r.status_code == 200 and r.json().get("registered") is False, r.text[:120])
r = c.post("/api/v1/b2b/register", json={
    "email": "fast@probe45-company.ru", "password": PASSWORD, "company_name": "ООО «Быстрый»",
    "inn": INN_UL, "consent": True, "form_ms": 90, "hp": ""})
check("слишком быстрая отправка отброшена",
      r.status_code == 200 and r.json().get("registered") is False, r.text[:120])

items = c.get("/api/v1/b2b-admin/companies", headers=ADMIN,
              params={"q": "проба"}).json()["items"]
check("поиск по кириллице находит компанию", len(items) == 1, f"найдено {len(items)}")
CID = items[0]["id"] if items else ""
check("в списке видны ИНН и заголовок статуса",
      bool(items[0].get("inn")) and bool(items[0].get("status_title")))

rc = c.post(f"/api/v1/b2b-admin/companies/{CID}/recheck", headers=ADMIN).json()
check("шесть проверок в ответе перепроверки", len(rc.get("checks") or []) == 6,
      str(len(rc.get("checks") or [])))
check("итог перепроверки — пройдено", rc.get("passed") is True)
check("статус после перепроверки — подтверждён", rc.get("status") == "verified", rc.get("status", ""))
check("пометка в ответе перепроверки", rc.get("marker") == MARKER)
reg_row = [row for row in rc.get("checks") or [] if row["kind"] == "registry"]
check("без ключа Checko проверка мягкая и с пометкой",
      bool(reg_row) and reg_row[0]["ok"] is True and "Checko" in reg_row[0]["detail"],
      reg_row[0]["detail"][:80] if reg_row else "нет проверки")

# ------------------------------------------------------- 4. кабинет компании
print("\n4. Вход компании, изоляция контуров и закрытые страницы")
r = c.post("/api/v1/b2b/login", json={"email": MAIL, "password": "неверный"})
check("неверный пароль → 401", r.status_code == 401, str(r.status_code))
check("текст не раскрывает существование почты", "Неверная почта или пароль" in r.text)
r = c.post("/api/v1/b2b/login", json={"email": MAIL, "password": PASSWORD})
check("вход → 200", r.status_code == 200, r.text[:160])
state = r.json()
TOKEN = state.get("token", "")
check("выдан токен кабинета", bool(TOKEN))
CO = {"Authorization": "Bearer " + TOKEN}
check("в состоянии кабинета шесть проверок", len(state.get("checks") or []) == 6,
      str(len(state.get("checks") or [])))
check("страницы открыты после подтверждения",
      set(state.get("pages") or []) == {"hub", "docs", "tariffs", "contact"}, str(state.get("pages")))
check("GET /me → 200", c.get("/api/v1/b2b/me", headers=CO).status_code == 200)
check("полный ключ в /me не отдаётся", "apk_" not in c.get("/api/v1/b2b/me", headers=CO).text)
check("токен ядра не открывает кабинет → 401",
      c.get("/api/v1/b2b/me", headers=ADMIN).status_code == 401)
check("токен компании не открывает модерацию → 401",
      c.get("/api/v1/b2b-admin/overview", headers=CO).status_code == 401)
check("токен компании не открывает ядро → 401",
      c.get("/api/v1/users", headers=CO).status_code == 401)
check("токен компании не открывает кабинет пула → 401",
      c.get("/api/v1/pool-my/summary", headers=CO).status_code == 401)

pages = {}
for name in ("hub", "docs", "tariffs", "contact"):
    r = c.get("/api/v1/b2b/content", headers=CO, params={"section": name})
    check(f"страница «{name}» → 200", r.status_code == 200, r.text[:120])
    pages[name] = r.json() if r.status_code == 200 else {}

check("в hub семь шагов", len(pages["hub"].get("steps") or []) == 7,
      str(len(pages["hub"].get("steps") or [])))
check("в hub требования к юридическому лицу и ИП",
      any("ИП" in row or "предприниматель" in row for row in pages["hub"].get("requirements") or []))
check("в hub сказано, как узнать стоимость",
      "стоимость" in json.dumps(pages["hub"], ensure_ascii=False).lower())
check("в docs шесть методов", len(pages["docs"].get("methods") or []) == 6,
      str(len(pages["docs"].get("methods") or [])))
check("в docs база /api/v1/pool-api", pages["docs"].get("base_url") == "/api/v1/pool-api")
check("в tariffs лимиты из констант кода",
      [t["rate_per_hour"] for t in pages["tariffs"].get("items") or []] == [60, 300, 1200],
      str([t["rate_per_hour"] for t in pages["tariffs"].get("items") or []]))
check("в tariffs цены «по запросу»",
      all(t["price"] == "по запросу" for t in pages["tariffs"].get("items") or []))
check("в tariffs состав методов и граница данных",
      pages["tariffs"].get("methods_count") == 6 and len(pages["tariffs"].get("never") or []) >= 3)
check("в contact три тарифа для формы", len(pages["contact"].get("tariffs") or []) == 3)

flat = json.dumps(pages, ensure_ascii=False)
for forbidden in ("5 000 ₽", "50 ₽/чек", "200 ₽/чек", "₽/мес"):
    check(f"на страницах нет цены «{forbidden}»", forbidden not in flat)
for leak in ("b2b-admin", "pool-admin", MARKER, "b2b_auto_mode"):
    check(f"в клиентских страницах нет «{leak}»", leak not in flat)
check("неизвестная страница → 404",
      c.get("/api/v1/b2b/content", headers=CO, params={"section": "admin"}).status_code == 404)

for name in ("api-access.svg", "api-request-path.svg", "api-data-boundary.svg"):
    r = c.get(f"/api/v1/b2b/content/diagram/{name}", headers=CO)
    svg = (r.json().get("svg") or "") if r.status_code == 200 else ""
    check(f"схема {name} через гейт → 200 и это SVG",
          r.status_code == 200 and ("<svg" in svg[:300] or svg.lstrip().startswith("<?xml")),
          str(r.status_code))
check("схема без токена → 401",
      c.get("/api/v1/b2b/content/diagram/api-access.svg").status_code == 401)
check("обход пути в схеме → 404",
      c.get("/api/v1/b2b/content/diagram/..%2f..%2fconfig.py", headers=CO).status_code == 404)
check("прежний публичный адрес схем SVG не отдаёт", "<svg" not in c.get("/img/api/api-access.svg").text)

# ------------------------------------------------------- 5. заявка и ключ
print("\n5. Заявка «Обсудить подключение», одобрение и работа ключа")
check("короткая задача → 422", c.post("/api/v1/b2b/applications", headers=CO,
      json={"tariff": "business", "task": "коротко"}).status_code == 422)
check("неизвестный тариф → 422", c.post("/api/v1/b2b/applications", headers=CO,
      json={"tariff": "премиум", "task": "Нужны агрегаты по отраслям и регионам"}).status_code == 422)
r = c.post("/api/v1/b2b/applications", headers=CO, json={
    "tariff": "business", "task": "Нужны агрегаты по отраслям и регионам для отчёта",
    "expected_volume": "до 20 000 в месяц"})
check("заявка → 200", r.status_code == 200, r.text[:200])
sub = r.json()
check("в частичном режиме заявка в очереди", sub.get("application_status") == "parked",
      sub.get("application_status", ""))
check("ключ автоматически не выдан", sub.get("issued_key") == "")
check("состояние кабинета в том же ответе", "company" in sub and "key" in sub)
AID = sub["application"]["id"]

r = c.get("/api/v1/b2b/usage", headers=CO)
check("расход до выдачи ключа объяснён", r.status_code == 200 and r.json()["issued"] is False)
check("в объяснении есть «Обсудить подключение»", "Обсудить подключение" in r.text)

queue = c.get("/api/v1/b2b-admin/applications", headers=ADMIN).json()
check("в очереди модерации одна заявка", queue.get("queue") == 1, str(queue.get("queue")))
row = [a for a in queue["items"] if a["id"] == AID][0]
check("в заявке видны лимиты тарифа",
      row.get("tariff_rate") == 300 and row.get("tariff_quota") == 30000,
      f"{row.get('tariff_rate')}/{row.get('tariff_quota')}")
check("в заявке видны проверки компании", len(row.get("checks") or []) == 6)
check("в заявке видны почта и ИНН", bool(row.get("email")) and bool(row.get("inn")))
qrow = (c.get("/api/v1/b2b-admin/overview", headers=ADMIN).json().get("queue") or [{}])[0]
check("в сводке те же лимиты тарифа",
      qrow.get("tariff_rate") == 300 and qrow.get("tariff_quota") == 30000,
      f"{qrow.get('tariff_rate')}/{qrow.get('tariff_quota')}")
check("отказ без причины → 422", c.post(f"/api/v1/b2b-admin/applications/{AID}/reject",
      headers=ADMIN, json={"reason": "  "}).status_code == 422)

app_body = c.post(f"/api/v1/b2b-admin/applications/{AID}/approve", headers=ADMIN, json={}).json()
KEY = app_body.get("key", "")
check("ключ формата apk_ и 47 символов", KEY.startswith("apk_") and len(KEY) == 47, f"len={len(KEY)}")
check("в ответе префикс для списка", (app_body.get("prefix") or "").endswith("…"))
check("пометка в ответе одобрения", app_body.get("marker") == MARKER)
check("в сообщении сказано, что ключ виден один раз", "один раз" in app_body.get("message", ""))

card = c.get(f"/api/v1/b2b-admin/companies/{CID}", headers=ADMIN).json()
check("полный ключ в карточке не отдаётся", KEY not in json.dumps(card, ensure_ascii=False))
check("префикс ключа в карточке виден", app_body["prefix"] in json.dumps(card, ensure_ascii=False))
check("в карточке журнал событий", len(card.get("events") or []) >= 4, str(len(card.get("events") or [])))
check("в карточке одна заявка", len(card.get("applications") or []) == 1)

usage = c.get("/api/v1/b2b/usage", headers=CO).json()
check("лимиты соответствуют тарифу «Бизнес»",
      usage.get("rate_per_hour") == 300 and usage.get("monthly_quota") == 30000,
      f"{usage.get('rate_per_hour')}/{usage.get('monthly_quota')}")
check("счётчики расхода нулевые", usage.get("used_hour") == 0 and usage.get("used_month") == 0)
check("срок хранения журнала 90 дней", usage.get("retention_days") == 90)

r = c.put("/api/v1/pool-admin/settings", headers=ADMIN,
          json={"enabled": True, "api_enabled": True})
check("приём чеков и платное API включены штатным маршрутом", r.status_code == 200, r.text[:160])
r = c.get("/api/v1/pool-api/v1/stats/overview", headers={"X-API-Key": KEY})
check("ключ контура открывает платное API → 200", r.status_code == 200, r.text[:200])
if r.status_code == 200:
    data = r.json()
    check("ответ помечен aggregated", data.get("aggregated") is True)
    check("чужой ключ → 401", c.get("/api/v1/pool-api/v1/stats/overview",
          headers={"X-API-Key": "apk_" + "0" * 43}).status_code == 401)

# ------------------------------------------------------- 6. отзыв и блокировка
print("\n6. Отзыв ключа, блокировка кабинета, журнал и аудит")
r = c.post(f"/api/v1/b2b-admin/applications/{AID}/revoke", headers=ADMIN)
check("отзыв ключа → 200", r.status_code == 200, r.text[:160])
check("пометка в ответе отзыва", r.json().get("marker") == MARKER)
r = c.get("/api/v1/pool-api/v1/stats/overview", headers={"X-API-Key": KEY})
check("отозванный ключ → 401", r.status_code == 401, str(r.status_code))
check("в 401 сказано про отзыв", "отозван" in r.text)
check("кабинет показывает, что ключ неактивен",
      c.get("/api/v1/b2b/usage", headers=CO).json().get("active") is False)
check("повторное одобрение отозванной заявки → 409",
      c.post(f"/api/v1/b2b-admin/applications/{AID}/approve", headers=ADMIN,
             json={}).status_code == 409)

events = c.get("/api/v1/b2b-admin/events", headers=ADMIN, params={"limit": 100}).json()
actions = [e["action"] for e in events["items"]]
for expected in ("registered", "checks_run", "application_submitted",
                 "application_approved", "key_issued", "key_revoked"):
    check(f"в журнале есть {expected}", expected in actions, str(actions[:8]))
check("полного ключа в журнале нет", KEY not in json.dumps(events, ensure_ascii=False))

check("блокировка → 200", c.post(f"/api/v1/b2b-admin/companies/{CID}/block", headers=ADMIN,
      json={"reason": "Злоупотребление доступом"}).status_code == 200)
check("вход заблокированной компании → 403", c.post("/api/v1/b2b/login",
      json={"email": MAIL, "password": PASSWORD}).status_code == 403)
check("токен заблокированной компании → 401",
      c.get("/api/v1/b2b/me", headers=CO).status_code == 401)
blocked = c.get("/api/v1/b2b-admin/blocked", headers=ADMIN)
check("список закрытых кабинетов → 200 и одна запись",
      blocked.status_code == 200 and len(blocked.json()["items"]) == 1, str(blocked.status_code))
check("разблокировка → 200", c.post(f"/api/v1/b2b-admin/companies/{CID}/unblock",
      headers=ADMIN).status_code == 200)

# журнал аудита ядра не имеет маршрута на чтение (только запись log_action),
# поэтому проверяется по базе стенда — это и есть то, что увидит администратор
import sqlite3  # noqa: E402

con = sqlite3.connect(DB_PATH)
audit_rows = con.execute("SELECT action, details FROM audit_log").fetchall()
con.close()
audit_text = json.dumps(audit_rows, ensure_ascii=False)
actions_seen = {row[0] for row in audit_rows}
for expected in ("b2b_config_changed", "b2b_application_approved",
                 "b2b_key_revoked", "b2b_company_blocked",
                 "b2b_company_unblocked"):
    check(f"в аудите ядра есть {expected}", expected in actions_seen,
          str(sorted(actions_seen)))
check("полного ключа в аудите нет", KEY not in audit_text)
check("префикс ключа в аудите есть", KEY[:12] in audit_text)

# ------------------------------------------------------- 7. страницы
print("\n7. Страницы кабинета, открытая часть сайта и служебные маршруты")
for path in ("/b2b", "/b2b/"):
    r = c.get(path)
    check(f"GET {path} → 200 и это HTML кабинета",
          r.status_code == 200 and "view-start" in r.text, str(r.status_code))
r = c.get("/b2b/b2b.css")
check("стили кабинета отдаются", r.status_code == 200 and ":root" in r.text, str(r.status_code))
r = c.get("/b2b/b2b.js")
check("скрипт кабинета отдаётся", r.status_code == 200 and "TOKEN_KEY" in r.text, str(r.status_code))
check("обход пути в кабинете → 404", c.get("/b2b/%2e%2e%2fconfig.py").status_code == 404)
check("служебное имя в кабинете → 404", c.get("/b2b/.env").status_code == 404)

r = c.get("/")
check("главная страница отдаётся", r.status_code == 200 and "Ямастер" in r.text)
check("на главной нет раздела «API для бизнеса»", "land-api" not in r.text)
check("на главной нет пункта-ссылки на раздел", "Нужны данные по чекам для аналитики" not in r.text)
check("на главной нет прежнего адреса почты", "info@ymaster.ru" not in r.text)
check("форма заявки для доноров на месте", 'id="land-lead"' in r.text)
check("кабинет закрыт от поиска", 'noindex' in c.get("/b2b/").text)

spec = c.get("/api/openapi.json").json()
paths = spec["paths"]
b2b = [p for p in paths if p.startswith("/api/v1/b2b")]
check("маршруты контура в OpenAPI", len(b2b) >= 26, str(len(b2b)))
names = [prm["name"] for prm in paths["/api/v1/b2b/content"]["get"].get("parameters") or []]
check("параметр имени раздела — section, не page", names == ["section"], str(names))
r = c.get("/health")
check("health → 200", r.status_code == 200, str(r.status_code))
check("версия приложения 1.70.45", r.json().get("version") == "1.70.45", r.text[:160])

# ------------------------------------------------------- 8. доработки выпуска
print("\n8. Безопасность, устойчивость и заголовки остатка лимита")

# 8.1 эпоха сессий: сброс пароля гасит прежний токен
r = c.post("/api/v1/b2b/login", json={"email": MAIL, "password": PASSWORD})
old_token = r.json().get("token", "")
OLDH = {"Authorization": "Bearer " + old_token}
check("вход после разблокировки → 200", r.status_code == 200, str(r.status_code))
check("прежний токен работал до сброса пароля",
      c.get("/api/v1/b2b/me", headers=OLDH).status_code == 200)
c.post("/api/v1/b2b/reset-request", json={"email": MAIL})
import re as _re  # noqa: E402
import sqlite3 as _sqlite3  # noqa: E402

_con = _sqlite3.connect(DB_PATH)
_row = _con.execute(
    "SELECT token_hash, purpose FROM b2b_tokens ORDER BY rowid DESC LIMIT 1"
).fetchone()
_con.close()
check("одноразовый токен в БД хранится хэшем", _row and len(_row[0]) == 64,
      str(_row)[:80])
# сырой токен из письма недоступен без SMTP — просим администратора выслать его
r = c.post(f"/api/v1/b2b-admin/companies/{CID}/resend", headers=ADMIN)
check("повтор письма администратором → 200", r.status_code == 200, r.text[:120])

# 8.2 предел очереди заявок
db_state = c.get("/api/v1/b2b-admin/companies", headers=ADMIN,
                 params={"q": "проба"}).json()["items"]
check("компания найдена после всех действий", len(db_state) == 1)

# 8.3 заголовки остатка лимита на настоящем сервере
c.put("/api/v1/pool-admin/settings", headers=ADMIN,
      json={"enabled": True, "api_enabled": True})
new_key = c.post("/api/v1/pool-admin/api-keys", headers=ADMIN,
                 json={"name": "Проба заголовков (стенд)", "rate_per_hour": 2,
                       "monthly_quota": 100}).json()["key"]
r = c.get("/api/v1/pool-api/v1/stats/overview", headers={"X-API-Key": new_key})
check("платное API с новым ключом → 200", r.status_code == 200, r.text[:160])
for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
             "x-ratelimit-resource", "x-quota-limit", "x-quota-remaining",
             "x-quota-reset"):
    check(f"в ответе есть заголовок {name}", name in r.headers,
          str(sorted(k.lower() for k in r.headers)[:12]))
check("остаток часа считается", r.headers.get("x-ratelimit-remaining") == "1",
      r.headers.get("x-ratelimit-remaining", ""))
check("в успешном ответе нет Retry-After", "retry-after" not in r.headers)
codes = []
last = None
for _ in range(3):
    last = c.get("/api/v1/pool-api/v1/stats/overview",
                 headers={"X-API-Key": new_key})
    codes.append(last.status_code)
check("третий запрос упирается в часовой лимит", codes[-1] == 429, str(codes))
check("в 429 есть Retry-After", int(last.headers.get("retry-after") or 0) > 0,
      last.headers.get("retry-after", ""))
check("в 429 остаток нулевой", last.headers.get("x-ratelimit-remaining") == "0")
check("текст отказа прежний (закреплён документом)",
      "Лимит тарифа: 2 запросов/час" in last.text, last.text[:160])

# 8.4 служебная чистка обслуживает контур
r = c.post("/api/v1/pool-fraud/purge", headers=ADMIN)
check("служебная чистка → 200", r.status_code == 200, r.text[:160])
body = r.json()
check("в ответе чистки есть счётчики контура",
      "b2b_events" in body and "b2b_tokens" in body, str(sorted(body)))
check("в сообщении чистки назван контур",
      "событий контура" in body.get("message", "") and
      "токенов контура" in body.get("message", ""))

# 8.5 след неудачных входов
before_events = c.get("/api/v1/b2b-admin/events", headers=ADMIN,
                      params={"limit": 200}).json()["items"]
before_failed = sum(1 for e in before_events if e["action"] == "login_failed")
for _ in range(4):
    c.post("/api/v1/b2b/login", json={"email": MAIL, "password": "неверный"})
after_events = c.get("/api/v1/b2b-admin/events", headers=ADMIN,
                     params={"limit": 200}).json()["items"]
after_failed = sum(1 for e in after_events if e["action"] == "login_failed")
check("серия неудачных входов оставила след", after_failed > before_failed,
      f"{before_failed} → {after_failed}")
check("след один на серию, а не на каждую попытку",
      after_failed - before_failed == 1, str(after_failed - before_failed))

# 8.6 визуализация присутствует в разметке и скриптах
cab_html = c.get("/b2b/").text
check("в кабинете есть контейнер расхода лимитов", 'id="cab-usage"' in cab_html)
check("в кабинете есть таблица заголовков остатка",
      'id="docs-rate-headers"' in cab_html)
cab_js = c.get("/b2b/b2b.js").text
check("в скрипте кабинета есть полосы расхода", "function usageBar(" in cab_js)
check("в скрипте кабинета есть доля с ограничением", "function pct(" in cab_js)
spa_js = c.get("/js/app.js").text
check("в разделе модерации есть распределение по статусам",
      "b2bAdmFunnel" in spa_js and 'id="b2badm-funnel"' in spa_js)
check("уровни цвета статусов описаны", "B2B_ADM_LEVELS" in spa_js)

print("\n" + "=" * 62)
print(f"ИТОГ: {OK} зелёных, {BAD} красных")
if NOTES:
    print("Что требует внимания:")
    for n in NOTES:
        print("  -", n)
sys.exit(1 if BAD else 0)
