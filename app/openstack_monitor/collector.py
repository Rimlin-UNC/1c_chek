# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сбор показателей OpenStack (v1.70.51).
# Техническое задание: docs/Monitor.md, разделы 6.2, 6.4 и 7.
#
# Порядок сбора — точно как в ТЗ: статистика гипервизоров, квоты проекта,
# инвентарь Placement (только если задан resource_provider_uuid) и трафик.
# Каждый шаг отделён: отсутствие сервиса не роняет весь сбор, а только
# оставляет своё поле пустым — критерий приёмки 7 требует «traffic = null
# без падения».
#
# Секреты здесь не появляются: сборщик работает с уже построенным
# подключением и в журнал пишет только числа и имена сервисов.
#
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
from datetime import datetime

from ..models import iso_z
from . import connection
from .models import DEFAULT_THRESHOLDS, _utcnow

# Пределы чтения списков: сбор не должен вычитывать тысячи объектов.
MAX_LOAD_BALANCERS = 50

# Имена полей квот в ответе Nova (ТЗ, раздел 6.2).
QUOTA_FIELDS = {
    "max_cores": "maxTotalCores",
    "used_cores": "totalCoresUsed",
    "max_ram_mb": "maxTotalRAMSize",
    "used_ram_mb": "totalRAMUsed",
    "max_instances": "maxTotalInstances",
    "used_instances": "totalInstancesUsed",
}

def pct(used, total) -> float:
    """Доля использованного в процентах; ноль вместо деления на ноль."""
    try:
        used_f = float(used or 0)
        total_f = float(total or 0)
    except (TypeError, ValueError):
        return 0.0
    if total_f <= 0:
        return 0.0
    return round(used_f / total_f * 100.0, 2)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def hypervisor_stats(conn) -> tuple[dict, str]:
    """Статистика гипервизоров Nova. → (числа, причина отказа).

    v1.70.56: сводка берётся через connection.hypervisor_totals — в
    openstacksdk 4.x метода get_hypervisor_statistics нет (отказ владельца:
    AttributeError 'Proxy' object has no attribute), документированная
    замена — сумма полей hypervisors(details=True). Форма ответа прежняя:
    проводные имена vcpus/memory_mb/local_gb, пороги и история не меняются.
    """
    try:
        raw = connection.hypervisor_totals(conn)
    except Exception as exc:                               # noqa: BLE001
        return {}, connection.exc_summary(exc)
    out = {}
    for key in ("vcpus", "vcpus_used", "memory_mb", "memory_mb_used",
                "free_ram_mb", "local_gb", "local_gb_used", "free_disk_gb",
                "running_vms", "current_workload"):
        value = raw.get(key)
        if value is not None:
            out[key] = _as_int(value)
    return out, ""


def _absolute_map(limits) -> dict:
    """absolute из Nova приходит списком пар или словарём — берём и то и то."""
    absolute = getattr(limits, "absolute", None)
    if absolute is None and isinstance(limits, dict):
        absolute = limits.get("absolute")
    out: dict = {}
    if isinstance(absolute, dict):
        for name, value in absolute.items():
            out[str(name)] = value
        return out
    if isinstance(absolute, (list, tuple)):
        for entry in absolute:
            if isinstance(entry, dict) and "name" in entry:
                out[str(entry["name"])] = entry.get("value")
    return out


def quota_limits(conn) -> tuple[dict, str]:
    """Квоты проекта (ТЗ, раздел 6.2, шаг 2). → (числа, причина отказа)."""
    try:
        limits = conn.compute.get_limits()
    except Exception as exc:                               # noqa: BLE001
        return {}, connection.exc_summary(exc)
    absolute = _absolute_map(limits)
    out: dict = {}
    for short, nova_name in QUOTA_FIELDS.items():
        if nova_name in absolute:
            out[short] = _as_int(absolute[nova_name], -1)
    return out, ""


