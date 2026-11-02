# 🧾 1c_chek — база знаний проекта

> **Основной контекст агента на каждую новую сессию.** Начинайте с
> [«Алгоритма работы агента»](07_agent_algorithm.md); вход для новых сессий —
> [`AGENTS.md`](../../AGENTS.md). База знаний живёт в репозитории (ADR-16):
> страницы — Markdown, базы — CSV. Notion не используется.
>
> Обновлено: 2026-11-02 · ветка `arena/c7f9d34a-1c-chek` · код проверен CI на
> коммите [`820bf7c`](https://github.com/Rimlin-UNC/1c_chek/commit/820bf7c9df7008aeacca88b1955a5ba8dca8fea2).

## Состояние на 2026-11-02

| Что | Состояние | Источник |
|---|---|---|
| Последний выпуск | **v1.70.82** — стабильный единый реестр чеков, готов к установке из программы | [PR #3](https://github.com/Rimlin-UNC/1c_chek/pull/3), [CHANGELOG](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/CHANGELOG.md) |
| Рабочий сервер | по последнему отчёту владельца (01.11.2026): v1.70.80, сборка `b3db4fe`. Установлена ли 1.70.82 — **требует уточнения** | [issue #4](https://github.com/Rimlin-UNC/1c_chek/issues/4) |
| `main` | v1.70.15 (`01c72d7`, слияние PR #1) | [GitHub](https://github.com/Rimlin-UNC/1c_chek/tree/main) |
| Открытые PR | #2 — WIP, целиком вошёл в #3; #3 — v1.70.82 | [PR #2](https://github.com/Rimlin-UNC/1c_chek/pull/2), [PR #3](https://github.com/Rimlin-UNC/1c_chek/pull/3) |
| Задачи | issues #4–#18 = база «Задачи и roadmap»; статус — в CSV (метки созданы, но не прикреплены — 403) | [открытые issues](https://github.com/Rimlin-UNC/1c_chek/issues?q=is%3Aissue+is%3Aopen) |
| CI | GitHub Actions на каждый push: Python 3.11 и 3.12, PostgreSQL 17, вся цепочка; ворота — 0 упавших и 0 пропущенных. Первый зелёный прогон (`820bf7c`): 4555 passed, 0 skipped на обеих версиях | [run 41402055601](https://github.com/Rimlin-UNC/1c_chek/actions/runs/41402055601), [ci.yml](https://github.com/Rimlin-UNC/1c_chek/blob/820bf7c9df7008aeacca88b1955a5ba8dca8fea2/.github/workflows/ci.yml) |
| Supabase `chek.base` | только синтетический демо-контур `chek_demo`; файлы миграций = применённая история (#8); обзор безопасности — критичных проблем нет, решения владельца — #18; осталась схема `qa_lock_…` (удаление — только по «да») | [05_db_and_migrations.md](05_db_and_migrations.md) |
| Vercel `1c-chek` | превью на каждый push; production из `main` (v1.70.15); роль — требует уточнения | [issue #13](https://github.com/Rimlin-UNC/1c_chek/issues/13) |
| Миграция | единый слой — в 1.70.82 в режиме совместимости, установка проверена; перенос архива и переход рабочей БД на PostgreSQL/Supabase **не готовы для рабочего сервера** | [05_db_and_migrations.md](05_db_and_migrations.md) |
| Notion | не используется — решение владельца 02.11.2026 (ADR-16); T-14 отменена | [issue #17](https://github.com/Rimlin-UNC/1c_chek/issues/17) |
| Презентация для владельца | «Ямастер Чек — состояние и план (02.11.2026)», 8 слайдов | [Google Slides](https://docs.google.com/presentation/d/1Y6xLDOvQKaBcpTJLjsGLS5KjpN4N4kOxbYA3H6egdoc) |

## Разделы

1. [Обзор проекта](01_overview.md)
2. [Задачи и roadmap](02_roadmap.md) — база [`02_roadmap.csv`](02_roadmap.csv)
3. [Архитектура и решения (ADR)](03_adr.md) — база [`03_adr.csv`](03_adr.csv)
4. [Runbook](04_runbook.md)
5. [Состояние БД и миграции](05_db_and_migrations.md)
6. [Открытые вопросы и блокеры](06_open_questions.md) — база [`06_open_questions.csv`](06_open_questions.csv)
7. [Алгоритм работы агента](07_agent_algorithm.md)
8. [Журнал сессий](08_session_journal.md) — база [`08_session_journal.csv`](08_session_journal.csv)

Связи между базами — по идентификаторам: задачи `T-NN`, решения `ADR-NN`,
вопросы `Q-NN`. Каждая задача ссылается на свой issue в GitHub.

## Правила

- **Репозиторий публичный.** Секреты (значения переменных окружения, ключи,
  токены, строки подключения), персональные данные и строки из таблиц БД сюда не
  попадают — только имена, схема и агрегаты.
- Неизвестное или противоречивое — «**требует уточнения**», без догадок.
- У каждой записи есть ссылка на источник: файл на коммите, PR/issue, схема
  Supabase, деплой Vercel.
- Сначала прочитать, потом обновлять. Дубликаты не создавать. **Статус задачи
  ведётся в CSV.** Токен интеграции GitHub может создавать issues, но не
  комментировать и не править их (HTTP 403, проверено 2026-11-02). Issue
  закрывается через `Closes #N` в описании PR при слиянии или владельцем.
- Канонические технические детали остаются в `docs/` репозитория. Здесь —
  статус, решения, процесс и ссылки.

## Презентация для владельца

[«Ямастер Чек — состояние и план»](https://docs.google.com/presentation/d/1Y6xLDOvQKaBcpTJLjsGLS5KjpN4N4kOxbYA3H6egdoc) в Google Slides — короткий срез
этой базы знаний для владельца: где мы сейчас, как поставить обновление,
качество и CI, база данных и Supabase, решения владельца, что дальше, где всё
лежит.

Обновляет агент в конце сессии через коннектор Google Slides, только по фактам
этой базы знаний:

- `slide02`, `slide04`–`slide07` — срез, CI, БД, решения, roadmap;
- `slide03` — если меняется порядок обновления; `slide08` — ссылки;
- дата среза — в названии презентации и на титульном слайде.

Секретов, строк данных и персональных данных в презентации нет — как и здесь.
