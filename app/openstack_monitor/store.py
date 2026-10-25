# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — хранилище контура мониторинга OpenStack (v1.70.51).
# Техническое задание: docs/Monitor.md, разделы 4, 5, 8 и 11.
#
# Настройки подключения — одна строка на программу. Секрет пишется через
# штатный app/services/secretbox.py (Fernet, доменно-разделённый ключ) и
# наружу не возвращается: вместо значения отдаётся признак его наличия.
#
# Ретенция: снимки старше history_days удаляются, журнал держится в том же
# окне — иначе таблица росла бы бесконечно.
#
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import Integer, func

from ..models import iso_z
from ..services import secretbox
from . import connection
from .models import (
    AUTH_TYPES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_INTERVAL_SEC,
    DEFAULT_THRESHOLDS,
    DEFAULT_TIMEOUT_SEC,
    INTERFACES,
    INTERVAL_MAX_SEC,
    INTERVAL_MIN_SEC,
    LOG_ERROR,
    LOG_INFO,
    LOG_LEVELS,
    OpenstackMetric,
    OpenstackSettings,
    OpenstackSyncLog,
    _utcnow,
    thresholds_of,
)

# Предел точек в ответе истории: больше графику не нужно, а запрос тяжелеет.
MAX_HISTORY_POINTS = 2000

# Предел записей журнала в одном ответе.
MAX_LOG_ROWS = 500

# Границы, за которыми значение считается ошибкой ввода, а не опечаткой.
HISTORY_DAYS_MIN = 1
HISTORY_DAYS_MAX = 365
TIMEOUT_MIN_SEC = 1
TIMEOUT_MAX_SEC = 300

# Поля формы, которые сохраняются как есть (строки).
TEXT_FIELDS = ("auth_url", "username", "app_cred_id", "project_name",
               "user_domain_name", "project_domain_name", "region_name",
               "resource_provider_uuid")


