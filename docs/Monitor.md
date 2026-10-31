ТЗ: блок «Мониторинг OpenStack» в chek.ymaster.ru

1. Контекст и цель

В программе chek.ymaster.ru добавить отдельный блок в разделе «Настройки» — «Мониторинг OpenStack». Блок должен подключаться к OpenStack по API через Python SDK openstacksdk, периодически собирать основные показатели сервера (CPU, RAM, диск, квоты, сетевой трафик), хранить историю и визуализировать данные в интерфейсе программы.

Блок встраивается в существующий backend и frontend chek.ymaster.ru, использует текущую авторизацию/RBAC и не является отдельным приложением.

---

2. Технический стек

· Backend: Python 3.10+, фреймворк — тот, что уже используется в chek.ymaster.ru.
· SDK: openstacksdk (обязательно), keystoneauth1 для аутентификации.
· БД: существующая БД программы (PostgreSQL/MySQL/SQLite — по факту).
· Frontend: существующий стек chek.ymaster.ru (Vue/React/HTML+JS) + библиотека графиков (Chart.js / ECharts / Recharts).
· Планировщик: встроенный scheduler (APScheduler, Celery beat или cron — по инфраструктуре проекта).

Установка зависимостей:

```bash
pip install openstacksdk keystoneauth1
```

---

3. Структура блока в интерфейсе

Раздел Настройки → Мониторинг OpenStack, 4 вкладки:

1. Подключение — параметры Keystone и тест соединения.
2. Пороги и опрос — интервал сбора, глубина хранения, пороги предупреждений.
3. Показатели — текущие метрики и графики истории.
4. Журнал — статус синхронизации и ошибки.

Доступ — только роли openstack_monitor.manage (администратор).

---

4. Вкладка «Подключение»

Поля формы

Поле Тип Обяз. Описание
auth_url URL да Keystone endpoint, напр. https://<cloud>/identity/v3
auth_type select да password / application_credential
username string да* для password
password secret да* для password
app_cred_id string да** для application_credential
app_cred_secret secret да** для application_credential
project_name string да проект/tenant
user_domain_name string да обычно Default
project_domain_name string да обычно Default
region_name string да напр. RegionOne
interface select да public / internal / admin
verify_tls bool да по умолчанию true
timeout_sec int да по умолчанию 30
resource_provider_uuid string нет для Placement API (если нужен точный инвентарь)

* для password, ** для application_credential.

Кнопки

· Проверить подключение — выполняет пробный запрос к Keystone и к Nova (get_hypervisor_statistics).
· Сохранить — сохраняет настройки (секреты шифруются).
· Отключить мониторинг — выключает сбор.

Требования

· Секреты (password, app_cred_secret) хранятся зашифрованными (Fernet / AES-GCM / штатное шифрование проекта). В ответах API не возвращаются.
· Тест соединения возвращает: ok / error, текст ошибки, список доступных сервисов.

---

5. Вкладка «Пороги и опрос»

Поле По умолчанию
interval_sec — интервал опроса 60
history_days — глубина хранения 30
threshold_cpu_pct 80
threshold_ram_pct 80
threshold_disk_pct 80
threshold_traffic_gb_day — (опционально)

Валидация: интервал от 30 до 3600 сек.

---

6. Backend-модуль openstack_monitor

6.1. Подключение (пример)

```python
import openstack
from keystoneauth1.identity import v3
from keystoneauth1 import session

def build_connection(cfg: dict):
    if cfg["auth_type"] == "application_credential":
        auth = v3.ApplicationCredential(
            auth_url=cfg["auth_url"],
            application_credential_id=cfg["app_cred_id"],
            application_credential_secret=cfg["app_cred_secret"],
        )
    else:
        auth = v3.Password(
            auth_url=cfg["auth_url"],
            username=cfg["username"],
            password=cfg["password"],
            project_name=cfg["project_name"],
            user_domain_name=cfg["user_domain_name"],
            project_domain_name=cfg["project_domain_name"],
        )
    sess = session.Session(auth=auth, verify=cfg["verify_tls"], timeout=cfg["timeout_sec"])
    return openstack.connection.Connection(
        session=sess,
        region_name=cfg["region_name"],
        interface=cfg["interface"],
    )
```