def placement_inventories(conn, cfg: dict) -> tuple[dict | None, str]:
    """Инвентарь Placement — только если задан resource_provider_uuid.

    ТЗ, раздел 6.2, шаг 3: обращение идёт по адресу
    /resource_providers/<uuid>/inventories. Если uuid не задан, шаг
    пропускается — это не ошибка.
    """
    rp_uuid = str(cfg.get("resource_provider_uuid") or "").strip()
    if not rp_uuid:
        return None, ""
    try:
        raw = conn.placement.get(f"/resource_providers/{rp_uuid}/inventories")
    except Exception as exc:                               # noqa: BLE001
        return None, connection.exc_summary(exc)
    if isinstance(raw, dict):
        return raw, ""
    # v1.70.57: сырой запрос openstacksdk 4.x возвращает объект Response,
    # а не словарь — прежде он проваливался в json.dumps(default=str) и
    # «разбирался» в строку "<Response [200]>". Документированный способ
    # прочитать тело — его json().
    body_json = getattr(raw, "json", None)
    if callable(body_json):
        try:
            parsed = body_json()
        except ValueError as exc:
            return None, f"не удалось разобрать ответ Placement: {exc}"
        if isinstance(parsed, dict):
            return parsed, ""
    # Прокси может вернуть объект ресурса — приводим к словарю.
    body = getattr(raw, "body", None) or getattr(raw, "_body", None)
    if isinstance(body, dict):
        return body, ""
    try:
        return json.loads(json.dumps(raw, default=str)), ""
    except (TypeError, ValueError) as exc:
        return None, f"не удалось разобрать ответ Placement: {exc}"


def traffic_bytes(conn) -> tuple[int | None, int | None, str]:
    """Трафик через Octavia. → (в байтах внутрь, наружу, пояснение).

    ТЗ, раздел 6.2, шаг 4: если сервиса нет — пропустить и вернуть пустые
    значения без падения. Поля bytes_in и bytes_out — это статистика
    балансировщика Octavia, других источников трафика ТЗ не называет.
    """
    proxy = getattr(conn, "load_balancer", None)
    if proxy is None:
        return None, None, "сервис балансировщиков не подключён"
    try:
        balancers = list(proxy.load_balancers())[:MAX_LOAD_BALANCERS]
    except Exception as exc:                               # noqa: BLE001
        return None, None, f"список балансировщиков недоступен: {exc}"
    if not balancers:
        return None, None, "в проекте нет балансировщиков"

    # v1.70.57: метода по-балансировочной статистики в openstacksdk 4.x
    # НЕТ (замер: у Proxy отсутствует «statistics») — шаг вечно кончался
    # AttributeError. Документированная статистика Octavia — сквозной
    # запрос GET /v2/octavia/statistics (bytes_in, bytes_out); он и
    # используется, когда родного метода в библиотеке нет.
    statistics = getattr(proxy, "statistics", None)
    if not callable(statistics):
        try:
            resp = proxy.get("octavia/statistics")
        except Exception as exc:                           # noqa: BLE001
            return None, None, ("статистика балансировщиков недоступна: "
                                + connection.exc_summary(exc))
        data = resp if isinstance(resp, dict) else None
        if data is None:
            resp_json = getattr(resp, "json", None)
            if callable(resp_json):
                try:
                    parsed = resp_json()
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
        if data is None:
            return None, None, "ответ статистики балансировщиков не разобран"
        return (_as_int(data.get("bytes_in")),
                _as_int(data.get("bytes_out")),
                "сквозная статистика балансировщиков")

    total_in = 0
    total_out = 0
    counted = 0
    last_error = ""
    for lb in balancers:
        lb_id = getattr(lb, "id", None) or (lb.get("id")
                                            if isinstance(lb, dict) else None)
        if not lb_id:
            continue
        try:
            stats = proxy.statistics(lb_id)
        except Exception as exc:                           # noqa: BLE001
            last_error = connection.exc_summary(exc)
            continue
        raw_in = getattr(stats, "bytes_in", None)
        raw_out = getattr(stats, "bytes_out", None)
        if raw_in is None and isinstance(stats, dict):
            raw_in, raw_out = stats.get("bytes_in"), stats.get("bytes_out")
        if raw_in is None and raw_out is None:
            continue
        total_in += _as_int(raw_in)
        total_out += _as_int(raw_out)
        counted += 1
    if counted == 0:
        return None, None, (last_error or "статистика балансировщиков пуста")
    return total_in, total_out, f"собрано с балансировщиков: {counted}"


