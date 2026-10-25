# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — API контура мониторинга OpenStack (v1.70.51).
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: chek@ymaster.ru
#
# Техническое задание: docs/Monitor.md, раздел 7. Восемь внутренних
# маршрутов: настройки (чтение и сохранение), проверка подключения,
# отключение, текущие показатели, история, состояние и журнал.
#
# Отклонение от ТЗ, согласованное с владельцем: адреса живут под общим
# префиксом программы /api/v1/openstack/… вместо /api/settings/openstack и
# /api/openstack/*. Так все 40+ маршрутов проекта остаются в одном стиле,
# а префикс api/ уже исключён из SPA-перехвата.
#
# Второе отклонение: доступ — require_admin. ТЗ называет роль
# openstack_monitor.manage, но в программе нет таблицы точечных прав,
# а само ТЗ в разделе 3 помечает блок как администраторский.
#
# Секрет не возвращается ни в одном ответе: вместо значения отдаётся
# признак secret_set (ТЗ, разделы 4 и 11).
# ======================================================================
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..services.audit import log_action
from . import collector, connection, store, worker
from .models import (
    AUTH_TYPES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_INTERVAL_SEC,
    INTERVAL_MAX_SEC,
    INTERVAL_MIN_SEC,
    _utcnow,
    thresholds_of,
)

router = APIRouter(prefix="/api/v1/openstack", tags=["Мониторинг OpenStack"])

# Относительный шаг укрупнения истории: 5m, 1h, 6h, 1d.
_STEP_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_STEP_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Граница периода истории: дальше history_days данные уже удалены.
MAX_RANGE_DAYS = 366

# Шаг укрупнения по умолчанию для каждого периода вкладки «Показатели».
PERIOD_STEPS = {"1h": 60, "24h": 900, "7d": 3600, "30d": 21600}


class Thresholds(BaseModel):
    """Пороги в процентах (ТЗ, раздел 5). Ноль отключает проверку."""

    threshold_cpu_pct: int = Field(80, ge=0, le=100)
    threshold_ram_pct: int = Field(80, ge=0, le=100)
    threshold_disk_pct: int = Field(80, ge=0, le=100)
    threshold_traffic_gb_day: float = Field(0, ge=0)


class SettingsBody(BaseModel):
    """Параметры подключения и опроса из формы вкладки «Подключение»."""

    auth_url: str = ""
    auth_type: str = "password"
    username: str = ""
    app_cred_id: str = ""                # ТЗ, раздел 4: для application credential
    secret: str = ""                     # пустая строка — не менять сохранённый
    project_name: str = ""
    user_domain_name: str = "Default"
    project_domain_name: str = "Default"
    # v1.70.53: файл панели провайдера задаёт домен идентификатором
    # (OS_PROJECT_DOMAIN_ID) — он имеет приоритет над именем.
    user_domain_id: str = ""
    project_domain_id: str = ""
    region_name: str = ""
    interface: str = "public"
    verify_tls: bool = True
    timeout_sec: int = Field(30, ge=1, le=300)
    resource_provider_uuid: str = ""
    interval_sec: int = Field(DEFAULT_INTERVAL_SEC,
                              ge=INTERVAL_MIN_SEC, le=INTERVAL_MAX_SEC)
    history_days: int = Field(DEFAULT_HISTORY_DAYS, ge=1, le=365)
    thresholds: Thresholds | None = None
    enabled: bool = False


class TestBody(BaseModel):
    """Проверка подключения: форма может быть ещё не сохранена."""

    auth_url: str = ""
    auth_type: str = "password"
    username: str = ""
    app_cred_id: str = ""
    secret: str = ""
    project_name: str = ""
    user_domain_name: str = "Default"
    project_domain_name: str = "Default"
    # v1.70.53: файл панели провайдера задаёт домен идентификатором
    # (OS_PROJECT_DOMAIN_ID) — он имеет приоритет над именем.
    user_domain_id: str = ""
    project_domain_id: str = ""
    region_name: str = ""
    interface: str = "public"
    verify_tls: bool = True
    timeout_sec: int = Field(30, ge=1, le=300)
    resource_provider_uuid: str = ""


