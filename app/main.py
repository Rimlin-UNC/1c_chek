# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru

Главный модуль: FastAPI-приложение, WebSocket-канал статусов, раздача SPA.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .security import (rate_limit_middleware, secret_is_weak,
                       security_headers_middleware)
from .database import init_db
from .approvals import router as approvals_router
from .bank import router as bank_router
from .clients import router as clients_router
from .clients import service as clients_service
from .counterparties.router import router as counterparties_router  # v1.71.0: контрагенты (T-80)
from .exportpkg import router as exportpkg_router
from .goods import router as goods_router
from .docreq import router as docreq_router
from .primary import mail_router as primary_mail_router
from .primary import router as primary_router
from .routers import (admin, auth_routes, companies, dashboard, invites, mail_admin, maxapp, navigation, onec,
                      receipts, settings_routes, supabase_demo, users, webauthn)
from .services.events import broadcast, register_loop, resubscribe, subscribe, unsubscribe
from .unified import router as unified_router

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
    # v1.66.0: ассистент MAX — отдельный long-poll поток (если включён)
    try:
        from .services import max_bot as mx
        if mx.start_worker():
            log.info("max worker auto-started")
    except Exception:                                    # noqa: BLE001
        log.exception("max worker start failed")
    # v1.67.0: исходящие webhooks — воркер доставки (если включён и URL задан)
    try:
        from .services import webhooks
        if webhooks.start_worker():
            log.info("webhooks worker auto-started")
    except Exception:                                    # noqa: BLE001
        log.exception("webhooks worker start failed")

    # v1.68.0: демоны очереди автозагрузки деталей чеков (2 воркера)
    try:
        from .routers.receipts import (start_fetch_workers,
                                       start_limit_retry_worker)
        _n = start_fetch_workers()
        if _n:
            log.info("fetch queue workers started: %d", _n)
        # v1.70.70: чеки, ожидавшие сброса суточного лимита ФНС,
        # проверяются автоматически после его сброса
        if start_limit_retry_worker():
            log.info("fns limit retry worker started")
    except Exception:                                    # noqa: BLE001
        log.exception("fetch queue workers start failed")

    # v1.70.51: мониторинг OpenStack — поток опроса, если включён в настройках
    try:
        from .openstack_monitor import worker as os_worker
        if os_worker.start_worker():
            log.info("openstack monitor worker auto-started")
    except Exception:                                    # noqa: BLE001
        log.exception("openstack monitor worker start failed")

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

    # v1.70.97 (T-58): напоминания «что нужно от клиента» — тик раз в минуту, отдельно от ботов:
    # приходят по любому подключённому каналу (Telegram, MAX, почта) у компаний, где их включил бухгалтер
    _bank_purged = {"day": ""}

    def _doc_requests_once() -> None:
        from .bank import service as bank
        from .database import SessionLocal
        from .docreq import service as docreq
        db = SessionLocal()
        try:
            docreq.tick(db)
            # v1.70.98 (T-45): срок хранения банковских операций — раз в сутки, даже если выписки больше не грузят
            today = docreq.now_msk().date().isoformat()
            if _bank_purged["day"] != today:
                _bank_purged["day"] = today
                bank.purge(db)
        finally:
            db.close()
        # v1.70.99 (ADR-29): выписки из банка по API — подключённым компаниям раз в 6 часов (не больше трёх
        # за тик); отдельная сессия и свой перехват: сбой банка не мешает напоминаниям, и наоборот
        from .bank import connect as bank_api
        db = SessionLocal()
        try:
            bank_api.sync_due(db)
        except Exception:                                # noqa: BLE001
            log.exception("bank api tick failed")
        finally:
            db.close()
        # v1.70.104 (T-63, ADR-34): первичка из почтовых ящиков компаний — раз в 15 минут (не больше трёх ящиков
        # за тик), распознавание очереди PDF и сканов, раз в сутки — чистка; свой перехват, как у банка
        from .primary import mailbox as primary_mailbox
        db = SessionLocal()
        try:
            primary_mailbox.check_due(db)
        except Exception:                                # noqa: BLE001
            log.exception("primary mailbox tick failed")
        finally:
            db.close()
        # v1.70.112 (T-46, ADR-24): согласование авансовых отчётов — этап без ответа за срок компании (72 ч)
        # считается согласованным, затем уведомления в Telegram и MAX; свой перехват, как у банка и почты
        from .approvals import service as approvals
        db = SessionLocal()
        try:
            approvals.tick(db)
        except Exception:                                # noqa: BLE001
            log.exception("approvals tick failed")
        finally:
            db.close()

    async def _doc_requests_loop() -> None:
        import asyncio as _aio
        while True:
            await _aio.sleep(60)
            try:      # в отдельном потоке: отправка по сети не должна держать сервер
                await _aio.to_thread(_doc_requests_once)
            except Exception:                            # noqa: BLE001
                log.exception("doc requests tick failed")

    import asyncio as _asyncio
    _asyncio.get_running_loop().create_task(_mail_bot_loop())
    if _os.environ.get("YM_DOCREQ_OFF") != "1":           # тесты вызывают тик сами, с подменёнными каналами
        _asyncio.get_running_loop().create_task(_doc_requests_loop())
    if secret_is_weak(settings.SECRET_KEY):              # v1.67.1: аудит
        log.warning("SECRET_KEY не задан, дефолтный или короче 32 символов — "
                    "задайте длинный случайный SECRET_KEY в .env (иначе "
                    "токены подделываемы). Прод: install.sh/deploy.sh "
                    "генерируют его сами.")
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
        "chek@ymaster.ru\n\n"
        "Разделы: Аутентификация · Чеки · Аналитика · Настройки · Пользователи · Интеграция 1С"
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)


