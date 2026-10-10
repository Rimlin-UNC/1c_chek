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
from .routers import (admin, auth_routes, companies, dashboard, invites, mail_admin, onec,
                      receipts, settings_routes, users, webauthn)
from .services.events import broadcast, register_loop, subscribe, unsubscribe

import datetime as _dt
import os as _os
import time as _time

try:
    _MSK = _dt.ZoneInfo("Europe/Moscow")
except Exception:                       # нет tzdata — фиксированный UTC+3
    _MSK = _dt.timezone(_dt.timedelta(hours=3), "Europe/Moscow")

# v1.58.2: программа работает по Московскому времени — процесс и журнал
if _os.environ.get("TZ") != "Europe/Moscow":
    _os.environ["TZ"] = "Europe/Moscow"
    _time.tzset()

class _MskFormatter(logging.Formatter):
    """Времена журнала — строго по Московскому времени (UTC+3)."""
    def formatTime(self, record, datefmt=None):
        return _dt.datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S") + " +0300"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_msk_fmt = _MskFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
for _h in logging.getLogger().handlers:
    _h.setFormatter(_msk_fmt)
log = logging.getLogger("ymaster")


@asynccontextmanager
async def lifespan(app: FastAPI):
    register_loop()
    init_db()
    # v1.5.0: автоматическая ежедневная копия БД + архив месяца (в фоне)
    from .services.backups import daily_backup
    daily_backup.start()
    from .seed import seed_if_needed
    seed_if_needed()
    # v1.16.0: Telegram-воркер (long-poll + напоминания), если включён
    try:
        from .services import telegram_bot as tg
        if tg.start_worker():
            log.info("telegram worker auto-started")
    except Exception:                                    # noqa: BLE001
        log.exception("telegram worker start failed")

    # v1.44.0: почтовый бот — тик раз в минуту (расписания и условия)
    async def _mail_bot_loop() -> None:
        import asyncio as _aio
        while True:
            await _aio.sleep(60)
            try:
                from .database import SessionLocal
                from .services import mail_center
                db = SessionLocal()
                try:
                    mail_center.tick(db)
                finally:
                    db.close()
            except Exception:                            # noqa: BLE001
                log.exception("mail bot tick failed")

    import asyncio as _asyncio
    _asyncio.get_running_loop().create_task(_mail_bot_loop())
    log.info("%s v%s запущен. Разработчик: %s (%s)",
             settings.APP_NAME, settings.APP_VERSION,
             settings.VENDOR, settings.VENDOR_SITE)
    # v1.46.0: приложение успешно стартовало — фиксируем версию как рабочую
    # (реестр «последних рабочих версий» для отката из приложения/терминала)
    try:
        from .services.releases import mark_healthy
        from .services.updater import _local_commit
        mark_healthy(settings.APP_VERSION, _local_commit(),
                     note="успешный запуск")
    except Exception:                                        # noqa: BLE001
        pass
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


# v1.6.0: сжатие ответов — меньше трафика между браузером и сервером
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=600)


