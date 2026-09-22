# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Подключение к базе данных (SQLAlchemy).
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""
    pass


_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
    """Включаем WAL для SQLite, чтобы параллельные чтения не блокировали запись."""
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db():
    """FastAPI-зависимость: сессия БД на время запроса."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Создание таблиц при первом запуске + лёгкая миграция новых колонок."""
    from . import models  # noqa: F401 — регистрируем модели
    Base.metadata.create_all(bind=engine)
    _ensure_schema()


def _ensure_schema() -> None:
    """Добавление новых колонок к существующим таблицам (безопасно для SQLite/PG)."""
    from sqlalchemy import inspect, text
    migrations = {
        "users": [
            ("must_change_password",
             "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"),
        ],
        "receipts": [
            ("assignee",
             "ALTER TABLE receipts ADD COLUMN assignee VARCHAR(200) DEFAULT ''"),
            ("comment",
             "ALTER TABLE receipts ADD COLUMN comment TEXT DEFAULT ''"),
        ],
    }
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, columns in migrations.items():
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in columns:
                if col not in cols:
                    conn.execute(text(ddl))
