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

# v1.11.0: подписчик = (очередь, компания|None, аутентифицирован?)
#   компания None + аутентифицирован  → администратор: все события;
#   компания «C»                      → события компании C + платформенные;
#   не аутентифицирован               → только платформенные (без чеков).
_subscribers: dict[asyncio.Queue, tuple[str | None, bool]] = {}
_loop: asyncio.AbstractEventLoop | None = None


def register_loop() -> None:
    """Запомнить главный event-loop (вызывается при старте приложения)."""
    global _loop
    _loop = asyncio.get_running_loop()


async def subscribe(company_id: str | None = None,
                    authenticated: bool = False) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers[q] = (company_id, authenticated)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.pop(q, None)


def broadcast(event_type: str, payload: dict[str, Any]) -> None:
    """Отправить событие ПОДПИСЧИКАМ СВОЕГО пространства (потокобезопасно).

    v1.11.0: если в payload есть company_id — событие получают только
    администратор и подписчики этой компании; платформенные события
    (без company_id — server_update и т.п.) получают все.
    """
    message = json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False)
    if _loop is None or not _subscribers:
        return
    cid = payload.get("company_id") if isinstance(payload, dict) else None
    for q, (sub_company, authed) in list(_subscribers.items()):
        if cid is not None:                      # событие конкретной компании
            if not authed:
                continue                         # аноним: чеки не раскрываем
            if sub_company is not None and cid != sub_company:
                continue                         # чужая компания
        elif not authed and event_type not in ("server_update",):
            # события без компании видят аутентифицированные (кроме server_update)
            continue
        try:
            asyncio.run_coroutine_threadsafe(_safe_put(q, message), _loop)
        except RuntimeError:
            pass


async def _safe_put(q: asyncio.Queue, message: str) -> None:
    try:
        q.put_nowait(message)
    except asyncio.QueueFull:
        log.warning("Подписчик WS не успевает обрабатывать события — сообщение пропущено")
