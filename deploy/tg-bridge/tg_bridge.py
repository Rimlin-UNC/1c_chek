#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — Telegram API-мост для Ubuntu-сервера (v1.70.12).
#
# Зачем: api.telegram.org из многих сетей РФ недоступен, а внешний
# socks5-прокси есть не у всех. Этот крошечный сервис ставится на любую
# машину с доступом к Telegram (дешёвый VPS, домашний сервер) и
# пересылает запросы бота по цепочке:
#
#   сервер «Ямастер Чек» ──HTTP──▶ этот мост ──▶ api.telegram.org
#
# Токен бота остаётся зашифрованным на вашем сервере — мост лишь
# ретранслирует запрос по тому же пути /bot…/method (собственно, как
# любой сервер Telegram). Мост ничего не пишет на диск и не требует
# ключей; работает только как ретранслятор Telegram (белый список
# хостов, открытый прокси из него не сделать).
#
# Запуск:  python3 tg_bridge.py [порт]     (по умолчанию 8077)
# systemd: см. tg-bridge.service рядом.
# Проверка: curl http://127.0.0.1:8077/health   →  {"ok": true}
# ======================================================================
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error

UPSTREAM = "https://api.telegram.org"
ALLOWED_HOSTS = {"api.telegram.org"}     # /proxy/<хост>/… — только эти хосты

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8077


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else None
        url = self.path
        # /proxy/<хост>/<путь> — универсальный режим (хост из белого списка)
        if url.startswith("/proxy/"):
            rest = url[len("/proxy/"):]
            host, _, path = rest.partition("/")
            if host not in ALLOWED_HOSTS:
                self.send_error(403, "host not allowed")
                return
            url = "https://" + host + "/" + path
        if not url.startswith("/bot") and not url.startswith("/file/bot"):
            self.send_error(404, "only bot paths are relayed")
            return
        req = urllib.request.Request(UPSTREAM + url, data=body, method=self.command)
        for h in ("Content-Type", "Authorization"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()
                status = r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data = e.read()
            status = e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except Exception:
            data = json.dumps({"ok": False, "error_code": 502,
                               "description": "bridge: upstream unavailable"}).encode()
            status, ctype = 502, "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._relay()

    def do_POST(self):
        self._relay()

    def log_message(self, fmt, *args):
        sys.stderr.write("tg-bridge: " + (fmt % args) + "\n")


if __name__ == "__main__":
    print("tg-bridge: listening on 0.0.0.0:%d -> %s" % (PORT, UPSTREAM))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
