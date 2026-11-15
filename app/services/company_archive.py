"""Общая защита архивации последней активной компании."""
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from ..models import Company

# Отдельный ключ блокировки для операции архивации. Обе точки API берут его до подсчёта.
_ARCHIVE_LOCK = 6436867884863310150


class LastActiveCompany(ValueError):
    """Нельзя отключить последнюю действующую компанию."""


def archive_company(db: Session, company: Company) -> None:
    """Архивировать внутри транзакции; блокировка сохраняется до commit/rollback вызывающего кода."""
    conn = db.connection()
    if conn.dialect.name == 'postgresql':
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': _ARCHIVE_LOCK})
    elif conn.dialect.name == 'sqlite':
        # SELECT до этого места в sqlite3 не начинает транзакцию. BEGIN IMMEDIATE
        # сериализует писателей до чтения счётчика, в том числе между процессами.
        driver = conn.connection.driver_connection
        if not driver.in_transaction:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            # Если вызов уже в транзакции, требуем блокировку записи до подсчёта.
            conn.execute(update(Company).where(Company.id == company.id).values(is_active=Company.is_active))
    else:
        raise RuntimeError('Безопасное архивирование не поддерживает эту базу данных')
    # Компания могла измениться, пока запрос ждал другого писателя.
    db.refresh(company, ['is_active'])
    if not company.is_active:
        return
    other = db.scalar(select(func.count(Company.id)).where(Company.is_active.is_(True), Company.id != company.id))
    if not other:
        raise LastActiveCompany('Нельзя архивировать последнюю активную компанию')
    company.is_active = False
