"""Компактные счётчики меню без загрузки карточек и без изменения данных."""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..approvals.models import AdvanceApproval
from ..approvals.service import turn_filter
from ..auth import ROLE_ACCOUNTANT, ROLE_ADMIN
from ..b2b.models import AP_NEW, AP_PARKED, B2bApplication
from ..bank.models import BankStatement
from ..clients.service import allowed_company_ids
from ..counterparties.models import Counterparty
from ..docreq.models import DocRequest
from ..models import Company, Receipt, User
from ..pool.models import PoolReceipt, PoolSignal, PoolUser
from ..primary.models import PrimaryDocument
from ..primary.service import scope as scope_primary
from .scoping import scope_receipts


def counts(db: Session, user: User, company_id: str | None) -> dict[str, int]:
    """Числа соответствуют правам и выбранному пространству; без компании не раскрываем чужие данные."""
    company = company_id if user.role == ROLE_ADMIN else user.company_id
    receipts = scope_receipts(db.query(Receipt), user, company_id)
    result = {'receipts': receipts.count()}
    if user.role in (ROLE_ADMIN, ROLE_ACCOUNTANT, 'manager', 'user'):
        result['approvals'] = db.query(AdvanceApproval).filter(or_(*turn_filter(user, company_id))).count()
    if user.role in (ROLE_ADMIN, ROLE_ACCOUNTANT):
        ids = allowed_company_ids(db, user)
        clients = db.query(Company).filter(Company.is_active.is_(True))
        if ids is not None:
            clients = clients.filter(Company.id.in_(ids))
        result['clients'] = clients.count()
        result['primary'] = scope_primary(db.query(PrimaryDocument), user, company_id).count()

        def scoped(model):
            query = db.query(model)
            # Для сотрудника без компании нельзя превращать None в «все».
            if user.role != ROLE_ADMIN or company is not None:
                query = query.filter(model.company_id == company)
            return query

        result['counterparties'] = scoped(Counterparty).count()
        result['bank'] = scoped(BankStatement).count()
        result['docreq'] = scoped(DocRequest).filter(DocRequest.status == 'open').count()
        result['export'] = receipts.filter(Receipt.status == 'verified', Receipt.exported.is_(False)).count()
    if user.role == ROLE_ADMIN:
        result.update({
            'companies': db.query(Company).count(),
            'users': db.query(User).count(),
            'pooladmin': db.query(PoolReceipt).count(),
            'poolpeople': db.query(PoolUser).count(),
            'fraud': db.query(PoolSignal).filter(PoolSignal.status == 'new').count(),
            'b2badmin': db.query(B2bApplication).filter(B2bApplication.status.in_((AP_NEW, AP_PARKED))).count(),
        })
    return result