def collect_metrics(conn, cfg: dict) -> dict:
    """Собрать один снимок показателей (ТЗ, раздел 6.2, шаг 5).

    Возвращает нормализованный словарь: шесть основных чисел, трафик
    (может быть пустым), квоты и raw_json со всем, что удалось снять.
    Отказ отдельного шага не отменяет снимок — в raw_json остаётся причина.
    """
    stats, stats_err = hypervisor_stats(conn)
    quotas, quotas_err = quota_limits(conn)
    inventories, inv_err = placement_inventories(conn, cfg)
    traffic_in, traffic_out, traffic_note = traffic_bytes(conn)

    snapshot: dict = {
        "collected_at": _utcnow(),
        "vcpus_total": stats.get("vcpus", 0),
        "vcpus_used": stats.get("vcpus_used", 0),
        "ram_total_mb": stats.get("memory_mb", 0),
        "ram_used_mb": stats.get("memory_mb_used", 0),
        "disk_total_gb": stats.get("local_gb", 0),
        "disk_used_gb": stats.get("local_gb_used", 0),
        "traffic_in_bytes": traffic_in,
        "traffic_out_bytes": traffic_out,
        "quotas": quotas,
        "placement_inventories": inventories,
        "notes": {
            "hypervisor_error": stats_err,
            "quota_error": quotas_err,
            "placement_error": inv_err,
            "traffic_note": traffic_note,
        },
    }
    snapshot["raw_json"] = json.dumps(
        {k: v for k, v in snapshot.items() if k != "raw_json"},
        ensure_ascii=False, default=str)
    return snapshot


def current_view(snapshot: dict, thresholds: dict | None = None) -> dict:
    """Снимок → формат ответа metrics/current (ТЗ, раздел 7)."""
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    cpu_total = _as_int(snapshot.get("vcpus_total"))
    cpu_used = _as_int(snapshot.get("vcpus_used"))
    ram_total = _as_int(snapshot.get("ram_total_mb"))
    ram_used = _as_int(snapshot.get("ram_used_mb"))
    disk_total = _as_int(snapshot.get("disk_total_gb"))
    disk_used = _as_int(snapshot.get("disk_used_gb"))
    quotas = snapshot.get("quotas") or {}
    collected_at = snapshot.get("collected_at")
    if isinstance(collected_at, datetime):
        collected_at = iso_z(collected_at)

    traffic_in = snapshot.get("traffic_in_bytes")
    traffic_out = snapshot.get("traffic_out_bytes")
    traffic = None
    if traffic_in is not None or traffic_out is not None:
        traffic = {"in_bytes": traffic_in, "out_bytes": traffic_out}

    view = {
        "collected_at": collected_at,
        "cpu": {"total": cpu_total, "used": cpu_used,
                "pct": pct(cpu_used, cpu_total)},
        "ram": {"total_mb": ram_total, "used_mb": ram_used,
                "free_mb": max(ram_total - ram_used, 0),
                "pct": pct(ram_used, ram_total)},
        "disk": {"total_gb": disk_total, "used_gb": disk_used,
                 "free_gb": max(disk_total - disk_used, 0),
                 "pct": pct(disk_used, disk_total)},
        "quotas": {
            "max_cores": quotas.get("max_cores"),
            "used_cores": quotas.get("used_cores"),
            "max_ram_mb": quotas.get("max_ram_mb"),
            "used_ram_mb": quotas.get("used_ram_mb"),
            "max_instances": quotas.get("max_instances"),
            "used_instances": quotas.get("used_instances"),
        },
        "traffic": traffic,
        "placement": snapshot.get("placement_inventories"),
    }
    # Предупреждения считаются от уже собранного представления: внутри
    # словаря-литерала имя view ещё не существует (F821).
    view["warnings"] = warnings_of(view, limits)
    return view


def warnings_of(view: dict, thresholds: dict | None = None) -> list[str]:
    """Сравнить проценты с порогами (ТЗ, раздел 6.4).

    Возвращает список предупреждений на русском: их показывает баннер на
    вкладке «Показатели». Порог 0 означает, что проверка отключена.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    out: list[str] = []
    pairs = (
        ("Процессор", view["cpu"]["pct"], limits["threshold_cpu_pct"]),
        ("Оперативная память", view["ram"]["pct"], limits["threshold_ram_pct"]),
        ("Диск", view["disk"]["pct"], limits["threshold_disk_pct"]),
    )
    for name, value, limit in pairs:
        if limit and limit > 0 and value >= limit:
            out.append(f"{name}: {value} % при пороге {limit} %")

    traffic = view.get("traffic") or {}
    limit_gb = limits.get("threshold_traffic_gb_day") or 0
    if limit_gb > 0 and traffic:
        total_gb = ((_as_int(traffic.get("in_bytes"))
                     + _as_int(traffic.get("out_bytes"))) / 1_073_741_824)
        if total_gb >= limit_gb:
            out.append(f"Трафик за сутки: {round(total_gb, 2)} ГБ "
                       f"при пороге {limit_gb} ГБ")
    return out
