"""«Что нужно от клиента» (T-58, v1.70.97): запросы документов, журнал и настройки компании.

Новые таблицы — только добавление (ADR-03); внешних ключей на старые таблицы нет (ADR-09):
старая версия после отката их просто не видит. Компания — колонка company_id, как в единой
базе (ADR-20). Журнал — письменный след: что, кому, каким каналом и когда отправлено; записи
не удаляются, а запросы не удаляются, а отменяются.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, false, true
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import uid, utcnow


class DocRequestSettings(Base):
    """Срок сдачи, «час икс» и расписание напоминаний компании. Нет строки — значения по умолчанию."""
    __tablename__ = "doc_request_settings"

    company_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # автонапоминания включает бухгалтер: программа не пишет клиентам без его решения
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # перед плановым напоминанием — сначала найти недостающее (иначе напоминают только о том, что в списке)
    auto_detect: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    deadline_day: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    x_hour: Mapped[str] = mapped_column(String(5), default="18:00", server_default="18:00")
    remind_days: Mapped[str] = mapped_column(String(40), default="3,1,0", server_default="3,1,0")
    repeat_every: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    repeat_max: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    channels: Mapped[str] = mapped_column(String(60), default="telegram,max,email",
                                          server_default="telegram,max,email")
    roles: Mapped[str] = mapped_column(String(60), default="manager", server_default="manager")
    contact_email: Mapped[str] = mapped_column(String(200), default="", server_default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(36), default="", server_default="")


class DocRequest(Base):
    """Что нужно от клиента: один документ за отчётный месяц (period = «ГГГГ-ММ»)."""
    __tablename__ = "doc_requests"
    __table_args__ = (Index("ix_doc_requests_company_period", "company_id", "period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(16), default="manual")      # expected | receipt | upd_fix | payment | manual
    title: Mapped[str] = mapped_column(String(500), default="")
    counterparty_name: Mapped[str] = mapped_column(String(500), default="")
    counterparty_inn: Mapped[str] = mapped_column(String(12), default="")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_type: Mapped[str] = mapped_column(String(16), default="")        # seller | receipt | primary | bank_op | ""
    ref_id: Mapped[str] = mapped_column(String(64), default="")
    assignee_id: Mapped[str | None] = mapped_column(String(36), nullable=True)   # кому напоминать лично
    status: Mapped[str] = mapped_column(String(12), default="open")      # open | received | cancelled
    created_by: Mapped[str] = mapped_column(String(36), default="")      # пусто — нашла программа
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by: Mapped[str] = mapped_column(String(36), default="")
    close_note: Mapped[str] = mapped_column(String(500), default="")


class DocRequestEvent(Base):
    """Журнал: создан, напоминание (канал, кому, текст), получен, отменён. Письменный след."""
    __tablename__ = "doc_request_events"
    __table_args__ = (Index("ix_doc_request_events_company_period", "company_id", "period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    period: Mapped[str] = mapped_column(String(7))
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(20))       # created | reminder | received | cancelled | reopened
    slot: Mapped[str] = mapped_column(String(16), default="")     # D-3, D-1, D0, D+2 … или manual
    channel: Mapped[str] = mapped_column(String(12), default="")  # telegram | max | email
    recipient: Mapped[str] = mapped_column(String(200), default="")
    actor_id: Mapped[str] = mapped_column(String(36), default="")  # пусто — программа
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    text: Mapped[str] = mapped_column(Text, default="")
