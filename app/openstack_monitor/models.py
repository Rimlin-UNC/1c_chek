# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — модели контура мониторинга OpenStack (v1.70.51).
# Техническое задание: docs/Monitor.md, раздел 8 «Модель данных».
#
# Три таблицы: настройки подключения (одна строка на программу), история
# метрик и журнал синхронизации. Миграция только добавляющая: create_all
# создаёт свои таблицы и не трогает таблицы ядра, Чек-Пула и контура B2B,
# поэтому обновление сервера не требует ручных действий с базой.
#
# Секрет (пароль или секрет application credential) хранится в одном поле
# secret_encrypted и никогда не отдаётся в ответах API — шифрование штатным
# app/services/secretbox.py (Fernet, доменно-разделённый ключ).
#
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import iso_z

# Способ аутентификации в Keystone (ТЗ, раздел 4).
AUTH_PASSWORD = "password"
AUTH_APP_CRED = "application_credential"
AUTH_TYPES = (AUTH_PASSWORD, AUTH_APP_CRED)

# Интерфейс каталога сервисов Keystone (ТЗ, раздел 4).
INTERFACES = ("public", "internal", "admin")

# Уровни записей журнала (ТЗ, раздел 10: фильтры INFO/WARN/ERROR).
LOG_INFO = "INFO"
LOG_WARN = "WARN"
LOG_ERROR = "ERROR"
LOG_LEVELS = (LOG_INFO, LOG_WARN, LOG_ERROR)

# Значения по умолчанию (ТЗ, раздел 5).
DEFAULT_INTERVAL_SEC = 60
DEFAULT_HISTORY_DAYS = 30
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_THRESHOLDS = {
    "threshold_cpu_pct": 80,
    "threshold_ram_pct": 80,
    "threshold_disk_pct": 80,
    "threshold_traffic_gb_day": 0,      # 0 — порог не задан
}

# Границы интервала опроса (ТЗ, раздел 5: от 30 до 3600 секунд).
INTERVAL_MIN_SEC = 30
INTERVAL_MAX_SEC = 3600


def _uid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OpenstackSettings(Base):
    """Настройки подключения к OpenStack — одна строка на программе.

    Поля соответствуют форме вкладки «Подключение» (ТЗ, раздел 4), состав
    колонок — разделу 8. Секрет один на два способа аутентификации: при
    auth_type=password это пароль, при application_credential — секрет
    учётных данных приложения. Активен тот, что соответствует auth_type.
    """

    __tablename__ = "openstack_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    auth_url: Mapped[str] = mapped_column(String(500), default="")
    auth_type: Mapped[str] = mapped_column(String(30), default=AUTH_PASSWORD)
    username: Mapped[str] = mapped_column(String(200), default="")
    # Идентификатор учётных данных приложения (ТЗ, раздел 4). Тайной не
    # является, поэтому хранится открыто; раздел 8 ТЗ эту колонку не называет,
    # но поле формы требует места — перегружать username было бы нечестно.
    app_cred_id: Mapped[str] = mapped_column(String(200), default="")
    # Один зашифрованный секрет на оба способа аутентификации — как предписывает
    # раздел 8 ТЗ: активен тот способ, что выбран в auth_type.
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    project_name: Mapped[str] = mapped_column(String(200), default="")
    user_domain_name: Mapped[str] = mapped_column(String(200), default="Default")
    project_domain_name: Mapped[str] = mapped_column(String(200),
                                                     default="Default")
    # v1.70.53: домен идентификатором — так его задаёт файл openrc.sh, который
    # панели провайдеров отдают администратору (OS_PROJECT_DOMAIN_ID). Имя
    # домена у провайдера может не совпадать с «Default» буквально, и тогда
    # Keystone отвечает безликим HTTP 401; идентификатор имеет приоритет.
    user_domain_id: Mapped[str] = mapped_column(String(64), default="")
    project_domain_id: Mapped[str] = mapped_column(String(64), default="")
    region_name: Mapped[str] = mapped_column(String(100), default="")
    # «interface» — имя поля из ТЗ; в Python это не ключевое слово.
    interface: Mapped[str] = mapped_column(String(20), default="public")
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=DEFAULT_TIMEOUT_SEC)
    resource_provider_uuid: Mapped[str] = mapped_column(String(64), default="")

    # Опрос и хранение (ТЗ, раздел 5).
    interval_sec: Mapped[int] = mapped_column(Integer,
                                              default=DEFAULT_INTERVAL_SEC)
    history_days: Mapped[int] = mapped_column(Integer,
                                              default=DEFAULT_HISTORY_DAYS)
    thresholds_json: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    # Состояние последней синхронизации (ТЗ, раздел 6.3): ошибка не стирает
    # прежние метрики, а успех и отказ хранятся отдельными отметками времени.
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                             nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                           nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")

    def public_dict(self) -> dict:
        """Настройки без секрета — то, что разрешено отдать в ответе.

        Вместо секрета возвращается признак его наличия: форме нужно знать,
        заполнен ли секрет, но само значение наружу не выходит (ТЗ,
        разделы 4 и 11).
        """
        return {
            "auth_url": self.auth_url,
            "auth_type": self.auth_type,
            "username": self.username,
            "app_cred_id": self.app_cred_id,
            "secret_set": bool(self.secret_encrypted),
            "project_name": self.project_name,
            "user_domain_name": self.user_domain_name,
            "project_domain_name": self.project_domain_name,
            "user_domain_id": self.user_domain_id,
            "project_domain_id": self.project_domain_id,
            "region_name": self.region_name,
            "interface": self.interface,
            "verify_tls": bool(self.verify_tls),
            "timeout_sec": self.timeout_sec,
            "resource_provider_uuid": self.resource_provider_uuid,
            "interval_sec": self.interval_sec,
            "history_days": self.history_days,
            "enabled": bool(self.enabled),
            "thresholds": thresholds_of(self),
            "last_success_at": iso_z(self.last_success_at),
            "last_error_at": iso_z(self.last_error_at),
            "last_error": self.last_error,
            "updated_at": iso_z(self.updated_at),
        }


