"""Граничные месяцы во всех потребителях общих периодов (#81), только синтетика."""
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.docreq.models import DocRequest, DocRequestEvent, DocRequestSettings
from app.main import app
from tests import test_primary_upd as _upd

firms = _upd.firms


@pytest.fixture
def safe_client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _counts(cid, period):
    with SessionLocal() as db:
        return (db.query(DocRequest).filter_by(company_id=cid, period=period).count(),
                db.query(DocRequestEvent).filter_by(company_id=cid, period=period).count())


def _erase_synthetic(cid, period):
    with SessionLocal() as db:
        db.query(DocRequestEvent).filter_by(company_id=cid, period=period).delete()
        db.query(DocRequest).filter_by(company_id=cid, period=period).delete()
        db.commit()


@pytest.mark.parametrize('period', ['0000-01', '9999-12'])
def test_package_rejects_unrepresentable_bounds(safe_client, firms, period):
    response = safe_client.get('/api/v1/export/package', params={'period': period}, headers=firms[1]['hdr'])
    assert response.status_code == 422, response.text


@pytest.mark.parametrize('period', ['0000-01', '9999-12'])
@pytest.mark.parametrize('method,path', [
    ('get', '/api/v1/doc-requests'), ('post', '/api/v1/doc-requests/detect'),
    ('post', '/api/v1/doc-requests'), ('post', '/api/v1/doc-requests/remind'),
    ('get', '/api/v1/doc-requests/preview'),
])
def test_docreq_rejects_unsafe_month_before_side_effects(safe_client, firms, period, method, path, monkeypatch):
    from app.docreq import service

    sent = []
    monkeypatch.setattr(service, 'SENDERS', {key: lambda *a, **kw: sent.append(True) for key in service.SENDERS})
    a = firms[1]
    assert _counts(a['id'], period) == (0, 0)
    if method == 'get':
        response = safe_client.get(path, params={'period': period}, headers=a['hdr'])
    else:
        response = safe_client.post(path, json={'period': period, 'title': 'Синтетический запрос'}, headers=a['hdr'])
    try:
        assert response.status_code == 422, (path, period, response.status_code, response.text)
        assert _counts(a['id'], period) == (0, 0)
        assert not sent
    finally:
        _erase_synthetic(a['id'], period)


@pytest.mark.parametrize('period', ['0000-01', '9999-12'])
def test_bulk_remind_does_not_write_or_send_for_invalid_month(safe_client, firms, period, monkeypatch):
    from app.docreq import service

    sent = []
    monkeypatch.setattr(service, 'SENDERS', {key: lambda *a, **kw: sent.append(True) for key in service.SENDERS})
    a = firms[1]
    assert _counts(a['id'], period) == (0, 0)
    response = safe_client.post('/api/v1/clients/bulk', headers=a['hdr'], json={
        'action': 'remind', 'company_ids': [a['id']], 'period': period})
    try:
        assert response.status_code == 200, response.text
        assert response.json()['done'] == 0 and response.json()['failed'] == 1
        assert _counts(a['id'], period) == (0, 0)
        assert not sent
    finally:
        _erase_synthetic(a['id'], period)


@pytest.mark.parametrize('period', ['0001-01', '0001-02', '0001-03', '0001-04', '9999-11', '2026-10'])
@pytest.mark.parametrize('path', ['/api/v1/doc-requests', '/api/v1/doc-requests/detect'])
def test_docreq_representable_boundary_months_work(safe_client, firms, period, path):
    a = firms[1]
    if path.endswith('/detect'):
        response = safe_client.post(path, json={'period': period}, headers=a['hdr'])
    else:
        response = safe_client.get(path, params={'period': period}, headers=a['hdr'])
    assert response.status_code == 200, (path, period, response.text)
    data = response.json()
    assert (data.get('overview') or data)['period'] == period


@pytest.mark.parametrize('path', ['/api/v1/doc-requests', '/api/v1/doc-requests/detect'])
def test_schedule_overflow_is_rejected_before_refresh_or_detect(safe_client, firms, path, monkeypatch):
    from app.docreq import service

    a, period = firms[1], '9999-11'
    defaults = {**service.DEFAULTS, 'deadline_day': 28, 'repeat_every': 14, 'repeat_max': 2}
    monkeypatch.setattr(service, 'settings_of', lambda db, cid: DocRequestSettings(company_id=cid, **defaults))
    called = []
    monkeypatch.setattr(service, 'refresh', lambda *a, **kw: called.append('refresh'))
    if path.endswith('/detect'):
        response = safe_client.post(path, json={'period': period}, headers=a['hdr'])
    else:
        response = safe_client.get(path, params={'period': period}, headers=a['hdr'])
    assert response.status_code == 422, response.text
    assert not called


def test_journal_can_read_legacy_unrepresentable_period(safe_client, firms):
    for period in ('0000-01', '9999-12'):
        response = safe_client.get('/api/v1/doc-requests/journal', params={'period': period}, headers=firms[1]['hdr'])
        assert response.status_code == 200, response.text


@pytest.mark.parametrize('period', ['0001-01', '9999-11'])
def test_package_valid_boundary_months(safe_client, firms, period):
    import io
    import json
    import zipfile

    response = safe_client.get('/api/v1/export/package', params={'period': period}, headers=firms[1]['hdr'])
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    assert manifest['period']['month'] == period
    assert manifest['period']['from'] == period + '-01'


