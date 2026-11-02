"""Синтетические данные для Playwright; только новая временная SQLite-база."""
import os
import tempfile
from datetime import datetime
from pathlib import Path

url = os.environ.get('DATABASE_URL', '')
prefix = 'sqlite:///'
if not url.startswith(prefix):
    raise SystemExit('Playwright требует отдельную временную SQLite-базу')
path = Path(url[len(prefix):])
if (not path.is_absolute() or path.name != 'test.db' or path.exists()
        or path.parent.parent != Path(tempfile.gettempdir())
        or not path.parent.name.startswith('ymaster-playwright-')):
    raise SystemExit('Отказ: путь не является новой временной базой Playwright')

from app.database import SessionLocal, init_db  # noqa: E402
from app.pool.models import PoolItem, PoolReceipt, PoolUser  # noqa: E402
from app.models import Company, Receipt, ReceiptItem  # noqa: E402
from app.services import appsettings  # noqa: E402
from app.unified import service as unified  # noqa: E402

init_db()
with SessionLocal() as db:
    appsettings.set_setting(db, 'pool_enabled', '1')
    appsettings.set_setting(db, 'pool_pick_monthly_limit', '100')
    for number in (1, 2):
        donor = PoolUser(vid=f'e2e-donor-{number}')
        db.add(donor)
        db.flush()
        receipt = PoolReceipt(
            pool_user_id=donor.id, source='web', fn=f'999999999999900{number}',
            fd=str(number), fp=f'999000{number}', receipt_date=datetime(2026, 9, 22, 15, 56),
            total_sum=100 * number, operation=1, merchant_name=f'E2E магазин {number}',
            status='verified', full_data=True, verified_at=datetime(2026, 9, 23),
        )
        db.add(receipt)
        db.flush()
        db.add(PoolItem(receipt_id=receipt.id, name=f'E2E товар {number}',
                        quantity=1, price=100 * number, total=100 * number))
    # v1.70.83: спор о владельце для экрана «Разбор конфликтов» — одна
    # фискальная тройка в реестре компании «E2E Альфа» и в пуле у «E2E Бета».
    alpha, beta = Company(name='E2E Альфа'), Company(name='E2E Бета')
    db.add_all([alpha, beta])
    db.flush()
    fn, fd, fp = '9999999999990077', '77', '9990077'
    when = datetime(2026, 9, 24, 12, 30)
    qr = f't=20260924T1230&s=777.00&fn={fn}&i={fd}&fp={fp}&n=1'
    registry = Receipt(qr_data=qr, fn=fn, fd=fd, fp=fp, receipt_date=when, total_sum=777, operation=1,
                       company_id=alpha.id, merchant_name='E2E спорный магазин',
                       merchant_inn='4703148343')
    db.add(registry)
    db.flush()
    db.add(ReceiptItem(receipt_id=registry.id, name='E2E спорный товар', quantity=1,
                       price=777, total=777, position=0))
    disputed = PoolReceipt(source='web', qr_data=qr, fn=fn, fd=fd, fp=fp, receipt_date=when, total_sum=777,
                           operation=1, merchant_name='E2E спорный магазин',
                           merchant_inn='4703148343', status='verified', full_data=True,
                           verified_at=datetime(2026, 9, 25), assigned_company_id=beta.id)
    db.add(disputed)
    db.flush()
    db.add(PoolItem(receipt_id=disputed.id, name='E2E спорный товар', quantity=1,
                    price=777, total=777))
    db.flush()
    unified.register_source(db, unified.REGISTRY, registry)
    doc = unified.register_source(db, unified.POOL, disputed)
    if not doc.review_flags & 8:
        raise SystemExit('Сид: спор о владельце не возник — экран разбора нечего показать')
    db.commit()