# --------------------------------------------------------------------------
#  v1.70.38: честная схема OpenAPI
#
#  Фаззинг по схеме (schemathesis, 36 723 тест-кейса) показал 110 расхождений
#  между документацией и тем, что приложение отдаёт на самом деле. Причины три,
#  все лечатся в одном месте и НЕ меняют поведение приложения — только описание:
#   • на 422 FastAPI документирует {"detail": [{loc, msg, type}, …]}, а наш
#     обработчик (_safe_validation_handler ниже) отдаёт {"detail": "текст"} —
#     строку человеческим языком, её показывает интерфейс. Сгенерированный
#     клиент спотыкался бы об это на каждой ошибке валидации;
#   • ответы 401/403/429 не были объявлены вовсе, хотя приложение их
#     возвращает: авторизация, права роли и лимит частоты по IP;
#   • CSV-выгрузки отдают text/csv, а схема обещала application/json.
#  Проверка «документация = факт» зафиксирована тестами TestOpenApiContract в
#  tests/test_v17038.py.
# --------------------------------------------------------------------------
_API_ERROR_SCHEMA = {
    "type": "object",
    "title": "ApiError",
    "properties": {
        "detail": {
            "type": "string",
            "description": "Причина отказа человеческим языком",
        },
    },
    "required": ["detail"],
}


def _error_response(description: str) -> dict:
    """Описание ответа-ошибки: у всех отказов приложения одна форма тела."""
    return {
        "description": description,
        "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/ApiError"}}},
    }


_default_openapi = app.openapi


