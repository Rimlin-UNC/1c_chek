# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — подключение к OpenStack (v1.70.51).
# Техническое задание: docs/Monitor.md, разделы 6.1, 6.3 и 11.
#
# Импорт SDK ленивый: пока администратор не сохранил параметры Keystone,
# программа openstacksdk не загружает вовсе. Поэтому отсутствие библиотеки
# в экзотическом окружении не мешает запуску — блок мониторинга просто
# сообщает об этом в интерфейс, а остальная программа работает как раньше.
#
# Секреты маскируются в любом тексте, который уходит в журнал или в ответ:
# ни пароль, ни секрет учётных данных приложения, ни токен Keystone не
# должны покидать процесс (ТЗ, раздел 11).
#
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

from .models import AUTH_APP_CRED, AUTH_TYPES, INTERFACES

# Маска вместо секрета в журнале и в сообщениях об отказе.
MASK = "***"

# Начало предупреждения о незащищённом канале. По этому префиксу
# hard_problems отличает сведение для администратора от жёсткого замечания:
# раньше префикс был продублирован литералом внутри test_connection.
TLS_NOTE = "адрес Keystone без TLS"

# Сервисы, доступность которых блок показывает администратору после
# проверки подключения (ТЗ, раздел 4: «список доступных сервисов»).
PROBED_SERVICES = ("compute", "placement", "load_balancer", "metric",
                   "network", "image", "volume")


def sdk_available() -> tuple[bool, str]:
    """Доступен ли openstacksdk. → (доступен, пояснение)."""
    try:
        import openstack  # noqa: F401
        from keystoneauth1 import session  # noqa: F401
        from keystoneauth1.identity import v3  # noqa: F401
    except ImportError as exc:
        return False, f"библиотека openstacksdk не установлена: {exc}"
    return True, ""


SECRET_KEYS = ("secret", "password", "app_cred_secret")


def secrets_of(cfg: dict) -> tuple:
    """Все значения секретов из словаря подключения — для маскирования.

    Ключей два (password и app_cred_secret, как в примере ТЗ), плюс общий
    secret из формы: маскировать нужно каждый, иначе значение может уйти
    в журнал вместе с текстом ошибки.
    """
    return tuple(str(cfg.get(key) or "") for key in SECRET_KEYS)


def secret_of_cfg(cfg: dict) -> str:
    """Секрет, относящийся к выбранному способу аутентификации."""
    if cfg.get("auth_type") == AUTH_APP_CRED:
        return str(cfg.get("app_cred_secret") or cfg.get("secret") or "")
    return str(cfg.get("password") or cfg.get("secret") or "")


def mask_secret(text: str, *secrets: str) -> str:
    """Заменить секрет на маску в любом тексте.

    Текст ошибки из SDK может содержать значение пароля или токен; прежде
    чем писать его в журнал или показывать администратору, секрет
    вырезается. Пустые значения не заменяются — иначе маска расползлась бы
    по всему тексту.
    """
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, MASK)
    return out


def validate_cfg(cfg: dict) -> list[str]:
    """Проверить параметры подключения до попытки соединения.

    Возвращает список замечаний на русском: пустой список означает, что
    форма заполнена правильно. Проверка нужна, чтобы отказ был понятным,
    а не сводился к исключению из глубин SDK.
    """
    problems: list[str] = []
    auth_url = str(cfg.get("auth_url") or "").strip()
    if not auth_url:
        problems.append("не задан адрес Keystone (auth_url)")
    elif not auth_url.startswith(("http://", "https://")):
        problems.append("адрес Keystone должен начинаться с http:// или https://")

    auth_type = cfg.get("auth_type")
    if auth_type not in AUTH_TYPES:
        problems.append("неизвестный способ аутентификации: "
                        f"{auth_type!r}; допустимы {', '.join(AUTH_TYPES)}")

    interface = cfg.get("interface")
    if interface not in INTERFACES:
        problems.append("недопустимый интерфейс каталога: "
                        f"{interface!r}; допустимы {', '.join(INTERFACES)}")

    if auth_type == AUTH_APP_CRED:
        if not str(cfg.get("app_cred_id") or "").strip():
            problems.append("не задан идентификатор учётных данных приложения")
        if not secret_of_cfg(cfg):
            problems.append("не задан секрет учётных данных приложения")
    else:
        if not str(cfg.get("username") or "").strip():
            problems.append("не задано имя пользователя")
        if not secret_of_cfg(cfg):
            problems.append("не задан пароль")
        if not str(cfg.get("project_name") or "").strip():
            problems.append("не задан проект (project_name)")

    # v1.70.53: домен допускается задавать именем ИЛИ идентификатором —
    # файл провайдера может содержать только OS_PROJECT_DOMAIN_ID.
    for axis, name_key, id_key in (
            ("пользователя", "user_domain_name", "user_domain_id"),
            ("проекта", "project_domain_name", "project_domain_id")):
        if not (str(cfg.get(name_key) or "").strip()
                or str(cfg.get(id_key) or "").strip()):
            problems.append(f"не задан домен {axis}: нужно имя "
                            f"({name_key}) или идентификатор ({id_key})")
    if not str(cfg.get("region_name") or "").strip():
        problems.append("не задано поле region_name")

    try:
        timeout = int(cfg.get("timeout_sec") or 0)
    except (TypeError, ValueError):
        problems.append("таймаут должен быть целым числом секунд")
        timeout = 0
    if timeout <= 0:
        problems.append("таймаут должен быть больше нуля")

    if auth_url.startswith("http://") and cfg.get("verify_tls", True):
        # Не ошибка, но администратор должен видеть, что канал не шифруется.
        problems.append(f"{TLS_NOTE}: параметры передачи "
                        "доступны всем, кто видит этот трафик")
    return problems


