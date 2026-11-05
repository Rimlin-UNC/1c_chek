"""Клиенты бухгалтера-аутсорсера (T-50, v1.70.100, ADR-30).

Бухгалтер по-прежнему принадлежит своей компании (users.company_id). Дополнительные компании-клиенты
назначает только администратор — строкой в этой таблице. Новая таблица — только добавление (ADR-03);
внешних ключей на старые таблицы нет (ADR-09): старая версия после отката её просто не видит, а
бухгалтер снова работает только в своей компании.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import uid, utcnow


class AccountantClient(Base):
    """Компания-клиент, которую ведёт бухгалтер (кроме своей)."""
    __tablename__ = "accountant_clients"
    __table_args__ = (UniqueConstraint("user_id", "company_id", name="uq_accountant_clients_user_company"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    granted_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