def _clamp(value, low: int, high: int, default: int) -> int:
    """Целое в границах; мусор → значение по умолчанию."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def get_settings(db) -> OpenstackSettings:
    """Строка настроек; создаётся при первом обращении."""
    row = (db.query(OpenstackSettings)
           .order_by(OpenstackSettings.created_at.asc()).first())
    if row is not None:
        return row
    row = OpenstackSettings(id="openstack-settings",
                            thresholds_json=json.dumps(DEFAULT_THRESHOLDS))
    db.add(row)
    db.commit()
    return row


def secret_of(row: OpenstackSettings) -> str:
    """Расшифрованный секрет — только для построения подключения.

    Значение не должно попадать ни в ответ API, ни в журнал: функция
    используется внутри контура, а наружу отдаётся secret_set.
    """
    return secretbox.unseal(row.secret_encrypted or "")


def connection_cfg(row: OpenstackSettings) -> dict:
    """Строка настроек → словарь для connection.build_connection.

    Ключи секретов названы так же, как в примере ТЗ (раздел 6.1): password
    и app_cred_secret. Значение у них одно — расшифрованный secret_encrypted,
    потому что активен только выбранный способ аутентификации.
    """
    secret = secret_of(row)
    return {
        "auth_url": row.auth_url,
        "auth_type": row.auth_type,
        "username": row.username,
        "app_cred_id": row.app_cred_id,
        "password": secret,
        "app_cred_secret": secret,
        "secret": secret,
        "project_name": row.project_name,
        "user_domain_name": row.user_domain_name,
        "project_domain_name": row.project_domain_name,
        "region_name": row.region_name,
        "interface": row.interface,
        "verify_tls": bool(row.verify_tls),
        "timeout_sec": row.timeout_sec,
        "resource_provider_uuid": row.resource_provider_uuid,
    }


def _check_problems(payload: dict) -> list[str]:
    """Замечания по форме до сохранения — чтобы отказ был понятным."""
    problems: list[str] = []
    auth_type = payload.get("auth_type")
    if auth_type is not None and auth_type not in AUTH_TYPES:
        problems.append("недопустимый способ аутентификации: "
                        f"{auth_type!r}; допустимы {', '.join(AUTH_TYPES)}")
    interface = payload.get("interface")
    if interface is not None and interface not in INTERFACES:
        problems.append("недопустимый интерфейс каталога: "
                        f"{interface!r}; допустимы {', '.join(INTERFACES)}")
    thresholds = payload.get("thresholds")
    if isinstance(thresholds, dict):
        for key in ("threshold_cpu_pct", "threshold_ram_pct",
                    "threshold_disk_pct"):
            if key not in thresholds:
                continue
            try:
                number = int(thresholds[key])
            except (TypeError, ValueError):
                problems.append(f"порог {key} должен быть целым числом")
                continue
            if not 0 <= number <= 100:
                problems.append(f"порог {key} должен быть от 0 до 100 %")
        traffic = thresholds.get("threshold_traffic_gb_day")
        if traffic not in (None, ""):
            try:
                if float(traffic) < 0:
                    problems.append("порог трафика не может быть отрицательным")
            except (TypeError, ValueError):
                problems.append("порог трафика должен быть числом")
    return problems


def apply_settings(db, payload: dict) -> tuple[OpenstackSettings, list[str]]:
    """Сохранить параметры из формы. → (строка, замечания).

    Пустой секрет означает «не менять»: поле в форме закрыто маской, и
    администратор не обязан вводить его заново при каждом сохранении
    (ТЗ, раздел 4: «Секрет пишется в поле password и не возвращается»).
    """
    problems = _check_problems(payload)
    row = get_settings(db)

    for field in TEXT_FIELDS:
        if field in payload and payload[field] is not None:
            setattr(row, field, str(payload[field]).strip())

    if "auth_type" in payload and payload["auth_type"] in AUTH_TYPES:
        row.auth_type = payload["auth_type"]
    if "interface" in payload and payload["interface"] in INTERFACES:
        row.interface = payload["interface"]
    if "verify_tls" in payload:
        row.verify_tls = bool(payload["verify_tls"])
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])

    secret = payload.get("secret")
    if secret:
        row.secret_encrypted = secretbox.seal(str(secret))

    row.interval_sec = _clamp(payload.get("interval_sec", row.interval_sec),
                              INTERVAL_MIN_SEC, INTERVAL_MAX_SEC,
                              DEFAULT_INTERVAL_SEC)
    row.history_days = _clamp(payload.get("history_days", row.history_days),
                              HISTORY_DAYS_MIN, HISTORY_DAYS_MAX,
                              DEFAULT_HISTORY_DAYS)
    row.timeout_sec = _clamp(payload.get("timeout_sec", row.timeout_sec),
                             TIMEOUT_MIN_SEC, TIMEOUT_MAX_SEC,
                             DEFAULT_TIMEOUT_SEC)

    thresholds = payload.get("thresholds")
    if isinstance(thresholds, dict):
        merged = thresholds_of(row)
        for key in DEFAULT_THRESHOLDS:
            if key not in thresholds:
                continue
            try:
                merged[key] = int(float(thresholds[key]))
            except (TypeError, ValueError):
                continue
        row.thresholds_json = json.dumps(merged, ensure_ascii=False)

    row.updated_at = _utcnow()

    # Обязательные поля (ТЗ, раздел 4) проверяются по ИТОГОВОМУ состоянию, а не
    # по сырой форме: пустой секрет означает «не менять», поэтому судить нужно
    # о результате применения. Проверяется только включённый опрос — иначе
    # администратор не смог бы поправить пороги, пока подключение не заполнено,
    # а выключенный мониторинг всё равно ничего не опрашивает.
    if row.enabled:
        problems.extend(connection.hard_problems(connection_cfg(row)))

    if problems:
        db.rollback()          # отказ не должен оставлять половину формы
        return row, problems

    db.commit()
    return row, problems


def write_metric(db, snapshot: dict) -> OpenstackMetric:
    """Сохранить снимок показателей (ТЗ, раздел 6.2, шаг 5)."""
    collected_at = snapshot.get("collected_at") or _utcnow()
    if not isinstance(collected_at, datetime):
        collected_at = _utcnow()
    row = OpenstackMetric(
        collected_at=collected_at,
        vcpus_total=int(snapshot.get("vcpus_total") or 0),
        vcpus_used=int(snapshot.get("vcpus_used") or 0),
        ram_total_mb=int(snapshot.get("ram_total_mb") or 0),
        ram_used_mb=int(snapshot.get("ram_used_mb") or 0),
        disk_total_gb=int(snapshot.get("disk_total_gb") or 0),
        disk_used_gb=int(snapshot.get("disk_used_gb") or 0),
        traffic_in_bytes=snapshot.get("traffic_in_bytes"),
        traffic_out_bytes=snapshot.get("traffic_out_bytes"),
        raw_json=str(snapshot.get("raw_json") or ""),
    )
    db.add(row)
    db.commit()
    return row


def write_log(db, level: str, message: str, error_code: str = "") -> None:
    """Запись в журнал синхронизации (ТЗ, разделы 6.3 и 10)."""
    db.add(OpenstackSyncLog(level=level if level in LOG_LEVELS else LOG_INFO,
                            message=message, error_code=error_code))
    db.commit()


def mark_success(db, row: OpenstackSettings, message: str) -> None:
    """Успех синхронизации: отметка времени и прежняя ошибка сняты."""
    row.last_success_at = _utcnow()
    row.last_error = ""
    row.last_error_at = None
    row.updated_at = _utcnow()
    db.commit()
    write_log(db, LOG_INFO, message)


def mark_error(db, row: OpenstackSettings, message: str,
               error_code: str = "") -> None:
    """Отказ синхронизации: ошибка сохранена, прежние метрики не тронуты.

    ТЗ, раздел 6.3: при ошибке обновить last_error и last_error_at, а
    собранные ранее показатели оставить — интерфейс показывает последние
    успешные данные и причину отказа рядом с ними.
    """
    row.last_error = message[:2000]
    row.last_error_at = _utcnow()
    row.updated_at = _utcnow()
    db.commit()
    write_log(db, LOG_ERROR, message, error_code)


def latest_metric(db) -> OpenstackMetric | None:
    return (db.query(OpenstackMetric)
            .order_by(OpenstackMetric.collected_at.desc()).first())


def metrics_count(db) -> int:
    return db.query(func.count(OpenstackMetric.id)).scalar() or 0


def _bucket_history(db, since: datetime, until: datetime,
                    step_sec: int) -> list[dict]:
    """История с укрупнением шага средствами SQLite (функция strftime)."""
    model = OpenstackMetric
    epoch = func.strftime("%s", model.collected_at).cast(Integer)
    bucket = (epoch / int(step_sec)).cast(Integer)
    rows = (db.query(bucket.label("bucket"),
                     func.min(model.collected_at),
                     func.avg(model.vcpus_used), func.avg(model.vcpus_total),
                     func.avg(model.ram_used_mb), func.avg(model.ram_total_mb),
                     func.avg(model.disk_used_gb), func.avg(model.disk_total_gb),
                     func.avg(model.traffic_in_bytes),
                     func.avg(model.traffic_out_bytes))
            .filter(model.collected_at >= since, model.collected_at <= until)
            .group_by(bucket).order_by(bucket.asc())
            .limit(MAX_HISTORY_POINTS).all())
    out = []
    for (stamp, first_at, cpu_used, cpu_total, ram_used, ram_total,
         disk_used, disk_total, t_in, t_out) in rows:
        out.append({
            "collected_at": iso_z(first_at),
            "vcpus_used": int(cpu_used or 0),
            "vcpus_total": int(cpu_total or 0),
            "ram_used_mb": int(ram_used or 0),
            "ram_total_mb": int(ram_total or 0),
            "disk_used_gb": int(disk_used or 0),
            "disk_total_gb": int(disk_total or 0),
            "traffic_in_bytes": None if t_in is None else int(t_in),
            "traffic_out_bytes": None if t_out is None else int(t_out),
        })
    return out


def metric_history(db, since: datetime, until: datetime,
                   step_sec: int = 0) -> list[dict]:
    """История снимков за период (ТЗ, раздел 7).

    Если задан шаг укрупнения и база — SQLite, точки усредняются в запросе:
    за 30 суток при опросе раз в минуту это 43 200 строк, а графику хватает
    сотен точек. Для других баз применяется переносимый путь с прореживанием.
    """
    model = OpenstackMetric
    dialect = db.get_bind().dialect.name if db.get_bind() is not None else ""
    if step_sec > 0 and dialect == "sqlite":
        return _bucket_history(db, since, until, step_sec)

    rows = (db.query(model)
            .filter(model.collected_at >= since, model.collected_at <= until)
            .order_by(model.collected_at.asc())
            .limit(MAX_HISTORY_POINTS * 4).all())
    if len(rows) > MAX_HISTORY_POINTS:
        every = -(-len(rows) // MAX_HISTORY_POINTS)      # округление вверх
        rows = rows[::every]
    return [row.public_dict() for row in rows]


def recent_logs(db, limit: int = 100, level: str = "") -> list[dict]:
    """Последние записи журнала, новые сверху (ТЗ, раздел 10)."""
    query = db.query(OpenstackSyncLog)
    if level in LOG_LEVELS:
        query = query.filter(OpenstackSyncLog.level == level)
    rows = (query.order_by(OpenstackSyncLog.ts.desc(), OpenstackSyncLog.id.desc())
            .limit(max(1, min(int(limit or 100), MAX_LOG_ROWS))).all())
    return [row.public_dict() for row in rows]


def status_of(db) -> dict:
    """Состояние контура для вкладки «Показатели» (ТЗ, раздел 7)."""
    row = get_settings(db)
    last = latest_metric(db)
    return {
        "enabled": bool(row.enabled),
        "configured": bool(row.auth_url and row.secret_encrypted),
        "interval_sec": row.interval_sec,
        "history_days": row.history_days,
        "last_success_at": iso_z(row.last_success_at),
        "last_error_at": iso_z(row.last_error_at),
        "last_error": row.last_error,
        "last_collected_at": iso_z(last.collected_at) if last else None,
        "metrics_count": metrics_count(db),
    }


def prune_history(db, row: OpenstackSettings | None = None) -> int:
    """Удалить снимки и записи журнала старше history_days. → сколько удалено.

    ТЗ, раздел 8: «Ретенция: удалять метрики старше history_days». Журнал
    чистится по тому же окну — иначе он рос бы без границы.
    """
    if row is None:
        row = get_settings(db)
    days = _clamp(row.history_days, HISTORY_DAYS_MIN, HISTORY_DAYS_MAX,
                  DEFAULT_HISTORY_DAYS)
    cutoff = _utcnow() - timedelta(days=days)
    removed = (db.query(OpenstackMetric)
               .filter(OpenstackMetric.collected_at < cutoff).delete())
    removed += (db.query(OpenstackSyncLog)
                .filter(OpenstackSyncLog.ts < cutoff).delete())
    db.commit()
    return int(removed or 0)


def describe_prune(removed: int) -> str:
    """Человеческий текст для журнала о результате очистки."""
    if removed == 0:
        return "очистка истории: удалять нечего"
    return f"очистка истории: удалено записей — {removed}"