def custom_openapi() -> dict:
    """Штатная схема, приведённая к фактическим ответам приложения."""
    schema = _default_openapi()          # собирает и кэширует штатную схему
    schema.setdefault("components", {}).setdefault(
        "schemas", {})["ApiError"] = _API_ERROR_SCHEMA
    for path, item in (schema.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if not isinstance(op, dict) or not isinstance(op.get("responses"), dict):
                continue
            responses = op["responses"]
            if "422" in responses:
                responses["422"] = _error_response(
                    "Запрос не прошёл проверку: поле пустое, значение вне "
                    "диапазона или не того типа")
            if op.get("security"):
                responses.setdefault("401", _error_response(
                    "Нет токена, токен истёк или подделан"))
                responses.setdefault("403", _error_response(
                    "Прав недостаточно: роль не та или объект чужой"))
            if path.startswith(("/api/", "/onec/")):
                responses.setdefault("429", _error_response(
                    "Слишком много запросов: повторите позже "
                    "(интервал — в заголовке Retry-After)"))
            if path.endswith(("export.csv", "export-csv")) and "200" in responses:
                ok = responses["200"]
                if isinstance(ok, dict):
                    ok["description"] = ("CSV-файл: разделитель «;», "
                                         "кодировка UTF-8")
                    ok["content"] = {"text/csv": {
                        "schema": {"type": "string", "format": "binary"}}}
    return schema


app.openapi = custom_openapi


@app.exception_handler(RequestValidationError)
async def _safe_validation_handler(request: Request, exc: RequestValidationError):
    """422 без падений (v1.67.1, аудит): бинарные тела (multipart в JSON
    эндпоинт) роняли стандартный кодировщик ошибок → 500 + трейсбэк."""
    items = []
    for e in (exc.errors() or [])[:5]:
        loc = "/".join(str(x) for x in (e.get("loc") or []) if x != "body")
        items.append(f"{loc}: {str(e.get('msg', ''))[:160]}".strip(": "))
    return JSONResponse(status_code=422,
                        content={"detail": "; ".join(items) or "Некорректный запрос"})

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
    allow_origins=settings.CORS_ORIGINS,   # v1.70.11: без wildcard-дефолта
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
    from .services.ws_security import origin_allowed
    import math
    import time

    origin_ok = origin_allowed(ws, settings.WS_TRUSTED_ORIGINS)
    if origin_ok is False:
        await ws.close(code=1008)
        return
    # Проверка до принятия: без Origin разрешены только проверенные токены.
    company_id, authed, ws_error, ws_user_id = None, False, "", ""
    expires = None
    token = ws.query_params.get("token") or ws.cookies.get("ymaster_token")
    try:
        from .auth import ROLE_ADMIN, decode_token
        from .database import SessionLocal
        from .models import User
        if token:
            payload = decode_token(token)
            exp = payload.get("exp")
            if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
                raise ValueError("Токен без корректного срока")
            expires = float(exp)
            db = SessionLocal()
            try:
                u = db.get(User, payload.get("sub", ""))
                if u is not None and u.is_active:
                    authed, ws_user_id = True, u.id
                    company_id = (u.company_id or "") if u.role != ROLE_ADMIN else None
                    # v1.72.1 (T-91): активный клиент бухгалтера (?company=…) — те же права, что у HTTP (ADR-30);
                    # отказ не рвёт соединение: подписчик остаётся на своей компании, а причину получает сообщением
                    requested = (ws.query_params.get("company") or "").strip()
                    if requested:
                        from .clients import service as clients
                        try:
                            company_id = clients.ws_company(db, u, requested) or company_id
                        except clients.ClientsError as exc:
                            ws_error = str(exc)
            finally:
                db.close()
    except Exception:
        authed = False                            # анонимный подписчик
    if (token and not authed) or (origin_ok is None and not authed) or (expires is not None and expires <= time.time()):
        await ws.close(code=1008)
        return
    await ws.accept()
    queue = await subscribe(company_id, authed)
    connected = {"ok": True}

    async def _pump_incoming():
        """Следим за разрывом соединения; v1.72.1 — переключение клиента без переподключения сокета.

        Сообщение `{"type": "client", "company": "…"}` переводит подписку на активного клиента: интерфейс
        переключает компанию чаще, чем нужно рвать соединение. Право проверяется тем же ws_company.
        """
        nonlocal company_id
        try:
            while True:
                raw = await ws.receive_text()
                if expires is not None and time.time() >= expires:
                    return
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue                                   # ping и прочие строки
                if not isinstance(msg, dict) or msg.get("type") != "client":
                    continue
                with SessionLocal() as db:
                    target, error = company_id, ""
                    u = db.get(User, ws_user_id) if ws_user_id else None
                    if u is None or not u.is_active:
                        error = "Клиент сменить нельзя — войдите заново"
                    else:
                        try:
                            target = clients_service.ws_company(db, u, msg.get("company"))
                            if target is None and u.role != ROLE_ADMIN:
                                target = u.company_id or ""
                        except clients_service.ClientsError as exc:
                            error = str(exc)
                if expires is not None and time.time() >= expires:
                    return
                if error:
                    await ws.send_json({"type": "client_error", "payload": {"detail": error}})
                    continue
                resubscribe(queue, target)
                company_id = target
                await ws.send_json({"type": "client_ok", "payload": {"company": target or ""}})
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
                                        "vendor": settings.VENDOR,
                                        **({"warning": ws_error} if ws_error else {})}})
        # v1.2.0: ping каждые ~20 с — прокси/NAT не рвут «пустое» соединение,
        # телефон перестаёт циклично переподключаться («офлайн/мерцание»).
        idle_ticks = 0
        while connected["ok"]:
            remaining = expires - time.time() if expires is not None else None
            if remaining is not None and remaining <= 0:
                await ws.close(code=1008)
                break
            try:
                message = await asyncio.wait_for(queue.get(), timeout=min(5.0, remaining) if remaining is not None else 5.0)
                idle_ticks = 0
            except asyncio.TimeoutError:
                if expires is not None and time.time() >= expires:
                    await ws.close(code=1008)
                    break
                idle_ticks += 1
                if idle_ticks % 4 == 0:
                    await ws.send_json({"type": "ping", "payload": {"demo": False}})
                continue
            if expires is not None and time.time() >= expires:
                await ws.close(code=1008)
                break
            # Очередь может содержать событие предыдущего клиента, а доступ к текущему
            # могли отозвать уже после подключения. Проверяем право перед отправкой.
            event_company = json.loads(message).get("payload", {}).get("company_id")
            if event_company is not None and ws_user_id:
                if company_id is not None and event_company != company_id:
                    continue
                with SessionLocal() as db:
                    u = db.get(User, ws_user_id)
                    error = ""
                    if u is None or not u.is_active:
                        error = "Клиент сменить нельзя — войдите заново"
                        fallback = ""
                    else:
                        fallback = (u.company_id or "") if u.role != ROLE_ADMIN else None
                        try:
                            clients_service.ws_company(db, u, event_company)
                        except clients_service.ClientsError as exc:
                            error = str(exc)
                    if error:
                        company_id = fallback
                        resubscribe(queue, fallback)
                if expires is not None and time.time() >= expires:
                    await ws.close(code=1008)
                    break
                if error:
                    await ws.send_json({"type": "client_error", "payload": {"detail": error}})
                    continue
            if expires is not None and time.time() >= expires:
                await ws.close(code=1008)
                break
            await ws.send_text(message)
    except (WebSocketDisconnect, RuntimeError):
        pass                        # клиент отключился — это не ошибка сервера
    finally:
        pump.cancel()
        unsubscribe(queue)


