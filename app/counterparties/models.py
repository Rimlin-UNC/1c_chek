"""Три новые таблицы контрагентов (T-80, v1.71.0, ADR-48).

Внешние ключи — только внутри подсистемы: откат к 1.70.114 оставляет новые
таблицы нетронутыми, старые таблицы и документы не изменяются (ADR-03/ADR-09).
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import uid, utcnow


class Counterparty(Base):
    __tablename__ = "counterparties"
    __table_args__ = (
        UniqueConstraint("company_id", "inn", name="uq_counterparties_company_inn"),
        Index("ix_counterparties_company_name", "company_id", "name_full"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    inn: Mapped[str] = mapped_column(String(12), default="", index=True)
    kpp: Mapped[str] = mapped_column(String(9), default="")
    ogrn: Mapped[str] = mapped_column(String(15), default="")
    name_full: Mapped[str] = mapped_column(String(500), default="")
    name_short: Mapped[str] = mapped_column(String(200), default="")
    opf: Mapped[str] = mapped_column(String(100), default="")
    address: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(100), default="")
    risk_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    verify_source: Mapped[str] = mapped_column(String(30), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signatory_post: Mapped[str] = mapped_column(String(100), default="")
    signatory_name: Mapped[str] = mapped_column(String(200), default="")
    signatory_basis: Mapped[str] = mapped_column(String(200), default="")
    genitive: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(String(1000), default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    accounts: Mapped[list["CounterpartyBankAccount"]] = relationship(
        back_populates="counterparty", cascade="all, delete-orphan", lazy="selectin")
    aliases: Mapped[list["CounterpartyAlias"]] = relationship(
        back_populates="counterparty", cascade="all, delete-orphan", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "company_id": self.company_id, "inn": self.inn,
            "kpp": self.kpp, "ogrn": self.ogrn, "name_full": self.name_full,
            "name_short": self.name_short, "opf": self.opf, "address": self.address,
            "status": self.status, "source": self.source,
            "verify_source": self.verify_source,
            "verified_at": self.verified_at.isoformat() + "Z" if self.verified_at else None,
            "signatory_post": self.signatory_post, "signatory_name": self.signatory_name,
            "signatory_basis": self.signatory_basis, "genitive": self.genitive,
            "note": self.note, "created_by": self.created_by,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "risk": self.risk(),
        }

    def risk(self) -> dict:
        try:
            value = json.loads(self.risk_json or "{}")
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}


class CounterpartyBankAccount(Base):
    __tablename__ = "counterparty_bank_accounts"
    __table_args__ = (UniqueConstraint("account", name="uq_counterparty_bank_accounts_account"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    counterparty_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("counterparties.id", ondelete="CASCADE"), index=True)
    account: Mapped[str] = mapped_column(String(20), default="")
    bik: Mapped[str] = mapped_column(String(9), default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="")
    corr_account: Mapped[str] = mapped_column(String(20), default="")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    counterparty: Mapped[Counterparty] = relationship(back_populates="accounts")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "counterparty_id": self.counterparty_id,
            "account": self.account, "bik": self.bik, "bank_name": self.bank_name,
            "corr_account": self.corr_account, "is_primary": self.is_primary,
        }


class CounterpartyAlias(Base):
    __tablename__ = "counterparty_aliases"
    __table_args__ = (
        UniqueConstraint("counterparty_id", "name", name="uq_counterparty_aliases_pair"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    counterparty_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("counterparties.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    counterparty: Mapped[Counterparty] = relationship(back_populates="aliases")

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "source": self.source}