def hard_problems(cfg: dict) -> list[str]:
    """Замечания, с которыми подключение в принципе невозможно.

    Предупреждение о незащищённом адресе сюда не входит: оно служит сведению
    администратора, а не поводом отказать. Без этого разделения внутренний
    Keystone по http:// нельзя было бы сохранить вовсе, хотя раздел 11 ТЗ
    требует явного предупреждения, а не запрета.
    """
    return [item for item in validate_cfg(cfg)
            if not item.startswith(TLS_NOTE)]


def build_connection(cfg: dict):
    """Собрать подключение к OpenStack (ТЗ, раздел 6.1).

    Оба способа аутентификации из ТЗ: пароль и application credential.
    Функция не ловит исключения намеренно — вызывающий код решает, как
    представить отказ администратору.
    """
    import openstack
    from keystoneauth1 import session
    from keystoneauth1.identity import v3

    auth_type = cfg.get("auth_type")
    if auth_type == AUTH_APP_CRED:
        # v1.70.53: «Проверить подключение» с только что введённым секретом
        # приходит без ключа app_cred_secret (форма отправляет его под именем
        # secret) — раньше здесь падал KeyError вместо проверки.
        auth = v3.ApplicationCredential(
            auth_url=cfg["auth_url"],
            application_credential_id=cfg["app_cred_id"],
            application_credential_secret=(cfg.get("app_cred_secret")
                                           or cfg.get("secret") or ""),
        )
    else:
        # v1.70.53: домен может быть задан идентификатором — так его отдаёт
        # файл openrc.sh из панелей провайдеров (OS_PROJECT_DOMAIN_ID). У
        # провайдера имя домена может не совпадать с «Default» буквально, и
        # тогда Keystone отвечает безликим HTTP 401. Идентификатор имеет
        # приоритет над именем — как в keystoneauth.
        user_domain: dict[str, str] = {}
        if str(cfg.get("user_domain_id") or "").strip():
            user_domain["user_domain_id"] = str(cfg["user_domain_id"]).strip()
        else:
            user_domain["user_domain_name"] = cfg["user_domain_name"]
        project_domain: dict[str, str] = {}
        if str(cfg.get("project_domain_id") or "").strip():
            project_domain["project_domain_id"] = \
                str(cfg["project_domain_id"]).strip()
        else:
            project_domain["project_domain_name"] = cfg["project_domain_name"]
        auth = v3.Password(
            auth_url=cfg["auth_url"],
            username=cfg["username"],
            password=cfg.get("password") or cfg.get("secret") or "",
            project_name=cfg["project_name"],
            **user_domain,
            **project_domain,
        )
    sess = session.Session(auth=auth,
                           verify=bool(cfg.get("verify_tls", True)),
                           timeout=int(cfg.get("timeout_sec") or 30))
    return openstack.connection.Connection(
        session=sess,
        region_name=cfg.get("region_name") or None,
        interface=cfg.get("interface") or "public",
    )


def _catalog_entries(conn) -> list[dict]:
    """Сырой каталог сервисов Keystone: список словарей type/endpoints.

    v1.70.54: прежде здесь перебирали сам объект ServiceCatalogV3 — он не
    итерируется, исключение глушилось, и каталог не читался ВОВСЕ: значки
    «доступен» определялись наличием прокси, а не записями провайдера.
    Список сервисов лежит в атрибуте catalog; подставные подключения без
    сессии (тесты) дают пустой список, а не исключение.
    """
    try:
        access = conn.session.auth.get_access(conn.session)
        entries = getattr(getattr(access, "service_catalog", None),
                          "catalog", None)
        if isinstance(entries, list):
            return [item for item in entries if isinstance(item, dict)]
    except Exception:                                      # noqa: BLE001
        return []
    return []


