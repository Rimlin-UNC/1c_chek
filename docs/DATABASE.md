# База данных и миграции

[AGENTS.md](../AGENTS.md) · [Обзор](OVERVIEW.md) · [Архитектура (ADR)](ARCHITECTURE.md) · [Runbook](RUNBOOK.md) · [База данных](DATABASE.md) · [Задачи и журнал](kb/README.md)

Канонический документ (ADR-19); перенесён из `docs/kb/05_db_and_migrations.md` 2026-11-02.
Связанные решения — ADR-03, 06–10, 14, 18; задачи — T-04…T-08, T-24, T-25;
вопросы — Q-01, Q-06, Q-10, Q-11, Q-15, Q-16.

Проверено 2026-11-02. Supabase — только запросами на чтение. Здесь только
схема и агрегаты, без строк данных.

## Статус миграции

Формулировка утверждена владельцем 2026-11-02 и уточнена его решением 2026-11-03
(ADR-14):

> **Единый слой — в 1.70.82 в режиме совместимости, установка проверена.
> Перенос архива не готов для рабочего сервера (запуск — по «да» владельца).
> Рабочая база остаётся на рабочем сервере: переход на PostgreSQL/Supabase не
> выполняется, здесь — только синтетические тесты.**

| Компонент | Статус | Основание |
|---|---|---|
| Единый слой (`unified_*`, хуки, сверка при старте) | выпущен в v1.70.82 ([PR #3](https://github.com/Rimlin-UNC/1c_chek/pull/3)); с v1.70.88 — сверка копий реестра и Чек-Пула с единой записью и отчёт «Готовность единой записи» (`python -m app.unified.readiness`, только числа). На рабочем сервере, по последнему отчёту, ещё v1.70.80 — **требует уточнения** (Q-02) | автотесты; `qa/upgrade_rehearsal.py` 48/48 от 12 версий (v1.70.93); настоящее обновление из программы с откатом `qa/live_update.py` 21/21 от 12 версий (v1.70.93); сквозной тест [#20](https://github.com/Rimlin-UNC/1c_chek/issues/20) |
| Перенос архива (`python -m app.unified.backfill`) | **не готов для рабочего сервера**: запуск — только по «да» владельца (T-07, #10). С v1.70.88 есть мягкая регистрация (`--soft`: только чеки без расхождений, спорные пропускаются) — тоже только командой владельца после `--soft --dry-run` (Q-26). Экран разбора конфликтов — с v1.70.83 (T-06); путь с переносом, разбором спора, откатом и повторным обновлением проверен сквозным тестом (#20) | [unified_receipts.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts.md) |
| Переход рабочей БД на PostgreSQL/Supabase | **не выполняется** — решение владельца 03.11.2026 (ADR-14, T-08): рабочая база остаётся на рабочем сервере (SQLite), её копии и выгрузки сюда не переносятся; здесь — только синтетические тесты. Совместимость кода с PostgreSQL 17 по-прежнему проверяет CI на синтетических данных (сквозной тест #20 нашёл и исправил: чистая установка на PostgreSQL падала на компании по умолчанию). Перенос рабочей базы с поштучной сверкой ([#23](https://github.com/Rimlin-UNC/1c_chek/issues/23)) отменён | [unified_receipts_wip.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts_wip.md) |
| Прототип миграции копии `qa/migrate_receipt_copy.py` | экспериментальный, не для рабочих данных | unified_receipts_wip.md |
| PR #2 | WIP, «не готов к эксплуатации»; целиком вошёл в PR #3 | [PR #2](https://github.com/Rimlin-UNC/1c_chek/pull/2) |
| Совместимость схемы приложения с проектом Supabase | **проверена в CI**: все 52 таблицы приложения (с 1.70.85 — и `unified_company_links`, с 1.70.94 — `primary_documents` и `primary_document_lines`) создаются на PostgreSQL 17 рядом с демо-контуром, аудит RLS/FK без ошибок | `tests/test_migration_compat_postgres.py`, раздел «Совместимость схемы приложения с Supabase» |
| Сквозной тест миграции (1.70.80 → текущая → перенос архива → сверка → откат и повтор) | **реализован**: `tests/test_migration_e2e.py`, CI на Python 3.11 и 3.12, SQLite и PostgreSQL 17 — 32 проверки на каждом (с 1.70.88 — сверка копий после правки старой версией, с 1.70.89 — правило слияния, с 1.70.90 — сохранение формы правки у чека по QR, с 1.70.91 — данные единой записи в карточке); шаг 6 (перенос SQLite → PostgreSQL) отменён решением владельца 03.11.2026 (ADR-14, [#23](https://github.com/Rimlin-UNC/1c_chek/issues/23)) | [issue #20](https://github.com/Rimlin-UNC/1c_chek/issues/20), раздел «Сквозной тест миграции (#20)» |

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
  5. с v1.70.82 — сверка единого реестра `app/unified/reconcile.py`; с v1.70.88 —
     сверка копий `app/unified/copies.py`: фискальные данные и позиции реестра и
     Чек-Пула приводятся к единой записи (схема не меняется); с v1.70.89 — по правилу
     слияния: пустое поле копии ничего не стирает.

  Версии схемы отслеживаются по CHANGELOG.
- **Таблицы.** 52 объявления `__tablename__` в коде (с 1.70.85 — `unified_company_links`, с 1.70.94 — `primary_documents` и `primary_document_lines`):

| Домен | Таблицы |
|---|---|
| Учёт чеков | `receipts`, `receipt_items`, `receipt_identity_locks`, `fns_log`, `fns_usage`, `merchant_rules`, `mapping_settings`, `advance_report_counters`, `advance_report_reservations` |
| Компании и доступ | `companies`, `users`, `invites`, `audit_log`, `app_settings`, `webauthn_credentials`, `webauthn_challenges`, `max_login_codes`, `telegram_login_codes` |
| Чек-Пул | `pool_users`, `pool_receipts`, `pool_items`, `pool_points`, `pool_consents`, `pool_tokens`, `pool_fingerprints`, `pool_ip_log`, `pool_signals`, `pool_withdrawals`, `pool_achievements`, `pool_referrals`, `pool_leads`, `pool_api_keys`, `pool_api_calls` |
| Единый реестр (v1.70.81–85) | `unified_documents`, `unified_items`, `unified_ownership`, `unified_origins`, `unified_events`, `unified_company_links` (v1.70.85) |
| Первичка (v1.70.94) | `primary_documents`, `primary_document_lines` — электронные УПД и счета-фактуры |
| B2B | `b2b_applications`, `b2b_companies`, `b2b_tokens`, `b2b_checks`, `b2b_events` |
| Почта и события | `mail_rules`, `mail_log`, `webhook_logs` |
| Мониторинг OpenStack | `openstack_settings`, `openstack_metrics`, `openstack_sync_log` |

- **Единый реестр**
  ([docs/unified_receipts.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts.md)):
  - `identity_key` UNIQUE = sha256(ФН, ФД, ФП); неполная тройка — `NULL`, без склейки;
  - `unified_origins` UNIQUE(`source_kind`, `source_id`) связывает документ с проекциями `receipts` и `pool_receipts`;
  - `unified_ownership` — без внешних ключей на `companies`/`users` (ADR-09);
  - `unified_events` — журнал без фискальных значений и ПДн;
  - `unified_company_links` (v1.70.85, «Единая база», T-34) — заявки компаний на чужие чеки и
    прежние владельцы: UNIQUE(`document_id`, `company_id`), каскад от `unified_documents`,
    `company_id` без внешнего ключа (как в `unified_ownership`); число организаций по чеку —
    владелец + заявки `open`/`declined` ([single_base_design.md, раздел 10](single_base_design.md#10-фазы-12-сделано-t-34));
  - показ числа организаций компаниям (v1.70.86, «Единая база», T-35) — без новых таблиц:
    колонка `companies.show_companies_count` (логическая, NOT NULL, по умолчанию «нет»;
    добавляется `_ensure_schema`) и ключи `app_settings` `companies_count_mode`
    (`off` по умолчанию / `all` / `selected`) и `companies_count_roles` (`accountant`, `manager`;
    «ни одной роли» хранится как `none`) ([single_base_design.md, раздел 11](single_base_design.md#11-фаза-3-сделано-t-35-и-решение-q-24));
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

## Проверки RLS и внешних ключей

[`supabase/checks/rls_fk_audit.sql`](https://github.com/Rimlin-UNC/1c_chek/blob/arena/c7f9d34a-1c-chek/supabase/checks/rls_fk_audit.sql) —
один SELECT только для чтения; строка на проверку и объект, результат `OK`,
`ВНИМАНИЕ` (известно и задокументировано) или `ОШИБКА`.

| Код | Что проверяет |
|---|---|
| R1 | RLS включён на каждой таблице пользовательских схем |
| R2 | у `anon` и `authenticated` нет прав INSERT, UPDATE, DELETE, TRUNCATE |
| R3 | представления в `public` без `security_invoker` (обходят RLS) |
| R4 | у функций SECURITY DEFINER фиксирован `search_path` |
| F1 | внешние ключи провалидированы (`convalidated`): ссылочная целостность гарантирована базой |
| F2 | под внешним ключом есть индекс |
| S1 | временная схема `qa_lock_…` и доступ `anon` к ней |
| S2 | событийный триггер `ensure_rls` |
| M1 | история миграций = файлам `supabase/migrations` |

Поиска «висячих» строк нет намеренно: для ключа с `convalidated = true` их не
может быть, а ключ `NOT VALID` проверка F1 считает ошибкой.

**Результат на проекте `chek.base` (2026-11-02):** 22 строки — 15 OK, 7 ВНИМАНИЕ,
**0 ОШИБОК**.

| Код | Результат |
|---|---|
| F1, F2 | 4 внешних ключа `chek_demo` провалидированы, под каждым есть индекс — OK |
| M1 | 20261101154331, 20261101155225, 20261101164303 — OK |
| R1 | 4 таблицы `chek_demo`: RLS принудителен, политик 0 — OK; `qa_lock_…receipt_identity_locks` — ВНИМАНИЕ (#7) |
| R2, R4 | записи для `anon`/`authenticated` нет; `search_path` у функций SECURITY DEFINER фиксирован — OK |
| R3 | 4 представления `chek_demo_*` — ВНИМАНИЕ (Q-15, #18) |
| S1, S2 | схема `qa_lock_…` (USAGE у `anon` нет), триггер `ensure_rls` — ВНИМАНИЕ (#7, #11) |

## Совместимость схемы приложения с Supabase

`tests/test_migration_compat_postgres.py` (CI, PostgreSQL 17) собирает на
одноразовой базе то, что есть в проекте: роли, историю миграций, копию
`ensure_rls`, три миграции демо-контура — и создаёт рядом все таблицы
приложения, как при переносе рабочей БД. Результат 2026-11-02 (v1.70.85, с таблицей
`unified_company_links`):

- 50 таблиц приложения создаются без ошибок DDL, коллизий имён нет;
  `supabase/verify.sql` даёт прежнее ожидание — демо-контур цел;
- аудит RLS/FK — 122 строки, 0 ошибок; 26 внешних ключей приложения провалидированы;
- `ensure_rls` включил RLS на всех 54 таблицах (50 приложения и 4 демо-контура).

**Вывод:** перенос схемы приложения не сломает текущую структуру Supabase. Одно
условие: триггер `ensure_rls` включает RLS на каждой новой таблице в `public`,
и роль, не владеющая таблицами, видит их пустыми (тест это подтверждает:
строку в `companies` владелец видит, роль без владения — нет). При переносе
(#11) подключаться владельцем таблиц или создавать их в отдельной схеме.

## Сквозной тест миграции (#20)

`tests/test_migration_e2e.py` (сценарий и проверки — `qa/migration_e2e.py`) проходит
путь рабочей базы целиком и выполняется в CI на каждом push: Python 3.11 и 3.12,
SQLite и PostgreSQL 17. Каждая фаза — отдельный процесс с кодом своей версии,
общая у фаз только база; на PostgreSQL — временная база, удаляется после прогона.

| Шаг | Что происходит | Что проверяется |
|---|---|---|
| 1 | Код 1.70.80 (`b3db4fe`) создаёт базу: 4 компании, 5 пользователей, 22 чека реестра (один без ФП), 6 чеков Чек-Пула, исторический двойник чека Альфы в пуле Беты | состав базы; списки по ролям в 1.70.80 — эталон |
| 2 | Обновление: `init_db()` со сверкой при старте | 5 таблиц `unified_*`, перенос при старте не запускается; старые таблицы побайтно те же; каждая роль видит ровно те же чеки |
| 3 | `python -m app.unified.backfill --dry-run`, затем настоящий и повторный | к переносу 22 + 6; зарегистрировано 28; повторный — 0 |
| 4 | Инварианты, затем решение спора через API «Разбора конфликтов» | одна каноническая запись на тройку ФН + ФД + ФП (27 записей на 28 источников); старые таблицы побайтно те же; видимость по ролям в реестре и едином реестре; двойник в карантине — скрыт от обеих компаний, виден администратору; счётчики; после решения — видимость как в 1.70.80, меняется только `pool_receipts` |
| 5 | Откат на 1.70.80: скан, перенос, удаление чека, правка, удаление компании; повторное обновление и повторный перенос | 3 исправления сверки в журнале (`reconcile`), повторная сверка — 0; старые таблицы не меняются; каждая роль видит то же, что в 1.70.80; чек со времени отката подхватывает повторный перенос |

Итого 21 проверка на каждом бэкенде; любое нарушение роняет тест со списком
нарушенных проверок на русском. Тест проверен подставными ошибками (02.11.2026):
утечка видимости, отключённая сверка, изменение старых таблиц переносом и
несклеенная тройка — каждая роняет свои проверки.

**PostgreSQL.** Тест нашёл ошибку: первый `init_db()` вставлял компанию по
умолчанию с `is_active = 1` — целое число в булев столбец; PostgreSQL это
отвергает, поэтому ни 1.70.80, ни текущая версия не ставились на чистый
PostgreSQL. В текущем коде исправлено (`app/database.py`, проверяет
`test_fresh_install_postgres`); выйдет со следующим выпуском, на SQLite поведение
прежнее. Для 1.70.80 тест создаёт таблицы и ту же компанию по умолчанию её
собственными моделями, дальше всё штатно; откат на 1.70.80 на уже созданной базе
PostgreSQL работает.

**Не входит:** перенос самой рабочей базы SQLite → PostgreSQL с поштучной сверкой
(шаг 6 спецификации) — отменён решением владельца 03.11.2026 (ADR-14): рабочая база
остаётся на рабочем сервере, здесь — только синтетические тесты; задача
[#23](https://github.com/Rimlin-UNC/1c_chek/issues/23) не выполняется.

Запуск:

```bash
.venv/bin/python -m pytest tests/test_migration_e2e.py      # SQLite; PostgreSQL — пропуск
python3 qa/pg_local.py pytest tests/test_migration_e2e.py   # оба бэкенда, временный PostgreSQL
.venv/bin/python -m qa.migration_e2e [--postgres]           # отчёт по фазам вне pytest
```

## Интеграция Supabase ↔ GitHub

Основная (production) ветка проекта `chek.base` связана с git-веткой
**`arena/c7f9d34a-1c-chek`** (ветка PR #3): владелец переключил её 2026-11-02 в
10:17 UTC (`list_branches`). До этого была ветка PR #2 `arena/071f4163-1c-chek`
со старыми файлами миграций `2026110101–03`.

- Сразу после переключения Supabase развернул ветку (статус ветки
  `FUNCTIONS_DEPLOYED`). История миграций и аудит RLS/FK не изменились: файлы
  ветки совпадают с применённой историей, аудит — 22 строки (15 OK,
  7 ВНИМАНИЕ, 0 ОШИБКА).
- **Push разворачивает ветку в рабочий проект** (проверено 02.11.2026): после
  push `03c8fb8` ветка Supabase прошла `RUNNING_MIGRATIONS` → `FUNCTIONS_DEPLOYED`,
  проверка «Supabase Preview» в PR — success, история миграций не изменилась.
- **Следствие для работы:** новые файлы `supabase/migrations` и `supabase/functions`
  применятся к рабочему проекту при первом же push. Такие файлы — только после
  «да» владельца (стоп-лист в [AGENTS.md](../AGENTS.md)).
- После слияния PR #3 production-ветку переключить на `main` —
  [#21](https://github.com/Rimlin-UNC/1c_chek/issues/21), T-25. Агент настройки
  интеграции не меняет.

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
- **Проверка 2026-11-02:** 3 строки (счётчик), зависимых объектов 0, USAGE у `anon`
  нет (проверки R1 и S1). Удаление на структуру не повлияет: схема ни с чем не связана.

## Проверка состояния в новой сессии (только чтение)

Через коннектор Supabase `list_migrations` и `execute_query`. Каждый вызов
`execute_query` возвращает только результат последнего оператора. Полная
проверка одним запросом — `supabase/checks/rls_fk_audit.sql`.

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
владельцем ([алгоритм](../AGENTS.md), стоп-лист).
