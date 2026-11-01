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
from app.services import appsettings  # noqa: E402

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
    db.commit()
