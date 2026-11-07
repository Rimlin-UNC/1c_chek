# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — модели контура B2B (v1.70.45): доступ компаний и ИП к
# платному API. Третий контур приложения: ядро (сотрудники, typ=core) и
# Чек-Пул (сдатчики чеков, typ=pool) уже разделены; клиент API — ни то,
# ни другое, поэтому у него свои таблицы и свой JWT (typ=b2b).
# Ключи API НЕ дублируются: они живут в pool_api_keys, заявка хранит
# только ссылку key_id — лимиты, счётчики и отзыв работают прежним
# механизмом app/pool/router_api.py.
# Миграция только добавляющая: create_all для новых таблиц, существующие
# колонки ядра и пула не изменяются и не удаляются.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import iso_z


def _uid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Состояния и справочники контура (единственный источник для кода и страниц)
# --------------------------------------------------------------------------
# Компания: зарегистрировалась → подтвердила почту → прошла проверки
ST_NEW = "new"                    # запись создана, письмо не подтверждено
ST_EMAIL_PENDING = "email_pending"  # ждём перехода по ссылке из письма
ST_CHECKS = "checks"              # проверки выполняются / выполнены
ST_VERIFIED = "verified"          # статус компании подтверждён — раздел открыт
ST_REJECTED = "rejected"          # проверки не пройдены (причина сохранена)
ST_BLOCKED = "blocked"            # закрыт администратором

COMPANY_STATUSES = (ST_NEW, ST_EMAIL_PENDING, ST_CHECKS, ST_VERIFIED,
                    ST_REJECTED, ST_BLOCKED)

STATUS_TITLES = {
    ST_NEW: "Регистрация не завершена",
    ST_EMAIL_PENDING: "Ждём подтверждения корпоративной почты",
    ST_CHECKS: "Проверки статуса",
    ST_VERIFIED: "Статус компании подтверждён",
    ST_REJECTED: "Отклонено",
    ST_BLOCKED: "Доступ закрыт администратором",
}

# Заявка на подключение к API
AP_NEW = "new"                    # принята от компании
AP_PARKED = "parked"              # автоматика проверила — ждёт администратора
AP_APPROVED = "approved"          # одобрена, ключ выдан
AP_REJECTED = "rejected"          # отклонена с причиной
AP_REVOKED = "revoked"            # ключ отозван

APPLICATION_STATUSES = (AP_NEW, AP_PARKED, AP_APPROVED, AP_REJECTED, AP_REVOKED)

APPLICATION_TITLES = {
    AP_NEW: "Новая заявка",
    AP_PARKED: "Ждёт решения администратора",
    AP_APPROVED: "Одобрена, ключ выдан",
    AP_REJECTED: "Отклонена",
    AP_REVOKED: "Ключ отозван",
}

# Виды проверок (пишутся в b2b_checks.kind)
CK_EMAIL_FORMAT = "email_format"
CK_EMAIL_DOMAIN = "email_domain"
CK_INN_FORMAT = "inn_format"
CK_REGISTRY = "registry"
CK_DUPLICATE = "duplicate"
CK_RISK = "risk"

CHECK_TITLES = {
    CK_EMAIL_FORMAT: "Формат корпоративной почты",
    CK_EMAIL_DOMAIN: "Почтовый домен компании",
    CK_INN_FORMAT: "ИНН: контрольная сумма и тип",
    CK_REGISTRY: "Карточка ЕГРЮЛ/ЕГРИП (Checko)",
    CK_DUPLICATE: "Дубликаты по ИНН и почте",
    CK_RISK: "Оценка риска по карточке",
}

# Назначения одноразовых токенов
PURPOSE_VERIFY = "verify"         # подтверждение корпоративной почты
PURPOSE_RESET = "reset"           # восстановление доступа

# Кто совершил действие (b2b_events.actor)
ACTOR_COMPANY = "company"
ACTOR_ADMIN = "admin"
ACTOR_SYSTEM = "system"

# Типы организаций по длине ИНН (app/services/companies_util.inn_kind)
KIND_UL = "ООО"
KIND_IP = "ИП"

# --------------------------------------------------------------------------
# Тарифы подключения. Названия живут здесь (нейтральный модуль), чтобы письма
# и страницы не импортировали конвейер; лимиты тарифов — в pipeline.py, они
# выводятся из констант app/pool/router_api.py и не назначаются вручную.
# --------------------------------------------------------------------------
TARIFF_START = "start"
TARIFF_BUSINESS = "business"
TARIFF_ANALYTICS = "analytics"
TARIFFS = (TARIFF_START, TARIFF_BUSINESS, TARIFF_ANALYTICS)

TARIFF_TITLES = {
    TARIFF_START: "Старт",
    TARIFF_BUSINESS: "Бизнес",
    TARIFF_ANALYTICS: "Аналитика",
}


