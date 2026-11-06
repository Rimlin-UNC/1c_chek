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

from app.auth import hash_password  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.pool.models import PoolItem, PoolReceipt, PoolUser  # noqa: E402
from app.models import Company, Receipt, ReceiptItem, User  # noqa: E402
from app.seed import seed_if_needed  # noqa: E402
from app.services import appsettings  # noqa: E402
from app.unified import links  # noqa: E402
from app.unified import service as unified  # noqa: E402

init_db()
# Администратор по умолчанию создаётся только в пустой базе: сид добавляет
# бухгалтера, поэтому сначала — первый запуск, как у приложения.
seed_if_needed()
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
    # v1.70.86 («Единая база», фаза 3): чек «E2E Альфы», на который претендовала
    # «E2E Бета». Заявка «отклонена» — она в числе организаций (2), но не в
    # значке меню, поэтому сценарий разбора конфликтов по-прежнему видит «1».
    fn, fd, fp = '9999999999990088', '88', '9990088'
    claimed = Receipt(qr_data=f't=20260925T1015&s=555.00&fn={fn}&i={fd}&fp={fp}&n=1', fn=fn, fd=fd, fp=fp,
                      receipt_date=datetime(2026, 9, 25, 10, 15), total_sum=555, operation=1,
                      company_id=alpha.id, merchant_name='E2E магазин заявки', merchant_inn='4703148343')
    db.add(claimed)
    db.flush()
    claimed_doc = unified.register_source(db, unified.REGISTRY, claimed)
    when = datetime(2026, 9, 26, 9, 0)
    links._upsert(db, [links._row(claimed_doc.id, beta.id, links.LINK_CLAIM, 'declined', 'scan_qr',
                                  attempts=1, first_at=when, last_at=when)], None)
    db.add(User(username='e2e_acc_alpha', full_name='E2E Бухгалтер Альфы', role='accountant',
                company_id=alpha.id, password_hash=hash_password('e2e-accountant-password')))
    # v1.70.112 (T-46): согласование авансового отчёта — своя компания, чтобы не менять числа других сценариев;
    # сотрудник с двумя чеками за октябрь, руководитель и бухгалтер. Включает согласование администратор в сценарии.
    approval_co = Company(name='E2E Согласование')
    db.add(approval_co)
    db.flush()
    for login_name, full_name, role in (('e2e_emp_appr', 'E2E Сотрудник Согласования', 'user'),
                                        ('e2e_boss_appr', 'E2E Руководитель Согласования', 'manager'),
                                        ('e2e_acc_appr', 'E2E Бухгалтер Согласования', 'accountant')):
        db.add(User(username=login_name, full_name=full_name, role=role, company_id=approval_co.id,
                    password_hash=hash_password('e2e-approval-password')))
    for number, (day, total) in enumerate(((6, 1250.0), (14, 640.5)), start=1):
        # фискальные номера — свои: …0101 занят чеком сценария «понимание чеков»
        fn, fd, fp = f'99999999999903{number:02d}', f'3{number:02d}', f'99903{number:02d}'
        spent = Receipt(qr_data=f't=202610{day:02d}T1100&s={total:.2f}&fn={fn}&i={fd}&fp={fp}&n=1', fn=fn, fd=fd,
                        fp=fp, receipt_date=datetime(2026, 10, day, 11, 0), total_sum=total, operation=1,
                        company_id=approval_co.id, merchant_name=f'E2E Стройбаза {number}',
                        merchant_inn='4703148343', assignee='E2E Сотрудник Согласования',
                        assignee_lc='e2e сотрудник согласования', status='verified', fns_status='valid',
                        category='Материалы')
        db.add(spent)
        db.flush()
        unified.register_source(db, unified.REGISTRY, spent)
    db.commit()
