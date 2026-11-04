"""Банковская выписка (T-45, v1.70.98): выписки, операции без документов, решения о возмещении, настройки.

Новые таблицы — только добавление (ADR-03), внешних ключей на старые таблицы нет (ADR-09): старая версия
после отката их просто не видит. Компания — колонка company_id, как в единой базе (ADR-20). Хранится
минимум данных (ADR-28): из операций — только списания, которым нужны документы (оплаты поставщикам и
покупки по картам); налоги, зарплата, комиссии, поступления и переводы между своими счетами считаются,
но не сохраняются. Полных номеров счетов нет — только последние 4 цифры. Срок хранения — настройкой.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import uid, utcnow


class BankStatement(Base):
    """Загруженная выписка: период, счёт (последние 4 цифры) и итоги разбора. Сам файл не хранится."""
    __tablename__ = "bank_statements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    file_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    account_tail: Mapped[str] = mapped_column(String(40), default="")     # «0001» или «0001, 0002»
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    ops_total: Mapped[int] = mapped_column(Integer, default=0)            # операций в файле
    ops_saved: Mapped[int] = mapped_column(Integer, default=0)            # сохранено: нужны документы
    ops_skipped: Mapped[int] = mapped_column(Integer, default=0)          # не нужны документы — не храним
    duplicates: Mapped[int] = mapped_column(Integer, default=0)           # уже были в прошлых выписках
    summary_json: Mapped[str] = mapped_column(Text, default="{}")         # счётчики по видам операций
    uploaded_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BankOperation(Base):
    """Списание, которому нужны документы: оплата поставщику или покупка по карте."""
    __tablename__ = "bank_operations"
    __table_args__ = (UniqueConstraint("company_id", "op_hash", name="uq_bank_operations_company_hash"),
                      Index("ix_bank_operations_company_date", "company_id", "op_date"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    statement_id: Mapped[str] = mapped_column(String(36), index=True)
    op_hash: Mapped[str] = mapped_column(String(64))
    op_date: Mapped[date] = mapped_column(Date)
    number: Mapped[str] = mapped_column(String(20), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    kind: Mapped[str] = mapped_column(String(10), default="transfer")     # card | transfer
    card_tail: Mapped[str] = mapped_column(String(4), default="")
    counterparty_name: Mapped[str] = mapped_column(String(200), default="")
    counterparty_inn: Mapped[str] = mapped_column(String(12), default="")
    purpose: Mapped[str] = mapped_column(String(210), default="")
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)   # open | matched | ignored
    match_type: Mapped[str] = mapped_column(String(10), default="")       # receipt | primary
    match_id: Mapped[str] = mapped_column(String(36), default="")
    match_note: Mapped[str] = mapped_column(String(300), default="")
    decided_by: Mapped[str] = mapped_column(String(36), default="")       # пусто — решила программа
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BankReimbursement(Base):
    """Решение по чеку, оплаченному сотрудником лично: возмещено или не возмещать (с причиной)."""
    __tablename__ = "bank_reimbursements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    receipt_id: Mapped[str] = mapped_column(String(36), unique=True)
    user_id: Mapped[str] = mapped_column(String(36), default="")          # сотрудник — кто сдал чек
    amount: Mapped[float] = mapped_column(Float, default=0.0)             # к учёту на момент решения
    status: Mapped[str] = mapped_column(String(10), default="paid")       # paid | declined
    note: Mapped[str] = mapped_column(String(300), default="")
    decided_by: Mapped[str] = mapped_column(String(36), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BankSettings(Base):
    """Срок хранения операций и карты сотрудников (последние 4 цифры → сотрудник). Нет строки — по умолчанию."""
    __tablename__ = "bank_settings"

    company_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=1095, server_default="1095")
    cards_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
