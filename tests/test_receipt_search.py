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


def test_vat_marking_and_missing_filters(client, data):
    import json

    from app.database import SessionLocal, _backfill_marking
    from app.models import Receipt, ReceiptItem

    hdr, user, c1, c2, ids = data
    with SessionLocal() as db:
        items = db.query(ReceiptItem).filter_by(receipt_id=ids[0]).order_by(ReceiptItem.id).all()
        items[0].vat_rate = '1'  # код источника, не приписываем ему ставку
        items[0].extra_json = json.dumps({'marking': [{'gtin': '012345'}]})
        items[1].vat_rate = '1'
        items[2].extra_json = '{invalid'
        db.add(ReceiptItem(receipt_id=ids[1], name='другая', vat_rate='20', extra_json='{}'))
        db.add(ReceiptItem(receipt_id=ids[2], name='чужая', vat_rate='secret',
                           extra_json=json.dumps({'marking': [{'raw': 'code'}]})))
        receipt = db.get(Receipt, ids[0])
        receipt.merchant_inn = '  \t '
        receipt.merchant_address = 'адрес'
        receipt.total_sum = 0
        db.commit()
        assert items[0].marking_present is True
        assert items[2].marking_present is False
        # Имитируем данные до миграции и проверяем пакетное заполнение.
        db.query(ReceiptItem).filter(ReceiptItem.id == items[0].id).update(
            {ReceiptItem.marking_present: None}, synchronize_session=False)
        db.commit()
    _backfill_marking()

    def search(**params):
        response = client.get('/api/v1/receipts', params=params, headers=user)
        assert response.status_code == 200, response.text
        return response.json()

    def found(**params):
        return {r['id'] for r in search(**params)['items']}

    assert found(vat_rate='1', marking='present', city='санкт-петербург') == {ids[0]}
    assert found(vat_rate='20', marking='not_saved') == {ids[1]}
    assert found(marking='present') == {ids[0]}
    assert found(marking='not_saved') == {ids[1]}
    assert ids[2] not in found(marking='present', company_id=c2)
    assert ids[0] in found(missing='seller_inn')
    assert ids[0] not in found(missing='address')
    assert ids[0] not in found(missing='fiscal')
    assert not found(missing='items')
    assert ids[0] in found(missing='any')
    assert search(marking='present', page_size=1)['total'] == 1
    facets = client.get('/api/v1/receipts/facets', headers=user).json()['vat_rates']
    assert next(x['count'] for x in facets if x['value'] == '1') == 1
    assert 'secret' not in {x['value'] for x in facets}
    assert 'secret' in {x['value'] for x in client.get('/api/v1/receipts/facets',
        params={'company_id': c2}, headers=hdr).json()['vat_rates']}
    assert client.get('/api/v1/receipts', params={'marking': 'no'}, headers=user).status_code == 422

    with SessionLocal() as db:
        item = db.query(ReceiptItem).filter_by(receipt_id=ids[0], marking_present=True).first()
        item.extra_json = '{}'
        db.commit()
        assert item.marking_present is False
    assert not found(marking='present')
    assert found(marking='not_saved') == set(ids[:2])


def test_saved_marking_recognises_only_meaningful_data():
    from app.models import has_saved_marking

    for value in ('', 'broken', '{}', '{"marking": []}',
                  '{"marking": [{"gtin": "  ", "type": 0}]}', '{"marking": "oops"}'):
        assert not has_saved_marking(value)
    assert has_saved_marking('{"marking": [{"serial": "abc"}]}')
    assert has_saved_marking('{"marking": {"raw": "code"}}')


def test_same_position_and_payment_components(client, data):
    from app.database import SessionLocal
    from app.models import Receipt, ReceiptItem

    _, user, _, _, ids = data
    with SessionLocal() as db:
        rows = db.query(ReceiptItem).filter_by(receipt_id=ids[0]).order_by(ReceiptItem.id).all()
        rows[0].name, rows[0].vat_rate, rows[0].price, rows[0].quantity = 'Гипсокартон', '11', 665, 7
        rows[1].name, rows[1].vat_rate, rows[1].price, rows[1].quantity = 'Шпаклёвка', '20', 1109, 1
        rec = db.get(Receipt, ids[0])
        rec.ecash_sum, rec.cash_sum, rec.pay_type = 5764, 0, ''
        db.commit()

    def found(**params):
        response = client.get('/api/v1/receipts', params=params, headers=user)
        assert response.status_code == 200, response.text
        return {x['id'] for x in response.json()['items']}

    assert found(item_name='гипсокартон', vat_rate='11', item_price_max=700,
                 item_quantity_min=7, payment_component='ecash') == {ids[0]}
    assert not found(item_name='гипсокартон', vat_rate='20')
    assert not found(item_name='гипсокартон', item_price_min=1000)
    assert not found(item_name='шпаклёвка', item_quantity_min=7)
    assert ids[0] not in found(payment_component='cash')
    assert client.get('/api/v1/receipts', params={'item_price_min': -1}, headers=user).status_code == 422