def _naive_utc(moment: datetime) -> datetime:
    """Осознанный момент → наивный UTC: так время хранит весь проект."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_moment(text: str, default: datetime) -> datetime:
    """Разобрать момент времени: ISO-8601 или число секунд с эпохи.

    Не разобралось — значение по умолчанию, а не отказ: история должна
    открываться даже при неаккуратном параметре запроса.
    """
    raw = (text or "").strip()
    if not raw:
        return default
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        try:
            return _naive_utc(datetime.fromtimestamp(float(raw),
                                                      tz=timezone.utc))
        except (ValueError, OverflowError, OSError):
            return default
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return default
    if parsed.tzinfo is not None:
        parsed = _naive_utc(parsed)
    return parsed


def _parse_step(text: str) -> int:
    """Шаг укрупнения в секундах: число или запись вида 5m / 1h / 1d."""
    raw = (text or "").strip()
    if not raw:
        return 0
    match = _STEP_RE.match(raw)
    if match:
        return int(match.group(1)) * _STEP_MULT[match.group(2).lower()]
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return 0


def _restart_if_needed(before: bool, after: bool) -> None:
    """Перезапустить поток, если включение изменилось; иначе разбудить."""
    if before != after:
        worker.restart_worker()
    else:
        worker.wake()


@router.get("/settings")
def read_settings(user=Depends(require_admin), db: Session = Depends(get_db)):
    """Параметры подключения без секрета (ТЗ, раздел 7)."""
    row = store.get_settings(db)
    return {"ok": True, "settings": row.public_dict(),
            "auth_types": list(AUTH_TYPES),
            "interfaces": ["public", "internal", "admin"],
            "interval_min_sec": INTERVAL_MIN_SEC,
            "interval_max_sec": INTERVAL_MAX_SEC,
            "sdk": {"available": connection.sdk_available()[0]}}


@router.post("/settings")
def save_settings(body: SettingsBody, user=Depends(require_admin),
                  db: Session = Depends(get_db)):
    """Сохранить параметры (ТЗ, раздел 4, кнопка «Сохранить»)."""
    row = store.get_settings(db)
    was_enabled = bool(row.enabled)
    payload = body.model_dump()
    row, problems = store.apply_settings(db, payload)
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))

    log_action(user, "openstack_settings_saved", entity_type="openstack",
               entity_id=row.id, details={
                   "enabled": bool(row.enabled),
                   "interval_sec": row.interval_sec,
                   "history_days": row.history_days,
                   "auth_type": row.auth_type,
                   "secret_changed": bool(payload.get("secret")),
               })
    _restart_if_needed(was_enabled, bool(row.enabled))
    return {"ok": True, "settings": row.public_dict(),
            "worker_alive": worker.worker_is_alive()}


@router.post("/test")
def test_connection(body: TestBody, user=Depends(require_admin),
                    db: Session = Depends(get_db)):
    """Проверить подключение (ТЗ, раздел 4, кнопка «Проверить подключение»).

    Пустой секрет означает «взять сохранённый»: администратор не обязан
    вводить пароль заново только чтобы проверить уже работающее подключение.
    """
    payload = body.model_dump()
    if not payload.get("secret"):
        # Секрет не введён — берём сохранённый: администратор не обязан
        # вводить его заново, чтобы проверить уже работающее подключение.
        row = store.get_settings(db)
        stored = store.connection_cfg(row)
        payload["secret"] = stored["secret"]
        payload["password"] = stored["password"]
        payload["app_cred_secret"] = stored["app_cred_secret"]
        if not payload.get("app_cred_id"):
            payload["app_cred_id"] = stored["app_cred_id"]

    result = connection.test_connection(payload)
    log_action(user, "openstack_test", entity_type="openstack", details={
        "ok": bool(result.get("ok")),
        "error_code": result.get("error_code", ""),
        "services": sorted(k for k, v in (result.get("services") or {}).items()
                           if v == "доступен"),
    })
    store.write_log(db, "INFO" if result.get("ok") else "ERROR",
                    ("проверка подключения пройдена" if result.get("ok")
                     else f"проверка подключения не пройдена: "
                          f"{result.get('error', '')}"),
                    result.get("error_code", ""))
    return {"ok": bool(result.get("ok")), **result}


@router.post("/disable")
def disable_monitoring(user=Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Отключить мониторинг (ТЗ, раздел 4, кнопка «Отключить мониторинг»).

    Поток останавливается сразу; собранные данные и журнал сохраняются.
    """
    row = store.get_settings(db)
    row.enabled = False
    row.updated_at = _utcnow()
    db.commit()
    worker.stop_worker()
    store.write_log(db, "INFO", "мониторинг отключён администратором")
    log_action(user, "openstack_disabled", entity_type="openstack",
               entity_id=row.id, details={})
    return {"ok": True, "enabled": False, "worker_alive": worker.worker_is_alive(),
            "settings": row.public_dict()}


