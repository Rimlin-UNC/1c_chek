"""Первичка: электронные УПД и счета-фактуры (T-53, v1.70.94).

Новые таблицы — только добавление (ADR-03); внешних ключей на старые таблицы нет
(ADR-09): старая версия после отката их просто не видит. Компания — колонка
company_id, как в единой базе (ADR-20). Исходный XML не хранится: только разобранные
реквизиты, строки, замечания проверки и хеш файла (повторная загрузка того же файла —
отказ).

С 1.70.95 (T-61) документ можно завести и из печатной формы: PDF, скан или фото распознаются
на своём сервере, бухгалтер сверяет черновик и подтверждает. Сам файл не хранится и здесь:
только реквизиты, строки, хеш файла и источник (колонка source).

С 1.70.104 (T-63, ADR-34) документы приходят и из почтового ящика компании: primary_mailboxes — подключение
по IMAP (пароль только зашифрованным), primary_mail_items — что программа сделала с каждым вложением
(журнал и черновики на сверку), primary_mail_files — само вложение, но только пока черновик ждёт решения
бухгалтера (после подтверждения, отказа или 30 дней файл удаляется). Колонка channel у документа — канал,
которым он пришёл: пусто — загрузка на экране, mail — из почты.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
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
    # v1.70.104 (T-63): канал — пусто (загрузка на экране) | mail (из почтового ящика). Добавляется к таблице
    # прежних версий через database._ensure_schema; server_default — строки после отката тоже получат пустое.
    channel: Mapped[str] = mapped_column(String(10), default="", server_default="")

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


# ------------------------------------------------------------------ v1.70.104 (T-63, ADR-34): первичка из почты
class PrimaryMailbox(Base):
    """Почтовый ящик компании для первички: IMAP только на чтение, одна строка на компанию.

    Обработанные письма помнятся по паре UIDVALIDITY + UID (как в paperless-ngx): новые письма — с UID больше
    last_uid; сменился UIDVALIDITY (ящик пересоздан) — заново с даты start_date, а уже разобранные вложения
    узнаются по хешу файла. Пароль (обычно пароль приложения) — только в зашифрованной колонке password_enc.
    """
    __tablename__ = "primary_mailboxes"
    __table_args__ = (UniqueConstraint("company_id", name="uq_primary_mailboxes_company"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    host: Mapped[str] = mapped_column(String(200), default="")
    port: Mapped[int] = mapped_column(Integer, default=993)
    security: Mapped[str] = mapped_column(String(8), default="ssl")            # ssl | starttls
    username: Mapped[str] = mapped_column(String(200), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    folder: Mapped[str] = mapped_column(String(200), default="INBOX")
    senders: Mapped[str] = mapped_column(Text, default="")                    # пусто — от всех; иначе адреса и @домены
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)      # первая проверка — письма с этой даты
    status: Mapped[str] = mapped_column(String(10), default="new")            # new | active | error
    uidvalidity: Mapped[str] = mapped_column(String(20), default="")
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    last_result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PrimaryMailItem(Base):
    """Вложение из письма или фото из бота и что с ним сделала программа: в реестр, дубль, отказ, черновик, пропуск."""
    __tablename__ = "primary_mail_items"
    __table_args__ = (Index("ix_primary_mail_items_company_result", "company_id", "result"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    mailbox_id: Mapped[str] = mapped_column(String(36), default="")
    message_uid: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[str] = mapped_column(String(255), default="")
    sender: Mapped[str] = mapped_column(String(200), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(10), default="xml")              # xml | pdf | scan
    # created | duplicate | rejected — XML сразу; queued → draft → confirmed | dismissed | expired — PDF и сканы;
    # skipped — не УПД (счёт на оплату, договор) или не читается
    result: Mapped[str] = mapped_column(String(12), default="queued")
    message: Mapped[str] = mapped_column(String(300), default="")
    document_id: Mapped[str] = mapped_column(String(36), default="")
    draft_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str] = mapped_column(String(36), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # v1.70.105 (T-64): откуда пришёл файл — mail (ящик компании) | telegram | max (фото сотрудника в боте);
    # user_id — сотрудник, приславший фото (для ботов), у писем пусто
    channel: Mapped[str] = mapped_column(String(10), default="mail", server_default="mail")
    user_id: Mapped[str] = mapped_column(String(36), default="", server_default="")


class PrimaryMailFile(Base):
    """Само вложение — только пока черновик ждёт сверки (ADR-34); строка удаляется после решения или срока."""
    __tablename__ = "primary_mail_files"

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(40), default="application/octet-stream")
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