class OpenstackMetric(Base):
    """Один снимок показателей (ТЗ, разделы 6.2 и 8).

    Трафик может отсутствовать: если Octavia или Gnocchi в облаке нет,
    поля остаются пустыми, а запись всё равно сохраняется — критерий
    приёмки 7 требует «traffic = null без падения».
    """

    __tablename__ = "openstack_metrics"
    __table_args__ = (
        Index("ix_openstack_metrics_collected_at", "collected_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    vcpus_total: Mapped[int] = mapped_column(Integer, default=0)
    vcpus_used: Mapped[int] = mapped_column(Integer, default=0)
    ram_total_mb: Mapped[int] = mapped_column(Integer, default=0)
    ram_used_mb: Mapped[int] = mapped_column(Integer, default=0)
    disk_total_gb: Mapped[int] = mapped_column(Integer, default=0)
    disk_used_gb: Mapped[int] = mapped_column(Integer, default=0)
    traffic_in_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_out_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="")

    def public_dict(self) -> dict:
        return {
            "collected_at": iso_z(self.collected_at),
            "vcpus_total": self.vcpus_total,
            "vcpus_used": self.vcpus_used,
            "ram_total_mb": self.ram_total_mb,
            "ram_used_mb": self.ram_used_mb,
            "disk_total_gb": self.disk_total_gb,
            "disk_used_gb": self.disk_used_gb,
            "traffic_in_bytes": self.traffic_in_bytes,
            "traffic_out_bytes": self.traffic_out_bytes,
        }


class OpenstackSyncLog(Base):
    """Журнал синхронизации (ТЗ, разделы 6.3, 8 и 10).

    Индекс по убыванию времени: вкладка «Журнал» всегда читает последние
    записи, и порядок запроса совпадает с порядком индекса.
    """

    __tablename__ = "openstack_sync_log"
    __table_args__ = (
        # Индекс по убыванию времени — как требует ТЗ, раздел 8. Строковое
        # имя колонки порядок не выражает, поэтому спуск задан явно.
        Index("ix_openstack_sync_log_ts_desc", text("ts DESC")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    level: Mapped[str] = mapped_column(String(10), default=LOG_INFO)
    message: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(60), default="")

    def public_dict(self) -> dict:
        return {
            "ts": iso_z(self.ts),
            "level": self.level,
            "message": self.message,
            "error_code": self.error_code,
        }


def thresholds_of(row: OpenstackSettings | None) -> dict:
    """Пороги из JSON-поля с значениями по умолчанию вместо пропусков."""
    import json
    out = dict(DEFAULT_THRESHOLDS)
    if row is None:
        return out
    try:
        saved = json.loads(row.thresholds_json or "{}")
    except ValueError:
        saved = {}
    if isinstance(saved, dict):
        for key in DEFAULT_THRESHOLDS:
            if key in saved:
                try:
                    out[key] = int(saved[key])
                except (TypeError, ValueError):
                    pass
    return out


def ensure_openstack_schema(engine) -> None:
    """Добавляющая миграция контура мониторинга (v1.70.51).

    Создаёт только свои три таблицы; таблицы ядра, Чек-Пула и контура B2B
    не трогает. Повторный вызов безопасен: create_all пропускает уже
    существующее, поэтому обновление сервера не требует ручных действий
    с базой.

    v1.70.53: create_all НЕ добавляет колонки в уже созданную таблицу, а
    настройки к этому выпуску уже живут на боевых серверах — поэтому
    недостающие колонки добавляются отдельным ALTER TABLE. Миграция строго
    добавляющая: существующие данные не переписываются, повторный вызов
    безопасен.
    """
    Base.metadata.create_all(bind=engine, tables=[
        OpenstackSettings.__table__, OpenstackMetric.__table__,
        OpenstackSyncLog.__table__])
    _ensure_settings_columns(engine)


# Колонки настроек, которые выпуск v1.70.53 добавляет к уже созданной таблице.
_ADDED_SETTINGS_COLUMNS = (
    ("user_domain_id", "VARCHAR(64) DEFAULT '' NOT NULL"),
    ("project_domain_id", "VARCHAR(64) DEFAULT '' NOT NULL"),
)


def _ensure_settings_columns(engine) -> None:
    """Добавить недостающие колонки в openstack_settings (v1.70.53).

    Инспекция диалектонезависима (sqlalchemy.inspect), ALTER TABLE ADD COLUMN
    понимают и SQLite, и PostgreSQL. Таблица к этому моменту существует —
    её создал create_all выше.
    """
    from sqlalchemy import inspect, text

    existing = {col["name"] for col in
                inspect(engine).get_columns("openstack_settings")}
    for name, ddl in _ADDED_SETTINGS_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE openstack_settings ADD COLUMN {name} {ddl}"))
