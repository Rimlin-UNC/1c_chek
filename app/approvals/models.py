"""Согласование авансовых отчётов (T-46, v1.70.112, ADR-24, ADR-42): отчёт на согласовании, журнал решений
с простой электронной подписью и настройки компании.

Новые таблицы — только добавление (ADR-03); внешних ключей на старые таблицы нет (ADR-09): старая версия
после отката их просто не видит. Компания — колонка company_id, как в единой базе (ADR-20). Журнал — письменный
след: строки только добавляются, каждая скреплена с предыдущей кодом целостности (HMAC-SHA256 на секретном
ключе сервера), поэтому правка или удаление записи задним числом видны при проверке.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import uid, utcnow


class ApprovalSettings(Base):
    """Согласование в компании. Включает администратор (ADR-24); нет строки — выключено."""
    __tablename__ = "approval_settings"

    company_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # этап без ответа столько часов считается согласованным (владелец: «в течение 3 суток»)
    timeout_hours: Mapped[int] = mapped_column(Integer, default=72, server_default="72")
    # пороги автосогласования, ₽ (0 — порог выключен): руководитель — по сумме; бухгалтер — по сумме,
    # если все чеки подтверждены ФНС, без пометок политики расходов и без правок после формирования
    manager_limit: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    accountant_limit: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    channels: Mapped[str] = mapped_column(String(40), default="telegram,max", server_default="telegram,max")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(36), default="", server_default="")


class AdvanceApproval(Base):
    """Авансовый отчёт на согласовании: снимок состава (то, что подписывают) и текущий этап маршрута."""
    __tablename__ = "advance_approvals"
    __table_args__ = (Index("ix_advance_approvals_company_status", "company_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    number: Mapped[str] = mapped_column(String(32), default="")          # из счётчика печатной формы АО-1
    employee_id: Mapped[str] = mapped_column(String(36), index=True)     # подотчётное лицо, подписывает первым
    employee_name: Mapped[str] = mapped_column(String(200), default="")
    date_from: Mapped[str] = mapped_column(String(10))
    date_to: Mapped[str] = mapped_column(String(10))
    purpose: Mapped[str] = mapped_column(String(500), default="")
    receipts_count: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)                # к учёту: без личных покупок, возвраты — минусом
    snapshot: Mapped[str] = mapped_column(Text, default="{}")             # канонический JSON состава
    doc_hash: Mapped[str] = mapped_column(String(64), default="")         # SHA-256 снимка: подпись — под ним
    status: Mapped[str] = mapped_column(String(12), default="draft")      # draft | pending | approved | rejected | cancelled
    stage: Mapped[str] = mapped_column(String(12), default="employee")    # employee | manager | accountant | ""
    stage_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    notified_key: Mapped[str] = mapped_column(String(48), default="")     # какое уведомление уже отправлено
    previous_id: Mapped[str] = mapped_column(String(36), default="")      # повторная отправка после отказа
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # оптимистическая блокировка: два одновременных решения по одному этапу — второе получает 409
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}


class AdvanceApprovalEvent(Base):
    """Журнал решений: кто, на каком этапе, что сделал, с какого адреса и над каким содержимым."""
    __tablename__ = "advance_approval_events"
    # номер записи уникален в пределах отчёта: две записи с одним номером (гонка) база не примет
    __table_args__ = (Index("ix_advance_approval_events_approval_seq", "approval_id", "seq", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    approval_id: Mapped[str] = mapped_column(String(36))
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(12), default="")
    action: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(36), default="")        # пусто — событие программы
    actor_name: Mapped[str] = mapped_column(String(200), default="")
    actor_role: Mapped[str] = mapped_column(String(20), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    doc_hash: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    seal: Mapped[str] = mapped_column(String(64), default="")             # HMAC цепочки журнала
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
