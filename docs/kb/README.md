# 🧾 1c_chek — база знаний проекта

> Здесь — **живые базы** проекта: задачи, открытые вопросы и журнал сессий
> (CSV). Алгоритм работы — [`AGENTS.md`](../../AGENTS.md); основные документы —
> в `docs/` (ADR-19). GitHub — единственный источник истины; Google Sheets, Docs
> и Slides — представления. Notion не используется.
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
| Задачи | issues #4–#21 = база «Задачи и roadmap»; статус — в CSV, канбан — в Google Sheets (доска GitHub Projects токену недоступна, метки не прикрепляются — 403) | [открытые issues](https://github.com/Rimlin-UNC/1c_chek/issues?q=is%3Aissue+is%3Aopen) |
| CI | GitHub Actions на каждый push: Python 3.11 и 3.12, PostgreSQL 17, вся цепочка; ворота — 0 упавших и 0 пропущенных. Прогон на `c0f5578`: 4562 passed, 0 skipped на обеих версиях, отпечаток набора `e9772826c527d81e` совпал с локальным | [run 41415352807](https://github.com/Rimlin-UNC/1c_chek/actions/runs/41415352807), [ci.yml](https://github.com/Rimlin-UNC/1c_chek/blob/820bf7c9df7008aeacca88b1955a5ba8dca8fea2/.github/workflows/ci.yml) |
| Supabase `chek.base` | только синтетический демо-контур `chek_demo`; файлы миграций = применённая история (#8); аудит RLS и внешних ключей — 0 ошибок; решения владельца — #18 и #21 (интеграция переключена на ветку PR #3, после слияния — на `main`); осталась схема `qa_lock_…` (удаление — только по «да», #7) | [DATABASE.md](../DATABASE.md) |
| Vercel `1c-chek` | превью на каждый push; production из `main` (v1.70.15); роль — требует уточнения | [issue #13](https://github.com/Rimlin-UNC/1c_chek/issues/13) |
| Миграция | единый слой — в 1.70.82 в режиме совместимости, установка проверена; перенос архива и переход рабочей БД на PostgreSQL/Supabase **не готовы для рабочего сервера**; совместимость схемы приложения с Supabase проверена в CI; сквозной тест — #20 | [DATABASE.md](../DATABASE.md) |
| Notion | не используется — решение владельца 02.11.2026 (ADR-16); T-14 отменена | [issue #17](https://github.com/Rimlin-UNC/1c_chek/issues/17) |
| Презентация для владельца | «Ямастер Чек — состояние и план (02.11.2026)», 8 слайдов | [Google Slides](https://docs.google.com/presentation/d/1Y6xLDOvQKaBcpTJLjsGLS5KjpN4N4kOxbYA3H6egdoc) |
| QA-панель и канбан | «Матрица тестирования миграции — 1c_chek»: все тесты, 12 пропусков (#19), проверки Supabase, канбан задач | [Google Sheets](https://docs.google.com/spreadsheets/d/1CERkw3bydU-avV7saZkNT0ibEkExFMx-hnKdKJn6VvI/edit) |
| Отчёт | состояние проекта и готовность миграции | [Google Docs](https://docs.google.com/document/d/1l-7ndekun1QeBWv4UF4dEcrK0lvcIsBY4mcbcS1X4Xk/edit) |

## Документы и базы

Основные документы (ADR-19):

- [AGENTS.md](../../AGENTS.md) — алгоритм работы агента и новых сессий;
- [OVERVIEW.md](../OVERVIEW.md) — обзор проекта;
- [ARCHITECTURE.md](../ARCHITECTURE.md) — архитектурные решения ADR-01…19;
- [RUNBOOK.md](../RUNBOOK.md) — запуск, тесты, CI, выпуск, деплой, QA-панель;
- [DATABASE.md](../DATABASE.md) — база данных, миграции, Supabase, `qa_lock_…`.

Живые базы здесь:

- [Задачи и roadmap](02_roadmap.md) — база [`02_roadmap.csv`](02_roadmap.csv);
- [Открытые вопросы и блокеры](06_open_questions.md) — база [`06_open_questions.csv`](06_open_questions.csv);
- [Журнал сессий](08_session_journal.md) — база [`08_session_journal.csv`](08_session_journal.csv).

Страницы `01`, `03`, `04`, `05`, `07` перенесены в документы выше и оставлены
ссылками.

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