@router.get("/metrics/current")
def metrics_current(user=Depends(require_admin), db: Session = Depends(get_db)):
    """Последний снимок в формате ТЗ, раздел 7."""
    row = store.get_settings(db)
    last = store.latest_metric(db)
    if last is None:
        empty = {"collected_at": None, "vcpus_total": 0, "vcpus_used": 0,
                 "ram_total_mb": 0, "ram_used_mb": 0, "disk_total_gb": 0,
                 "disk_used_gb": 0, "traffic_in_bytes": None,
                 "traffic_out_bytes": None, "quotas": {}}
        view = collector.current_view(empty, thresholds_of(row))
        return {"ok": True, "available": False, **view,
                "message": "снимков пока нет: monitoring не выполнялся"}

    quotas = {}
    try:
        raw = json.loads(last.raw_json or "{}")
        quotas = raw.get("quotas") or {}
    except ValueError:
        quotas = {}
    snapshot = {**last.public_dict(), "quotas": quotas,
                "collected_at": last.collected_at}
    view = collector.current_view(snapshot, thresholds_of(row))
    return {"ok": True, "available": True, **view,
            "status": store.status_of(db)}


@router.get("/metrics/history")
def metrics_history(from_: str = Query("", alias="from"), to: str = "",
                    step: str = "",
                    period: str = "", user=Depends(require_admin),
                    db: Session = Depends(get_db)):
    """История снимков за период (ТЗ, разделы 7 и 9).

    Период задаётся либо парой from/to, либо коротким именем 1h, 24h, 7d,
    30d — ровно четыре кнопки вкладки «Показатели». Шаг укрупнения можно
    не указывать: для каждого периода подобран свой.
    """
    row = store.get_settings(db)
    now = _utcnow()
    key = (period or "").strip().lower()
    if key in PERIOD_STEPS and not from_:
        span = {"1h": timedelta(hours=1), "24h": timedelta(hours=24),
                "7d": timedelta(days=7), "30d": timedelta(days=30)}[key]
        since = now - span
        step_sec = _parse_step(step) or PERIOD_STEPS[key]
    else:
        since = _parse_moment(from_, now - timedelta(days=DEFAULT_HISTORY_DAYS))
        step_sec = _parse_step(step)

    until = _parse_moment(to, now)
    if until <= since:
        until = now
    if (until - since) > timedelta(days=MAX_RANGE_DAYS):
        since = until - timedelta(days=MAX_RANGE_DAYS)

    points = store.metric_history(db, since, until, step_sec)
    return {"ok": True, "from": since.isoformat(), "to": until.isoformat(),
            "step_sec": step_sec, "period": key or None,
            "count": len(points), "points": points,
            "thresholds": thresholds_of(row)}


@router.get("/status")
def contour_status(user=Depends(require_admin), db: Session = Depends(get_db)):
    """Состояние контура (ТЗ, раздел 7): включён, успех, последняя ошибка."""
    return {"ok": True, **store.status_of(db),
            "worker_alive": worker.worker_is_alive(),
            "worker_off_by_env": worker.off_by_env(),
            "sdk_available": connection.sdk_available()[0]}


@router.get("/logs")
def contour_logs(limit: int = 100, level: str = "",
                 user=Depends(require_admin), db: Session = Depends(get_db)):
    """Журнал синхронизации, новые сверху (ТЗ, раздел 10)."""
    rows = store.recent_logs(db, limit=limit, level=(level or "").upper())
    return {"ok": True, "count": len(rows), "level": (level or "").upper(),
            "logs": rows}



