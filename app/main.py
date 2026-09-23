# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Главный модуль: FastAPI-приложение, WebSocket-канал статусов, раздача SPA.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .security import rate_limit_middleware, security_headers_middleware
from .database import init_db
from .routers import (auth_routes, dashboard, invites, onec, receipts,
                      settings_routes, users)
from .services.events import broadcast, register_loop, subscribe, unsubscribe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ymaster")


@asynccontextmanager
async def lifespan(app: FastAPI):
    register_loop()
    init_db()
    from .seed import seed_if_needed
    seed_if_needed()
    log.info("%s v%s запущен. Разработчик: %s (%s)",
             settings.APP_NAME, settings.APP_VERSION,
             settings.VENDOR, settings.VENDOR_SITE)
    yield


app = FastAPI(
    title=f"{settings.APP_NAME} — API",
    version=settings.APP_VERSION,
    description=(
        "REST API системы сканирования кассовых чеков и загрузки их в 1С.\n\n"
        "Разработчик и владелец идеи: **ООО «Ямастер»** — [ymaster.ru](https://ymaster.ru), "
        "info@ymaster.ru\n\n"
        "Разделы: Аутентификация · Чеки · Аналитика · Настройки · Пользователи · Интеграция 1С"
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

@app.middleware("http")
async def _rate_limit(request, call_next):
    return await rate_limit_middleware(request, call_next)


@app.middleware("http")
async def _sec_headers(request, call_next):
    return await security_headers_middleware(request, call_next)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
#  WebSocket: живые статусы обработки чеков
# --------------------------------------------------------------------------
@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    """Клиент подключается и получает события: receipt_created,
    receipt_duplicate, receipt_verifying, receipt_verified, receipt_deleted."""
    await ws.accept()
    queue = await subscribe()
    connected = {"ok": True}

    async def _pump_incoming():
        """Следим за разрывом соединения со стороны клиента."""
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            connected["ok"] = False

    pump = asyncio.create_task(_pump_incoming())
    try:
        await ws.send_json({"type": "connected",
                            "payload": {"app": settings.APP_NAME,
                                        "vendor": settings.VENDOR}})
        while connected["ok"]:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            await ws.send_text(message)
    finally:
        pump.cancel()
        unsubscribe(queue)


# --------------------------------------------------------------------------
#  Служебные эндпоинты
# --------------------------------------------------------------------------
@app.api_route("/health", methods=["GET", "HEAD"], tags=["Служебные"],
               summary="Проверка работоспособности")
def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION,
            "vendor": settings.VENDOR, "site": settings.VENDOR_SITE}


@app.get("/api/v1/about", tags=["Служебные"], summary="О системе")
def about():
    from .models import Receipt
    from .database import SessionLocal
    db = SessionLocal()
    try:
        receipts_count = db.query(Receipt).count()
    finally:
        db.close()
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "vendor": settings.VENDOR,
        "site": settings.VENDOR_SITE,
        "email": settings.VENDOR_EMAIL,
        "receipts_total": receipts_count,
        "fns_provider_default": settings.FNS_PROVIDER,
    }


# --------------------------------------------------------------------------
#  API-роутеры
# --------------------------------------------------------------------------
app.include_router(auth_routes.router)
app.include_router(receipts.router)
app.include_router(dashboard.router)
app.include_router(settings_routes.router)
app.include_router(users.router)
app.include_router(invites.router)
app.include_router(onec.router)


# --------------------------------------------------------------------------
#  Раздача веб-клиента (SPA) — все статические файлы из app/static
# --------------------------------------------------------------------------
from .config import BASE_DIR  # noqa: E402
STATIC_DIR = BASE_DIR / "app" / "static"

# Каталог assets должен существовать (git не хранит пустые папки — страховка)
(STATIC_DIR / "assets").mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def spa(full_path: str):
    """SPA-fallback: всё, что не API, отдаёт веб-клиент."""
    if full_path.startswith(("api/", "onec/", "ws/")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # Прямые файлы (manifest, sw, иконки)
    candidate = STATIC_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
