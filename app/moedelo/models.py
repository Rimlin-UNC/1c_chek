"""«Моё дело» по API (T-71, v1.70.107, ADR-37): подключение компании и отправленные документы.

Новые таблицы — только добавление (ADR-03), внешних ключей на старые таблицы нет (ADR-09): старая версия
после отката их просто не видит. Компания — колонка company_id, как в единой базе (ADR-20).

moedelo_connections — ключ API компании, одна строка на компанию. Ключ хранится только зашифрованным
(services.secretbox); наружу отдаются признак «задан» и последние 4 символа.

moedelo_links — какой документ первички отправлен и чем он стал в «Моём деле»: вид (акт), номер документа
в «Моём деле», поставщик, суммы. Одна строка на документ: второй раз тот же документ не уходит. Числовые
номера «Моего дела» (int64) хранятся строкой — одинаково в SQLite и PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import uid, utcnow


class MoedeloConnection(Base):
    """Подключение компании к «Моему делу»: ключ API (md-api-key) и итог последней проверки."""
    __tablename__ = "moedelo_connections"
    __table_args__ = (UniqueConstraint("company_id", name="uq_moedelo_connections_company"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    key_enc: Mapped[str] = mapped_column(Text, default="")                   # только зашифрованным
    key_tail: Mapped[str] = mapped_column(String(4), default="")             # последние 4 символа — для узнавания
    status: Mapped[str] = mapped_column(String(10), default="new")           # new | active | error
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MoedeloLink(Base):
    """Документ первички, отправленный в «Моё дело», и что из него получилось."""
    __tablename__ = "moedelo_links"
    __table_args__ = (UniqueConstraint("document_id", name="uq_moedelo_links_document"),
                      Index("ix_moedelo_links_company_created", "company_id", "created_at"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), default="")
    document_id: Mapped[str] = mapped_column(String(36))
    md_kind: Mapped[str] = mapped_column(String(10), default="act")          # act — акт покупки (услуги)
    md_id: Mapped[str] = mapped_column(String(20), default="")               # Id документа в «Моём деле»
    md_number: Mapped[str] = mapped_column(String(100), default="")
    kontragent_id: Mapped[str] = mapped_column(String(20), default="")
    kontragent_new: Mapped[int] = mapped_column(Integer, default=0)          # 1 — поставщик добавлен по ИНН
    sum_sent: Mapped[float] = mapped_column(Float, default=0.0)              # сумма с НДС по документу первички
    sum_md: Mapped[float | None] = mapped_column(Float, nullable=True)       # сумма, которую посчитало «Моё дело»
    status: Mapped[str] = mapped_column(String(10), default="sent")          # sent | found | mismatch
    message: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
