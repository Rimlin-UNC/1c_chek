# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

ORM-модели: чеки, позиции, маппинг, пользователи, аудит, журнал ФНС.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def uid() -> str:
    """Генерация UUID-строки (первичный ключ)."""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Текущее UTC-время."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
#  Пользователи
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    organization: Mapped[str] = mapped_column(String(200), default="")
    # Роли: admin (единственный) | accountant (бухгалтер) | user (пользователь)
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "organization": self.organization,
            "role": self.role,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


# --------------------------------------------------------------------------
#  Приглашения (регистрация только по ссылке от администратора)
# --------------------------------------------------------------------------
class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="user")   # user | accountant
    note: Mapped[str] = mapped_column(String(200), default="")      # для кого (памятка)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "token": self.token,
            "role": self.role,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "max_uses": self.max_uses,
            "used_count": self.used_count,
            "revoked": self.revoked,
            "valid": self.is_valid,
        }

    @property
    def is_valid(self) -> bool:
        if self.revoked or self.used_count >= self.max_uses:
            return False
        if self.expires_at and self.expires_at < utcnow():
            return False
        return True


# --------------------------------------------------------------------------
#  Чеки (54-ФЗ)
# --------------------------------------------------------------------------
class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("fn", "fd", "fp", name="uq_receipt_fnfd_fp"),
        Index("idx_receipts_date", "receipt_date"),
        Index("idx_receipts_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    qr_data: Mapped[str] = mapped_column(Text)                    # исходная строка QR
    # Фискальные реквизиты
    fn: Mapped[str] = mapped_column(String(20), index=True)       # ФН — заводской номер фискального накопителя
    fd: Mapped[str] = mapped_column(String(20), index=True)       # ФД — номер фискального документа
    fp: Mapped[str] = mapped_column(String(20), index=True)       # ФП/ФПД — фискальный признак
    receipt_date: Mapped[datetime] = mapped_column(DateTime)      # дата и время расчёта
    total_sum: Mapped[float] = mapped_column(Float, default=0.0)  # сумма расчёта, ₽
    operation: Mapped[int] = mapped_column(Integer, default=1)    # 1 — приход, 2 — возврат прихода
    # Статусы
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    #   new → verifying → verified / failed
    fns_status: Mapped[str] = mapped_column(String(20), default="unknown")
    #   unknown | valid | invalid | not_found
    fns_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fns_message: Mapped[str] = mapped_column(Text, default="")
    # Интеграция с 1С
    exported: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Источник
    source: Mapped[str] = mapped_column(String(20), default="web")  # web | mobile | camera | image | manual | api
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    # Для бухгалтерии: подотчётное лицо (сотрудник) и комментарий
    assignee: Mapped[str] = mapped_column(String(200), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    raw_data: Mapped[str] = mapped_column(Text, default="{}")       # JSON: полный разбор QR + данные ФНС
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    items: Mapped[list["ReceiptItem"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", lazy="selectin"
    )
    user: Mapped[User | None] = relationship(lazy="joined")

    def to_dict(self, with_items: bool = False) -> dict:
        d = {
            "id": self.id,
            "qr_data": self.qr_data,
            "fn": self.fn,
            "fd": self.fd,
            "fp": self.fp,
            "receipt_date": self.receipt_date.isoformat() if self.receipt_date else None,
            "total_sum": self.total_sum,
            "operation": self.operation,
            "status": self.status,
            "fns_status": self.fns_status,
            "fns_checked_at": self.fns_checked_at.isoformat() if self.fns_checked_at else None,
            "fns_message": self.fns_message,
            "exported": self.exported,
            "exported_at": self.exported_at.isoformat() if self.exported_at else None,
            "source": self.source,
            "assignee": self.assignee or "",
            "comment": self.comment or "",
            "created_by": (self.user.username if self.user else None),
            "created_by_id": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "items_count": len(self.items),
        }
        if with_items:
            d["items"] = [it.to_dict() for it in self.items]
        return d


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    receipt_id: Mapped[str] = mapped_column(String(36), ForeignKey("receipts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[str] = mapped_column(String(10), default="none")  # none|0|10|20|10/110|20/120
    vat_sum: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[int] = mapped_column(Integer, default=0)

    receipt: Mapped[Receipt] = relationship(back_populates="items")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "quantity": self.quantity,
            "price": self.price,
            "total": self.total,
            "vat_rate": self.vat_rate,
            "vat_sum": self.vat_sum,
        }


# --------------------------------------------------------------------------
#  Настройки маппинга реквизитов чека → документы 1С
# --------------------------------------------------------------------------
class MappingSetting(Base):
    __tablename__ = "mapping_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_field: Mapped[str] = mapped_column(String(100))     # поле чека, напр. total_sum
    target_object: Mapped[str] = mapped_column(String(100))    # документ 1С, напр. ПоступлениеТоваровУслуг
    target_field: Mapped[str] = mapped_column(String(100))     # реквизит 1С, напр. СуммаДокумента
    transform: Mapped[str] = mapped_column(String(50), default="direct")
    #   direct | sum100 | date_iso | operation_sign | constant
    transform_param: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_field": self.source_field,
            "target_object": self.target_object,
            "target_field": self.target_field,
            "transform": self.transform,
            "transform_param": self.transform_param,
            "is_active": self.is_active,
            "position": self.position,
        }


# --------------------------------------------------------------------------
#  Журнал аудита
# --------------------------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    username: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(50))
    entity_type: Mapped[str] = mapped_column(String(50), default="")
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "details": self.details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --------------------------------------------------------------------------
#  Журнал обращений к API ФНС (аудит проверок)
# --------------------------------------------------------------------------
class FnsLog(Base):
    __tablename__ = "fns_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    receipt_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_data: Mapped[str] = mapped_column(Text, default="")
    response_data: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------
#  Настройки приложения (key-value, для значений изменяемых через UI)
# --------------------------------------------------------------------------
class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