# --------------------------------------------------------------------------
#  Служебные эндпоинты
# --------------------------------------------------------------------------
# v1.70.38: было @app.api_route(..., methods=["GET", "HEAD"]) — FastAPI
# выдавал ОБА operation с одним operationId (health_health_get), схема
# OpenAPI получалась с дублем: генераторы клиентов и фаззеры на такой схеме
# спотыкаются. HEAD для GET-маршрута Starlette добавляет сам, поэтому
# достаточно @app.get — и проверка nginx/systemd (`curl -I /health`) работает.
@app.get("/health", tags=["Служебные"], summary="Проверка работоспособности")
@app.head("/health", include_in_schema=False)   # v1.70.103: HEAD явно — раньше отвечал SPA-фолбэк (теперь 404)
def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION,
            "vendor": settings.VENDOR, "site": settings.VENDOR_SITE}


@app.get("/api/v1/about", tags=["Служебные"], summary="О системе")
def about():
    from .models import Receipt
    from .database import SessionLocal
    from .services import max_bot as _mx
    from .services import splash as _splash
    db = SessionLocal()
    try:
        receipts_count = db.query(Receipt).count()
        mx_username = _mx.bot_username(db, refresh=False)  # v1.70.1
        splash_seconds = _splash.seconds(db)               # v1.70.100: длительность заставки (до входа)
        splash_guests = _splash.guests(db)                 # v1.70.103: заставка и для гостей (по умолчанию нет)
    finally:
        db.close()
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "vendor": settings.VENDOR,
        "site": settings.VENDOR_SITE,
        "email": settings.VENDOR_EMAIL,
        "max_bot_username": mx_username,
        "receipts_total": receipts_count,
        "fns_provider": "fns",
        "splash_seconds": splash_seconds,
        "splash_guests": splash_guests,
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
app.include_router(navigation.router)
app.include_router(supabase_demo.router)  # отдельный read-only Supabase демо-контур
app.include_router(unified_router.router)  # единый фискальный документ
# v1.70.104 (T-63): первичка из почты — раньше основного роутера первички, иначе /inbox поймал бы /{doc_id}
app.include_router(primary_mail_router.router)
app.include_router(primary_router.router)  # v1.70.94: первичка — электронные УПД
app.include_router(docreq_router.router)  # v1.70.97: что нужно от клиента (T-58)
app.include_router(bank_router.router)  # v1.70.98: банковская выписка (T-45)
app.include_router(clients_router.router)  # v1.70.100: рабочее место аутсорсера (T-50)
app.include_router(exportpkg_router.router)  # v1.70.100: пакет для других учётных систем (T-68)
app.include_router(goods_router.router)  # v1.70.101: понимание чеков (T-69)
app.include_router(approvals_router.router)  # v1.70.112: согласование авансовых отчётов (T-46)
app.include_router(counterparties_router)  # v1.71.0: контрагенты (T-80)
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
app.include_router(maxapp.router)          # v1.69.0: /api/v1/maxapp/* (мини-приложение MAX)