# v1.6.0: HTTP-кэш статики — браузер не перекачивает одни и те же файлы;
# sw.js и оболочка приложения — всегда свежие (no-cache)
@app.middleware("http")
async def _cache_headers(request, call_next):
    response = await call_next(request)
    p = request.url.path
    if p == "/sw.js" or p == "/manifest.webmanifest":
        response.headers.setdefault("Cache-Control", "no-cache")
    elif p.endswith((".js", ".css", ".webmanifest")):
        # v1.9.1: код приложения — всегда свежий у ВСЕХ пользователей сразу
        # после рестарта (revalidate по ETag — дёшево); иначе браузер мог бы
        # держать старый app.js до 7 дней, а бэкенд уже новый.
        # v1.10.0: правило — для ЛЮБОГО префикса: SPA отдаёт код и с корня
        # (/js/app.js, /css/app.css), а не только из /static/ и /assets/.
        response.headers.setdefault("Cache-Control", "no-cache")
    elif p.startswith(("/static/", "/assets/")):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    elif p.startswith("/img/"):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    elif "." not in p.rsplit("/", 1)[-1] and not p.startswith(("api/", "onec/", "ws/", "/api/", "/onec/", "/ws/")):
        # v1.10.0: оболочка SPA и deep-link'и (/receipts, /scan) — тоже всегда
        # свежие, без эвристического кэша браузера. API не трогаем.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


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
    receipt_duplicate, receipt_verifying, receipt_verified, receipt_deleted.
    v1.11.0: подписчик видит события СВОЕЙ компании (аутентификация —
    по ?token= или HttpOnly-cookie); без токена — только платформенные."""
    await ws.accept()
    # --- аутентификация подписчика (v1.11.0) ---
    company_id, authed = None, False
    try:
        from .auth import ROLE_ADMIN, decode_token
        from .database import SessionLocal
        from .models import User
        token = ws.query_params.get("token") or ws.cookies.get("ymaster_token")
        if token:
            payload = decode_token(token)
            db = SessionLocal()
            try:
                u = db.get(User, payload.get("sub", ""))
                if u is not None and u.is_active:
                    authed = True
                    company_id = u.company_id if u.role != ROLE_ADMIN else None
            finally:
                db.close()
    except Exception:
        authed = False                            # анонимный подписчик
    queue = await subscribe(company_id, authed)
    connected = {"ok": True}

    async def _pump_incoming():
        """Следим за разрывом соединения со стороны клиента."""
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            connected["ok"] = False

    pump = asyncio.create_task(_pump_incoming())
    # v1.56.2: закрытие соединения клиентом (вкладка, сон телефона, потеря
    # сети) — норма. Между проверкой флага в _pump_incoming и отправкой
    # ping/события есть гонка: send в уже закрытый сокет кидает
    # WebSocketDisconnect (1006). Раньше каждый такой случай оставлял
    # traceback в журнале; теперь обрабатывается тихо, подписка снимается.
    try:
        await ws.send_json({"type": "connected",
                            "payload": {"app": settings.APP_NAME,
                                        "vendor": settings.VENDOR}})
        # v1.2.0: ping каждые ~20 с — прокси/NAT не рвут «пустое» соединение,
        # телефон перестаёт циклично переподключаться («офлайн/мерцание»).
        idle_ticks = 0
        while connected["ok"]:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=5.0)
                idle_ticks = 0
            except asyncio.TimeoutError:
                idle_ticks += 1
                if idle_ticks % 4 == 0:
                    await ws.send_json({"type": "ping", "payload": {"demo": False}})
                continue
            await ws.send_text(message)
    except (WebSocketDisconnect, RuntimeError):
        pass                        # клиент отключился — это не ошибка сервера
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
app.include_router(webauthn.router)   # v1.43.0: быстрый вход (passkey)
app.include_router(mail_admin.router)  # v1.44.0: почтовый центр
app.include_router(mail_admin.public_router)   # v1.44.0: отписка (публично)
app.include_router(companies.router)   # v1.11.0: компании-клиенты
app.include_router(receipts.router)
app.include_router(dashboard.router)
app.include_router(settings_routes.router)
app.include_router(users.router)
app.include_router(invites.router)
app.include_router(onec.router)
app.include_router(admin.router)
from .routers import manual as manual_routes  # v1.21.0: инструкция
app.include_router(manual_routes.router)
from .pool import router_admin as pool_admin  # v1.30.0: Чек-Пул (Этап 1)
from .pool import router_public               # v1.31.0: приём чеков с сайта
from .pool import router_auth as pool_auth    # v1.32.0: кабинет — регистрация/вход
from .pool import router_api as pool_api      # v1.39.0: платное API (агрегаты)
from .pool import router_company, router_fraud as pool_fraud  # v1.34.0: антифрод — панель админа
from .pool import router_my as pool_my        # v1.32.0: кабинет участника
app.include_router(pool_admin.router)
app.include_router(router_public.router)   # v1.31.0: /api/v1/public/pool/*
app.include_router(pool_auth.router)       # v1.32.0: /api/v1/pool-auth/*
app.include_router(pool_fraud.router)      # v1.34.0: /api/v1/pool-fraud/*
app.include_router(router_company.router)  # v1.37.0: /api/v1/pool-company/*
app.include_router(pool_api.router)        # v1.39.0: /api/v1/pool-api/* (агрегаты)
app.include_router(pool_my.router)         # v1.32.0: /api/v1/pool-my/*


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
