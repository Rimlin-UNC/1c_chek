-- Проверки RLS, прав и внешних ключей проекта Supabase chek.base.
-- Только чтение: один SELECT, ничего не меняет. Выполнять в SQL-редакторе
-- Supabase или через коннектор. Тот же файл проверяет CI на чистом
-- PostgreSQL 17 со схемой приложения: tests/test_migration_compat_postgres.py.
--
-- Строка результата: код | проверка | объект | результат | подробности.
-- Результат: OK; ВНИМАНИЕ — известно и задокументировано (ссылка на issue);
-- ОШИБКА — нужно исправлять. Описание проверок — docs/DATABASE.md.
--
-- Поиска «висячих» строк здесь нет намеренно: для внешнего ключа с
-- convalidated = true база сама гарантирует ссылочную целостность, а ключ
-- NOT VALID (convalidated = false) проверка F1 помечает как ОШИБКУ.
with user_schemas as (
    select oid, nspname
    from pg_namespace
    where nspname not like 'pg\_%'
      and nspname not in ('information_schema', 'auth', 'storage', 'realtime', '_realtime',
                          'vault', 'graphql', 'graphql_public', 'extensions', 'pgbouncer',
                          'supabase_functions', 'supabase_migrations', 'net', 'cron',
                          'pgsodium', 'pgsodium_masks', '_analytics')
),
user_tables as (
    select c.oid, n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
           (select count(*) from pg_policy p where p.polrelid = c.oid) as policies
    from pg_class c
    join user_schemas n on n.oid = c.relnamespace
    where c.relkind in ('r', 'p')
),
user_fks as (
    select con.oid, con.conname, con.conrelid, con.confrelid, con.conkey, con.convalidated
    from pg_constraint con
    where con.contype = 'f'
      and con.connamespace in (select oid from user_schemas)
),
checks as (
    -- R1. RLS включён на каждой таблице пользовательских схем
    select 'R1' as code, 'RLS включён' as check_name, t.nspname || '.' || t.relname as object,
           case when t.relrowsecurity then 'OK'
                when t.nspname like 'qa\_lock\_%' then 'ВНИМАНИЕ'
                else 'ОШИБКА' end as result,
           case when t.relrowsecurity
                     then 'политик: ' || t.policies
                          || case when t.relforcerowsecurity then ', принудительно' else '' end
                          || case when t.policies = 0 then ' (доступ только владельцу и service_role)' else '' end
                when t.nspname like 'qa\_lock\_%'
                     then 'временная схема проверки: RLS выключен, доступ закрыт правами на схему (#7)'
                else 'таблица без RLS: доступна по правам роли в обход политик' end as details
    from user_tables t

    union all
    -- R2. У anon и authenticated нет прав на запись
    select 'R2', 'Запись для anon/authenticated',
           coalesce(string_agg(distinct g.table_schema || '.' || g.table_name || ': '
                               || g.grantee || ' ' || g.privilege_type, '; '), 'все пользовательские таблицы'),
           case when count(*) = 0 then 'OK' else 'ОШИБКА' end,
           case when count(*) = 0 then 'INSERT, UPDATE, DELETE, TRUNCATE не выданы'
                else 'выданы права на запись — отозвать' end
    from information_schema.role_table_grants g
    where g.grantee in ('anon', 'authenticated')
      and g.privilege_type in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
      and g.table_schema in (select nspname from user_schemas)

    union all
    -- R3. Представления в public без security_invoker
    select 'R3', 'Представление без security_invoker', n.nspname || '.' || c.relname, 'ВНИМАНИЕ',
           'выполняется с правами владельца и обходит RLS базовых таблиц; для синтетики — намеренно (Q-15, #18)'
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'v'
      and not coalesce(c.reloptions @> array['security_invoker=true'], false)

    union all
    -- R4. Функции SECURITY DEFINER с фиксированным search_path
    select 'R4', 'SECURITY DEFINER с search_path',
           coalesce(string_agg(n.nspname || '.' || p.proname, ', '), 'нет функций без search_path'),
           case when count(*) = 0 then 'OK' else 'ОШИБКА' end,
           case when count(*) = 0 then 'у всех функций SECURITY DEFINER search_path фиксирован'
                else 'без фиксированного search_path функцию можно подменить через схему' end
    from pg_proc p
    join user_schemas n on n.oid = p.pronamespace
    where p.prosecdef
      and not exists (select 1 from unnest(coalesce(p.proconfig, '{}'::text[])) cfg
                      where cfg like 'search\_path=%')

    union all
    -- F1. Внешние ключи провалидированы (convalidated)
    select 'F1', 'Внешний ключ провалидирован',
           f.conrelid::regclass::text || ' → ' || f.confrelid::regclass::text || ' (' || f.conname || ')',
           case when f.convalidated then 'OK' else 'ОШИБКА' end,
           case when f.convalidated then 'ссылочная целостность гарантирована базой'
                else 'NOT VALID: возможны строки без родителя — проверить и выполнить VALIDATE CONSTRAINT' end
    from user_fks f

    union all
    -- F2. Под внешним ключом есть индекс (иначе удаление родителя сканирует таблицу)
    select 'F2', 'Индекс под внешним ключом',
           f.conrelid::regclass::text || ' (' || f.conname || ')',
           case when exists (
                    select 1 from pg_index i
                    where i.indrelid = f.conrelid
                      and (string_to_array(i.indkey::text, ' ')::int2[])[1:cardinality(f.conkey)] = f.conkey)
                then 'OK' else 'ВНИМАНИЕ' end,
           case when exists (
                    select 1 from pg_index i
                    where i.indrelid = f.conrelid
                      and (string_to_array(i.indkey::text, ' ')::int2[])[1:cardinality(f.conkey)] = f.conkey)
                then 'есть индекс с этими колонками в начале'
                else 'индекса нет: на малых таблицах некритично, на рабочих данных — добавить' end
    from user_fks f

    union all
    -- S1. Временная схема qa_lock
    select 'S1', 'Временная схема qa_lock', n.nspname, 'ВНИМАНИЕ',
           'ждёт решения владельца об удалении (#7); USAGE у anon: '
           || case when has_schema_privilege('anon', n.oid, 'USAGE') then 'есть — ОПАСНО' else 'нет' end
    from pg_namespace n
    where n.nspname like 'qa\_lock\_%'

    union all
    -- S2. Автоматическое включение RLS на новых таблицах в public
    select 'S2', 'Событийный триггер ensure_rls', e.evtname, 'ВНИМАНИЕ',
           'включает RLS на каждой новой таблице в public: при переносе рабочей БД подключаться '
           || 'владельцем таблиц или создавать их в отдельной схеме (#11)'
    from pg_event_trigger e
    where e.evtname = 'ensure_rls'

    union all
    -- M1. История миграций совпадает с файлами supabase/migrations
    select 'M1', 'История миграций',
           coalesce(string_agg(m.version, ', ' order by m.version), 'пусто'),
           case when coalesce(array_agg(m.version order by m.version), '{}')
                     = array['20261101154331', '20261101155225', '20261101164303'] then 'OK' else 'ОШИБКА' end,
           'ожидается: 20261101154331, 20261101155225, 20261101164303 (= файлы supabase/migrations)'
    from supabase_migrations.schema_migrations m
)
select code as "код", check_name as "проверка", object as "объект",
       result as "результат", details as "подробности"
from checks
order by code, object;
