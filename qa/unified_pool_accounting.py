"""Атомарный экспериментальный подбор и учёт чека на SQLite-копии.

Это не маршрут API. Все операции должны вызываться с аутентифицированным
пользователем и завершаться внешним commit/rollback вызывающей стороны.
"""
from sqlalchemy.orm import Session

from app.models import User
from qa.unified_accounts import promote_pool_claim
from qa.unified_claims import claim_pool_document


def claim_and_account_pool_document(db: Session, document_id: str, company_id: str, user: User) -> tuple[bool, bool]:
    """Зафиксировать право и учёт вместе; ошибки не оставляют частичный подбор.

    Внешняя транзакция принадлежит вызывающему. В SQLite SAVEPOINT без явной
    транзакции становится внешней транзакцией и RELEASE её фиксирует, поэтому
    до SAVEPOINT требуется открыть настоящую транзакцию DBAPI. Другие СУБД
    не поддерживаются этим экспериментом.
    """
    connection = db.connection()
    if connection.dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    driver = connection.connection.driver_connection
    if not driver.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    with db.begin_nested():
        claimed = claim_pool_document(db, document_id, company_id, user)
        accounted = promote_pool_claim(db, document_id, company_id, user)
    return claimed, accounted
