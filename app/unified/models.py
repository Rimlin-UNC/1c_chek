"""Канонический фискальный документ, независимый от учётных представлений.

Старые таблицы ``receipts`` и ``pool_receipts`` остаются совместимыми
проекциями.  Фискальная идентичность, позиции и владение живут здесь один раз.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import uid, utcnow

# Битовые пометки ручного разбора.  Значения стабильны: их видит API и журнал.
REVIEW_DATE = 1
REVIEW_AMOUNT = 2
REVIEW_OPERATION = 4
REVIEW_OWNERSHIP = 8
REVIEW_ITEMS = 16
REVIEW_INCOMPLETE_IDENTITY = 32
REVIEW_MERCHANT = 64
REVIEW_VERIFICATION = 128
# Блокируют перенос/закрепление только расхождения между склеенными копиями и
# спор о владельце. Неполная тройка — пометка, а не конфликт: такой документ
# никогда не склеивается, у него одна копия, и спорить ей не с чем (v1.70.82:
# иначе чек архива без ФП нельзя было бы перенести, как в 1.70.80).
REVIEW_BLOCKING = (
    REVIEW_DATE | REVIEW_AMOUNT | REVIEW_OPERATION | REVIEW_OWNERSHIP
    | REVIEW_ITEMS | REVIEW_MERCHANT
)


class UnifiedDocument(Base):
    """Одна фискальная тройка — один документ (для полных реквизитов)."""

    __tablename__ = "unified_documents"
    __table_args__ = (
        Index("ix_canonical_documents_deleted", "deleted_at"),
        Index("ix_canonical_documents_review", "review_flags"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # NULL только у неполных исторических реквизитов: такие записи не склеиваются.
    identity_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    fn: Mapped[str] = mapped_column(String(20), default="")
    fd: Mapped[str] = mapped_column(String(20), default="")
    fp: Mapped[str] = mapped_column(String(20), default="")
    receipt_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_sum: Mapped[float] = mapped_column(Float, default=0.0)
    operation: Mapped[int] = mapped_column(Integer, default=1)
    merchant_name: Mapped[str] = mapped_column(String(500), default="")
    merchant_inn: Mapped[str] = mapped_column(String(20), default="")
    merchant_address: Mapped[str] = mapped_column(String(500), default="")
    cashier: Mapped[str] = mapped_column(String(200), default="")
    region_code: Mapped[str] = mapped_column(String(8), default="")
    region_city: Mapped[str] = mapped_column(String(128), default="")
    industry: Mapped[str] = mapped_column(String(128), default="")
    fns_status: Mapped[str] = mapped_column(String(20), default="unknown")
    fns_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_flags: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list["UnifiedItem"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        order_by="UnifiedItem.position", lazy="selectin",
    )
    ownership: Mapped["UnifiedOwnership | None"] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        uselist=False, lazy="joined",
    )
    origins: Mapped[list["UnifiedOrigin"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin",
    )
    events: Mapped[list["UnifiedEvent"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        order_by="UnifiedEvent.created_at", lazy="select",
    )


class UnifiedItem(Base):
    """Каноническая позиция; учётные и пуловые позиции — проекции."""

    __tablename__ = "unified_items"
    __table_args__ = (
        UniqueConstraint("document_id", "position", name="uq_canonical_item_position"),
        Index("ix_canonical_items_document", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unified_documents.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[str] = mapped_column(String(10), default="none")
    vat_sum: Mapped[float] = mapped_column(Float, default=0.0)
    product_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="")
    product_group: Mapped[str] = mapped_column(String(100), default="")
    industry: Mapped[str] = mapped_column(String(100), default="")
    extra_json: Mapped[str] = mapped_column(Text, default="")

    document: Mapped[UnifiedDocument] = relationship(back_populates="items")


class UnifiedOwnership(Base):
    """Единственная строка владения компанией; NULL — общий пул/платформа.

    Ссылки на ``companies``/``users`` — без внешних ключей БД, намеренно:
    после отката на версию без единого реестра (она об этих таблицах не знает)
    удаление компании старым кодом не должно падать на ограничении. Целостность
    держат операции сервиса (перенос при удалении компании) и сверка при старте
    (``app.unified.reconcile``), которая сводит владельца к рабочим таблицам.
    """

    __tablename__ = "unified_ownership"

    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unified_documents.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_by: Mapped[str] = mapped_column(String(36), default="")

    document: Mapped[UnifiedDocument] = relationship(back_populates="ownership")


class UnifiedOrigin(Base):
    """Ссылка на совместимую проекцию.  Сырые ответы и ПДн остаются там."""

    __tablename__ = "unified_origins"
    __table_args__ = (
        UniqueConstraint("source_kind", "source_id", name="uq_canonical_origin_source"),
        Index("ix_canonical_origins_document", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unified_documents.id", ondelete="CASCADE"), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # registry | pool
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[UnifiedDocument] = relationship(back_populates="origins")


class UnifiedEvent(Base):
    """Неизменяемый журнал операций без фискальных значений и ПДн."""

    __tablename__ = "unified_events"
    __table_args__ = (
        UniqueConstraint("actor_id", "idempotency_key", name="uq_canonical_event_command"),
        Index("ix_canonical_events_document", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unified_documents.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    to_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    document: Mapped[UnifiedDocument] = relationship(back_populates="events")
