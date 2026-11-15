"""Безопасность канала статусов: источники и срок жизни синтетической сессии (#77)."""
import time

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from tests import test_primary_upd as _upd

firms = _upd.firms


def _token(firms, lifetime=60, **claims):
    original = firms[1]['hdr']['Authorization'].split()[1]
    payload = jwt.decode(original, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    payload['exp'] = int(time.time()) + lifetime
    payload.update(claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def test_foreign_origin_cannot_use_cookie(client, firms):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/status', headers={'origin': 'https://attacker.example', 'cookie': f'ymaster_token={_token(firms)}'}):
            pass


def test_same_origin_cookie_and_anonymous_browser(client, firms):
    for cookie in (f'ymaster_token={_token(firms)}', ''):
        with client.websocket_connect('/ws/status', headers={'origin': 'http://testserver', 'cookie': cookie}) as ws:
            assert ws.receive_json()['type'] == 'connected'


def test_absent_origin_requires_valid_token(client, firms):
    for query in ('', '?token=invalid'):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/status' + query, headers={'cookie': ''}):
                pass
    with client.websocket_connect('/ws/status?token=' + _token(firms), headers={'cookie': ''}) as ws:
        assert ws.receive_json()['type'] == 'connected'


def test_origin_must_match_scheme_and_port(client, firms):
    token = _token(firms)
    for origin in ('https://testserver', 'http://testserver:81', 'null', 'http://testserver.attacker.example', 'http://testserver/path'):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/status?token=' + token, headers={'origin': origin}):
                pass


def test_explicit_trusted_origin(client, firms, monkeypatch):
    monkeypatch.setattr(settings, 'WS_TRUSTED_ORIGINS', ['https://frontend.example'])
    with client.websocket_connect('/ws/status?token=' + _token(firms), headers={'origin': 'https://frontend.example'}) as ws:
        assert ws.receive_json()['type'] == 'connected'


def test_token_without_exp_rejected(client, firms):
    original = _token(firms)
    payload = jwt.decode(original, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    del payload['exp']
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/status?token=' + token, headers={'cookie': ''}):
            pass


def test_expired_session_closes_even_without_events(client, firms):
    token = _token(firms, lifetime=2)
    with client.websocket_connect('/ws/status?token=' + token, headers={'cookie': ''}) as ws:
        assert ws.receive_json()['type'] == 'connected'
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_json()
        assert caught.value.code == 1008


def test_trusted_proxy_scope_and_untrusted_forwarded_headers():
    from starlette.websockets import WebSocket

    from app.services.ws_security import origin_allowed

    async def unused():
        pass

    scope = {'type': 'websocket', 'scheme': 'ws', 'path': '/ws/status', 'headers': [
        (b'host', b'check.example:8443'), (b'origin', b'https://check.example:8443'),
        (b'x-forwarded-proto', b'https'), (b'x-forwarded-host', b'check.example:8443'),
    ]}
    ws = WebSocket(scope, unused, unused)
    assert not origin_allowed(ws, [])  # подделанные заголовки сами по себе не меняют схему
    scope['scheme'] = 'wss'           # доверенный ASGI-прокси преобразовал схему
    assert origin_allowed(ws, [])
    scope['headers'].append((b'origin', b'https://attacker.example'))
    assert not origin_allowed(ws, [])


def test_expired_or_disabled_user_is_not_anonymous(client, firms):
    for token in (_token(firms, lifetime=-10), _token(firms, sub='missing-synthetic-user')):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/status?token=' + token, headers={'origin': 'http://testserver', 'cookie': ''}):
                pass


def test_expiry_stops_live_company_events(client, firms, monkeypatch):
    import asyncio

    from app.services import events

    token = _token(firms, lifetime=2)
    company = firms[1]['id']
    with client.websocket_connect('/ws/status?token=' + token, headers={'cookie': ''}) as ws:
        assert ws.receive_json()['type'] == 'connected'
        monkeypatch.setattr(events, '_loop', ws.portal.call(asyncio.get_running_loop))
        events.broadcast('receipt_created', {'company_id': company, 'id': 'before'})
        assert ws.receive_json()['payload']['id'] == 'before'
        time.sleep(2.1)
        events.broadcast('receipt_created', {'company_id': company, 'id': 'after'})
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_json()
        assert caught.value.code == 1008


def test_candidate_release_documented_and_version_synced():
    from pathlib import Path

    from app.services.blocks import BLOCKS

    root = Path(__file__).resolve().parent.parent
    assert tuple(map(int, settings.APP_VERSION.split('.'))) >= (1, 72, 5)
    assert tuple(map(int, BLOCKS['Роли и доступ'].split('.'))) >= (1, 72, 5)
    assert '## [1.72.5]' in (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert 'WS_TRUSTED_ORIGINS' in (root / 'docs/RUNBOOK.md').read_text(encoding='utf-8')
