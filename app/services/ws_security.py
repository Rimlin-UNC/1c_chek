"""Проверка источника WebSocket без доверия к клиентским X-Forwarded-* заголовкам."""
from urllib.parse import urlsplit


def _origin(value: str):
    """Канонический (схема, хост, порт) только для корректного HTTP Origin."""
    if not value or any(c.isspace() for c in value) or '\\' in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path or parsed.query or parsed.fragment or '?' in value or '#' in value):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        return parsed.scheme, parsed.hostname.lower(), port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return None


def origin_allowed(ws, trusted_origins):
    """Нет Origin — решение за аутентификацией, иначе same-origin или явный список.

    Внешнюю схему определяет ASGI scope, не сырой заголовок X-Forwarded-Proto.
    Прокси обязан передавать Host с портом и настроить доверие proxy headers в сервере.
    """
    origins = ws.headers.getlist('origin')
    if not origins:
        return None
    if len(origins) != 1:
        return False
    origin = _origin(origins[0])
    if origin is None:
        return False
    hosts = ws.headers.getlist('host')
    scheme = {'ws': 'http', 'wss': 'https'}.get(ws.scope.get('scheme'))
    own = _origin(f'{scheme}://{hosts[0]}') if scheme and len(hosts) == 1 else None
    return origin == own or origin in {parsed for item in trusted_origins if (parsed := _origin(item))}
