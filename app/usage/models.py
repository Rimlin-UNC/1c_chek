"""Добавляющая таблица событий для будущего наблюдаемого счётчика."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import uid, utcnow


class UsageEvent(Base):
    """Одно неизменяемое событие без ссылки-FK на старые таблицы.

    Не является платным списанием и само по себе не подтверждает проверку чека.
    """
    __tablename__ = 'usage_events'
    __table_args__ = (
        UniqueConstraint('company_id', 'metric', 'event_hash', name='uq_usage_event_source'),
        Index('idx_usage_event_period', 'company_id', 'metric', 'period_key'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    period_key: Mapped[str] = mapped_column(String(80), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