6.2. Сборщик метрик

Собирать каждые interval_sec секунд задание планировщика:

```python
def collect_metrics(conn):
    # 1. Гипервизоры: CPU/RAM/Disk
    stats = conn.compute.get_hypervisor_statistics()
    # stats.vcpus, stats.vcpus_used
    # stats.memory_mb, stats.memory_mb_used, stats.free_ram_mb
    # stats.local_gb, stats.local_gb_used, stats.free_disk_gb

    # 2. Квоты проекта
    limits = conn.compute.get_limits()
    # limits.absolute: maxTotalRAMSize, maxTotalCores, maxTotalInstances,
    # totalRAMUsed, totalCoresUsed, totalInstancesUsed

    # 3. Placement — инвентарь (если задан resource_provider_uuid)
    rp_uuid = cfg.get("resource_provider_uuid")
    inventories = None
    if rp_uuid:
        inventories = conn.placement.get(f"/resource_providers/{rp_uuid}/inventories")

    # 4. Трафик — если есть Octavia/Gnocchi
    # conn.load_balancer.get(...) или conn.metric.* / conn.telemetry.*
    # Взять bytes_in / bytes_out; если сервисов нет — пропустить

    # 5. Нормализовать и записать в БД
    return {
        "collected_at": now_utc(),
        "vcpus_total": stats.vcpus,
        "vcpus_used": stats.vcpus_used,
        "ram_total_mb": stats.memory_mb,
        "ram_used_mb": stats.memory_mb_used,
        "disk_total_gb": stats.local_gb,
        "disk_used_gb": stats.local_gb_used,
        "traffic_in_bytes": ...,
        "traffic_out_bytes": ...,
        "raw_json": {...},
    }
```

6.3. Обработка ошибок

· Ловить keystoneauth1.exceptions.*, openstack.exceptions.SDKException, сетевые таймауты.
· Писать в журнал без секретов и токенов.
· При ошибке — обновить last_error и last_error_at, не терять предыдущие метрики.
· При успехе — обновить last_success_at.

6.4. Пороговые проверки

После записи метрик сравнивать проценты с порогами. При превышении — создавать запись-предупреждение для отображения в интерфейсе (баннер/бейдж).

---

7. Внутренние API chek.ymaster.ru

Все endpoints защищены текущей авторизацией chek.ymaster.ru.

Метод Endpoint Описание
GET /api/settings/openstack Настройки без секретов
POST /api/settings/openstack Сохранить настройки
POST /api/settings/openstack/test Проверить подключение
POST /api/settings/openstack/disable Отключить мониторинг
GET /api/openstack/metrics/current Текущие метрики
GET /api/openstack/metrics/history?from=&to=&step= История
GET /api/openstack/status Статус последней синхронизации
GET /api/openstack/logs?limit=100 Последние записи журнала

Формат metrics/current:

```json
{
  "collected_at": "2026-10-25T12:00:00Z",
  "cpu":   {"total": 64, "used": 12, "pct": 18.75},
  "ram":   {"total_mb": 262144, "used_mb": 65536, "pct": 25.0},
  "disk":  {"total_gb": 2000, "used_gb": 400, "pct": 20.0},
  "quotas":{"max_cores": 100, "used_cores": 12, "max_ram_mb": 262144, "used_ram_mb": 65536},
  "traffic":{"in_bytes": 123456, "out_bytes": 654321},
  "warnings": ["ram > 80%"]
}
```

---

8. Модель данных

Таблица openstack_settings

id, auth_url, auth_type, username, secret_encrypted, project_name, user_domain_name, project_domain_name, region_name, interface, verify_tls, timeout_sec, resource_provider_uuid, interval_sec, history_days, thresholds_json, enabled, created_at, updated_at

Таблица openstack_metrics

