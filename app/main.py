# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Главный модуль: FastAPI-приложение, WebSocket-канал статусов, раздача SPA.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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


# --------------------------------------------------------------------------
#  v1.45.0: SEO — серверные мета-теги, JSON-LD, robots.txt и sitemap.xml.
#  Поисковики видят полный текст главной даже без исполнения JS.
# --------------------------------------------------------------------------
SEO_TITLE = ("Проверить чек онлайн по QR-коду — проверка кассового чека "
             "ФНС | Ямастер Чек")
SEO_DESC = ("Проверить чек онлайн бесплатно: наведите камеру телефона на "
            "QR-код кассового чека — мгновенно узнаете, что чек настоящий, "
            "и получите бонусы в программе Чек-Пул. Проверка чека по QR-коду "
            "без регистрации, 152-ФЗ.")
SEO_KW = ("проверить чек, проверить чек онлайн, проверка чека по qr коду, "
          "проверка кассового чека, чек фнс проверить, пробить чек, "
          "чек-пул бонусы")

SEO_LANDING_HTML = """
<section class="seo-landing" id="seo-landing">
  <h1>Проверить чек онлайн по QR-коду — бесплатно и без регистрации</h1>
  <p>Наведите камеру телефона на QR-код кассового чека — «Ямастер Чек»
  мгновенно проверит чек и покажет результат. Настоящий чек попадает в
  Чек-Пул, а вам начисляются бонусные баллы.</p>
  <h2>Как проверить кассовый чек по QR-коду</h2>
  <ol>
    <li>Нажмите «Проверить чек» — приложение само считает QR-код с чека.</li>
    <li>Мы сверяем фискальные признаки (ФН, ФД, ФП) чека по официальным
    источникам ФНС.</li>
    <li>Сразу видите результат: чек настоящий или нет. За каждый
    настоящий чек — баллы Чек-Пула.</li>
  </ol>
  <h2>Что даёт регистрация в Чек-Пуле</h2>
  <p>Бонусные баллы за чеки, история всех ваших чеков, подарки партнёров
  и кэшбэк, участие в рейтинге месяца. Регистрация бесплатная — по
  e-mail, без банковских карт.</p>
  <h2>Безопасно ли это</h2>
  <p>Мы не просим банковские данные. Хранятся только фискальные
  реквизиты чека, как того требует 152-ФЗ. Проверка чека анонимна —
  аккаунт нужен только для бонусов.</p>
  <h2>Частые вопросы о проверке чеков</h2>
  <p><b>Это бесплатно?</b> Да, проверка чека онлайн полностью бесплатна.<br>
  <b>Чек покупателя — это законно?</b> Да: вы проверяете свой чек и
  добровольно передаёте его фискальные данные в открытую базу Чек-Пула.<br>
  <b>Подойдёт ли бумажный чек?</b> Да, у любого кассового чека есть
  QR-код; электронный чек можно вставить строкой вручную.<br>
  <b>Что если чек не находится?</b> Чек не пробит по кассе или реквизиты
  повреждены: попросите у продавца корректный чек.</p>
  <p>«Ямастер Чек» — сервис ООО «Ямастер» (ymaster.ru): проверка чеков
  для покупателей и сдача чеков в бухгалтерию для компаний.</p>
</section>"""


