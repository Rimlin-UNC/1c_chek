# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Подключение к базе данных (SQLAlchemy).
"""
from __future__ import annotations

from sqlalchemy import create_engine, event, or_
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

import logging

import uuid as _uuid
from datetime import datetime as _dt, timezone as _tz


def uid() -> str:
    return str(_uuid.uuid4())


def _utcnow() -> "object":
    return _dt.now(_tz.utc).replace(tzinfo=None)


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
    _backfill_assignee_lc()
    # v1.12.0/1.12.1: распределение данных по компаниям из «памятки» + ремонт
    # названий. Страховка: сбой миграции НЕ должен останавливать сервис —
    # ошибка уходит в журнал, приложение стартует на прежней схеме.
    for step in (migrate_companies_from_notes, repair_company_names):
        try:
            result = step()
            if result and result.get("skipped") is None:
                print(f"  Миграция компаний ({step.__name__}): {result}")
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()


def _ensure_schema() -> None:
    """Добавление новых колонок к существующим таблицам (безопасно для SQLite/PG)."""
    from sqlalchemy import inspect, text
    migrations = {
        "users": [
            ("must_change_password",
             "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"),
            # --- v1.11.0: мультикомпанийность ---
            ("company_id", "ALTER TABLE users ADD COLUMN company_id VARCHAR(36) NULL"),
            # --- v1.16.0: Telegram-уведомления ---
            ("telegram_chat_id", "ALTER TABLE users ADD COLUMN telegram_chat_id VARCHAR(32) NULL"),
        ],
        "invites": [
            ("company_id", "ALTER TABLE invites ADD COLUMN company_id VARCHAR(36) NULL"),
        ],
        "companies": [
            ("card_json", "ALTER TABLE companies ADD COLUMN card_json TEXT DEFAULT '{}'"),
            ("card_updated_at", "ALTER TABLE companies ADD COLUMN card_updated_at DATETIME NULL"),
            # v1.22.0: сокращённое наименование из ЕГРЮЛ/ЕГРИП
            ("short_name", "ALTER TABLE companies ADD COLUMN short_name VARCHAR(200) DEFAULT ''"),
        ],
        "audit_log": [
            ("company_id", "ALTER TABLE audit_log ADD COLUMN company_id VARCHAR(36) NULL"),
        ],
        "receipts": [
            # v1.23.0: полные данные чека получены из сервиса проверки
            ("full_data", "ALTER TABLE receipts ADD COLUMN full_data BOOLEAN DEFAULT 0"),
            # v1.25.1: сотрудник в нижнем регистре (кириллица в фильтрах/поиске)
            ("assignee_lc", "ALTER TABLE receipts ADD COLUMN assignee_lc VARCHAR(200) DEFAULT ''"),
            ("assignee",
             "ALTER TABLE receipts ADD COLUMN assignee VARCHAR(200) DEFAULT ''"),
            ("comment",
             "ALTER TABLE receipts ADD COLUMN comment TEXT DEFAULT ''"),
            # --- v1.11.0: мультикомпанийность ---
            ("company_id", "ALTER TABLE receipts ADD COLUMN company_id VARCHAR(36) NULL"),
            # --- v1.2.0: полные данные чека + флаг уведомления ---
            ("merchant_name", "ALTER TABLE receipts ADD COLUMN merchant_name VARCHAR(500) DEFAULT ''"),
            ("merchant_inn", "ALTER TABLE receipts ADD COLUMN merchant_inn VARCHAR(20) DEFAULT ''"),
            ("merchant_address", "ALTER TABLE receipts ADD COLUMN merchant_address VARCHAR(500) DEFAULT ''"),
            ("cashier", "ALTER TABLE receipts ADD COLUMN cashier VARCHAR(200) DEFAULT ''"),
            ("cash_sum", "ALTER TABLE receipts ADD COLUMN cash_sum FLOAT DEFAULT 0"),
            ("ecash_sum", "ALTER TABLE receipts ADD COLUMN ecash_sum FLOAT DEFAULT 0"),
            ("details_source", "ALTER TABLE receipts ADD COLUMN details_source VARCHAR(30) DEFAULT ''"),
            ("details_fetched_at", "ALTER TABLE receipts ADD COLUMN details_fetched_at DATETIME NULL"),
            ("notified", "ALTER TABLE receipts ADD COLUMN notified BOOLEAN DEFAULT 0"),
            ("category", "ALTER TABLE receipts ADD COLUMN category VARCHAR(100) DEFAULT ''"),
            ("category_lc", "ALTER TABLE receipts ADD COLUMN category_lc VARCHAR(100) DEFAULT ''"),
            # --- v1.8.0: личные суммы в чеке ---
            ("personal_sum", "ALTER TABLE receipts ADD COLUMN personal_sum FLOAT DEFAULT 0"),
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
        # v1.11.0: индекс по компании для быстрых выборок пространства клиента
        if "receipts" in existing_tables:
            cols = {c["name"] for c in insp.get_columns("receipts")}
            if "company_id" in cols:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_receipts_company "
                    "ON receipts(company_id)"))
        _backfill_companies(conn, existing_tables)
        # v1.4.0: заполнить category_lc для существующих строк (lower() в SQLite
        # не понимает кириллицу — нормализуем в Python)
        if "receipts" in existing_tables and "category_lc" in {
                c["name"] for c in insp.get_columns("receipts")}:
            rows = conn.execute(text(
                "SELECT id, category FROM receipts WHERE category != '' "
                "AND (category_lc IS NULL OR category_lc = '')")).fetchall()
            for rid, cat in rows:
                conn.execute(text("UPDATE receipts SET category_lc = :lc WHERE id = :i"),
                             {"lc": (cat or "").casefold(), "i": rid})



def _backfill_assignee_lc() -> None:
    """v1.25.1: одноразовое заполнение assignee_lc (casefold) для старых чеков.

    lower() в SQLite не приводит регистр кириллицы, поэтому нормализуем в
    Python: фильтр «Сотрудник» и поиск по имени работают в любом регистре.
    """
    from . import models
    db = SessionLocal()
    try:
        R = models.Receipt
        rows = (db.query(R)
                .filter(or_(R.assignee_lc.is_(None), R.assignee_lc == ""))
                .filter(R.assignee.isnot(None), R.assignee != "")
                .all())
        for r in rows:
            r.assignee_lc = (r.assignee or "").strip().casefold()[:200]
        db.commit()
        if rows:
            logging.getLogger("ymaster").info("Назначено assignee_lc: %s чек(ов)", len(rows))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def _backfill_companies(conn, existing_tables: set) -> None:
    """v1.11.0: перевод существующих данных на мультикомпанийность.

    Если компаний ещё нет, а пользователи есть (обновление с ≤1.10.x):
    создаётся компания по умолчанию (из организации админа) и ВСЕ текущие
    пользователи, приглашения и чеки привязываются к ней — никто не теряет
    доступ к своим данным.
    """
    if "companies" not in existing_tables or "users" not in existing_tables:
        return
    from sqlalchemy import inspect, text             # локально — без циклов
    ucols = {c["name"] for c in inspect(engine).get_columns("users")}
    if "company_id" not in ucols:      # миграция колонок ещё не применена
        return
    has_companies = conn.execute(text("SELECT 1 FROM companies LIMIT 1")).first()
    if has_companies:
        return
    row = conn.execute(text(
        "SELECT organization FROM users WHERE role = 'admin' "
        "ORDER BY created_at LIMIT 1")).first()
    name = (row[0] if row and row[0] else "ООО «Ямастер»").strip()[:200]
    conn.execute(text(
        "INSERT INTO companies (id, name, short_name, inn, note, is_active, created_at) "   # v1.22.0: short_name
        "VALUES (:i, :n, '', '', 'Создана автоматически при обновлении до 1.11.0', 1, :t)"),
        {"i": uid(), "n": name, "t": _utcnow()})
    cid = conn.execute(text("SELECT id FROM companies ORDER BY created_at LIMIT 1")).scalar()
    conn.execute(text("UPDATE users SET company_id = :c WHERE company_id IS NULL "
                      "AND role != 'admin'"), {"c": cid})
    if "receipts" in existing_tables:
        conn.execute(text("UPDATE receipts SET company_id = :c WHERE company_id IS NULL"),
                     {"c": cid})
    if "invites" in existing_tables:
        conn.execute(text("UPDATE invites SET company_id = :c WHERE company_id IS NULL"),
                     {"c": cid})


def company_name_from_note(note: str) -> str:
    """v1.12.1: НАЗВАНИЕ компании из текста памятки/организации.

    Памятка могла быть вида «Иванова — ООО «Альфа-Трейд»» — компанией
    является часть ПОСЛЕ тире (—, – или - с пробелами). Если тире нет,
    берём текст целиком. Внутренние дефисы без пробелов («Альфа-Трейд»)
    не разрезаются."""
    import re as _re

    text_ = (note or "").strip()
    if not text_:
        return ""
    parts = _re.split(r"\s+[—–-]\s+", text_)
    return (parts[-1].strip() or text_) if len(parts) > 1 else text_


def migrate_companies_from_notes() -> dict:
    """v1.12.0: компании из «памятки» приглашений и «Организации» пользователей.

    Исторически клиентские группы различались текстом памятки приглашения
    (он же попадал в «Организацию» сотрудника). Эта процедура превращает
    каждый различающийся текст в компанию (если её ещё нет) и перепривязывает:
      * приглашения    (invites.note       → invites.company_id);
      * пользователей  (users.organization → users.company_id);
      * их чеки        (receipts, созданные этими пользователями и всё ещё
                        находящиеся в компании по умолчанию).
    Процедура идемпотентна: повторный запуск ничего не меняет. Вызывается
    при старте однократно (флаг в app_settings) — существующие данные
    распределяются по компаниям автоматически, ничего не теряется.
    """
    from sqlalchemy import inspect, text

    from .models import (AppSetting, Company, Invite, Receipt, User)

    FLAG = "v1112_notes_companies_done"
    db = SessionLocal()
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        for tbl, col in (("companies", "name"), ("invites", "note"),
                         ("users", "organization"), ("receipts", "company_id")):
            if tbl not in tables or col not in {c["name"] for c in insp.get_columns(tbl)}:
                return {"skipped": "schema"}
        out = {"companies_created": 0, "users_moved": 0,
               "invites_bound": 0, "receipts_moved": 0}
        if db.query(AppSetting).filter(AppSetting.key == FLAG).first():
            return {"skipped": "done"}

        def ensure_company(name: str):
            cf = name.casefold()
            for c in db.query(Company):
                if c.name.casefold() == cf:
                    return c
            c = Company(name=name[:200])
            db.add(c)
            db.flush()
            out["companies_created"] += 1
            return c

        # компания по умолчанию (создана бэкфиллом 1.11.0) — только из неё
        # забираем чеки, чтобы не трогать уже перенесённые администратором
        default_id = None
        admin = db.query(User).filter(User.role == "admin").first()
        first_comp = db.query(Company).order_by(Company.created_at).first()
        if first_comp is not None:
            default_id = first_comp.id

        # 1) приглашения: памятка → компания (по НАЗВАНИЮ, v1.12.1)
        for inv in db.query(Invite).all():
            note = (inv.note or "").strip()
            if not note or inv.company_id:
                continue
            comp = ensure_company(company_name_from_note(note))
            inv.company_id = comp.id
            out["invites_bound"] += 1

        # 2) пользователи: «Организация» → компания (кроме админа)
        for u in db.query(User).filter(User.role != "admin").all():
            org = (u.organization or "").strip()
            if not org:
                continue
            comp = ensure_company(company_name_from_note(org))
            if u.company_id != comp.id:
                u.company_id = comp.id
                out["users_moved"] += 1
                # 3) чеки сотрудника из прежней (дефолтной/без) компании — за ним
                #    (IN с NULL в SQL не матчит NULL — ветки через or_)
                from sqlalchemy import or_
                q = db.query(Receipt).filter(Receipt.created_by == u.id)
                q = (q.filter(or_(Receipt.company_id == default_id,
                                  Receipt.company_id.is_(None)))
                     if default_id else q.filter(Receipt.company_id.is_(None)))
                for r in q.all():
                    if r.company_id != comp.id:
                        r.company_id = comp.id
                        out["receipts_moved"] += 1

        row = db.query(AppSetting).filter(AppSetting.key == FLAG).first()
        if row:
            row.value = "1"
        else:
            db.add(AppSetting(key=FLAG, value="1"))
        db.commit()
        return out
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def repair_company_names() -> dict:
    """v1.12.1: ремонт после миграции 1.12.0.

    Если компания была создана из ПОЛНОГО текста памятки («Иванова — ООО
    «Альфа-Трейд»»), переименовывает её в название («ООО «Альфа-Трейд»») и,
    если такая компания уже есть, ПЕРЕПРИВЯЗЫВАЕТ сотрудников, приглашения,
    чеки и записи аудита в неё, а дубль удаляет. Идемпотентна: чистые имена
    не трогает, повторный запуск ничего не меняет. Выполняется однократно
    (флаг в app_settings) при старте."""
    from .models import (AppSetting, AuditLog, Company, Invite, Receipt,
                         User)

    FLAG = "v1112_company_names_repaired"
    db = SessionLocal()
    try:
        out = {"renamed": 0, "merged": 0}
        if db.query(AppSetting).filter(AppSetting.key == FLAG).first():
            return {"skipped": "done"}

        def find_by_name(cf: str):
            return next((c for c in db.query(Company)
                         if c.name.casefold() == cf), None)

        for comp in db.query(Company).all():
            canonical = company_name_from_note(comp.name)
            if not canonical or canonical == comp.name:
                continue                       # чистое имя — не трогаем
            target = find_by_name(canonical.casefold())
            if target is None:
                comp.name = canonical          # просто переименовать
                out["renamed"] += 1
                continue
            if target.id == comp.id:
                comp.name = canonical
                continue
            # слить: всё от comp → target
            for q, attr in ((db.query(User), User.company_id),
                            (db.query(Invite), Invite.company_id),
                            (db.query(Receipt), Receipt.company_id),
                            (db.query(AuditLog), AuditLog.company_id)):
                for row in q.filter(attr == comp.id).all():
                    setattr(row, attr.key, target.id)
            db.delete(comp)
            out["merged"] += 1

        row = db.query(AppSetting).filter(AppSetting.key == FLAG).first()
        if row:
            row.value = "1"
        else:
            db.add(AppSetting(key=FLAG, value="1"))
        db.commit()
        return out
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