id, collected_at (index), vcpus_total, vcpus_used, ram_total_mb, ram_used_mb, disk_total_gb, disk_used_gb, traffic_in_bytes, traffic_out_bytes, raw_json (jsonb)

Таблица openstack_sync_log

id, ts, level, message, error_code

Индексы: openstack_metrics(collected_at), openstack_sync_log(ts DESC).
Ретенция: удалять метрики старше history_days.

---

9. Вкладка «Показатели» (Frontend)

Виджеты

· CPU — карточка: total, used, %, цвет по порогу.
· RAM — total, used, free, %.
· Disk — total, used, free, %.
· Квоты — max / used для cores и RAM.
· Сеть — in/out за выбранный период.
· Статус — последнее успешное обновление / последняя ошибка.

Графики

· Линейный: CPU %, RAM %, Disk % по времени.
· Линейный: traffic in / out по времени.
· Выбор периода: 1ч / 24ч / 7д / 30д.
· Автообновление каждые 30 сек.
· Цветовая индикация: зелёный / жёлтый (≥ порога) / красный (≥ порога + 10%).

---

10. Вкладка «Журнал»

· Таблица: время, уровень, сообщение, код ошибки.
· Фильтры по уровню (INFO/WARN/ERROR).
· Кнопка «Обновить».
· Секреты и токены не выводятся.

---

11. Безопасность

· Использовать существующую аутентификацию и RBAC chek.ymaster.ru.
· Доступ к блоку — только роль с правом openstack_monitor.manage.
· Секреты шифровать; маскировать в логах (***).
· Отключение TLS-проверки — только при явном чекбоксе с предупреждением.
· API — только по HTTPS, с CSRF-защитой для POST.
· Ограничить исходящие соединения только к auth_url (allowlist на уровне проекта, если есть).

---

12. Критерии приёмки

1. В Настройки → Мониторинг OpenStack есть 4 вкладки.
2. Настройки сохраняются; секреты в БД зашифрованы; в GET-ответах их нет.
3. «Проверить подключение» возвращает ok/error с текстом.
4. После сохранения запускается планировщик с интервалом interval_sec.
5. Метрики CPU/RAM/Disk/quotas собираются через openstacksdk и пишутся в БД.
6. Если задан resource_provider_uuid, читается Placement-инвентарь.
7. Если доступен Octavia/Gnocchi, собирается трафик; иначе поле traffic = null без падения.
8. На вкладке «Показатели» отображаются карточки и графики за 1ч/24ч/7д/30д.
9. При превышении порога появляется визуальное предупреждение.
10. Ошибки подключения видны в «Журнале» и в «Статусе».
11. Ретенция удаляет метрики старше history_days.
12. Сбор не блокирует остальную работу chek.ymaster.ru (асинхронно).

---

13. Задачи для ИИ-агента (по порядку)

1. Добавить зависимость openstacksdk и keystoneauth1 в проект.
2. Создать миграции: openstack_settings, openstack_metrics, openstack_sync_log.
3. Реализовать сервис openstack_monitor.connection.build_connection(cfg).
4. Реализовать сервис openstack_monitor.collector.collect_metrics(conn).
5. Реализовать шифрование/дешифрование секретов.
6. Реализовать внутренние API (/api/settings/openstack, /api/openstack/*).
7. Реализовать тест подключения POST /api/settings/openstack/test.
8. Реализовать планировщик и задание сбора с интервалом.
9. Реализовать проверку порогов и запись предупреждений.
10. Реализовать ретенцию истории.
11. Сверстать 4 вкладки блока в существующем UI.
12. Реализовать графики и автообновление.
13. Реализовать «Журнал» с фильтрами.
14. Добавить RBAC-право openstack_monitor.manage и защиту endpoints.
15. Прогнать тесты по критериям приёмки (unit + e2e с mock-connector).

---

14. Что использовать в качестве референса

· openstacksdk docs: https://docs.openstack.org/openstacksdk/latest/
· API reference: https://docs.openstack.org/api-ref
· Примеры: openstack hypervisor stats show, openstack limits show — CLI-аналоги, которые удобно использовать для сверки данных с тем, что возвращает SDK.

