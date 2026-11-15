"""Безопасная проверка выгрузки клиента: синтетические данные, без восстановления в рабочую БД."""
from __future__ import annotations

import io
import json
import struct
import zipfile

from tests import test_docreq as _dr

client_firm = _dr.client_firm
firms = _dr.firms
PERIOD = _dr.PERIOD
CHECK = '/api/v1/export/package/check'


def _package(client, client_firm):
    response = client.get('/api/v1/export/package', params={'period': PERIOD, 'company_id': client_firm['a']['id']},
                          headers=client_firm['admin'])
    assert response.status_code == 200
    return response.content


def _post(client, headers, data):
    return client.post(CHECK, headers={**headers, 'Content-Type': 'application/zip'}, content=data)


def _rewrite(data, change):
    source = zipfile.ZipFile(io.BytesIO(data))
    files = [(n, source.read(n)) for n in source.namelist()]
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as result:
        for name, content in change(files):
            result.writestr(name, content)
    return output.getvalue()


def test_verification_and_limits_are_honest(client, client_firm):
    archive = _package(client, client_firm)
    res = _post(client, client_firm['admin'], archive)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['ok'] is True and body['counts']['receipts'] == 1
    assert body['skipped_upd'] == 1
    assert body['restore_tested'] is False
    assert 'не является резервной копией' in body['notice']
    assert res.headers['cache-control'] == 'no-store'


def test_corrupted_and_forged_archives_are_rejected(client, client_firm):
    admin = client_firm['admin']
    data = _package(client, client_firm)
    assert _post(client, admin, b'not zip').status_code == 422
    damaged = _rewrite(data, lambda files: [(n, b'[]' if n == 'receipts.json' else b) for n, b in files])
    assert _post(client, admin, damaged).status_code == 422
    duplicate = _rewrite(data, lambda files: files + [('receipts.json', b'[]')])
    assert _post(client, admin, duplicate).status_code == 422
    extra = _rewrite(data, lambda files: files + [('../outside', b'x')])
    assert _post(client, admin, extra).status_code == 422
    def wrong_count(files):
        out = []
        for name, raw in files:
            if name == 'manifest.json':
                m = json.loads(raw)
                m['counts']['receipts'] = 100
                raw = json.dumps(m).encode()
            out.append((name, raw))
        return out
    assert _post(client, admin, _rewrite(data, wrong_count)).status_code == 422


def test_verification_requires_accountant(client, client_firm):
    data = _package(client, client_firm)
    client.cookies.clear()
    assert _post(client, {}, data).status_code == 401
    employee = _dr._mk_user(client, client_firm['admin'], client_firm['a']['id'], 'package-check-user', role='user')
    assert _post(client, employee, data).status_code == 403


def test_package_with_upd_is_accepted(client, firms):
    from tests import test_primary_upd as upd

    admin, company, _ = firms
    response = upd._upload(client, company['hdr'], ('upd.xml', upd._for(company, number='CHECK-25', day='14.10.2026')))
    assert response.status_code == 200
    archive = client.get('/api/v1/export/package', params={'period': PERIOD}, headers=company['hdr'])
    result = _post(client, admin, archive.content)
    assert result.status_code == 200, result.text
    assert result.json()['counts']['upd_files'] == 1


def test_oversize_and_unpacked_limits(client, client_firm, monkeypatch):
    from app.exportpkg import check

    data = _package(client, client_firm)
    monkeypatch.setattr(check, 'MAX_UPLOAD', len(data) - 1)
    assert _post(client, client_firm['admin'], data).status_code == 413
    monkeypatch.setattr(check, 'MAX_UPLOAD', len(data) + 1)
    monkeypatch.setattr(check, 'MAX_UNPACKED', 1)
    assert _post(client, client_firm['admin'], data).status_code == 422


def test_stream_limit_without_content_length_and_content_type(client, client_firm, monkeypatch):
    from app.exportpkg import check

    monkeypatch.setattr(check, 'MAX_UPLOAD', 5)
    headers = {**client_firm['admin'], 'Content-Type': 'application/zip'}
    result = client.post(CHECK, headers=headers, content=iter([b'abc', b'def']))
    assert result.status_code == 413
    result = client.post(CHECK, headers={**headers, 'Content-Type': 'application/json'}, content=b'{}')
    assert result.status_code == 415