def test_legacy_place_backfill_filter_and_scoped_coverage(client, data):
    import json

    from app.database import SessionLocal, _backfill_retail_place
    from app.models import Receipt, ReceiptItem

    hdr, user, c1, c2, ids = data
    ticket = json.dumps({'fns': {'ticket': {'receipt': {'retailPlace': 'Магазин ТД Вимос'}}}},
                        ensure_ascii=False)
    with SessionLocal() as db:
        one, two, foreign = (db.get(Receipt, rid) for rid in ids)
        one.ext_json = json.dumps({'retail_place': 'МАГАЗИН ТД ВИМОС 100%_Тест'}, ensure_ascii=False)
        two.ext_json, two.raw_data = '{bad json', ticket
        foreign.ext_json = json.dumps({'retail_place': 'Секретный магазин'}, ensure_ascii=False)
        one.merchant_address = 'ПРИОЗЕРСК'
        items = db.query(ReceiptItem).filter_by(receipt_id=ids[0]).order_by(ReceiptItem.id).all()
        items[0].name, items[0].vat_rate, items[0].total = 'Гипсокартон', '11', 4655
        items[1].name, items[1].vat_rate, items[1].total = 'Шпаклёвка', '20', 1109
        db.add(ReceiptItem(receipt_id=ids[1], name='Товар', total=0))
        db.commit()
        assert one.retail_place_index == 'МАГАЗИН ТД ВИМОС 100%_Тест'
        assert two.retail_place_index == 'Магазин ТД Вимос'
        old_ext = one.ext_json
        # Имитация непромигрированных записей.
        db.query(Receipt).filter(Receipt.id.in_(ids)).update(
            {Receipt.retail_place_index: None}, synchronize_session=False)
        db.commit()
    _backfill_retail_place()
    _backfill_retail_place()  # повторный запуск безопасен
    with SessionLocal() as db:
        assert db.get(Receipt, ids[0]).ext_json == old_ext
        assert db.get(Receipt, ids[1]).raw_data == ticket
        assert db.get(Receipt, ids[1]).retail_place_index == 'Магазин ТД Вимос'

    def search(**params):
        response = client.get('/api/v1/receipts', params=params, headers=user)
        assert response.status_code == 200, response.text
        return response.json()

    def found(**params):
        return {row['id'] for row in search(**params)['items']}

    result = search(retail_place='тд вимос', item_total_min=4655, item_total_max=4655,
                    item_name='гипсокартон', vat_rate='11', page_size=1)
    assert result['total'] == 1 and result['items'][0]['id'] == ids[0]
    assert not found(retail_place='тд вимос', item_name='гипсокартон', item_total_max=1109)
    assert not found(item_name='гипсокартон', vat_rate='20', item_total_min=1000)
    assert found(retail_place='100%_тест') == {ids[0]}
    assert not found(retail_place='100ZZтест')
    assert not found(retail_place='ПРИОЗЕРСК')  # адрес — не название места расчёта
    assert not found(retail_place='секретный')
    assert not found(retail_place='секретный', company_id=c2)
    assert found(item_total_min=0, item_total_max=0)  # ноль может быть подстановкой
    assert client.get('/api/v1/receipts', headers=user,
                      params={'item_total_min': -1}).status_code == 422
    own = client.get('/api/v1/receipts/facets', headers=user).json()['retail_place_coverage']
    foreign_coverage = client.get('/api/v1/receipts/facets', headers=hdr,
                                  params={'company_id': c2}).json()['retail_place_coverage']
    assert own == {'saved': 2, 'total': 2}
    assert foreign_coverage == {'saved': 1, 'total': 1}

    with SessionLocal() as db:
        receipt = db.get(Receipt, ids[0])
        receipt.ext_json = json.dumps({'retail_place': 'Новое место'}, ensure_ascii=False)
        db.commit()
        assert receipt.retail_place_index == 'Новое место'
    assert found(retail_place='новое место') == {ids[0]}
    assert ids[0] not in found(retail_place='вимос')


def test_saved_retail_place_is_conservative():
    from app.models import saved_retail_place

    assert saved_retail_place('{bad', '{bad') == ''
    assert saved_retail_place('[]', '{"fns": {"cached": true}}') == ''
    assert saved_retail_place('{"retail_place": 42}', '{}') == ''
    assert saved_retail_place('{"retail_place": " "}',
        '{"fns":{"ticket":{"receipt":{"retailPlaceName":"Магазин"}}}}') == 'Магазин'
    assert saved_retail_place('{"retail_place":"Точка А"}',
        '{"fns":{"ticket":{"receipt":{"retailPlace":"Точка Б"}}}}') == 'Точка А'


def test_old_sqlite_schema_migration_and_backfill(monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    from app import database

    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE receipts (id VARCHAR(36) PRIMARY KEY, '
                          'ext_json TEXT, raw_data TEXT, category VARCHAR(100))'))
        conn.execute(text('INSERT INTO receipts (id, ext_json, raw_data) '
                          'VALUES (:id, :ext, :raw)'),
                     {'id': 'legacy', 'ext': '{"retail_place":"Старый магазин"}', 'raw': '{}'})
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(bind=engine, autoflush=False))
    database._ensure_schema()
    assert 'retail_place_index' in {c['name'] for c in inspect(engine).get_columns('receipts')}
    database._backfill_retail_place()
    database._ensure_schema()
    database._backfill_retail_place()
    with engine.connect() as conn:
        row = conn.execute(text('SELECT ext_json, retail_place_index FROM receipts WHERE id=:id'),
                           {'id': 'legacy'}).one()
        assert row[0] == '{"retail_place":"Старый магазин"}'
        assert row[1] == 'Старый магазин'
    engine.dispose()
