"""Повторная проверка полномочий на экспериментальной SQLite-копии.

Аутентификацию выполняет вызывающая сторона; переданное id не доказывает её.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session


def require_accounting_rights(db: Session, actor_id: str, company_id: str) -> None:
    """Сверить текущие полномочия непосредственно после блокировки записи."""
    actor = db.execute(text('SELECT role, is_active, company_id FROM users WHERE id=:id'),
                       {'id': actor_id}).first()
    if (not actor or not actor.is_active or actor.role not in ('admin', 'accountant')
            or not company_id or (actor.role != 'admin' and actor.company_id != company_id)):
        raise HTTPException(403, 'Для операции нужны действующие права бухгалтера компании')
