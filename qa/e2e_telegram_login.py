# -*- coding: utf-8 -*-
"""Живой сквозной прогон входа через Telegram (v1.70.69) — не тест, а проверка.

Поднимается настоящий uvicorn с отдельной базой, рядом — поддельный сервер
Bot API (127.0.0.1:8099), который отвечает как Telegram. Затем:

 1. администратор входит по паролю и сохраняет токен бота и адрес API;
 2. /auth/telegram/refresh-bot берёт имя бота через getMe (в поддельный API);
 3. /auth/telegram/start выдаёт код и ссылку t.me;
 4. опрос до подтверждения — pending;
 5. сообщение боту «/start tglogin_…» приходит тем же путём, что из
    long-poll (telegram_bot.handle_update) — отдельным процессом по той же БД;
 6. опрос → ok, cookie-сессия, /auth/me;
 7. повторный вход тем же чатом — та же учётная запись (без дубля);
 8. в базе только хэш кода, код погашен, в журнале аудита login_telegram;
 9. поддельный API получил sendMessage (значит путь отправки живой).

Запуск: DATABASE_URL=… .venv/bin/python qa/e2e_telegram_login.py <база>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8011"
FAKE = "http://127.0.0.1:8099"
BOT_TOKEN = "123456789:AAFakeTokenForEndToEndTestOnly"
CHAT = "700900123"
USERNAME = "e2e_tg_user"

ok, bad = [], []


def check(cond: bool, title: str, extra: str = "") -> None:
    (ok if cond else bad).append(title)
    print(("  ✔ " if cond else "  ✘ ") + title + (f" — {extra}" if extra and not cond else ""))


def call(method: str, path: str, body: dict | None = None,
         cookie: str = "", headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {}), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return e.code, payload, e.headers


def main() -> int:
    db_url = os.environ["DATABASE_URL"]
    # 1. вход администратора
    st, d, hdrs = call("POST", "/api/v1/auth/login",
                       {"username": "admin", "password": "admin123"})
    check(st == 200 and d.get("access_token"), "администратор вошёл по паролю",
          f"{st} {d}")
    admin = {"Authorization": "Bearer " + d.get("access_token", "")}

    # 2. настройки бота: токен + поддельный адрес API
    st, d, _ = call("PUT", "/api/v1/settings/telegram",
                    {"bot_token": BOT_TOKEN, "api_base": FAKE}, headers=admin)
    check(st == 200, "настройки Telegram сохранены", f"{st} {d}")

    # 3. getMe через поддельный API — имя бота для ссылки t.me
    st, d, _ = call("POST", "/api/v1/settings/telegram/refresh-bot", {},
                    headers=admin)
    check(st == 200 and d.get("bot_username") == "e2e_check_bot",
          "имя бота получено через getMe", f"{st} {d}")

    # 4. старт входа: код и ссылка
    st, d, _ = call("POST", "/api/v1/auth/telegram/start", {})
    code = d.get("code", "")
    check(st == 200 and code.startswith("tglogin_"), "код входа выдан", f"{st} {d}")
    check(d.get("tg_link") == f"https://t.me/e2e_check_bot?start={code}",
          "ссылка t.me собрана из имени бота", str(d.get("tg_link")))
    check(len(code) <= 64, "код помещается в start-параметр")

    # 5. до подтверждения — pending
    st, p, _ = call("POST", "/api/v1/auth/telegram/poll", {"code": code})
    check(st == 200 and p.get("status") == "pending", "до подтверждения — pending",
          f"{st} {p}")

    # 6. подтверждение сообщением боту (тот же путь, что long-poll)
    env = dict(os.environ, YM_NO_REEXEC="1", YM_FETCHQ_OFF="1")

    def inject(code_value: str) -> subprocess.CompletedProcess:
        """Сообщение «/start <код>» от чата — тем же путём, что long-poll."""
        update = {"message": {
            "chat": {"id": int(CHAT)}, "text": "/start " + code_value,
            "from": {"id": int(CHAT), "username": USERNAME,
                     "first_name": "Егор", "last_name": "Тестов"}}}
        script = (
            "import json\n"
            "from app.database import SessionLocal\n"
            "from app.services import telegram_bot as tg\n"
            f"upd = json.loads({json.dumps(json.dumps(update))})\n"
            "db = SessionLocal()\n"
            "tg.handle_update(db, upd)\n"
            "db.close()\n"
            "print('injected')\n"
        )
        return subprocess.run([sys.executable, "-c", script], env=env,
                              capture_output=True, text=True, timeout=180)

    proc = inject(code)
    check(proc.returncode == 0 and "injected" in proc.stdout,
          "сообщение боту обработано", proc.stdout + proc.stderr[-400:])

    # 7. вход выполнен, сессия сайта выдана
    st, p, hdrs = call("POST", "/api/v1/auth/telegram/poll", {"code": code})
    cookie = ""
    for raw in hdrs.get_all("Set-Cookie") or []:
        if raw.startswith("ymaster_token="):
            cookie = raw.split(";", 1)[0]
    check(st == 200 and p.get("status") == "ok" and p.get("access_token"),
          "после подтверждения — ok и токен", f"{st} {p}")
    check(p.get("user", {}).get("username") == USERNAME,
          "логин взят из имени в Telegram", str(p.get("user")))
    check(cookie.startswith("ymaster_token="), "выдана HttpOnly-cookie сессии")
    st, me, _ = call("GET", "/api/v1/auth/me", cookie=cookie)
    check(st == 200 and me.get("username") == USERNAME,
          "/auth/me отвечает по cookie", f"{st} {me}")

    # 8. повторно тем же кодом — уже нельзя
    st, p2, _ = call("POST", "/api/v1/auth/telegram/poll", {"code": code})
    check(p2.get("status") == "expired", "код одноразовый", str(p2))

    # 9. повторный вход тем же чатом — та же учётная запись
    st, d2, _ = call("POST", "/api/v1/auth/telegram/start", {})
    code2 = d2.get("code", "")
    inject(code2)
    st, p3, _ = call("POST", "/api/v1/auth/telegram/poll", {"code": code2})
    check(p3.get("status") == "ok" and p3.get("user", {}).get("id")
          == p.get("user", {}).get("id"), "повторный вход — та же запись",
          f"{p3.get('user')} vs {p.get('user')}")

    # 10. в базе только хэш, код погашен, журнал аудита
    db_path = db_url.split("sqlite:///")[-1]
    probe = (
        "import hashlib, json, sqlite3\n"
        f"db = sqlite3.connect({db_path!r})\n"
        "rows = db.execute('select code_hash, used_at from"
        " telegram_login_codes').fetchall()\n"
        "raw_hits = db.execute(\"select count(*) from telegram_login_codes"
        " where code_hash like 'tglogin%'\").fetchone()[0]\n"
        "audit = db.execute(\"select count(*) from audit_log where"
        " action='login_telegram'\").fetchone()[0]\n"
        f"users = db.execute(\"select count(*) from users where"
        f" telegram_chat_id={CHAT}\").fetchone()[0]\n"
        "print(json.dumps({'rows': len(rows),"
        " 'hashes': [r[0] for r in rows],"
        f" 'h1': hashlib.sha256({code!r}.encode()).hexdigest(),"
        f" 'h2': hashlib.sha256({code2!r}.encode()).hexdigest(),"
        " 'raw_plain': raw_hits, 'audit': audit, 'users': users,"
        " 'used': [bool(r[1]) for r in rows]}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, timeout=120)
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        info = {}
    tail = (proc.stdout + proc.stderr)[-500:]
    check(info.get("rows", 0) == 2, "в базе две записи кодов", tail)
    check(info.get("raw_plain") == 0, "открытых кодов в базе нет", tail)
    check(info.get("h1") in info.get("hashes", [])
          and info.get("h2") in info.get("hashes", []),
          "в базе лежат именно хэши выданных кодов", str(info.get("hashes")))
    check(len(info.get("used", [])) == 2 and all(info["used"]),
          "оба кода погашены", tail)
    check(info.get("audit", 0) >= 2, "журнал аудита: login_telegram", tail)
    check(info.get("users") == 1, "один аккаунт на чат", tail)
    # 11. поддельный API получил sendMessage (путь отправки живой)
    try:
        log = json.loads(open("/tmp/fake_tg.log", encoding="utf-8").read())
    except Exception:
        log = {}
    sent = log.get("sendMessage", [])
    texts = " ".join(str(x) for x in sent)
    check(len(sent) >= 2 and "подтверждён" in texts,
          "сообщения бота ушли через адрес API", f"{len(sent)}")

    print(f"\nИТОГ: {len(ok)} зелёных, {len(bad)} красных")
    for t in bad:
        print("  красный:", t)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
