# 5. Состояние БД и миграции

[← База знаний](README.md) · Связанные разделы: [ADR-03, 06–10, 14](03_adr.md) ·
[Задачи T-04…T-08](02_roadmap.md) · [Вопросы Q-01, Q-06, Q-09…Q-11](06_open_questions.md)

Проверено 2026-11-02. Supabase — только запросами на чтение. Здесь только
схема и агрегаты, без строк данных.

## Статус миграции

Формулировка утверждена владельцем 2026-11-02:

> **Единый слой — в 1.70.82 в режиме совместимости, установка проверена.
> Перенос архива и переход рабочей БД на PostgreSQL/Supabase не готовы для
> рабочего сервера.**

| Компонент | Статус | Основание |
|---|---|---|
| Единый слой (`unified_*`, хуки, сверка при старте) | выпущен в v1.70.82 ([PR #3](https://github.com/Rimlin-UNC/1c_chek/pull/3)). На рабочем сервере, по последнему отчёту, ещё v1.70.80 — **требует уточнения** (Q-02) | автотесты; `qa/upgrade_rehearsal.py` 26/26; настоящее обновление из программы с откатом |
| Перенос архива (`python -m app.unified.backfill`) | **не готов для рабочего сервера**: спорные чеки скрываются до разбора, а экрана разбора нет (T-06 → T-07) | [unified_receipts.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts.md) |
| Переход рабочей БД на PostgreSQL/Supabase | **не готов, не начат**, решения нет (ADR-14, T-08) | [unified_receipts_wip.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts_wip.md) |
| Прототип миграции копии `qa/migrate_receipt_copy.py` | экспериментальный, не для рабочих данных | unified_receipts_wip.md |
| PR #2 | WIP, «не готов к эксплуатации»; целиком вошёл в PR #3 | [PR #2](https://github.com/Rimlin-UNC/1c_chek/pull/2) |

## Рабочая БД (SQLite, SQLAlchemy)

- Расположение: `data/ymaster_check.db` (значение `DATABASE_URL` по умолчанию),
  режим WAL, `busy_timeout`, `foreign_keys=ON`. Источник:
  [app/database.py](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/app/database.py).
- **Механизм миграций — без Alembic.** При каждом старте и в проверке
  целостности обновления выполняется `init_db()`:
  1. `Base.metadata.create_all` и `_ensure_schema()` — только добавляющие
     `ALTER TABLE ADD COLUMN`;
  2. схемы модулей пула, B2B и OpenStack;
  3. идемпотентные дозаполнения данных;
  4. перенос компаний из «памятки»;
  5. с v1.70.82 — сверка единого реестра `app/unified/reconcile.py`.
  
  Версии схемы отслеживаются по CHANGELOG.
- **Таблицы.** 49 объявлений `__tablename__` в коде:

| Домен | Таблицы |
|---|---|
| Учёт чеков | `receipts`, `receipt_items`, `receipt_identity_locks`, `fns_log`, `fns_usage`, `merchant_rules`, `mapping_settings`, `advance_report_counters`, `advance_report_reservations` |
| Компании и доступ | `companies`, `users`, `invites`, `audit_log`, `app_settings`, `webauthn_credentials`, `webauthn_challenges`, `max_login_codes`, `telegram_login_codes` |
| Чек-Пул | `pool_users`, `pool_receipts`, `pool_items`, `pool_points`, `pool_consents`, `pool_tokens`, `pool_fingerprints`, `pool_ip_log`, `pool_signals`, `pool_withdrawals`, `pool_achievements`, `pool_referrals`, `pool_leads`, `pool_api_keys`, `pool_api_calls` |
| Единый реестр (v1.70.81–82) | `unified_documents`, `unified_items`, `unified_ownership`, `unified_origins`, `unified_events` |
| B2B | `b2b_applications`, `b2b_companies`, `b2b_tokens`, `b2b_checks`, `b2b_events` |
| Почта и события | `mail_rules`, `mail_log`, `webhook_logs` |
| Мониторинг OpenStack | `openstack_settings`, `openstack_metrics`, `openstack_sync_log` |

- **Единый реестр**
  ([docs/unified_receipts.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts.md)):
  - `identity_key` UNIQUE = sha256(ФН, ФД, ФП); неполная тройка — `NULL`, без склейки;
  - `unified_origins` UNIQUE(`source_kind`, `source_id`) связывает документ с проекциями `receipts` и `pool_receipts`;
  - `unified_ownership` — без внешних ключей на `companies`/`users` (ADR-09);
  - `unified_events` — журнал без фискальных значений и ПДн;
  - флаги разбора `review_flags`; блокирующие — дата, сумма, операция, владелец, позиции, продавец.
  
  После обновления таблицы пустые и заполняются при правке или проверке чека.

## Supabase `chek.base`

Проект `putddctwdskfdzichigx` · eu-west-1 · PostgreSQL 17.11 · ACTIVE_HEALTHY ·
[Dashboard](https://supabase.com/dashboard/project/putddctwdskfdzichigx).
Рабочие данные и персональные данные в Supabase не переносились. Рабочая
`DATABASE_URL` на Supabase не указывает (ADR-10).

**Применённые миграции и файлы репозитория**
([supabase/migrations](https://github.com/Rimlin-UNC/1c_chek/tree/820bf7c9df7008aeacca88b1955a5ba8dca8fea2/supabase/migrations)):

| Применено в проекте | Файл в репозитории | Совпадение |
|---|---|---|
| `20261101154331_chek_demo_readonly_contour` | то же имя + `.sql` | побайтно, md5 `3d6ffc1d5869edbe7ee4b031d31d4c56` |
| `20261101155225_chek_demo_fix_item_seed` | то же имя + `.sql` | побайтно, md5 `a92c63f2458786972a636675edd060f8` |
| `20261101164303_chek_demo_revoke_view_write_default_grants` | то же имя + `.sql` | побайтно, md5 `12006191ae3c852faf49b8c60cebc4f4` |

Выровнено 02.11.2026 (T-05, [issue #8](https://github.com/Rimlin-UNC/1c_chek/issues/8),
ADR-18): файлы переименованы под применённые версии, содержимое — текст из
`supabase_migrations.schema_migrations` (md5 и длина сверены на стороне
PostgreSQL). История в Supabase не менялась. В CI
`tests/test_supabase_migrations.py` применяет файлы к чистому PostgreSQL 17 и
сверяет `supabase/verify.sql`, затем запускает каждый файл повторно: повтор
`20261101154331` после `20261101155225` откатывается целиком (конфликт
`unique (receipt_id, position)`), контур не меняется.

**Схемы проекта**

| Схема | Объекты | Доступ anon/authenticated |
|---|---|---|
| `chek_demo` | 4 таблицы: `companies` (2), `participants` (3), `receipts` (12), `receipt_items` (24) — синтетика; RLS включён и принудителен; политик 0 | нет USAGE, нет прав на таблицы |
| `public` | 0 таблиц; 4 представления `chek_demo_*` (security_barrier, без security_invoker); RPC `chek_demo_snapshot()` (SECURITY DEFINER, `search_path=chek_demo, public`); функция `rls_auto_enable()` с событийным триггером `ensure_rls` — **не из миграций проекта** (Q-10) | SELECT на представления; EXECUTE на RPC; запись запрещена |
| `qa_lock_965266689e1c4867b98ef79644d72d2c` | см. ниже | нет |
| `supabase_migrations` | история миграций (3 записи) | нет |
| служебные | `auth`, `storage`, `realtime`, `vault`, `graphql`, `graphql_public`, `extensions`, `pgbouncer` — стандартные Supabase | по умолчанию Supabase |

**Остальное:**
- **Default privileges в `public`:** новые таблицы `postgres` получают SELECT
  (и REFERENCES/TRIGGER/MAINTAIN) для `anon`/`authenticated`. Запись отозвана
  миграцией `20261101164303`.
- **Exposed schemas PostgREST** через коннектор не видны — **требует уточнения**
  (Q-11). Переопределения `pgrst.db_schemas` у роли `authenticator` нет.
- **Auth:** регистрация отключена (`disable_signup=true`); приложение Supabase
  Auth не использует.
- **Расширения:** pg_stat_statements, pgcrypto, plpgsql, supabase_vault, uuid-ossp.
- **Edge functions:** нет.

## Обзор безопасности Supabase (02.11.2026, только чтение)

T-11, [issue #14](https://github.com/Rimlin-UNC/1c_chek/issues/14). Критичных проблем нет. Решения владельца —
T-22, [issue #18](https://github.com/Rimlin-UNC/1c_chek/issues/18).

| Проверка | Результат |
|---|---|
| Таблицы в `public` без RLS | нет |
| Права `anon`/`authenticated` на запись (INSERT, UPDATE, DELETE, TRUNCATE) | нет ни в одной схеме |
| RLS без политик | 4 таблицы `chek_demo` — намеренно: доступ только у владельца и `service_role` |
| Схемы с USAGE у `anon` | стандартные Supabase: `auth`, `extensions`, `graphql`, `graphql_public`, `public`, `realtime`, `storage`. У `chek_demo` и `qa_lock_…` — нет |
| Представления `public.chek_demo_*` | `security_barrier`, без `security_invoker`: выполняются с правами владельца и обходят RLS базовых таблиц. Для синтетики только на чтение — намеренно; линтер Supabase считает это ошибкой → Q-15 |
| Функции SECURITY DEFINER | `chek_demo_snapshot()`: `search_path=chek_demo, public`, EXECUTE у PUBLIC отозван. `rls_auto_enable()`: `search_path=pg_catalog`, EXECUTE у PUBLIC; функция событийного триггера `ensure_rls`, напрямую не вызывается — риск низкий, происхождение — требует уточнения (Q-10) |
| Расширения в `public` | нет |
| Событийные триггеры | служебные Supabase (`pgrst_ddl_watch`, `pgrst_drop_watch`, `issue_pg_cron_access`, `issue_pg_net_access`, `issue_pg_graphql_access`, `issue_graphql_placeholder`) и `ensure_rls` |
| Роль `authenticator` | `statement_timeout=8s`, `lock_timeout=8s`; `pgrst.db_schemas` не переопределён — exposed schemas требуют уточнения (Q-11) |
| Auth | регистрация выключена, анонимные пользователи выключены, ротация refresh-токенов включена, `jwt_exp=3600` |

Если подключать Supabase Auth (сейчас у приложения своя авторизация):
минимальная длина пароля 6 → 8+, проверка утёкших паролей (сейчас выключена),
`site_url` (сейчас `http://localhost:3000`).

## Временная схема `qa_lock_965266689e1c4867b98ef79644d72d2c`

- **Происхождение.** Оставлена в Supabase после проверки блокировок PostgreSQL
  (два параллельных SQL-сеанса, синтетические ключи) в ходе работ по PR #2.
  Источники: [docs/unified_receipts_wip.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts_wip.md),
  [docs/development_environment.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/development_environment.md),
  описание [PR #2](https://github.com/Rimlin-UNC/1c_chek/pull/2).
- **Удаление заблокировано провайдером коннектора** (фильтр безопасности).
- **Только синтетические данные:** 1 таблица `receipt_identity_locks`
  (1 колонка `key`, 3 строки синтетических ключей блокировки); RLS выключен.
  Доступ закрыт правами на схему.
- **Публичные права отозваны:** права на схему только у `postgres`
  (`{postgres=UC/postgres}`); у `anon`, `authenticated` и `PUBLIC` нет USAGE.
- **Нужна задача на очистку с явным подтверждением владельца** — T-04,
  [issue #7](https://github.com/Rimlin-UNC/1c_chek/issues/7). Агент её не удаляет.

## Проверка состояния в новой сессии (только чтение)

Через коннектор Supabase `list_migrations` и `execute_query`. Каждый вызов
`execute_query` возвращает только результат последнего оператора.

```sql
-- схемы проекта и доступ ролей
select n.nspname, has_schema_privilege('anon', n.oid, 'USAGE') anon_usage,
       has_schema_privilege('authenticated', n.oid, 'USAGE') auth_usage
from pg_namespace n
where n.nspname not like 'pg\_%' and n.nspname <> 'information_schema' order by 1;

-- схема qa_lock на месте? (агрегат, без строк)
select count(*) as tables from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'qa_lock_965266689e1c4867b98ef79644d72d2c' and c.relkind = 'r';

-- синтетика демо-контура (только счётчики)
select (select count(*) from chek_demo.companies) companies,
       (select count(*) from chek_demo.receipts) receipts;
```

Любые DDL, DML и изменения прав в Supabase — только по плану, подтверждённому
владельцем ([алгоритм](07_agent_algorithm.md), стоп-лист).
