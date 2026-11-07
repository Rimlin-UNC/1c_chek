# Обзор проекта

[AGENTS.md](../AGENTS.md) · [Обзор](OVERVIEW.md) · [Архитектура (ADR)](ARCHITECTURE.md) · [Runbook](RUNBOOK.md) · [База данных](DATABASE.md) · [Задачи и журнал](kb/README.md)

Канонический документ (ADR-19); перенесён из `docs/kb/01_overview.md` 2026-11-02.

## Что это

**«Ямастер Чек»** (репозиторий `Rimlin-UNC/1c_chek`) — веб-система учёта кассовых
чеков. Сотрудник сканирует QR-код чека (54-ФЗ) камерой, фотографией, PDF или
вручную. Система проверяет чек в ФНС, складывает его в базу своей компании и
выгружает в 1С (push-режим или файл EnterpriseData). Разработчик и владелец
идеи — ООО «Ямастер».
Источники: [README.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/README.md),
[docs/SPECIFICATION.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/SPECIFICATION.md).

## Для кого

| Роль | Кто это | Что делает |
|---|---|---|
| `admin` | администратор платформы, всегда один | всё: компании, пользователи, настройки, обновления, перенос чеков |
| `accountant` | бухгалтер компании | чеки своей компании, проверка ФНС, выгрузка в 1С, подбор из Чек-Пула |
| `manager` | руководитель | просмотр чеков компании и отчёт руководителя |
| `user` | сотрудник (подотчётное лицо) | сканирование и просмотр только своих чеков |
| `donor` | участник Чек-Пула | сдаёт чеки в общий пул за баллы |

Отдельно — партнёры платного B2B API (кабинет `/b2b/`).
Источник: [docs/rbac_matrix.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/rbac_matrix.md).
Раздел «Роли» в README синхронизирован с этой матрицей 02.11.2026
([issue #15](https://github.com/Rimlin-UNC/1c_chek/issues/15)).

## Ключевые компоненты

| Компонент | Код | Документация |
|---|---|---|
| Приём и сканирование (QR, фото, PDF, ручной ввод, офлайн-очередь) | `app/routers/receipts.py`, `app/services/qr.py`, `app/services/imaging.py` | [knowledge/qr_formats.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/knowledge/qr_formats.md) |
| Проверка в ФНС и внешние источники (**блок не трогать без владельца**) | `app/services/fns.py`, `app/services/external.py` | [knowledge/fns_auth.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/knowledge/fns_auth.md) |
| Выгрузка в 1С и маппинг | `app/services/exporter.py` | [INTEGRATION_1C.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/INTEGRATION_1C.md) |
| Компании, роли, изоляция данных | `app/routers/companies.py`, `app/services/rbac.py`, `app/services/scoping.py` | [rbac_matrix.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/rbac_matrix.md) |
| Чек-Пул: приём от доноров, баллы, антифрод, подбор и перенос в реестр | `app/pool/` | [dev_plan_checkpool.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/dev_plan_checkpool.md) |
| Единый реестр чеков (канонический документ, владение, журнал, сверка) | `app/unified/` | [unified_receipts.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/unified_receipts.md) |
| Платное B2B API | `app/b2b/` | [b2b_access.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/b2b_access.md) |
| Боты и вход через MAX/Telegram | `app/services/max_bot.py`, `app/services/telegram_bot.py` | [telegram_proxy_audit_v17068.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/telegram_proxy_audit_v17068.md) |
| Обновление из программы, откат, резервные копии | `app/services/updater.py`, `app/services/releases.py`, `app/services/backups.py` | [update_playbook.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/update_playbook.md) |
| Мониторинг OpenStack | `app/openstack_monitor/` | [Monitor.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/Monitor.md) |
| Supabase-демо (read-only, синтетика) | `app/routers/supabase_demo.py`, `supabase/` | [supabase/README.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/supabase/README.md) |
| Веб-клиент: SPA + PWA, мини-приложение MAX, кабинет B2B | `app/static/` | [ui_journal.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/ui_journal.md) |

## Стек

FastAPI · SQLAlchemy 2 · SQLite (WAL) или PostgreSQL 17 · OpenCV · jsQR ·
WebSocket · JWT/PBKDF2 · vanilla JS SPA + PWA · Docker · Nginx · systemd.
Python 3.11+.
Источник: README, разделы «Технологии» и «Быстрый старт».

## Окружения и ссылки

| Окружение | Что известно | Ссылка |
|---|---|---|
| Репозиторий | публичный; ветки `main`, `arena/01a0caaa-1c-chek` (PR #1), `arena/071f4163-1c-chek` (PR #2), `arena/c7f9d34a-1c-chek` (PR #3) | [GitHub](https://github.com/Rimlin-UNC/1c_chek) |
| Рабочий сервер | VPS: `/opt/ymaster-check`, systemd-сервис `ymaster-check`, установка `deploy.sh`, обновление из программы. Домен, IP и ветку-источник агент проверить не может — **требует уточнения** | [docs/DEPLOYMENT.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/DEPLOYMENT.md) |
| Supabase | проект `chek.base` (`putddctwdskfdzichigx`, eu-west-1, PostgreSQL 17), только демо-контур | [Dashboard](https://supabase.com/dashboard/project/putddctwdskfdzichigx) |
| Vercel | **демо-контур** (ADR-13): проект `1c-chek` (команда chek5, тариф Hobby), превью на каждый push и production из `main`, домен `1c-chek.vercel.app` за Vercel Authentication; рабочее окружение — сервер владельца, не Vercel; размер сборки держит `.vercelignore` | [Dashboard](https://vercel.com/chek5/1c-chek) |
| GitHub Actions | CI на каждый push в `main` и `arena/**`: Python 3.11 и 3.12, PostgreSQL 17 (ADR-17) | [Actions](https://github.com/Rimlin-UNC/1c_chek/actions/workflows/ci.yml) |
| Notion | не используется (ADR-16): база знаний — в `docs/kb`, для владельца — [презентация](https://docs.google.com/presentation/d/1Y6xLDOvQKaBcpTJLjsGLS5KjpN4N4kOxbYA3H6egdoc) | [issue #17](https://github.com/Rimlin-UNC/1c_chek/issues/17) |

## Источник истины и вспомогательные инструменты

GitHub — единственный источник истины (ADR-19). Остальное — представления,
которые строятся из репозитория и сами источником не являются.

| Инструмент | Что там | Роль |
|---|---|---|
| GitHub: код, `docs/*.md`, `AGENTS.md`, `docs/kb/*.csv`, issues, CI | всё о проекте | **источник истины** |
| [Google Sheets](https://docs.google.com/spreadsheets/d/1CERkw3bydU-avV7saZkNT0ibEkExFMx-hnKdKJn6VvI/edit) | QA-панель «Матрица тестирования миграции» и канбан задач | представление: матрица из `qa/test_matrix.py`, канбан импортирует `docs/kb/02_roadmap.csv` |
| [Google Docs](https://docs.google.com/document/d/1l-7ndekun1QeBWv4UF4dEcrK0lvcIsBY4mcbcS1X4Xk/edit) | отчёт о состоянии и готовности миграции | отчёт по фактам репозитория |
| [Google Slides](https://docs.google.com/presentation/d/1Y6xLDOvQKaBcpTJLjsGLS5KjpN4N4kOxbYA3H6egdoc) | презентация для владельца | срез для владельца |
| Supabase `chek.base` | синтетический демо-контур | не рабочая БД (ADR-10) |
| Vercel `1c-chek` | демо-контур: превью-сборки | сигнал сборки, решение 07.11.2026 — ADR-13 (#13) |
