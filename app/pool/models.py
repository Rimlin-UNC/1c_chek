# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — модели «Чек-Пула» (v1.30.0).
# Только НОВЫЕ таблицы (pool_*); существующие таблицы ядра не изменяются.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Пользователь пула (анонимный: ФИО не храним — 152-ФЗ)
# --------------------------------------------------------------------------
class PoolUser(Base):
    __tablename__ = "pool_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    tg_user_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    tg_username: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(256), default="")       # опционально
    points: Mapped[int] = mapped_column(Integer, default=0)
    referral_code: Mapped[str] = mapped_column(String(12), default="")  # этап 6
    trust_level: Mapped[int] = mapped_column(Integer, default=0)      # 0 новый…
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# Чек в пуле. UNIQUE (fn, fd, fp) — дедупликация на уровне БД (план, разд. 3)
# --------------------------------------------------------------------------
class PoolReceipt(Base):
    __tablename__ = "pool_receipts"
    __table_args__ = (
        UniqueConstraint("fn", "fd", "fp", name="uq_pool_fn_fd_fp"),
        Index("idx_pool_status", "status"),
        Index("idx_pool_user", "pool_user_id"),
        Index("idx_pool_dt", "receipt_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    pool_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pool_users.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="telegram_bot")
    # фискальные данные
    qr_data: Mapped[str] = mapped_column(Text, default="")
    fn: Mapped[str] = mapped_column(String(20), default="")
    fd: Mapped[str] = mapped_column(String(10), default="")
    fp: Mapped[str] = mapped_column(String(20), default="")
    receipt_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_sum: Mapped[float] = mapped_column(Float, default=0.0)
    operation: Mapped[int] = mapped_column(Integer, default=1)
    # продавец
    merchant_name: Mapped[str] = mapped_column(String(500), default="")
    merchant_inn: Mapped[str] = mapped_column(String(20), default="")
    merchant_address: Mapped[str] = mapped_column(String(500), default="")
    cashier: Mapped[str] = mapped_column(String(200), default="")
    # гео/отрасль — заполняются на Этапе 3 (колонки созданы сразу: миграции
    # только добавляющие, продакшн не потребует ALTER позже)
    region_code: Mapped[str] = mapped_column(String(8), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    industry: Mapped[str] = mapped_column(String(128), default="")
    geo_accuracy: Mapped[str] = mapped_column(String(8), default="")
    # приём/модерация
    status: Mapped[str] = mapped_column(String(16), default="pending")
    status_message: Mapped[str] = mapped_column(String(500), default="")
    full_data: Mapped[bool] = mapped_column(Boolean, default=False)
    points_awarded: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[str] = mapped_column(Text, default="")
    # привязка к компании (Этап 5)
    assigned_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PoolItem(Base):
    """Позиция чека пула (копия из данных источника)."""
    __tablename__ = "pool_items"
    __table_args__ = (Index("idx_pool_items_receipt", "receipt_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    receipt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pool_receipts.id"), default="")
    name: Mapped[str] = mapped_column(String(500), default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[str] = mapped_column(String(8), default="none")
    vat_sum: Mapped[float] = mapped_column(Float, default=0.0)


class PoolPoint(Base):
    """Журнал баллов (points_ledger из плана): каждое начисление — запись."""
    __tablename__ = "pool_points"
    __table_args__ = (Index("idx_pool_points_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("pool_users.id"))
    delta: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(64), default="receipt")
    ref_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