# v1.70.45: контур B2B — доступ компаний и ИП к платному API. Третий контур
# (ядро, Чек-Пул, B2B): свой JWT typ=b2b, свои таблицы b2b_*, свои страницы.
from .b2b import router_admin as b2b_admin  # noqa: E402
from .b2b import router_public as b2b_public  # noqa: E402

app.include_router(b2b_public.router)      # /api/v1/b2b/* (кабинет компании)
app.include_router(b2b_admin.router)       # /api/v1/b2b-admin/* (модерация)

# v1.70.51: мониторинг OpenStack (docs/Monitor.md). Внутренний контур для
# администратора: настройки подключения, показатели, история и журнал.
# Префикс общий для всей программы — /api/v1/openstack/*, поэтому в список
# исключений SPA-перехвата ниже он уже входит (api/).
from .openstack_monitor import router as openstack_router  # noqa: E402

app.include_router(openstack_router.router)  # /api/v1/openstack/* (админ)

# v1.70.47: публичная оферта. Страница /oferta должна отдаваться ДО
# SPA-перехвата ниже, иначе запрос уйдёт в веб-клиент и пользователь,
# поставивший галочку «согласен с условиями оферты», не увидел бы условий.
# Без авторизации: документ читается до принятия его условий.
from .legal import acceptance as legal_acceptance  # noqa: E402
from .legal import page as legal_page  # noqa: E402

app.include_router(legal_page.router)      # /oferta, /oferta.txt, /api/v1/legal/*
app.include_router(legal_acceptance.router)  # добровольное подтверждение действующего текста

# v1.70.106 (T-75, ADR-36): страницы под кластеры запросов — серверный HTML без скриптов, тоже до SPA-перехвата
from .services import seo_pages as _seo_pages  # noqa: E402

app.include_router(_seo_pages.router)      # /kak-proverit-chek, /cheki-sotrudnikov-v-1c, /dlya-buhgalterov

# T-82 (issue #72, ADR-47): /blog и десять статей-спутников — серверный HTML, тоже до SPA-перехвата
from .services import blog_pages as _blog_pages  # noqa: E402

app.include_router(_blog_pages.router)      # /blog и /blog/<slug>


# --------------------------------------------------------------------------
#  Раздача веб-клиента (SPA) — все статические файлы из app/static
# --------------------------------------------------------------------------
from .config import BASE_DIR  # noqa: E402
from .services import seo as _seo  # noqa: E402
STATIC_DIR = BASE_DIR / "app" / "static"


# v1.70.103 (T-74, ADR-33): SEO стартовой страницы. Главная отдаётся с подстановкой вместо метки seo:head:
# canonical при разрешённой индексации, иначе noindex, и коды подтверждения поисковиков. Индексация решается по
# адресу (Host), а не по роботу: люди и роботы получают одну и ту же страницу (не клоакинг).
def _scheme(request: Request) -> str:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()


def _index_response(request: Request, status_code: int = 200) -> HTMLResponse:
    from .database import SessionLocal
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    host = request.headers.get("host")
    cfg = _seo.load_safely(SessionLocal)          # база недоступна — значения по умолчанию, главная открывается
    enabled = _seo.indexing_enabled(cfg, host)
    body = _seo.render_index(template, cfg, host, _scheme(request))
    headers = {} if enabled and status_code == 200 else {"X-Robots-Tag": "noindex, nofollow"}
    return HTMLResponse(body, status_code=status_code, headers=headers)


