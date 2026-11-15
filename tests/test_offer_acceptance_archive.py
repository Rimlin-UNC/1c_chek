"""Архив явных подтверждений действующей оферты: только синтетические пользователи."""
from __future__ import annotations

import html
import re

import pytest

from tests import test_primary_upd as _upd

firms = _upd.firms
FORM = '/api/v1/legal/acceptance/form'
POST = '/api/v1/legal/acceptance'
HISTORY = '/api/v1/legal/acceptance/history'


def form(client, headers):
    response = client.get(FORM, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-frame-options'] == 'DENY'
    token = re.search(r'name="ticket" value="([^"]+)"', response.text).group(1)
    shown = html.unescape(re.search(r'<pre id="offer-text"[^>]*>(.*?)</pre>', response.text, re.S).group(1))
    return token, shown


def accept(client, headers, ticket, yes='yes'):
    return client.post(POST, headers=headers, data={'ticket': ticket, 'accept': yes}, follow_redirects=False)


def test_confirmation_archives_exact_shown_text_and_is_idempotent(client, firms):
    _, a, b = firms
    ticket, shown = form(client, a['hdr'])
    assert shown and 'лицензионного договора' in shown
    result = accept(client, a['hdr'], ticket)
    assert result.status_code == 303, result.text
    location = result.headers['location']
    assert accept(client, a['hdr'], ticket).headers['location'] == location
    detail = client.get(location, headers=a['hdr'])
    assert detail.status_code == 200 and html.escape(shown) in detail.text
    assert 'не доказывает прочтение' in detail.text
    assert 'Учётная запись:' in detail.text and 'Канал: веб-форма' in detail.text
    assert client.get(location, headers=b['hdr']).status_code == 404
    assert client.get(HISTORY, headers=a['hdr']).text.count(location) == 1
    assert location not in client.get(HISTORY, headers=b['hdr']).text


def test_requires_auth_explicit_agreement_and_valid_ticket(client, firms):
    _, a, b = firms
    ticket, _ = form(client, a['hdr'])
    assert accept(client, a['hdr'], ticket, '').status_code == 422
    assert accept(client, b['hdr'], ticket).status_code == 403
    assert accept(client, a['hdr'], ticket + 'x').status_code == 403
    client.cookies.clear()
    assert client.get(FORM).status_code == 401
    assert accept(client, {}, ticket).status_code == 401


def test_changed_document_requires_new_form(client, firms, monkeypatch):
    from app.legal import offerta
    _, a, _ = firms
    ticket, _ = form(client, a['hdr'])
    original = offerta.full_text
    monkeypatch.setattr(offerta, 'full_text', lambda db=None: original(db) + '\nНовая строка')
    result = accept(client, a['hdr'], ticket)
    assert result.status_code == 409
    assert 'измен' in result.text.lower()
    new_ticket, shown = form(client, a['hdr'])
    assert 'Новая строка' in shown
    assert accept(client, a['hdr'], new_ticket).status_code == 303


def test_impersonated_session_cannot_accept(client, firms):
    from app.auth import create_impersonation_token, decode_token
    from app.database import SessionLocal
    from app.models import User

    admin, a, _ = firms
    with SessionLocal() as db:
        actor = db.get(User, decode_token(admin['Authorization'][7:])['sub'])
        target = db.get(User, decode_token(a['hdr']['Authorization'][7:])['sub'])
        impersonated = {'Authorization': 'Bearer ' + create_impersonation_token(actor, target)}
    assert client.get(FORM, headers=impersonated).status_code == 403


def test_project_is_not_an_acceptance_document(client, firms):
    _, a, _ = firms
    ticket, shown = form(client, a['hdr'])
    assert 'ПРОЕКТ' not in shown and ticket
    assert 'не является офертой' in client.get('/oferta-proekt').text


@pytest.mark.parametrize('path', [FORM, HISTORY])
def test_responses_not_indexed_or_cached(client, firms, path):
    _, a, _ = firms
    response = client.get(path, headers=a['hdr'])
    assert response.status_code == 200
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-frame-options'] == 'DENY'


def test_ticket_is_not_a_login_token_and_expires(client, firms):
    from datetime import datetime, timedelta, timezone

    import jwt

    from app.legal import acceptance

    _, a, _ = firms
    ticket, _ = form(client, a['hdr'])
    client.cookies.clear()
    assert client.get('/api/v1/auth/me', headers={'Authorization': 'Bearer ' + ticket}).status_code == 401
    payload = jwt.decode(ticket, acceptance._key(), algorithms=['HS256'])
    payload['exp'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(payload, acceptance._key(), algorithm='HS256')
    assert accept(client, a['hdr'], expired).status_code == 403


def test_modified_snapshot_is_not_returned(client, firms):
    from app.database import SessionLocal
    from app.legal.acceptance import OfferAcceptance

    _, a, _ = firms
    ticket, _ = form(client, a['hdr'])
    result = accept(client, a['hdr'], ticket)
    location = result.headers['location']
    record_id = location.rsplit('/', 1)[-1]
    with SessionLocal() as db:
        db.get(OfferAcceptance, record_id).text += 'изменено'
        db.commit()
    response = client.get(location, headers=a['hdr'])
    assert response.status_code == 409
    assert 'сумма' in response.text


def test_form_escapes_document_and_enforces_body_bound(client, firms, monkeypatch):
    from app.legal import offerta

    _, a, _ = firms
    monkeypatch.setattr(offerta, 'full_text', lambda db=None: '<script>alert(1)</script>')
    ticket, shown = form(client, a['hdr'])
    assert shown == '<script>alert(1)</script>'
    response = client.get(FORM, headers=a['hdr'])
    assert '<script>alert(1)</script>' not in response.text
    assert accept(client, a['hdr'], ticket).status_code == 303
    res = client.post(POST, headers={**a['hdr'], 'Content-Type': 'application/x-www-form-urlencoded'},
                      content=iter([b'a' * 3000, b'b' * 3000]))
    assert res.status_code == 413
    assert client.post(POST, headers=a['hdr'], json={'ticket': ticket, 'accept': 'yes'}).status_code == 415


def test_snapshot_not_changed_if_offer_changes_after_acceptance(client, firms, monkeypatch):
    from app.legal import offerta

    _, a, _ = firms
    ticket, shown = form(client, a['hdr'])
    result = accept(client, a['hdr'], ticket)
    monkeypatch.setattr(offerta, 'full_text', lambda db=None: 'Другие условия')
    # Повтор того же успешно сохранённого действия возвращает ту же запись.
    assert accept(client, a['hdr'], ticket).headers['location'] == result.headers['location']
    assert html.escape(shown) in client.get(result.headers['location'], headers=a['hdr']).text


def test_concurrent_repeat_is_one_record_on_both_databases(client, store):
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from sqlalchemy import func, select

    from app.auth import create_access_token
    from app.legal.acceptance import OfferAcceptance
    from app.models import User

    with store.session() as db:
        user = User(username='legal_' + uuid4().hex, password_hash='not-a-login', role='user', is_active=True)
        db.add(user)
        db.commit()
        uid = user.id
        headers = {'Authorization': 'Bearer ' + create_access_token(user)}
    ticket, _ = form(client, headers)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: accept(client, headers, ticket), range(4)))
    assert {r.status_code for r in responses} == {303}
    assert len({r.headers['location'] for r in responses}) == 1
    with store.session() as db:
        assert db.scalar(select(func.count()).select_from(OfferAcceptance).where(OfferAcceptance.user_id == uid)) == 1


def test_full_history_can_be_paged(client, firms):
    from app.auth import decode_token
    from app.database import SessionLocal
    from app.legal.acceptance import OfferAcceptance, _digest

    _, a, _ = firms
    user_id = decode_token(a['hdr']['Authorization'][7:])['sub']
    with SessionLocal() as db:
        db.add_all([OfferAcceptance(id=f'{i:064x}', user_id=user_id, edition='тест', text='текст', sha256=_digest('текст'))
                    for i in range(101)])
        db.commit()
    first = client.get(HISTORY, headers=a['hdr'])
    second = client.get(HISTORY + '?page=2', headers=a['hdr'])
    assert first.status_code == second.status_code == 200
    assert f'{HISTORY}/{0:064x}' not in first.text
    assert f'{HISTORY}/{0:064x}' in second.text
    assert '?page=2' in first.text
    assert '?page=1' in second.text
