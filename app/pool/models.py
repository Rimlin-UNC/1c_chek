# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — модели «Чек-Пула» (v1.30 Этап 1 … v1.35 рефералы).
# Только НОВЫЕ таблицы (pool_*); существующие таблицы ядра не изменяются.
# Миграции — только добавляющие (ensure_pool_schema: новые таблицы +
# добавление колонок), продакшн-база никогда не требует разрушающих ALTER.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _uid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Пользователь пула (анонимный: ФИО не храним — 152-ФЗ)
# --------------------------------------------------------------------------
class PoolUser(Base):
    __tablename__ = "pool_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    tg_user_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    # v1.31.0: анонимный идентификатор гостя сайта (подписанная cookie
    # pool_vid). Чеки гостя копятся до регистрации; на Этапе 3 история
    # присоединяется к аккаунту.
    vid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    tg_username: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(256), default="")       # опционально
    # v1.32.0: кабинет участника — вход по e-mail+пароль (контур пула, ядро
    # пользователей компании не затрагивается); подтверждение e-mail — для
    # рефералов/вывода (следующие этапы).
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    points: Mapped[int] = mapped_column(Integer, default=0)
    referral_code: Mapped[str] = mapped_column(String(12), default="")  # этап 6
    trust_level: Mapped[int] = mapped_column(Integer, default=0)      # 0 новый…
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # v1.34.0: антифрод — risk_score 0–100 (сумма весов сигналов);
    # карантин вместо бана: с risk ≥ 71 новые баллы не начисляются,
    # разбор — в панели антифрода; чеки при этом сохраняются в пул.
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# Чек в пуле. UNIQUE (fn, fd, fp) — дедупликация на уровне БД (план, разд. 3)
# --------------------------------------------------------------------------
class PoolReceipt(Base):
    __tablename__ = "pool_receipts"
    __table_args__ = (
        UniqueConstraint("fn", "fd", "fp", name="uq_pool_fn_fd_fp"),
        Index("idx_pool_status", "status"),
        Index("idx_pool_user", "pool_user_id"),
        Index("idx_pool_dt", "receipt_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    pool_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pool_users.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="telegram_bot")
    # фискальные данные
    qr_data: Mapped[str] = mapped_column(Text, default="")
    fn: Mapped[str] = mapped_column(String(20), default="")
    fd: Mapped[str] = mapped_column(String(10), default="")
    fp: Mapped[str] = mapped_column(String(20), default="")
    receipt_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_sum: Mapped[float] = mapped_column(Float, default=0.0)
    operation: Mapped[int] = mapped_column(Integer, default=1)
    # продавец
    merchant_name: Mapped[str] = mapped_column(String(500), default="")
    merchant_inn: Mapped[str] = mapped_column(String(20), default="")
    merchant_address: Mapped[str] = mapped_column(String(500), default="")
    cashier: Mapped[str] = mapped_column(String(200), default="")
    # гео/отрасль — заполняются на Этапе 3 (колонки созданы сразу: миграции
    # только добавляющие, продакшн не потребует ALTER позже)
    region_code: Mapped[str] = mapped_column(String(8), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    industry: Mapped[str] = mapped_column(String(128), default="")
    geo_accuracy: Mapped[str] = mapped_column(String(8), default="")
    # приём/модерация
    status: Mapped[str] = mapped_column(String(16), default="pending")
    status_message: Mapped[str] = mapped_column(String(500), default="")
    full_data: Mapped[bool] = mapped_column(Boolean, default=False)
    points_awarded: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[str] = mapped_column(Text, default="")
    # привязка к компании (Этап 5; v1.37.0 — «Подбор из пула» и сценарий C)
    assigned_company_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PoolItem(Base):
    """Позиция чека пула (копия из данных источника)."""
    __tablename__ = "pool_items"
    __table_args__ = (Index("idx_pool_items_receipt", "receipt_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    receipt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pool_receipts.id"), default="")
    name: Mapped[str] = mapped_column(String(500), default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[str] = mapped_column(String(8), default="none")
    vat_sum: Mapped[float] = mapped_column(Float, default=0.0)


class PoolPoint(Base):
    """Журнал баллов (points_ledger из плана): каждое начисление — запись."""
    __tablename__ = "pool_points"
    __table_args__ = (Index("idx_pool_points_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("pool_users.id"))
    delta: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(64), default="receipt")
    ref_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# v1.31.0: журнал согласий с офертой (152-ФЗ). Факт согласия фиксируется
# ДО приёма чека: кто (vid), какая редакция оферты, когда, хэш IP.
# --------------------------------------------------------------------------
class PoolConsent(Base):
    __tablename__ = "pool_consents"
    __table_args__ = (Index("idx_pool_consents_vid", "vid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    vid: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pool_users.id"), nullable=True)
    offerta_version: Mapped[str] = mapped_column(String(16), default="")
    ip_hash: Mapped[str] = mapped_column(String(32), default="")   # sha256[:16]
    user_agent_hash: Mapped[str] = mapped_column(String(32), default="")
    visitor_hash: Mapped[str] = mapped_column(String(32), default="")  # v1.34.0
    form_ms: Mapped[int] = mapped_column(Integer, default=0)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


def ensure_pool_schema(engine) -> None:
    """Добавляющая миграция пула (v1.31.0): новые таблицы создаёт create_all,
    для уже существующей pool_users добавляет колонку vid (если её нет).
    Никогда не изменяет и не удаляет существующие колонки."""
    import sqlalchemy
    Base.metadata.create_all(bind=engine, tables=[
        PoolUser.__table__, PoolReceipt.__table__, PoolItem.__table__,
        PoolPoint.__table__, PoolConsent.__table__, PoolToken.__table__,
        PoolFingerprint.__table__, PoolIpLog.__table__, PoolSignal.__table__,
        PoolReferral.__table__, PoolAchievement.__table__,
        PoolWithdrawal.__table__])
    insp = sqlalchemy.inspect(engine)
    # v1.37.0: колонки подбора для уже существующей pool_receipts
    rcols = {c["name"] for c in insp.get_columns("pool_receipts")}
    r_add = (
        ("assigned_at",
         "ALTER TABLE pool_receipts ADD COLUMN assigned_at DATETIME"),
        ("assigned_by",
         "ALTER TABLE pool_receipts ADD COLUMN assigned_by VARCHAR(36) DEFAULT ''"),
    )
    for name, ddl in r_add:
        if name not in rcols:
            with engine.begin() as conn:
                conn.execute(sqlalchemy.text(ddl))
    cols = {c["name"] for c in insp.get_columns("pool_users")}
    add_cols = (
        ("vid", "ALTER TABLE pool_users ADD COLUMN vid VARCHAR(64)"),
        ("password_hash",
         "ALTER TABLE pool_users ADD COLUMN password_hash VARCHAR(256) DEFAULT ''"),
        ("email_verified",
         "ALTER TABLE pool_users ADD COLUMN email_verified BOOLEAN DEFAULT 0"),
        ("risk_score",
         "ALTER TABLE pool_users ADD COLUMN risk_score INTEGER DEFAULT 0"),
        ("quarantined_at",
         "ALTER TABLE pool_users ADD COLUMN quarantined_at TIMESTAMP NULL"),
    )
    cons_cols = {c["name"] for c in insp.get_columns("pool_consents")}
    if "visitor_hash" not in cons_cols:
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text(
                "ALTER TABLE pool_consents ADD COLUMN visitor_hash VARCHAR(32)"
                " DEFAULT ''"))
    with engine.begin() as conn:
        for name, ddl in add_cols:
            if name not in cols:
                conn.execute(sqlalchemy.text(ddl))


# --------------------------------------------------------------------------
# v1.32.0: одноразовые токены кабинета — magic link (вход по ссылке из
# письма) и подтверждение e-mail. В БД хранится только SHA-256 хэш токена.
# --------------------------------------------------------------------------
class PoolToken(Base):
    __tablename__ = "pool_tokens"
    __table_args__ = (Index("idx_pool_tokens_hash", "token_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    token_hash: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(String(16), default="login")  # login|verify
    email: Mapped[str] = mapped_column(String(256), default="")
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pool_users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# v1.34.0: антифрод. Слой 1 (Device): связь участника с обезличенным
# отпечатком устройства (X-Visitor-Id); хэшируем сразу — 152-ФЗ.
# --------------------------------------------------------------------------
class PoolFingerprint(Base):
    __tablename__ = "pool_fingerprints"
    __table_args__ = (
        Index("idx_pool_fp_visitor", "visitor_hash"),
        Index("idx_pool_fp_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("pool_users.id"))
    visitor_hash: Mapped[str] = mapped_column(String(64), default="")
    ua_hash: Mapped[str] = mapped_column(String(32), default="")
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    first_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# Слой 2 (Network): журнал сетей участников (ip, /24) — для кластеров.
class PoolIpLog(Base):
    __tablename__ = "pool_ip_log"
    __table_args__ = (
        Index("idx_pool_ip_user", "user_id"),
        Index("idx_pool_ip24", "ip24"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("pool_users.id"))
    ip: Mapped[str] = mapped_column(String(45), default="")
    ip24: Mapped[str] = mapped_column(String(18), default="")   # a.b.c
    ua_hash: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(16), default="receipt")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# Журнал сигналов: что заметили, насколько это плохо, как разобрали.
class PoolSignal(Base):
    __tablename__ = "pool_signals"
    __table_args__ = (
        Index("idx_pool_sig_user", "user_id"),
        Index("idx_pool_sig_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("pool_users.id"))
    receipt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    code: Mapped[str] = mapped_column(String(32), default="")     # MULTI_ACCOUNT_DEVICE…
    severity: Mapped[int] = mapped_column(Integer, default=2)     # 1..5
    points: Mapped[int] = mapped_column(Integer, default=0)       # вклад в risk_score
    details: Mapped[str] = mapped_column(Text, default="")        # json мелочей
    status: Mapped[str] = mapped_column(String(16), default="new")  # new|reviewing|false_positive|confirmed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str] = mapped_column(String(64), default="")  # username админа


# --------------------------------------------------------------------------
# v1.35.0: реферальная связь (один уровень, план разд. 2). UNIQUE по
# referred_id — у участника может быть только один пригласивший.
# --------------------------------------------------------------------------
class PoolReferral(Base):
    __tablename__ = "pool_referrals"
    __table_args__ = (
        Index("idx_pool_ref_referrer", "referrer_id"),
        UniqueConstraint("referred_id", name="uq_pool_ref_referred"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    referrer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pool_users.id"))
    referred_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pool_users.id"))
    code_used: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# v1.36.0 «Вовлечение»: ачивки («50 чеков», «3 отрасли», «первый чек
# региона»). UNIQUE (user_id, code) — каждая ачивка выдаётся один раз.
# --------------------------------------------------------------------------
class PoolAchievement(Base):
    __tablename__ = "pool_achievements"
    __table_args__ = (
        Index("idx_pool_ach_user", "user_id"),
        UniqueConstraint("user_id", "code", name="uq_pool_ach_user_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pool_users.id"))
    code: Mapped[str] = mapped_column(String(32), default="")
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------
# v1.36.0: заявка на вывод баллов. Телефон храним ТОЛЬКО хэшем
# (sha256) — 152-ФЗ; SMS-подтверждение первого вывода — код в
# pool_tokens (purpose="sms"), шлюз подключается отдельным решением.
# --------------------------------------------------------------------------
class PoolWithdrawal(Base):
    __tablename__ = "pool_withdrawals"
    __table_args__ = (Index("idx_pool_wd_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pool_users.id"))
    points: Mapped[int] = mapped_column(Integer, default=0)
    phone_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    sms_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