# --------------------------------------------------------------------------
# Кабинет компании / ИП
# --------------------------------------------------------------------------
class B2bCompany(Base):
    __tablename__ = "b2b_companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    company_name: Mapped[str] = mapped_column(String(300), default="")
    inn: Mapped[str] = mapped_column(String(12), default="", index=True)
    kind: Mapped[str] = mapped_column(String(10), default="")      # ООО | ИП
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(24), default=ST_NEW, index=True)

    # Подтверждение и решение
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")           # пометка админа

    # Согласие на обработку персональных данных — до отправки формы (152-ФЗ)
    consent_version: Mapped[str] = mapped_column(String(20), default="")
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Следы обращения — хэшем, как в pool_leads (не храним исходные значения)
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    user_agent_hash: Mapped[str] = mapped_column(String(64), default="")

    # Карточка реестра (последняя успешная проверка) и риск
    card_json: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[int] = mapped_column(Integer, default=0)

    # Поисковый ключ для модерации: почта + наименование + ИНН в нижнем
    # регистре. SQLite делает LIKE регистронезависимым только для ASCII,
    # поэтому «Ромашка» не находится по запросу «ромашка»; ключ заполняется
    # средствами Python (str.lower корректен для кириллицы) и индексируется.
    search_key: Mapped[str] = mapped_column(String(600), default="",
                                            index=True)

    # Эпоха сессий: номер входит в JWT, поэтому смена пароля и блокировка
    # гасят все ранее выданные токены сразу, не дожидаясь истечения 30 дней.
    # Без этого украденный токен продолжал бы работать после сброса пароля.
    token_epoch: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self, with_private: bool = False) -> dict:
        """Представление для кабинета (with_private=True — для администратора)."""
        data = {
            "id": self.id,
            "email": self.email,
            "company_name": self.company_name,
            "inn": self.inn,
            "kind": self.kind,
            "contact_name": self.contact_name,
            "status": self.status,
            "status_title": STATUS_TITLES.get(self.status, self.status),
            "email_verified_at": iso_z(self.email_verified_at)
            if self.email_verified_at else "",
            "verified_at": iso_z(self.verified_at) if self.verified_at else "",
            "reject_reason": self.reject_reason if with_private else "",
            "risk_score": self.risk_score,
            "created_at": iso_z(self.created_at) if self.created_at else "",
        }
        if with_private:
            data.update({
                "phone": self.phone,
                "notes": self.notes,
                "rejected_at": iso_z(self.rejected_at) if self.rejected_at else "",
                "last_login_at": iso_z(self.last_login_at)
                if self.last_login_at else "",
                "consent_version": self.consent_version,
                "consent_at": iso_z(self.consent_at) if self.consent_at else "",
                "ip_hash": self.ip_hash,
                "user_agent_hash": self.user_agent_hash,
            })
        return data


# --------------------------------------------------------------------------
# Заявка на подключение к API (одна компания — несколько заявок: история)
# --------------------------------------------------------------------------
class B2bApplication(Base):
    __tablename__ = "b2b_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    tariff: Mapped[str] = mapped_column(String(20), default="start")
    task: Mapped[str] = mapped_column(Text, default="")            # задача компании
    expected_volume: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20), default=AP_NEW, index=True)

    # Снимок автоматики: в каком режиме принято решение и что она решила
    auto_mode: Mapped[str] = mapped_column(String(12), default="")
    auto_decision: Mapped[str] = mapped_column(String(300), default="")

    key_id: Mapped[str] = mapped_column(String(36), default="")    # pool_api_keys.id
    key_prefix: Mapped[str] = mapped_column(String(16), default="")
    reject_reason: Mapped[str] = mapped_column(String(300), default="")

    decided_by: Mapped[str] = mapped_column(String(36), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company_id": self.company_id,
            "tariff": self.tariff,
            "task": self.task,
            "expected_volume": self.expected_volume,
            "status": self.status,
            "status_title": APPLICATION_TITLES.get(self.status, self.status),
            "auto_mode": self.auto_mode,
            "auto_decision": self.auto_decision,
            "key_id": self.key_id,
            "key_prefix": self.key_prefix,
            "reject_reason": self.reject_reason,
            "decided_by": self.decided_by,
            "decided_at": iso_z(self.decided_at) if self.decided_at else "",
            "created_at": iso_z(self.created_at) if self.created_at else "",
        }


# --------------------------------------------------------------------------
# Журнал проверок статуса компании
# --------------------------------------------------------------------------
class B2bCheck(Base):
    __tablename__ = "b2b_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    hard: Mapped[bool] = mapped_column(Boolean, default=True)      # обязательная
    weight: Mapped[int] = mapped_column(Integer, default=0)        # вклад в риск
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": CHECK_TITLES.get(self.kind, self.kind),
            "ok": bool(self.ok),
            "hard": bool(self.hard),
            "detail": self.detail,
            "created_at": iso_z(self.created_at) if self.created_at else "",
        }


