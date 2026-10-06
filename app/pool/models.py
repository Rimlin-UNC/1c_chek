# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — модели «Чек-Пула» (v1.30.0, v1.31.0 — веб-приём).
# Только НОВЫЕ таблицы (pool_*); существующие таблицы ядра не изменяются.
# Миграции — только добавляющие (ensure_pool_schema: новые таблицы +
# добавление колонок), продакшн-база никогда не требует разрушающих ALTER.
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
    # v1.31.0: анонимный идентификатор гостя сайта (подписанная cookie
    # pool_vid). Чеки гостя копятся до регистрации; на Этапе 3 история
    # присоединяется к аккаунту.
    vid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    tg_username: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(256), default="")       # опционально
    # v1.32.0: кабинет участника — вход по e-mail+пароль (контур пула, ядро
    # пользователей компании не затрагивается); подтверждение e-mail — для
    # рефералов/вывода (следующие этапы).
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
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


# --------------------------------------------------------------------------
# v1.31.0: журнал согласий с офертой (152-ФЗ). Факт согласия фиксируется
# ДО приёма чека: кто (vid), какая редакция оферты, когда, хэш IP.
# --------------------------------------------------------------------------
class PoolConsent(Base):
    __tablename__ = "pool_consents"
    __table_args__ = (Index("idx_pool_consents_vid", "vid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    vid: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pool_users.id"), nullable=True)
    offerta_version: Mapped[str] = mapped_column(String(16), default="")
    ip_hash: Mapped[str] = mapped_column(String(32), default="")   # sha256[:16]
    user_agent_hash: Mapped[str] = mapped_column(String(32), default="")
    form_ms: Mapped[int] = mapped_column(Integer, default=0)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


def ensure_pool_schema(engine) -> None:
    """Добавляющая миграция пула (v1.31.0): новые таблицы создаёт create_all,
    для уже существующей pool_users добавляет колонку vid (если её нет).
    Никогда не изменяет и не удаляет существующие колонки."""
    import sqlalchemy
    Base.metadata.create_all(bind=engine, tables=[
        PoolUser.__table__, PoolReceipt.__table__, PoolItem.__table__,
        PoolPoint.__table__, PoolConsent.__table__, PoolToken.__table__])
    insp = sqlalchemy.inspect(engine)
    cols = {c["name"] for c in insp.get_columns("pool_users")}
    add_cols = (
        ("vid", "ALTER TABLE pool_users ADD COLUMN vid VARCHAR(64)"),
        ("password_hash",
         "ALTER TABLE pool_users ADD COLUMN password_hash VARCHAR(256) DEFAULT ''"),
        ("email_verified",
         "ALTER TABLE pool_users ADD COLUMN email_verified BOOLEAN DEFAULT 0"),
    )
    with engine.begin() as conn:
        for name, ddl in add_cols:
            if name not in cols:
                conn.execute(sqlalchemy.text(ddl))


# --------------------------------------------------------------------------
# v1.32.0: одноразовые токены кабинета — magic link (вход по ссылке из
# письма) и подтверждение e-mail. В БД хранится только SHA-256 хэш токена.
# --------------------------------------------------------------------------
class PoolToken(Base):
    __tablename__ = "pool_tokens"
    __table_args__ = (Index("idx_pool_tokens_hash", "token_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    token_hash: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(String(16), default="login")  # login|verify
    email: Mapped[str] = mapped_column(String(256), default="")
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pool_users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
