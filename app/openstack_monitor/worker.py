# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — периодический опрос OpenStack (v1.70.51).
# Техническое задание: docs/Monitor.md, разделы 2, 5, 6.3 и 8.
#
# ТЗ допускает APScheduler, Celery beat или cron «по инфраструктуре».
# Ни одного из них в программе нет, а фоновые задачи уже решаются
# демоном-потоком: app/services/webhooks.py. Новый контур повторяет этот
# образец — тот же цикл, тот же стоп-сигнал, та же идемпотентность запуска,
# поэтому поведение программы остаётся предсказуемым.
#
# Сбор выполняется в своём потоке и не блокирует обработку запросов
# (критерий приёмки 6). Отказ облака не роняет воркер: ошибка пишется в
# журнал, last_error обновляется, а прежние снимки остаются на месте.
#
# Переменная окружения YM_OSMON_OFF=1 запрещает запуск воркера — так же,
# как YM_FETCHQ_OFF=1 в очереди чеков: тесты должны быть детерминированы.
#
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta

from . import collector, connection, store
from .models import (
    INTERVAL_MIN_SEC,
    LOG_INFO,
    LOG_WARN,
    OpenstackSettings,
    _utcnow,
    thresholds_of,
)

log = logging.getLogger("ymaster.openstack")

# Как часто поток просыпается, чтобы проверить, не пора ли собирать.
# Меньше INTERVAL_MIN_SEC — значит смена периода в настройках подхватывается
# сразу, а остановка воркера происходит без задержки.
TICK_SEC = 5.0

# Очистка истории — не чаще раза в час: удалять каждый цикл бессмысленно.
PRUNE_EVERY = timedelta(hours=1)

_worker: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()
_last_prune = None


def off_by_env() -> bool:
    """Предохранитель для тестов: YM_OSMON_OFF=1 запрещает запуск."""
    return os.environ.get("YM_OSMON_OFF", "").strip().lower() in ("1", "true",
                                                                  "yes", "on")


def _db_session():
    from ..database import SessionLocal
    return SessionLocal()


def worker_is_alive() -> bool:
    return _worker is not None and _worker.is_alive()


def is_enabled(db) -> bool:
    """Включён ли мониторинг в настройках."""
    try:
        return bool(store.get_settings(db).enabled)
    except Exception:                                      # noqa: BLE001
        # Таблиц ещё нет (сервис стартует до миграции) — считаем выключенным.
        return False


def is_due(db, row: OpenstackSettings) -> bool:
    """Пора ли собирать: сравниваем последний снимок с периодом опроса."""
    last = store.latest_metric(db)
    if last is None:
        return True
    interval = max(int(row.interval_sec or 0), INTERVAL_MIN_SEC)
    age = (_utcnow() - last.collected_at).total_seconds()
    return age >= interval


def collect_once(db, row: OpenstackSettings | None = None) -> dict:
    """Один цикл сбора. → результат для журнала и для проверки в тестах.

    Последовательность по ТЗ, раздел 6: построить подключение, собрать
    показатели, сохранить снимок, сравнить с порогами, обновить состояние,
    почистить историю. Любой отказ приводит к записи last_error, а не к
    падению потока.
    """
    global _last_prune
    if row is None:
        row = store.get_settings(db)
    cfg = store.connection_cfg(row)
    secrets = connection.secrets_of(cfg)
    conn = None
    try:
        conn = connection.build_connection(cfg)
        conn.authorize()
        snapshot = collector.collect_metrics(conn, cfg)
    except Exception as exc:                               # noqa: BLE001
        message = connection.mask_secret(
            f"{type(exc).__name__}: {exc}", *secrets)
        code = connection.classify_error(exc)
        store.mark_error(db, row, message, code)
        log.warning("openstack collection failed: %s", code)
        return {"ok": False, "error": message, "error_code": code,
                "warnings": [], "removed": 0}
    finally:
        if conn is not None:
            connection.close_connection(conn)

    store.write_metric(db, snapshot)
    view = collector.current_view(snapshot, thresholds_of(row))
    store.mark_success(db, row, "сбор выполнен: снимок сохранён")

    # Превышение порогов — отдельная запись журнала уровня WARN, чтобы
    # администратор видел причину в истории, а не только в баннере.
    for warning in view["warnings"]:
        store.write_log(db, LOG_WARN, f"превышен порог: {warning}")

    removed = 0
    now = _utcnow()
    if _last_prune is None or now - _last_prune >= PRUNE_EVERY:
        removed = store.prune_history(db, row)
        _last_prune = now
        store.write_log(db, LOG_INFO, store.describe_prune(removed))

    return {"ok": True, "error": "", "error_code": "",
            "warnings": view["warnings"], "removed": removed, "view": view}


def _worker_loop() -> None:
    while not _stop.is_set():
        _wake.wait(timeout=TICK_SEC)
        _wake.clear()
        if _stop.is_set():
            return
        db = _db_session()
        try:
            if not is_enabled(db):
                continue
            row = store.get_settings(db)
            if not is_due(db, row):
                continue
            collect_once(db, row)
        except Exception:                                  # noqa: BLE001
            log.exception("openstack monitor loop")
        finally:
            db.close()


def start_worker() -> bool:
    """Запустить поток опроса, если мониторинг включён. → запущен ли.

    Вызов идемпотентен: повторный запуск не создаёт второй поток. При
    выключенном мониторинге поток не создаётся вовсе — программа не несёт
    фоновой нагрузки за неиспользуемый контур.
    """
    global _worker
    if off_by_env():
        log.info("openstack monitor worker disabled by YM_OSMON_OFF")
        return False
    db = _db_session()
    try:
        enabled = is_enabled(db)
    finally:
        db.close()
    if not enabled:
        return False
    if _worker is not None and _worker.is_alive():
        return True
    _stop.clear()
    _wake.clear()
    _worker = threading.Thread(target=_worker_loop, daemon=True,
                               name="ymaster-openstack")
    _worker.start()
    log.info("openstack monitor worker started")
    return True


def stop_worker() -> None:
    _stop.set()
    _wake.set()


def restart_worker() -> bool:
    """Перезапустить поток после изменения настроек (период, включение)."""
    global _worker
    stop_worker()
    if _worker is not None:
        _worker.join(timeout=3)
    _worker = None
    return start_worker()


def wake() -> None:
    """Разбудить поток, чтобы смена периода подействовала без ожидания."""
    _wake.set()
