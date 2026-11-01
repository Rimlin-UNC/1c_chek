"""Отдельный Supabase демо-контур не заменяет рабочую БД и не отдаёт секреты."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.conftest import login


def test_config_requires_accountant_and_is_disabled_by_default(client, monkeypatch):
    client.cookies.clear()
    monkeypatch.setattr(settings, 'SUPABASE_DEMO_ENABLED', False)
    assert client.get('/api/v1/supabase-demo/config').status_code in {401, 403}
    headers = login(client, 'admin', 'admin123')
    data = client.get('/api/v1/supabase-demo/config', headers=headers).json()
    assert data == {'enabled': False, 'mode': 'read_only_demo'}


def test_config_returns_only_publishable_key(client, monkeypatch):
    monkeypatch.setattr(settings, 'SUPABASE_DEMO_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPABASE_URL', 'https://example.supabase.co/')
    monkeypatch.setattr(settings, 'SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_test_key_123456')
    headers = login(client, 'admin', 'admin123')
    data = client.get('/api/v1/supabase-demo/config', headers=headers).json()
    assert data['enabled'] is True
    assert data['url'] == 'https://example.supabase.co'
    assert data['rpc'] == 'chek_demo_snapshot'
    assert data['publishable_key'].startswith('sb_publishable_')
    assert 'service_role' not in str(data)


def test_secret_key_is_not_accepted_as_publishable(client, monkeypatch):
    monkeypatch.setattr(settings, 'SUPABASE_DEMO_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setattr(settings, 'SUPABASE_PUBLISHABLE_KEY', 'sb_secret_should_not_be_returned')
    headers = login(client, 'admin', 'admin123')
    data = client.get('/api/v1/supabase-demo/config', headers=headers).json()
    assert data['enabled'] is False
    assert 'sb_secret' not in str(data)


def test_csp_allows_configured_supabase_origin_only(monkeypatch):
    monkeypatch.setattr(settings, 'SUPABASE_DEMO_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPABASE_URL', 'https://example.supabase.co')
    with TestClient(app) as c:
        csp = c.get('/health').headers['content-security-policy']
    assert 'connect-src' in csp and 'https://example.supabase.co' in csp
    assert "frame-ancestors 'none'" in csp


def test_frame_ancestors_default_is_locked(monkeypatch):
    monkeypatch.setattr(settings, 'FRAME_ANCESTORS', "'none'")
    with TestClient(app) as c:
        r = c.get('/health')
    assert r.headers['x-frame-options'] == 'DENY'
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
