"""Память исправлений бухгалтера (T-69, v1.70.101, ADR-31).

Бухгалтер поправил категорию позиции — программа запоминает «ключ товара → категория» для своей
компании и сразу применяет к таким же позициям: «Цемент М500 50 кг» и «ЦЕМЕНТ М500 25КГ» дают один
ключ «цемент» (app/goods/names.name_key). Память — по компании (ADR-20): исправление в одной компании
не влияет на другие.

Новая таблица — только добавление (ADR-03); внешних ключей на старые таблицы нет (ADR-09): после
отката старая версия её просто не видит, а категории позиций остаются как были.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import iso_z, uid, utcnow


class GoodsMemory(Base):
    """«Этот товар в нашей компании — такая категория» (исправление бухгалтера)."""
    __tablename__ = "goods_memory"
    __table_args__ = (UniqueConstraint("company_id", "name_key", name="uq_goods_memory_company_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    name_key: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    sample_name: Mapped[str] = mapped_column(String(200), default="")
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_dict(self) -> dict:
        from ..services import goods
        cat = goods.category(self.category)
        return {"id": self.id, "company_id": self.company_id, "name_key": self.name_key,
                "category": cat.code, "category_title": cat.title, "group": cat.group, "icon": cat.icon,
                "sample_name": self.sample_name or "", "hits": int(self.hits or 0),
                "created_at": iso_z(self.created_at), "updated_at": iso_z(self.updated_at)}