def test_legacy_event_remains_readable(safe_client, firms):
    cid, period = firms[1]['id'], '9999-12'
    with SessionLocal() as db:
        event = DocRequestEvent(company_id=cid, period=period, kind='created', text='Синтетический старый журнал')
        db.add(event)
        db.commit()
        event_id = event.id
    try:
        response = safe_client.get('/api/v1/doc-requests/journal', params={'period': period}, headers=firms[1]['hdr'])
        assert response.status_code == 200, response.text
        assert any(e['id'] == event_id for e in response.json()['items'])
    finally:
        with SessionLocal() as db:
            db.query(DocRequestEvent).filter_by(id=event_id).delete()
            db.commit()


def test_due_window_at_the_end_of_datetime_range():
    from datetime import datetime

    from app.docreq import service

    settings = DocRequestSettings(company_id='synthetic', **{**service.DEFAULTS, 'deadline_day': 25,
                                                              'repeat_every': 2, 'repeat_max': 3})
    now = datetime(9999, 12, 31, 23, 0, tzinfo=service.MSK)
    assert service.due_slot('9999-11', settings, now, set()) == 'D+6'


def test_period_consumers_on_sqlite_and_postgresql(store):
    from uuid import uuid4

    from app.docreq import service
    from app.exportpkg import service as package
    from app.models import Company, User

    tag = uuid4().hex
    with store.session() as db:
        company = Company(name='Синтетический клиент ' + tag)
        db.add(company)
        db.flush()
        user = User(username='period_' + tag, password_hash='synthetic', role='accountant', company_id=company.id)
        db.add(user)
        db.commit()
        try:
            for period in ('0001-01', '0001-02', '0001-03', '9999-11'):
                assert service.detect(db, company.id, period) == {'created': 0, 'closed': 0}
                assert service.discipline(db, company.id, period)['received'] == 0
                _, data, manifest = package.build(db, company, period, user)
                assert data.startswith(b'PK') and manifest['period']['month'] == period
            for period in ('0000-01', '9999-12'):
                with pytest.raises(service.DocRequestError):
                    service.detect(db, company.id, period)
                with pytest.raises(package.PackageError):
                    package.build(db, company, period, user)
        finally:
            db.delete(user)
            db.flush()
            db.delete(company)
            db.commit()


@pytest.mark.parametrize('clock', ['0001-01-15', '9999-12-31'])
def test_scheduler_handles_calendar_edges_without_sending(store, clock, monkeypatch):
    from datetime import datetime
    from uuid import uuid4

    from app.docreq import service
    from app.models import Company

    with store.session() as db:
        company = Company(name='Проверка календаря ' + uuid4().hex)
        db.add(company)
        db.flush()
        settings = DocRequestSettings(company_id=company.id, **{**service.DEFAULTS, 'enabled': True,
            'auto_detect': False, 'deadline_day': 28, 'repeat_every': 14, 'repeat_max': 2})
        db.add(settings)
        db.commit()
        calls = []
        monkeypatch.setattr(service, 'send_reminders', lambda *a, **kw: calls.append(True))
        try:
            now = datetime.fromisoformat(clock + 'T23:00:00').replace(tzinfo=service.MSK)
            assert service.tick(db, now, company.id) == []
            assert not calls
        finally:
            db.delete(settings)
            db.delete(company)
            db.commit()


@pytest.mark.parametrize(('clock', 'expected'), [('0001-01-15', ['0001-01']),
                                                 ('9999-12-15', ['9999-11', '9999-10', '9999-09'])])
def test_period_choices_never_offer_unrepresentable_months(clock, expected):
    from datetime import datetime

    from app.clients import service as clients
    from app.docreq import service

    now = datetime.fromisoformat(clock + 'T12:00:00').replace(tzinfo=service.MSK)
    choices = clients.period_choices(now, count=3)
    actual = [item['period'] for item in choices]
    assert set(actual) == set(expected)
    assert len(actual) == len(set(actual))
    for period in actual:
        service.validate_operational_period(period)


def test_near_start_of_calendar_uses_available_history(store):
    from datetime import date
    from uuid import uuid4

    from app.docreq import service
    from app.models import Company
    from app.primary.models import PrimaryDocument

    with store.session() as db:
        company = Company(name='История ' + uuid4().hex)
        db.add(company)
        db.flush()
        for month in (1, 2):
            db.add(PrimaryDocument(company_id=company.id, kind='УПД', doc_number=f'B-{month}',
                                   doc_date=date(1, month, 15), seller_name='Тестовый поставщик',
                                   seller_inn='7701000001', total_with_vat=100.0, status='ok'))
        db.commit()
        cid = company.id
        try:
            result = service.detect(db, cid, '0001-03')
            assert result == {'created': 1, 'closed': 0}
            assert db.query(DocRequest).filter_by(company_id=cid, period='0001-03', kind='expected').count() == 1
        finally:
            db.query(DocRequestEvent).filter_by(company_id=cid).delete()
            db.query(DocRequest).filter_by(company_id=cid).delete()
            db.query(PrimaryDocument).filter_by(company_id=cid).delete()
            db.delete(company)
            db.commit()


def test_period_candidate_release_is_documented():
    from pathlib import Path

    from app.config import settings
    from app.services.blocks import BLOCKS

    root = Path(__file__).resolve().parent.parent
    assert tuple(map(int, settings.APP_VERSION.split('.'))) >= (1, 72, 6)
    assert tuple(map(int, BLOCKS['Что нужно от клиента'].split('.'))) >= (1, 72, 6)
    assert '## [1.72.6]' in (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert 'ADR-59' in (root / 'docs/ARCHITECTURE.md').read_text(encoding='utf-8')
