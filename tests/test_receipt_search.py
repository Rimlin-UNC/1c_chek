"""Фасеты и поиск по сохранённым чекам с изоляцией компаний."""
from datetime import datetime
from uuid import uuid4

import pytest

from tests.conftest import login
from tests.test_v1140 import _mk_user


@pytest.fixture
def data(client):
    from app.database import SessionLocal
    from app.models import Receipt, ReceiptItem
    hdr = login(client, 'admin', 'admin123')
    c1 = client.post('/api/v1/companies', headers=hdr, json={'name': 'Поиск А ' + uuid4().hex}).json()['id']
    c2 = client.post('/api/v1/companies', headers=hdr, json={'name': 'Поиск Б ' + uuid4().hex}).json()['id']
    user = _mk_user(client, hdr, c1, 'search_' + uuid4().hex[:12], role='accountant')
    db = SessionLocal()
    ids = [str(uuid4()) for _ in range(3)]
    try:
        for index, (cid, city, group, industry) in enumerate([
            (c1, 'Санкт-Петербург', 'Продукты', 'Магазин'),
            (c1, 'Тихвин', '', ''), (c2, 'Секретный город', 'Секрет', 'Секретная отрасль'),
        ]):
            rid = ids[index]
            db.add(Receipt(id=rid, qr_data=rid, fn='fn'+rid[:16], fd='fd'+rid[:16], fp='fp'+rid[:16],
                           company_id=cid, receipt_date=datetime(2026, 10, 1), total_sum=500, operation=1 if index != 1 else 2,
                           region_name='Ленинградская область' if index != 2 else 'Чужой регион',
                           region_city=city, industry=industry, product_group=group,
                           merchant_name='МАГАЗИН ЁЖ'))
            db.flush()
            if index == 0:
                for name in ('Молоко 2,5%', 'Кефир', 'Кефир'):
                    db.add(ReceiptItem(receipt_id=rid, name=name, product_group='Молочные продукты',
                                       industry='Продукты питания'))
        db.commit()
    finally:
        db.close()
    yield hdr, user, c1, c2, ids
    with SessionLocal() as db:
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id.in_(ids)).delete(synchronize_session=False)
        db.query(Receipt).filter(Receipt.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def test_facets_scope_and_distinct_counts(client, data):
    hdr, user, c1, c2, ids = data
    own = client.get('/api/v1/receipts/facets', headers=user).json()
    assert {x['value'] for x in own['cities']} >= {'Тихвин', 'Санкт-Петербург'}
    assert 'Секретный город' not in {x['value'] for x in own['cities']}
    assert 'Секрет' not in {x['value'] for x in own['product_groups']}
    assert next(x['count'] for x in own['product_groups'] if x['value'] == 'Молочные продукты') == 1
    assert next(x['count'] for x in own['industries'] if x['value'] == 'Продукты питания') == 1
    filtered = client.get('/api/v1/receipts/facets', params={'company_id': c2}, headers=hdr).json()
    assert {x['value'] for x in filtered['cities']} == {'Секретный город'}
    assert 'Секретный город' not in {x['value'] for x in client.get(
        '/api/v1/receipts/facets', params={'company_id': c2}, headers=user).json()['cities']}


def test_combined_filters_and_unicode(client, data):
    hdr, user, c1, _, ids = data
    def search(**params):
        response = client.get('/api/v1/receipts', params=params, headers=user)
        assert response.status_code == 200, response.text
        return {r['id'] for r in response.json()['items']}
    assert search(region_name='ЛЕНИНГРАДСКАЯ ОБЛАСТЬ', city='санкт-петербург',
                  industry='ПРОДУКТЫ ПИТАНИЯ', product_group='молочные продукты',
                  item_name='МОЛОКО', operation=1) == {ids[0]}
    assert search(product_group='Продукты') == {ids[0]}
    assert search(item_name='%') == {ids[0]}  # процент — литерал, не SQL-шаблон
    assert search(item_name='несуществующий') == set()
    assert search(merchant='магазин ёж') == set(ids[:2])
    assert search(city='тихвин', operation=2, has_items='false') == {ids[1]}
    assert search(city='секретный город') == set()


def test_end_date_includes_fractional_seconds(client, data):
    from datetime import datetime

    from app.database import SessionLocal
    from app.models import Receipt
    _, user, _, _, ids = data
    with SessionLocal() as db:
        r = db.get(Receipt, ids[0])
        r.receipt_date = datetime(2026, 10, 1, 23, 59, 59, 900000)
        db.commit()
    result = client.get('/api/v1/receipts', params={'date_to': '2026-10-01'}, headers=user)
    assert ids[0] in {r['id'] for r in result.json()['items']}


def test_enrich_populates_city_without_replacing_manual_region(client, data):
    from app.database import SessionLocal
    from app.models import Receipt
    _, user, _, _, ids = data
    with SessionLocal() as db:
        r = db.get(Receipt, ids[1])
        r.region_city = ''
        r.region_code = '99'
        r.region_name = 'Ручной регион'
        r.details_source = 'manual_edit'
        r.merchant_address = 'Ленинградская область, г. Тихвин'
        db.commit()
    result = client.post('/api/v1/receipts/enrich', headers=user,
                         json={'receipt_ids': [ids[1]], 'only_empty': True})
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        r = db.get(Receipt, ids[1])
        assert r.region_city == 'Тихвин'
        assert r.region_name == 'Ручной регион' and r.region_code == '99'
