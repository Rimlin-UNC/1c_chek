# Supabase: отдельный демонстрационный контур

Проект: `chek.base` (`putddctwdskfdzichigx`, PostgreSQL 17).

Контур `chek_demo` содержит **только синтетические данные**: 2 компании,
3 участника, 12 чеков и 24 позиции. Он не заменяет рабочие таблицы приложения,
не хранит реальные ФН/ФД/ФП, контакты, пароли или согласия. Рабочая `DATABASE_URL`
приложения не меняется.

## Доступ

- Базовые таблицы `chek_demo.*`: RLS включён и принудителен; `anon` и
  `authenticated` не имеют доступа к схеме и таблицам.
- Чтение для UI: только представления `public.chek_demo_*` и RPC
  `public.chek_demo_snapshot()`, возвращающие синтетический read-only снимок.
- Запись из браузера не предусмотрена. Сервисный ключ в браузер не передаётся.
- В Supabase Auth отключена свободная регистрация (`disable_signup=true`).
  Приложение использует собственную авторизацию; Supabase Auth пока не подключён.

## Применение

Миграции уже применены к проекту через Supabase. Для другого проекта примените
файлы из `supabase/migrations` по порядку в SQL-редакторе или Supabase CLI.
Затем задайте в `.env` приложения:

```dotenv
SUPABASE_DEMO_ENABLED=1
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
```

Используйте **publishable** key. Secret/service_role ключи запрещены в клиентской
конфигурации. Страница «Supabase демо» доступна администратору и бухгалтеру.