# --------------------------------------------------------------------------
# Одноразовые токены (ссылки в письмах): в БД только SHA-256, как в pool_tokens
# --------------------------------------------------------------------------
class B2bToken(Base):
    __tablename__ = "b2b_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    purpose: Mapped[str] = mapped_column(String(12), default=PURPOSE_VERIFY)
    email: Mapped[str] = mapped_column(String(254), default="")
    company_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# Аудит контура: кто и что сделал (company | admin | system)
# --------------------------------------------------------------------------
class B2bEvent(Base):
    __tablename__ = "b2b_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    company_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    actor: Mapped[str] = mapped_column(String(12), default=ACTOR_SYSTEM)
    actor_id: Mapped[str] = mapped_column(String(36), default="")
    action: Mapped[str] = mapped_column(String(40), default="", index=True)
    details: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company_id": self.company_id,
            "actor": self.actor,
            "action": self.action,
            "details": self.details,
            "created_at": iso_z(self.created_at) if self.created_at else "",
        }


Index("ix_b2b_events_company_created", B2bEvent.company_id, B2bEvent.created_at)


def refresh_search_key(company: "B2bCompany") -> str:
    """Пересчитывает поисковый ключ компании (почта, наименование, ИНН)."""
    key = f"{company.email or ''} {company.company_name or ''} {company.inn or ''}"
    company.search_key = " ".join(key.lower().split())[:600]
    return company.search_key


def ensure_b2b_schema(engine) -> None:
    """Добавляющая миграция контура B2B (v1.70.45).

    Создаёт только свои таблицы; таблицы ядра и пула не трогает. Повторный вызов
    безопасен (create_all пропускает существующее), поэтому обновление сервиса
    не требует ручных действий с базой.
    """
    import sqlalchemy
    Base.metadata.create_all(bind=engine, tables=[
        B2bCompany.__table__, B2bApplication.__table__, B2bCheck.__table__,
        B2bToken.__table__, B2bEvent.__table__])
    insp = sqlalchemy.inspect(engine)
    cols = {c["name"] for c in insp.get_columns("b2b_companies")}
    add_cols = (
        ("search_key",
         "ALTER TABLE b2b_companies ADD COLUMN search_key VARCHAR(600) "
         "DEFAULT ''"),
        ("token_epoch",
         "ALTER TABLE b2b_companies ADD COLUMN token_epoch INTEGER DEFAULT 0"),
    )
    for name, ddl in add_cols:
        if name not in cols:
            with engine.begin() as conn:
                conn.execute(sqlalchemy.text(ddl))
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS ix_b2b_companies_search_key "
            "ON b2b_companies (search_key)"))
        # разовое заполнение для строк, созданных до появления колонки:
        # lower() в SQLite не знает кириллицы, поэтому приводим в Python
        rows = conn.execute(sqlalchemy.text(
            "SELECT id, email, company_name, inn FROM b2b_companies "
            "WHERE search_key IS NULL OR search_key = ''")).fetchall()
        for row in rows:
            key = " ".join(f"{row[1] or ''} {row[2] or ''} {row[3] or ''}"
                           .lower().split())[:600]
            conn.execute(sqlalchemy.text(
                "UPDATE b2b_companies SET search_key = :key WHERE id = :cid"),
                {"key": key, "cid": row[0]})


def log_event(db, company_id: str, actor: str, action: str,
              details: str = "", actor_id: str = "") -> B2bEvent:
    """Запись в аудит контура. Не делает commit — транзакцию ведёт вызывающий."""
    rec = B2bEvent(company_id=company_id or "", actor=actor or ACTOR_SYSTEM,
                   actor_id=actor_id or "", action=action or "",
                   details=(details or "")[:1000])
    db.add(rec)
    return rec


# Журнал контура хранится год: этого хватает, чтобы разобрать спорную заявку,
# и не даёт таблице расти бесконечно. Чистка вызывается служебным маршрутом
# обслуживания вместе с журналом вызовов API и журналом писем.
EVENTS_RETENTION_DAYS = 365


def purge_events(db, days: int = EVENTS_RETENTION_DAYS) -> int:
    """Удаляет записи журнала контура старше `days` дней. Возвращает число."""
    from datetime import timedelta
    cutoff = _utcnow() - timedelta(days=max(1, int(days or EVENTS_RETENTION_DAYS)))
    removed = (db.query(B2bEvent)
               .filter(B2bEvent.created_at < cutoff)
               .delete(synchronize_session=False))
    db.commit()
    return int(removed or 0)