def _catalog_types(conn) -> list[str]:
    """Типы сервисов из каталога Keystone — по возможности, без падения."""
    return sorted({str(item["type"]) for item in _catalog_entries(conn)
                   if item.get("type")})


def service_endpoints(conn, service_type: str) -> list[dict]:
    """Эндпоинты одного сервиса из каталога; пустой список, если сервиса нет."""
    for entry in _catalog_entries(conn):
        if entry.get("type") == service_type:
            endpoints = entry.get("endpoints")
            if isinstance(endpoints, list):
                return [item for item in endpoints if isinstance(item, dict)]
            return []
    return []


def _disabled_reason(proxy) -> str:
    """SDK подменил прокси заглушкой отключённого сервиса → причина.

    Заглушка _ServiceDisabledProxyShim не бросает исключение при создании —
    она бросает его при ПЕРВОМ же обращении к методу, поэтому значок
    «доступен» раньше был ложью. Атрибуты service_type и reason у заглушки
    собственные: __getattr__ срабатывает только для недостающих имён, и
    чтение причины безопасно.
    """
    if type(proxy).__name__ != "_ServiceDisabledProxyShim":
        return ""
    return str(getattr(proxy, "reason", "") or "")


def _catalog_mismatch(conn, cfg: dict) -> str:
    """Каталог не совпал с выбранными регионом/интерфейсом → сведение.

    Жалоба владельца v1.70.53: Keystone пройден, а Nova ответила
    ServiceDisabledException «admin endpoint for compute service in fi
    region not found» — в сохранённых настройках лежали прежние регион и
    интерфейс, а файл провайдера задаёт другие. Без сверки с каталогом
    администратор видит только сторонний текст отказа.
    """
    endpoints = service_endpoints(conn, "compute")
    if not endpoints:
        return ""
    region = str(cfg.get("region_name") or "").strip()
    interface = str(cfg.get("interface") or "public").strip()
    for item in endpoints:
        if interface and item.get("interface") != interface:
            continue
        if region and region not in (item.get("region"), item.get("region_id")):
            continue
        return ""
    regions = sorted({str(item.get("region") or item.get("region_id") or "")
                      for item in endpoints} - {""})
    interfaces = sorted({str(item.get("interface") or "")
                         for item in endpoints} - {""})
    where = f" в регионе «{region}»" if region else ""
    return ("Keystone пройден, но в каталоге облака нет сервиса compute "
            f"для интерфейса «{interface}»{where}. Каталог предлагает "
            "регионы: " + (", ".join(regions) or "—") + "; интерфейсы: "
            + (", ".join(interfaces) or "—") + ". Регион и интерфейс нужно "
            "брать из файла провайдера: строки OS_REGION_NAME и "
            "OS_INTERFACE, поля «Регион» и «Интерфейс каталога» на вкладке "
            "«Подключение».")


def _service_names(conn, region_name: str = "",
                   interface: str = "") -> dict[str, str]:
    """Доступность сервисов: имя → «доступен» / причина недоступности.

    Каталог Keystone читается первым; если он недоступен (например, права
    не позволяют), доступность определяется по наличию прокси сервиса
    в SDK. Проверка не выполняет сетевых запросов, поэтому остаётся быстрой.

    v1.70.54: статус сверяется с выбранными регионом и интерфейсом — сервис
    из каталога без эндпоинта нужной пары больше не значится «доступен», а
    заглушка отключённого сервиса отдаёт свою причину вместо лжи.
    """
    out: dict[str, str] = {}
    catalog = set(_catalog_types(conn))
    for name in PROBED_SERVICES:
        if catalog:
            if name not in catalog:
                out[name] = "нет в каталоге"
                continue
            if region_name or interface:
                suitable = [
                    item for item in service_endpoints(conn, name)
                    if (not interface or item.get("interface") == interface)
                    and (not region_name or region_name
                         in (item.get("region"), item.get("region_id")))
                ]
                if not suitable:
                    out[name] = ("в каталоге, но не для выбранных региона "
                                 "и интерфейса")
                    continue
            out[name] = "доступен"
            continue
        try:
            proxy = getattr(conn, name, None)
            if proxy is None:
                out[name] = "не подключён"
                continue
            reason = _disabled_reason(proxy)
            out[name] = (f"недоступен: {reason}" if reason else "доступен")
        except Exception:                                  # noqa: BLE001
            out[name] = "не подключён"
    return out


