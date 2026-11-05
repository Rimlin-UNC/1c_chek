"""Банковская выписка (T-45, v1.70.98): выписки, операции без документов, решения о возмещении, настройки.

Новые таблицы — только добавление (ADR-03), внешних ключей на старые таблицы нет (ADR-09): старая версия
после отката их просто не видит. Компания — колонка company_id, как в единой базе (ADR-20). Хранится
минимум данных (ADR-28): из операций — только списания, которым нужны документы (оплаты поставщикам и
покупки по картам); налоги, зарплата, комиссии, поступления и переводы между своими счетами считаются,
но не сохраняются. Полных номеров счетов нет — только последние 4 цифры. Срок хранения — настройкой.

v1.70.99 (ADR-29): подключение к банку по API (Точка, OAuth 2.0 или JWT-ключ) — таблица bank_connections.
Секреты (client_secret, JWT-ключ, токены доступа) и идентификаторы счетов хранятся только зашифрованными
(services.secretbox); наружу отдаются лишь признаки «задан» и последние 4 цифры счетов.
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
    # v1.70.99 (ADR-29): file — загруженный файл; tochka — выписка из API банка (одна на счёт и месяц,
    # дополняется при каждой загрузке). Колонка добавляется к базам 1.70.98 в database._ensure_schema.
    # v1.70.102 (ADR-32): ещё tbank | sber | alfa | modul — выписки из API других банков.
    source: Mapped[str] = mapped_column(String(10), default="file", server_default="file")


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


class BankConnection(Base):
    """Подключение компании к банку по API (v1.70.99, ADR-29): одна строка на компанию и банк.

    Способ — OAuth 2.0 (рекомендуется: клиент сам разрешает доступ в интернет-банке, срок разрешения
    виден) или JWT-ключ из интернет-банка. Секреты — только в зашифрованных колонках *_enc.

    v1.70.102 (ADR-32): банков у компании может быть несколько (Точка, Т-Банк, Сбер, Альфа-Банк, Модульбанк).
    jwt_enc хранит постоянный ключ: JWT-ключ Точки или ключ API (Т-Банк, Модульбанк, Альфа-Банк); refresh_enc —
    refresh_token (Точка, Сбер); cert_enc — сертификат клиента с закрытым ключом (PEM) для взаимного TLS.
    """
    __tablename__ = "bank_connections"
    __table_args__ = (UniqueConstraint("company_id", "bank", name="uq_bank_connections_company_bank"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    bank: Mapped[str] = mapped_column(String(16), default="tochka")
    method: Mapped[str] = mapped_column(String(8), default="oauth")          # oauth | jwt | token | refresh
    client_id: Mapped[str] = mapped_column(String(200), default="")         # пусто — приложение сервера (.env)
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    jwt_enc: Mapped[str] = mapped_column(Text, default="")
    access_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_enc: Mapped[str] = mapped_column(Text, default="")
    # v1.70.102 (ADR-32): сертификат клиента с ключом (PEM); server_default — 1.70.101 после отката пишет без него
    cert_enc: Mapped[str] = mapped_column(Text, default="", server_default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consent_id: Mapped[str] = mapped_column(String(128), default="")         # consentId — до 128 символов
    consent_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    customer_code: Mapped[str] = mapped_column(String(40), default="")
    accounts_enc: Mapped[str] = mapped_column(Text, default="")             # [{id, tail, currency, status}]
    oauth_state: Mapped[str] = mapped_column(String(64), default="", index=True)
    oauth_state_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    oauth_user: Mapped[str] = mapped_column(String(36), default="")
    status: Mapped[str] = mapped_column(String(10), default="new")          # new | awaiting | active | reauth | error
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)     # первая загрузка — с этой даты
    synced_until: Mapped[date | None] = mapped_column(Date, nullable=True)   # по какой день выписка получена
    pending_json: Mapped[str] = mapped_column(Text, default="[]")           # выписки, которые банк ещё готовит
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    last_result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