def _seo_inject(html: str) -> str:
    if "seo-injected" in html:
        return html
    json_ld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebApplication", "name": "Ямастер Чек",
             "applicationCategory": "FinanceApplication",
             "operatingSystem": "Web",
             "url": "https://chek.ymaster.ru/",
             "description": SEO_DESC,
             "offers": {"@type": "Offer", "price": "0",
                        "priceCurrency": "RUB"},
             "publisher": {"@type": "Organization",
                           "name": "ООО «Ямастер»",
                           "url": "https://ymaster.ru"}},
            {"@type": "FAQPage", "mainEntity": [
                {"@type": "Question", "name": "Это бесплатно?",
                 "acceptedAnswer": {"@type": "Answer", "text":
                     "Да, проверка чека онлайн полностью бесплатна."}},
                {"@type": "Question",
                 "name": "Как проверить кассовый чек по QR-коду?",
                 "acceptedAnswer": {"@type": "Answer", "text":
                     "Наведите камеру телефона на QR-код чека — сервис "
                     "сверит фискальные признаки и покажет результат сразу."}},
                {"@type": "Question",
                 "name": "Что даёт регистрация в Чек-Пуле?",
                 "acceptedAnswer": {"@type": "Answer", "text":
                     "Бонусные баллы за чеки, история чеков, подарки "
                     "партнёров и кэшбэк, рейтинг месяца."}},
                {"@type": "Question", "name": "Что если чек не находится?",
                 "acceptedAnswer": {"@type": "Answer", "text":
                     "Чек не пробит по кассе или реквизиты повреждены — "
                     "попросите у продавца корректный чек."}}]},
            {"@type": "HowTo", "name": "Как проверить чек онлайн по QR-коду",
             "step": [
                 {"@type": "HowToStep", "name": "Открыть камеру",
                  "text": "Нажмите «Проверить чек» и разрешите доступ к камере."},
                 {"@type": "HowToStep", "name": "Навести на QR-код",
                  "text": "Наведите камеру на QR-код кассового чека."},
                 {"@type": "HowToStep", "name": "Получить результат",
                  "text": "Сервис проверит чек по фискальным данным и покажет результат."}]},
        ]}, ensure_ascii=False)
    head = ('<meta name="keywords" content="' + SEO_KW + '">\n'
            '<link rel="canonical" href="https://chek.ymaster.ru/">\n'
            '<meta property="og:type" content="website">\n'
            '<meta property="og:site_name" content="Ямастер Чек">\n'
            '<meta property="og:title" content="' + SEO_TITLE + '">\n'
            '<meta property="og:description" content="' + SEO_DESC + '">\n'
            '<meta property="og:url" content="https://chek.ymaster.ru/">\n'
            '<meta property="og:image" '
            'content="https://chek.ymaster.ru/img/manual/scan.jpg">\n'
            '<meta property="og:locale" content="ru_RU">\n'
            '<meta name="twitter:card" content="summary_large_image">\n'
            '<meta name="twitter:title" content="' + SEO_TITLE + '">\n'
            '<meta name="twitter:description" content="' + SEO_DESC + '">\n'
            '<script type="application/ld+json">' + json_ld + '</script>\n')
    html = re.sub(r"<title>.*?</title>",
                  "<title>" + SEO_TITLE + "</title>", html, count=1,
                  flags=re.S)
    html = re.sub(r'<meta name="description" content="[^"]*">',
                  '<meta name="description" content="' + SEO_DESC + '">',
                  html, count=1)
    html = html.replace("</head>", head + "</head>", 1)
    # пререндер: поисковик видит текст даже без JS; SPA убирает блок.
    # v1.45.2: regex вместо точного "<body>" — тег с атрибутами тоже матчится,
    # а если body вдруг не найден, секция встаёт сразу после <head>.
    html, n = re.subn(r"(<body[^>]*>)",
                      r"\1<!-- seo-injected -->" + SEO_LANDING_HTML, html, count=1)
    if n == 0:
        html = html.replace("</head>",
                            "</head><!-- seo-injected -->" + SEO_LANDING_HTML, 1)
    return html


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "User-agent: *\nAllow: /\n"
        "Disallow: /api/\n"
        "Sitemap: https://chek.ymaster.ru/sitemap.xml\n")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    from fastapi.responses import Response
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '<url><loc>https://chek.ymaster.ru/</loc>'
            '<changefreq>daily</changefreq><priority>1.0</priority></url>\n'
            '<url><loc>https://chek.ymaster.ru/#/my</loc>'
            '<changefreq>weekly</changefreq><priority>0.5</priority></url>\n'
            '<url><loc>https://chek.ymaster.ru/#/partners</loc>'
            '<changefreq>weekly</changefreq><priority>0.6</priority></url>\n'
            '</urlset>')
    return Response(content=body, media_type="application/xml")


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def spa(full_path: str):
    """SPA-fallback: всё, что не API, отдаёт веб-клиент (с SEO-блоком)."""
    if full_path.startswith(("api/", "onec/", "ws/")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # Прямые файлы (manifest, sw, иконки)
    candidate = STATIC_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    from fastapi.responses import HTMLResponse
    # v1.45.2: оболочке запрещаем кэш — браузер после релиза всегда берёт
    # свежий index.html (иначе старая оболочка ссылается на старые ?v=)
    return HTMLResponse(
        _seo_inject((STATIC_DIR / "index.html").read_text(encoding="utf-8")),
        headers={"Cache-Control": "no-cache"})