def classify_error(exc: BaseException) -> str:
    """Код ошибки для журнала (ТЗ, раздел 8: поле error_code).

    Код короткий и устойчивый: по нему администратор ищет причину, а в
    журнал не попадает текст с деталями аутентификации.
    """
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    lowered = f"{module}.{name}".lower()
    text = str(exc).lower()
    if "timeout" in lowered or "timeout" in text:
        return "timeout"
    if "connectionerror" in lowered or "connection" in lowered:
        return "connection"
    # v1.70.54: отказ каталога (регион/интерфейс/сервис) — ДО ветки «auth»:
    # имя модуля keystoneauth1 содержит «auth» и иначе перехватило бы
    # EndpointNotFound себе.
    if "servicedisabled" in lowered or "endpointnotfound" in lowered:
        return "catalog"
    if "unauthorized" in lowered or "forbidden" in lowered:
        return "auth_denied"
    # v1.70.53: openstacksdk заворачивает отказ Keystone в SDKException, и
    # безликий «HTTP 401» от провайдера раньше получал бесполезный код «sdk».
    if "(http 401)" in text or "(http 403)" in text:
        return "auth_denied"
    if "auth" in lowered:
        return "auth"
    if "notfound" in lowered:
        return "not_found"
    if "sdkexception" in lowered:
        return "sdk"
    return name[:60] or "error"


def test_connection(cfg: dict) -> dict:
    """Проверить подключение (ТЗ, раздел 4).

    Последовательность из ТЗ: пробный запрос к Keystone, затем к Nova
    (get_hypervisor_statistics). Возвращает ok/error, текст ошибки и
    список доступных сервисов. Секрет в ответ не попадает.
    """
    secrets = secrets_of(cfg)
    ok, why = sdk_available()
    if not ok:
        return {"ok": False, "error": why, "error_code": "no_sdk",
                "services": {}, "authorized": False, "hypervisor": None}

    problems = validate_cfg(cfg)
    hard = hard_problems(cfg)
    if hard:
        return {"ok": False, "error": "; ".join(hard), "error_code": "form",
                "services": {}, "authorized": False, "hypervisor": None,
                "warnings": problems}

    conn = None
    try:
        conn = build_connection(cfg)
        conn.authorize()
    except Exception as exc:                               # noqa: BLE001
        return {"ok": False,
                "error": mask_secret(f"{type(exc).__name__}: {exc}", *secrets),
                "error_code": classify_error(exc),
                "services": {}, "authorized": False, "hypervisor": None,
                "warnings": problems}

    services = _service_names(conn, str(cfg.get("region_name") or "").strip(),
                              str(cfg.get("interface") or "").strip())

    # v1.70.54: сверить регион и интерфейс с каталогом РАНЬШЕ обращения к
    # Nova — иначе отказ приходит посторонним текстом ServiceDisabledException.
    mismatch = _catalog_mismatch(conn, cfg)
    if mismatch:
        return {"ok": False, "error": mismatch, "error_code": "catalog",
                "services": services, "authorized": True, "hypervisor": None,
                "warnings": problems}

    stats = None
    try:
        raw = conn.compute.get_hypervisor_statistics()
        stats = _as_plain(raw)
    except Exception as exc:                               # noqa: BLE001
        return {"ok": False,
                "error": mask_secret(f"Keystone пройден, но Nova отказала: "
                                     f"{type(exc).__name__}: {exc}", *secrets),
                "error_code": classify_error(exc),
                "services": services, "authorized": True, "hypervisor": None,
                "warnings": problems}

    return {"ok": True, "error": "", "error_code": "",
            "services": services, "authorized": True, "hypervisor": stats,
            "warnings": problems}


def _as_plain(obj) -> dict:
    """Ресурс SDK → обычный словарь: в JSON нельзя отдать объект прокси."""
    out: dict = {}
    for key in ("vcpus", "vcpus_used", "memory_mb", "memory_mb_used",
                "free_ram_mb", "local_gb", "local_gb_used", "free_disk_gb",
                "current_workload", "running_vms", "disk_available_least"):
        value = getattr(obj, key, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(key)
        if value is not None:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = value
    return out


def close_connection(conn) -> bool:
    """Закрыть сессию, если SDK это умеет. → закрыта ли сессия.

    Отказ закрытия не страшен: сбор уже завершён, а соединение всё равно
    разрывается вместе с потоком. Возвращаемое значение нужно, чтобы в теле
    был смысл, а не пустая ветка.
    """
    try:
        close = getattr(getattr(conn, "session", None), "close", None)
        if close is None:
            return False
        close()
        return True
    except Exception:                                      # noqa: BLE001
        return False
