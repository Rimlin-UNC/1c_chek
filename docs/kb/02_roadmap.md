# 2. Задачи и roadmap

[← База знаний](README.md) · База: [`02_roadmap.csv`](02_roadmap.csv) ·
Связанные разделы: [Открытые вопросы](06_open_questions.md) · [ADR](03_adr.md) ·
[Алгоритм](07_agent_algorithm.md)

Канонический список — **`02_roadmap.csv`**: в Notion это база с теми же
свойствами. Каждая открытая задача — issue в GitHub
([#4–#17](https://github.com/Rimlin-UNC/1c_chek/issues?q=is%3Aissue+is%3Aopen)).
В GitHub не было issues до 2026-11-02: задачи собраны из PR #1–#3, документов
репозитория и проверенного состояния Supabase и Vercel.

## Свойства базы

| Свойство | Значения |
|---|---|
| Статус | `todo` · `in-progress` · `blocked` · `done` |
| Приоритет | `P0` срочно · `P1` высокий · `P2` средний · `P3` низкий |
| Область | Релиз · Единый реестр · Миграция/БД · Supabase · Vercel · CI · Документация |
| Нужно подтверждение владельца | «да» — агент не выполняет без явного «да» (см. стоп-лист в [алгоритме](07_agent_algorithm.md)) |
| Блокер | что мешает (для `blocked`) |
| Issue/PR, Источник | обратные ссылки |
| Связанные ADR / вопросы | `ADR-NN`, `Q-NN` |

В репозитории созданы метки `roadmap`, `priority:P0…P3`, `area:*`, `blocked` и
`needs-owner-decision`, повторяющие свойства базы. Прикрепить их к issues токен
интеграции не может: при создании issue метки молча не применились, а правка
отвечает 403. Владелец может проставить их вручную; до тех пор приоритет и
область — только в CSV.

## Срез на 2026-11-02

**Ждут владельца (P0–P1):**
- [#4](https://github.com/Rimlin-UNC/1c_chek/issues/4) T-01 — установить v1.70.82 на рабочий сервер (P0).
- [#5](https://github.com/Rimlin-UNC/1c_chek/issues/5) T-02 — что делать с PR #2.
- [#6](https://github.com/Rimlin-UNC/1c_chek/issues/6) T-03 — сливать ли PR #3 в `main`.
- [#13](https://github.com/Rimlin-UNC/1c_chek/issues/13) T-10 — роль Vercel.

**Заблокированы:**
- [#7](https://github.com/Rimlin-UNC/1c_chek/issues/7) T-04 — схема `qa_lock_…` в Supabase: удаление заблокировано фильтром провайдера, нужно явное подтверждение.
- [#10](https://github.com/Rimlin-UNC/1c_chek/issues/10) T-07 — перенос архива: ждёт экрана разбора конфликтов.
- [#17](https://github.com/Rimlin-UNC/1c_chek/issues/17) T-14 — перенос базы знаний в Notion: нет родительской страницы.

**Можно брать агенту без подтверждения** (работа в ветке, затем PR):
- [#9](https://github.com/Rimlin-UNC/1c_chek/issues/9) T-06 — экран разбора конфликтов (P1).
- [#12](https://github.com/Rimlin-UNC/1c_chek/issues/12) T-09 — CI (P2).
- [#8](https://github.com/Rimlin-UNC/1c_chek/issues/8) T-05 — переименование файлов миграций; менять историю миграций в Supabase — только с подтверждения.
- [#14](https://github.com/Rimlin-UNC/1c_chek/issues/14) T-11 — обзор безопасности Supabase, только чтение (P2).
- [#15](https://github.com/Rimlin-UNC/1c_chek/issues/15) T-12 — README (P3).
- [#16](https://github.com/Rimlin-UNC/1c_chek/issues/16) T-13 — формат блоков в журнале обновления (P3).

**Требует уточнения:** [#11](https://github.com/Rimlin-UNC/1c_chek/issues/11)
T-08 — переход рабочей БД на PostgreSQL/Supabase.

**Готово:** T-15…T-20 — выпуски v1.70.9…v1.70.82, исторические дорожные карты
и эта база знаний. Подробности — в CSV и [журнале сессий](08_session_journal.md).

## Как обновлять

1. Взяли задачу — статус `in-progress` в CSV.
2. Открыли PR — ссылка в «Issue/PR»; в описании PR `Refs #N`.
3. Задача выполнена — `done` в CSV; в описании PR `Closes #N` (issue закроется
   при слиянии). Иначе закрывает владелец. Задачи, требующие решения владельца,
   закрываются только после его подтверждения.

> Токен интеграции GitHub (2026-11-02) создаёт issues, но комментарии и правка
> issues отвечают HTTP 403. Поэтому живой статус — в CSV, а не в issue.
4. Новая задача — сначала issue (метки — если токен позволит), затем строка в
   CSV с новым `T-NN`. Номера не переиспользуются.
