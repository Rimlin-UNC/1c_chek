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
        return "login", 10, 60          # подбор пароля бессмыслен
    if path.startswith(("/api/v1/auth/register", "/api/v1/auth/invite-info")):
        return "register", 20, 3600
    if path.startswith("/onec/"):
        return "onec", 120, 60
    if path.startswith("/api/"):
        return "api", 240, 60
    return None


async def rate_limit_middleware(request: Request, call_next):
    """ASGI-прослойка: ограничение частоты запросов по IP."""
    zone = _zone_for(request.url.path)
    if zone:
        name, limit, window = zone
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
            or (request.client.host if request.client else "?")
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

    def __init__(self):
        self._fails: dict[tuple, collections.deque] = {}
        self._blocked_until: dict[tuple, float] = {}

    def check(self, key: tuple) -> None:
        until = self._blocked_until.get(key)
        if until and until > time.monotonic():
            minutes = max(1, int((until - time.monotonic()) / 60) + 1)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Слишком много неудачных попыток входа. Повторите через {minutes} мин.",
            )

    def record_fail(self, key: tuple) -> None:
        now = time.monotonic()
        dq = self._fails.setdefault(key, collections.deque())
        dq.append(now)
        while dq and dq[0] <= now - self.WINDOW_S:
            dq.popleft()
        if len(dq) >= self.MAX_FAILS:
            self._blocked_until[key] = now + self.BLOCK_S
            dq.clear()

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
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; media-src 'self'; object-src 'none'; frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=(), microphone=()")
    return response