def test_invalid_deflate_stream_is_rejected(client, client_firm):
    archive = bytearray(_package(client, client_firm))
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        entry = source.getinfo('manifest.json')
        name_len, extra_len = struct.unpack_from('<HH', archive, entry.header_offset + 26)
        start = entry.header_offset + 30 + name_len + extra_len
        assert entry.compress_type == zipfile.ZIP_DEFLATED and entry.compress_size > 0
        archive[start] = (archive[start] & 0xf8) | 0x07
    assert _post(client, client_firm['admin'], bytes(archive)).status_code == 422


def _forge_json(data, name, change):
    """Изменить JSON и пересчитать манифест: проверяем связи отдельно от хэша."""
    import hashlib

    def edit(files):
        values = dict(files)
        rows = json.loads(values[name])
        change(rows)
        values[name] = json.dumps(rows, ensure_ascii=False).encode()
        manifest = json.loads(values['manifest.json'])
        for entry in manifest['files']:
            if entry['name'] == name:
                entry['bytes'] = len(values[name])
                entry['sha256'] = hashlib.sha256(values[name]).hexdigest()
        manifest['counts'][name.removesuffix('.json')] = len(rows)
        values['manifest.json'] = json.dumps(manifest, ensure_ascii=False).encode()
        return list(values.items())
    return _rewrite(data, edit)


def test_checks_relationships_despite_valid_hashes(client, client_firm):
    data = _package(client, client_firm)
    headers = client_firm['admin']
    report = _post(client, headers, data)
    assert report.status_code == 200
    assert report.json()['relationships_checked'] is True

    # Файлы и счётчики согласованы с манифестом, но содержание повреждено.
    orphan_item = _forge_json(data, 'receipt_items.json', lambda rows: rows.append({'receipt_id': 'missing'}))
    duplicate_receipt = _forge_json(data, 'receipts.json', lambda rows: rows.append(dict(rows[0])))
    missing_receipt_id = _forge_json(data, 'receipts.json', lambda rows: rows[0].pop('id'))
    orphan_line = _forge_json(data, 'primary_lines.json', lambda rows: rows.append({'document_id': 'missing'}))
    duplicate_document = _forge_json(data, 'primary_documents.json', lambda rows: rows.append(dict(rows[0])))
    for archive in (orphan_item, duplicate_receipt, missing_receipt_id, orphan_line, duplicate_document):
        response = _post(client, headers, archive)
        assert response.status_code == 422, response.text


def test_upd_link_and_skip_list_must_match_documents(client, client_firm):
    data = _package(client, client_firm)
    headers = client_firm['admin']
    # Пропуск УПД описывает несуществующий документ; суммы не меняются.
    def alter_skip(files):
        result = []
        for name, raw in files:
            if name == 'manifest.json':
                manifest = json.loads(raw)
                manifest['skipped_upd'][0]['document_id'] = 'missing'
                raw = json.dumps(manifest, ensure_ascii=False).encode()
            result.append((name, raw))
        return result
    assert _post(client, headers, _rewrite(data, alter_skip)).status_code == 422
    # В карточке указан отсутствующий файл при прежнем списке файлов.
    broken_link = _forge_json(data, 'primary_documents.json', lambda rows: rows[0].update(upd_file='upd/missing.xml'))
    assert _post(client, headers, broken_link).status_code == 422


def test_unreferenced_upd_is_not_reported_as_valid(client, firms):
    from tests import test_primary_upd as upd

    admin, company, _ = firms
    assert upd._upload(client, company['hdr'], ('upd.xml', upd._for(company, number='CHECK-LINK', day='14.10.2026'))).status_code == 200
    archive = client.get('/api/v1/export/package', params={'period': PERIOD}, headers=company['hdr']).content
    assert _post(client, admin, archive).status_code == 200
    broken = _forge_json(archive, 'primary_documents.json', lambda rows: rows[0].update(upd_file=''))
    assert _post(client, admin, broken).status_code == 422


def test_invalid_reference_types_fail_cleanly(client, client_firm):
    data = _package(client, client_firm)
    for filename, mutate in (
        ('receipts.json', lambda rows: rows[0].update(id=[])),
        ('receipt_items.json', lambda rows: rows.append({'receipt_id': []})),
        ('primary_lines.json', lambda rows: rows.append({'document_id': {}})),
        ('primary_documents.json', lambda rows: rows[0].update(upd_file=[])),
    ):
        result = _post(client, client_firm['admin'], _forge_json(data, filename, mutate))
        assert result.status_code == 422, result.text