@app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
def robots_txt(request: Request):
    """Правила для роботов: на рабочем адресе — открыто всё, кроме служебных разделов; иначе — всё закрыто."""
    from .database import SessionLocal
    text = _seo.robots_txt(_seo.load_safely(SessionLocal), request.headers.get("host"), _scheme(request))
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@app.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
def sitemap_xml(request: Request):
    """Карта сайта — только при разрешённой индексации (иначе 404); дата — последнего обновления главной."""
    from datetime import date

    from .database import SessionLocal
    lastmod = date.fromtimestamp((STATIC_DIR / "index.html").stat().st_mtime).isoformat()
    xml = _seo.sitemap_xml(_seo.load_safely(SessionLocal), request.headers.get("host"), _scheme(request), lastmod)
    if xml is None:
        return JSONResponse({"detail": "Not found"}, status_code=404, headers={"X-Robots-Tag": "noindex"})
    return Response(xml, media_type="application/xml; charset=utf-8")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
def favicon():
    """Значок по адресу по умолчанию (его запрашивают браузеры и роботы) — PNG 64×64."""
    return FileResponse(STATIC_DIR / "img" / "icon-64.png", media_type="image/png")

# Каталог assets должен существовать (git не хранит пустые папки — страховка)
(STATIC_DIR / "assets").mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/maxapp", include_in_schema=False)
@app.get("/maxapp/", include_in_schema=False)
@app.head("/maxapp/", include_in_schema=False)   # v1.70.103: HEAD явно (SPA-фолбэк теперь 404)
@app.get("/maxapp/{fname}", include_in_schema=False)
async def maxapp_page(fname: str = ""):
    """Мини-приложение MAX (v1.69.0): статика без авторизации.

    MAX грузит страницу в вебвью; авторизация — подпись initData,
    API-токен выдаёт POST /api/v1/maxapp/session."""
    if "/" in fname or ".." in fname or fname.startswith("."):
        raise HTTPException(status_code=404)
    static = BASE_DIR / "app" / "static" / "maxapp"
    if fname:
        f = static / fname
        return FileResponse(f) if f.is_file() \
            else JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(static / "index.html")


@app.get("/b2b", include_in_schema=False)
@app.get("/b2b/", include_in_schema=False)
@app.head("/b2b/", include_in_schema=False)      # v1.70.103: HEAD явно (SPA-фолбэк теперь 404)
@app.get("/b2b/{fname}", include_in_schema=False)
async def b2b_page(fname: str = ""):
    """Кабинет компании для доступа к платному API (v1.70.45).

    Страница отдельная (как /maxapp/) и НЕ связана ссылками с публичным
    лендингом: раздел «API для бизнеса» виден только компаниям,
    подтвердившим статус. Содержимое раздела отдаёт /api/v1/b2b/content
    после проверки статуса — сама статика данных не содержит.
    """
    if "/" in fname or ".." in fname or fname.startswith("."):
        raise HTTPException(status_code=404)
    static = BASE_DIR / "app" / "static" / "b2b"
    if fname:
        f = static / fname
        return FileResponse(f) if f.is_file() \
            else JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(static / "index.html")


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def spa(full_path: str, request: Request = None):
    """SPA-fallback: всё, что не API, отдаёт веб-клиент.

    v1.70.103: главная — «/» и «/index.html» (200); прочие несуществующие адреса — 404 с той же оболочкой
    и noindex: раньше любой адрес отвечал 200 копией главной («мягкие 404», дубли в поиске). Программа
    ходит по разделам через «#/…», поэтому живых адресов без файла у неё нет.
    """
    if full_path.startswith(("api/", "onec/", "ws/", "maxapp/", "b2b/")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if ".." in full_path:
        # v1.70.11: страховка от траверсала сверх нормализации сервера
        # (живая проверка v1.70.11: и так не эксплуатируется, но замок важнее)
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # главная и «/index.html» — всегда с подстановкой seo:head (canonical или noindex), а не сырым файлом
    if full_path in ("", "index.html") and request is not None:
        return _index_response(request, 200)
    # Прямые файлы (manifest, sw, иконки, шрифты)
    candidate = STATIC_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    status_code = 200 if full_path in ("", "index.html") else 404
    if request is None:                                  # прямой вызов функции (тесты) — без подстановки
        return FileResponse(STATIC_DIR / "index.html", status_code=status_code)
    return _index_response(request, status_code)
