# -*- coding: utf-8 -*-
"""Поддельный сервер Telegram Bot API для живой проверки (qa/).

Отвечает как Telegram: /bot<token>/getMe, /sendMessage и любой другой метод
— {"ok": true, "result": {...}}. Все вызовы складывает в /tmp/fake_tg.log,
чтобы проверка могла убедиться, что сообщения действительно уходили.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = "/tmp/fake_tg.log"
_lock = threading.Lock()
_seen: dict[str, list] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        path = self.path.split("?")[0]
        api = path.rsplit("/", 1)[-1]
        with _lock:
            _seen.setdefault(api, [])
            if api == "sendMessage":
                _seen[api].append(raw[:400])
            else:
                _seen[api].append("")
            with open(LOG, "w", encoding="utf-8") as f:
                json.dump(_seen, f, ensure_ascii=False)
        if api == "getMe":
            self._reply({"ok": True, "result": {
                "id": 123456, "is_bot": True, "first_name": "Проверка",
                "username": "e2e_check_bot"}})
        elif api == "getUpdates":
            self._reply({"ok": True, "result": []})   # long-poll не висит
        else:
            self._reply({"ok": True, "result": {"message_id": 1}})

    def do_GET(self) -> None:      # noqa: N802 — имена методов HTTP
        self._handle("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._handle("POST")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    open(LOG, "w", encoding="utf-8").write("{}")
    srv = ThreadingHTTPServer(("127.0.0.1", 8099), Handler)
    print("фальшивый Bot API на 127.0.0.1:8099", flush=True)
    srv.serve_forever()
