# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Центр событий (WebSocket hub): широковещательная рассылка статусов обработки.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("ymaster.events")

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def register_loop() -> None:
    """Запомнить главный event-loop (вызывается при старте приложения)."""
    global _loop
    _loop = asyncio.get_running_loop()


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def broadcast(event_type: str, payload: dict[str, Any]) -> None:
    """Отправить событие всем подключённым клиентам (потокобезопасно)."""
    message = json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False)
    if _loop is None or not _subscribers:
        return
    for q in list(_subscribers):
        try:
            asyncio.run_coroutine_threadsafe(_safe_put(q, message), _loop)
        except RuntimeError:
            pass


async def _safe_put(q: asyncio.Queue, message: str) -> None:
    try:
        q.put_nowait(message)
    except asyncio.QueueFull:
        log.warning("Подписчик WS не успевает обрабатывать события — сообщение пропущено")
