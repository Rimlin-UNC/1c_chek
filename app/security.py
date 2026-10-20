# -*- coding: utf-8 -*-
"""
Ямастер Чек — защита от злоупотреблений (ООО «Ямастер», ymaster.ru).

Механизмы (все бесплатные, на стороне приложения):
  * Rate limiting — скользящее окно по IP на группы эндпоинтов (429 + Retry-After);
  * Login guard — блокировка перебора паролей: 5 неудач за 15 минут → блок 15 минут;
  * Security headers — CSP, X-Frame-Options, nosniff, Referrer-Policy и др.

Работает в связке с Nginx (limit_req/limit_conn), UFW и fail2ban — см. deploy/.
"""
from __future__ import annotations

import collections
import time

from fastapi import HTTPException, Request, status
from starlette.responses import JSONResponse, Response

# --------------------------------------------------------------------------
#  Скользящее окно (sliding window) — без внешних зависимостей
# --------------------------------------------------------------------------
class SlidingWindowLimiter:
    """Простой и быстрый лимитер: ключ → deque временных меток."""

    def __init__(self, max_keys: int = 20000):
        self._hits: dict[str, collections.deque] = {}
        self._max_keys = max_keys
        self._calls = 0

    def allow(self, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        dq = self._hits.get(key)
        if dq is None:
            if len(self._hits) >= self._max_keys:
                self._hits.clear()  # защита от роста памяти
            dq = collections.deque()
            self._hits[key] = dq
        while dq and dq[0] <= now - window_s:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:
            self._cleanup(now)
        return True

    def _cleanup(self, now: float) -> None:
        dead = [k for k, dq in self._hits.items() if not dq]
        for k in dead:
            self._hits.pop(k, None)


limiter = SlidingWindowLimiter()

# Зоны: (префикс пути, ключ, лимит, окно сек)
def _zone_for(path: str):
    if path.startswith("/api/v1/auth/login"):
        # Подбор пароля бессмыслен: LoginGuard блокирует по (IP+логин) после 5 неудач.
        # Лимит зоны выше, т.к. за одним IP превью/прокси могут быть несколько людей.
        return "login", 25, 60
    if path.startswith(("/api/v1/auth/register", "/api/v1/auth/invite-info")):
        return "register", 40, 3600
    if path.startswith("/onec/"):
        return "onec", 120, 60
    if path.startswith("/api/"):
        return "api", 240, 60
    return None


def _is_trusted_peer(host: str) -> bool:
    """Прямой сосед — доверенный обратный прокси? (loopback / приватные сети)."""
    if not host:
        return False
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return host in ("localhost",)


def real_client_ip(request) -> str:
    """IP клиента для лимитов и аудита (v1.67.1, аудит 12.10.2026).

    X-Forwarded-For принимается ТОЛЬКО если прямой сосед — доверенный
    прокси (nginx на той же машине/приватной сети). При прямом доступе
    присланный злоумышленником XFF игнорируется — иначе ротация заголовка
    обходила и лимит частоты, и блокировку перебора пароля."""
    host = request.client.host if getattr(request, "client", None) else ""
    if _is_trusted_peer(host or ""):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            entries = [x.strip() for x in fwd.split(",") if x.strip()]
            if entries:
                # v1.68.0 (рекомендация аудита): правый край XFF — то, что
                # видел НАШ прокси. Устойчиво к обоим режимам nginx:
                # $remote_addr (один элемент) и $proxy_add_x_forwarded_for
                # (клиентская подделка слева игнорируется — прокси дописал
                # реального соседа правее).
                return entries[-1][:45]
    return (host or "unknown")[:45]


def secret_is_weak(secret: str) -> bool:
    """v1.67.1: дефолтный или слишком короткий ключ подписи токенов."""
    return (not secret
            or secret == "ymaster-dev-secret-change-me-in-production"
            or len(secret) < 32)


async def rate_limit_middleware(request: Request, call_next):
    """ASGI-прослойка: ограничение частоты запросов по IP."""
    zone = _zone_for(request.url.path)
    if zone:
        name, limit, window = zone
        ip = real_client_ip(request)             # v1.67.1: доверенный прокси
        if not limiter.allow(f"{name}:{ip}", limit, window):
            return JSONResponse(
                {"detail": "Слишком много запросов. Попробуйте позже."},
                status_code=429,
                headers={"Retry-After": str(window)},
            )
    return await call_next(request)


# --------------------------------------------------------------------------
#  Защита входа от перебора паролей
# --------------------------------------------------------------------------
class LoginGuard:
    """(IP, логин) → блокировка после 5 неудач за 15 минут на 15 минут."""

    MAX_FAILS = 5
    WINDOW_S = 15 * 60
    BLOCK_S = 15 * 60
    # v1.70.38: память под контролем (как в SlidingWindowLimiter выше).
    # Ключи (IP, логин) приходят из интернета: без чистки словари росли
    # вечно, и перебор с подставными IP/логинами превращался в медленный
    # отказ обслуживания по памяти — защита от перебора сама становилась
    # вектором атаки.
    MAX_KEYS = 5000
    PRUNE_EVERY = 512

    def __init__(self):
        self._fails: dict[tuple, collections.deque] = {}
        self._blocked_until: dict[tuple, float] = {}
        self._calls = 0

    def check(self, key: tuple) -> None:
        until = self._blocked_until.get(key)
        if until and until > time.monotonic():
            minutes = max(1, int((until - time.monotonic()) / 60) + 1)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Слишком много неудачных попыток входа. Повторите через {minutes} мин.",
            )
        if until:
            self._blocked_until.pop(key, None)   # истёк — освобождаем память

    def _prune(self, now: float) -> None:
        """Убирает протухшие ключи: и пустые окна неудач, и истёкшие блокировки."""
        for k in [k for k, dq in self._fails.items()
                  if not dq or dq[-1] <= now - self.WINDOW_S]:
            self._fails.pop(k, None)
        for k in [k for k, until in self._blocked_until.items() if until <= now]:
            self._blocked_until.pop(k, None)

    def record_fail(self, key: tuple) -> None:
        now = time.monotonic()
        self._calls += 1
        if self._calls % self.PRUNE_EVERY == 0:
            self._prune(now)
        dq = self._fails.setdefault(key, collections.deque())
        dq.append(now)
        while dq and dq[0] <= now - self.WINDOW_S:
            dq.popleft()
        if len(dq) >= self.MAX_FAILS:
            self._blocked_until[key] = now + self.BLOCK_S
            dq.clear()
        if len(self._fails) > self.MAX_KEYS:
            self._prune(now)
            if len(self._fails) > self.MAX_KEYS:
                # крайний случай (все ключи «свежие»): память важнее истории —
                # активные блокировки при этом сохраняются
                self._fails.clear()

    def record_ok(self, key: tuple) -> None:
        self._fails.pop(key, None)
        self._blocked_until.pop(key, None)


login_guard = LoginGuard()


# --------------------------------------------------------------------------
#  Security headers
# --------------------------------------------------------------------------
async def security_headers_middleware(request: Request, call_next) -> Response:
    response: Response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' https://st.max.ru; "
        "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; media-src 'self'; object-src 'none'; frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=(), microphone=()")
    # v1.70.38 (чек-лист API-безопасности: HSTS в списке обязательных
    # заголовков) — но ТОЛЬКО когда запрос действительно пришёл по HTTPS:
    # схема или X-Forwarded-Proto от локального nginx (та же логика, что у
    # Secure-флага cookie в app/auth.py::cookie_secure). Отдать HSTS на голом
    # http — значит заставить браузер навсегда ходить на этот хост по https,
    # и локальное превью/ LAN перестанут открываться.
    if (request.url.scheme == "https"
            or (request.headers.get("x-forwarded-proto") or "").lower() == "https"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response
