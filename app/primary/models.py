"""Первичка: электронные УПД и счета-фактуры (T-53, v1.70.94).

Новые таблицы — только добавление (ADR-03); внешних ключей на старые таблицы нет
(ADR-09): старая версия после отката их просто не видит. Компания — колонка
company_id, как в единой базе (ADR-20). Исходный XML не хранится: только разобранные
реквизиты, строки, замечания проверки и хеш файла (повторная загрузка того же файла —
отказ).

С 1.70.95 (T-61) документ можно завести и из печатной формы: PDF, скан или фото распознаются
на своём сервере, бухгалтер сверяет черновик и подтверждает. Сам файл не хранится и здесь:
только реквизиты, строки, хеш файла и источник (колонка source).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import uid, utcnow


class PrimaryDocument(Base):
    __tablename__ = "primary_documents"
    __table_args__ = (
        Index("ix_primary_documents_company_date", "company_id", "doc_date"),
        Index("ix_primary_documents_seller_number", "seller_inn", "doc_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30), default="УПД")            # УПД | Счёт-фактура
    function: Mapped[str] = mapped_column(String(12), default="")          # СЧФ | СЧФДОП | ДОП
    format_version: Mapped[str] = mapped_column(String(10), default="")
    file_id: Mapped[str] = mapped_column(String(255), default="")          # ИдФайл
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    doc_number: Mapped[str] = mapped_column(String(100), default="")
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    correction_number: Mapped[str] = mapped_column(String(20), default="")
    correction_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    seller_name: Mapped[str] = mapped_column(String(500), default="")
    seller_inn: Mapped[str] = mapped_column(String(12), default="")
    seller_kpp: Mapped[str] = mapped_column(String(9), default="")
    buyer_name: Mapped[str] = mapped_column(String(500), default="")
    buyer_inn: Mapped[str] = mapped_column(String(12), default="")
    buyer_kpp: Mapped[str] = mapped_column(String(9), default="")
    currency: Mapped[str] = mapped_column(String(3), default="643")
    total_without_vat: Mapped[float] = mapped_column(Float, default=0.0)
    vat_total: Mapped[float] = mapped_column(Float, default=0.0)
    total_with_vat: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="ok")          # ok | warnings | errors
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # v1.70.95 (T-61): откуда документ — xml (файл ЭДО) | pdf | scan (скан или фото) | manual (введён вручную).
    # Колонка добавляется к таблице 1.70.94 через database._ensure_schema; server_default — чтобы
    # строки, записанные прежней версией после отката, тоже получили «xml».
    source: Mapped[str] = mapped_column(String(10), default="xml", server_default="xml")

    lines: Mapped[list["PrimaryDocumentLine"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        order_by="PrimaryDocumentLine.position", lazy="selectin")


class PrimaryDocumentLine(Base):
    __tablename__ = "primary_document_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("primary_documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(Text, default="")
    unit_code: Mapped[str] = mapped_column(String(10), default="")
    unit_name: Mapped[str] = mapped_column(String(40), default="")
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sum_without_vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat_rate: Mapped[str] = mapped_column(String(10), default="none")     # как fns_refs.vat_canonical
    vat_sum: Mapped[float | None] = mapped_column(Float, nullable=True)
    sum_with_vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    item_code: Mapped[str] = mapped_column(String(100), default="")

    document: Mapped[PrimaryDocument] = relationship(back_populates="lines")
