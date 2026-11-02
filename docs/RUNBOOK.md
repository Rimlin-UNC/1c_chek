# Runbook: запуск, тесты, CI, выпуск и деплой

[AGENTS.md](../AGENTS.md) · [Обзор](OVERVIEW.md) · [Архитектура (ADR)](ARCHITECTURE.md) · [Runbook](RUNBOOK.md) · [База данных](DATABASE.md) · [Задачи и журнал](kb/README.md)

Канонический документ (ADR-19); перенесён из `docs/kb/04_runbook.md` 2026-11-02.

Только то, что подтверждено README, конфигами и `docs/`. Ссылки ведут на файлы
коммита `cd76923`; база ссылок — `https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/`.

## Локальный запуск

Источник: README, раздел «Быстрый старт».

```bash
pip3 install -r requirements.txt
python3 -m app.seed --demo          # админ + маппинг + демо-чеки
python3 run.py --port 8000          # http://localhost:8000, Swagger: /api/docs
```

Вход по умолчанию — `admin` / `admin123`; система требует сменить пароль.

## Окружение разработки

Источник: [docs/development_environment.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/development_environment.md).

```bash
qa/setup_dev.sh --with-postgres            # .venv, requirements-dev.txt, psycopg 3, node_modules
qa/setup_dev.sh --full                     # + pgserver и валидаторы stylelint, html-validate — всё как в CI
qa/setup_dev.sh --check --full             # проверка без установки
python3 qa/session_check.py                # старт сессии: git против GitHub, окружение, вход, последний CI
```

> **Сессии Arena:** `.venv` и `node_modules` не сохраняются между сессиями —
> пересоздавать. Если после восстановления локальная ветка указывает на старый
> коммит, а файлы показаны как изменённые, — см. «Проверку git» в
> [AGENTS.md](../AGENTS.md), шаг 0. Клон неглубокий: для истории нужен
> `git fetch --deepen=300 origin <ветка>`.

## Тесты и качество

| Что | Команда | Источник |
|---|---|---|
| Критичный набор (~1 мин) | `./test.sh` | `test.sh` |
| UI-харнесс | `./test.sh ui` | `test.sh` |
| Полный прогон (до 4 воркеров) | `./test.sh full -j2` | `test.sh` |
| pytest напрямую | `.venv/bin/pytest tests -q -n 2` | development_environment.md |
| Ruff (должно быть «All checks passed!») | `.venv/bin/ruff check app tests` | `ruff.toml` |
| Храповик долга линтера | входит в pytest: `tests/test_v17038.py::TestLintDebtRatchet` | `tests/test_v17038.py` |
| ESLint | `npm run lint` | `package.json` |
| Vitest | `npm run test:js` | `package.json` |
| Playwright (свой сервер на 127.0.0.1:8123, отдельная БД) | `npm run test:e2e`; браузер — `npx playwright install chromium` или `YM_PLAYWRIGHT_CHROMIUM` | `playwright.config.js` |
| Postman/Newman | `npm run test:api` (ожидает сервер на 127.0.0.1:8002) | `package.json` |
| PostgreSQL (opt-in) | `UNIFIED_POSTGRES_TEST_URL=postgresql+psycopg://… .venv/bin/pytest tests/test_postgres_identity_optin.py tests/test_unified_postgres.py tests/test_supabase_migrations.py tests/test_migration_compat_postgres.py -q` | docstrings тестов, development_environment.md |
| Локальный PostgreSQL и прогон как в CI | `python qa/pg_local.py` — тесты PostgreSQL на временном сервере; `python qa/pg_local.py full` — весь набор с отчётами JUnit и воротами | `qa/pg_local.py` |
| Ворота отчётов JUnit (как в CI) | `YM_JUNIT_DIR=/tmp/junit ./test.sh full -j2` → `python3 qa/junit_gate.py /tmp/junit/*.xml` | `qa/junit_gate.py`, `test.sh` |
| CI | push в `main` и `arena/**`; вручную — Actions → CI → Run workflow | `.github/workflows/ci.yml` |
| Матрица тестирования (QA-панель) | `python qa/test_matrix.py --junit /tmp/junit/*.xml --commit <sha> --ci-run <id> --ci-fingerprint <отпечаток> --out /tmp/test_matrix.csv` | `qa/test_matrix.py` |
| Аудит RLS и внешних ключей Supabase (только чтение) | `supabase/checks/rls_fk_audit.sql` — в SQL-редакторе или коннектором | [DATABASE.md](DATABASE.md#проверки-rls-и-внешних-ключей) |
| Репетиция обновления | `.venv/bin/python -m qa.upgrade_rehearsal --from-ref <коммит на сервере>` | `qa/upgrade_rehearsal.py` |
| Этап Б: обновление из программы с GitHub, откат и повтор (после push) | `.venv/bin/python -m qa.live_update --from-ref <коммит на сервере>` | `qa/live_update.py` |
| Сквозной тест миграции (#20): 1.70.80 → текущая → перенос архива → сверка → откат и повтор | `python3 qa/pg_local.py pytest tests/test_migration_e2e.py` (SQLite и PostgreSQL); отчёт по фазам — `.venv/bin/python -m qa.migration_e2e [--postgres]` | `tests/test_migration_e2e.py`, `qa/migration_e2e.py`, [DATABASE.md](DATABASE.md#сквозной-тест-миграции-20) |

Правила:
- Прогон pytest **не прерывать**: обрыв оставляет мусор в БД тестов.
- Выпуск — только при **0 failed** ([update_playbook](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/update_playbook.md)).
- Тесты PostgreSQL — **только на одноразовой базе**. Коннектор Supabase не даёт
  песочнице прямого сетевого доступа к PostgreSQL. Локально — временный сервер
  `qa/pg_local.py` (pip-пакет `pgserver`, вне зависимостей проекта).
- **В CI пропусков 0** — ворота `qa/junit_gate.py` считают пропуск провалом.
  Локально пропуски зависят от окружения: в песочнице Arena — 1 (браузер
  Playwright не скачать: CDN закрыт), без npm и PostgreSQL — 14. На коммите
  `ffe77e3` без npm и PostgreSQL их было 12, из них критичны для миграции 2 —
  разбор в [#19](https://github.com/Rimlin-UNC/1c_chek/issues/19), данные — `qa/reports/skips_ffe77e3.csv`.

## CI

[`.github/workflows/ci.yml`](https://github.com/Rimlin-UNC/1c_chek/blob/820bf7c9df7008aeacca88b1955a5ba8dca8fea2/.github/workflows/ci.yml) (ADR-17):

- **Когда:** push в `main` и `arena/**`, PR из форков в `main`, вручную. Для
  веток этого репозитория прогон по `pull_request` пропускается — их уже
  проверил push.
- **Что:** матрица Python 3.11 и 3.12 (3.12 — как на Ubuntu 24.04 рабочего
  сервера). Сервис `postgres:17` без пароля, доступный только заданию, включает
  тесты PostgreSQL через `UNIFIED_POSTGRES_TEST_URL`. Окружение — как
  `qa/setup_dev.sh --with-postgres`, плюс Node 22, stylelint, html-validate и
  Chromium для Playwright. Шаги: ruff `app tests qa`, ESLint,
  `./test.sh full -j4` с отчётами JUnit, ворота.
- **Ворота** `qa/junit_gate.py`: зелёный только при 0 упавших и 0 пропущенных.
  Каждое падение и пропуск — аннотация с причиной и хвостом трассировки, итог —
  аннотация `notice` с отпечатком набора тестов (sha256 отсортированных
  идентификаторов). Отчёты JUnit — артефакт `junit-python-*` (14 дней).
- **Время** — около 5 минут. Секретов нет.

Песочница Arena не открывает хосты логов и артефактов GitHub — результат
читается через API (аннотации отдаёт `api.github.com`):

```bash
gh api "repos/Rimlin-UNC/1c_chek/actions/runs?branch=<ветка>&event=push&per_page=1" \
  --jq '.workflow_runs[] | [.id, .status, .conclusion, .head_sha[0:7]] | @tsv'
gh api "repos/Rimlin-UNC/1c_chek/commits/<sha>/check-runs" \
  --jq '.check_runs[] | [.id, .name, .conclusion] | @tsv'
gh api "repos/Rimlin-UNC/1c_chek/check-runs/<id>/annotations" --jq '.[] | .title + "\n" + .message'
```

Падение в CI, которого нет локально, — признак зависимости теста от машины.
Так первый прогон нашёл два теста апдейтера, зависевших от версии systemd
(252 в песочнице, 255 на Ubuntu 24.04). Чинить тест — изолировать окружение, —
а не ослаблять CI.

## QA-панель в Google Sheets

[«Матрица тестирования миграции — 1c_chek»](https://docs.google.com/spreadsheets/d/1CERkw3bydU-avV7saZkNT0ibEkExFMx-hnKdKJn6VvI/edit) — представление репозитория:

| Лист | Откуда данные |
|---|---|
| «Тесты» | все тесты прогона: область, критичность для миграции, статус локально и в CI, пропуск на `ffe77e3`, issue, ссылка на файл — `qa/test_matrix.py` |
| «12 пропусков» | `qa/reports/skips_ffe77e3.csv` (#19) |
| «Сводка» | формулы по листу «Тесты» и итоги прогона CI |
| «Supabase» | результат `supabase/checks/rls_fk_audit.sql` на проекте |
| «Задачи (GitHub)» и «Канбан» | `IMPORTDATA` из `docs/kb/02_roadmap.csv` в ветке; колонки Todo / In Progress / Blocked / Done |

Обновление листа «Тесты»:

1. Полный прогон с отчётами: `python qa/pg_local.py full` — печатает каталог
   отчётов JUnit и отпечаток набора тестов.
2. Отпечаток набора из прогона CI того же коммита — аннотация `notice` (раздел «CI»).
3. `python qa/test_matrix.py --junit /tmp/junit/*.xml --commit <sha> --ci-run <id> --ci-fingerprint <отпечаток> --out /tmp/test_matrix.csv`.
   Статус «прошёл в CI» ставится только при совпадении отпечатков.
4. Загрузить CSV коннектором Google Drive (`upload_file` с
   `target_mime_type = application/vnd.google-apps.spreadsheet`), скопировать
   лист в панель (`copy_sheet`), временный файл — в корзину.

Канбан берёт статусы из CSV в GitHub. Лист «Задачи (GitHub)» — живой
`IMPORTDATA`: он заработает после того, как владелец один раз нажмёт
«Разрешить доступ» в браузере (через API этого не сделать). До тех пор канбан
показывает лист «Задачи (снимок)» — копию CSV на последнем коммите, и сам пишет,
какой источник использует. Снимок обновляет агент в конце сессии. Доску GitHub
Projects токен интеграции создать не может (403).

## Превью Vercel

- Каждый push в ветку собирает превью автоматически; проверка — последний деплой
  ветки в статусе `READY` и журнал сборки без ошибок (`list_deployments`,
  `get_deployment_events`).
- Ручной тестовый деплой — коннектором `create_deployment` с источником Git
  (ветка, без `target`): это превью, не production. Обычно он не нужен:
  Git-интеграция уже собирает превью того же коммита, и ручной деплой его
  дублирует (02.11.2026, коммит `c0f5578`: автоматический
  `dpl_4TxLe4gQNPYWnX1Kd8expSwCMEvg` и ручной `dpl_7LYmkypQ3StrJ2v98L1rkBeue4V3`).
  Ручной — READY, сборка 13 с, ошибок нет; Python 3.12 по умолчанию; пакет
  339 МБ больше стандартного, Vercel оптимизирует зависимости — риск холодного
  старта для #13. Превью `6444b58` — `dpl_DMuqvtGwAj1WyhhSxpXXH5YBc85A`, READY
  через ~30 с после создания.
- Работоспособность приложения на Vercel проверить нельзя: деплои закрыты
  Vercel Authentication, а песочница не открывает `vercel.app`. Роль проекта —
  требует уточнения ([#13](https://github.com/Rimlin-UNC/1c_chek/issues/13)).

## Стандарт выпуска

Источник: [docs/update_playbook.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/update_playbook.md).

- Версия синхронно в «7 местах»:
  - `app/config.py`;
  - `app/static/index.html` ×2 (`app.css?v=`, `app.js?v=`);
  - `app/static/sw.js` ×3 (CACHE и два `?v=`);
  - `manifest.webmanifest`;
  - пины изменённых блоков в `app/services/blocks.py`;
  - `CHANGELOG.md`;
  - `WHATS_NEW` в `app/static/js/app.js`.
- Тесты релиза дополнительно проверяют:
  - `maxapp/index.html` и `b2b/index.html` (`?v=`);
  - `docs/ui_journal.md` (`## vX.Y.Z`);
  - `VERSION` в `tests/test_v17033–37.py`.
- Окно обновления показывает первые 4000 символов CHANGELOG — запись о выпуске
  должна в них помещаться.
- Коммит: `vX.Y.Z: суть; NNN passed`.
- Не трогать: блок «Проверка чеков (ФНС и источники)», паузу антибана и квоту.
  Миграции — только добавляющие. Без Celery/Redis. Соблюдать 152-ФЗ. Не трогать
  `chek.ymaster.ru` и инфраструктуру деплоя.
- Если менялась схема БД или пути данных — прогнать `qa/upgrade_rehearsal.py`
  от коммита, установленного на сервере.
- Менялись единый реестр, перенос архива, сверка или `init_db` — сквозной тест
  миграции на обоих бэкендах: `python3 qa/pg_local.py pytest tests/test_migration_e2e.py`
  (в общем прогоне и в CI он идёт всегда).
- После push выпуска — этап Б: `qa/live_update.py` от коммита на сервере.
  Настоящее обновление из программы с GitHub (неглубокий клон, как
  `deploy.sh`; запуск командой systemd-юнита), откат из «Последних рабочих
  версий» и повторное обновление — 15 проверок; с 1.70.84 — ещё пакетное
  перемещение на обновлённом сервере и журнал карточки у бухгалтера другой
  компании.

## Рабочий сервер (VPS)

Источники: README, [docs/DEPLOYMENT.md](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/docs/DEPLOYMENT.md),
`app/services/updater.py`.

- **Установка.** `sudo bash deploy.sh` (опции: `--update`, `--branch=<ветка>`,
  `--with-ssl=<домен>`); классический вариант —
  `sudo bash install.sh --with-nginx --with-ssl=<домен>`.
- **Расположение.** Каталог `/opt/ymaster-check`, systemd-сервис `ymaster-check`.
  Проверка: `curl 127.0.0.1:8000/api/v1/about` или `/health`.
- **Обновление из программы.** Настройки → «🔄 Обновления» →
  «🎛 Источник обновлений» (owner/repo и ветка) → «🔍 Проверить обновления» →
  «⬇ Обновить». Конвейер:
  1. резервная копия БД;
  2. fetch и reset на ветку;
  3. pip — только если изменился `requirements.txt`;
  4. проверка целостности (`init_db` в отдельном процессе, 120 с);
  5. перезапуск: sudoers или re-exec.

  Обновление работает только вперёд по версии.
- **Откат.**
  - «↩ Последние рабочие версии» → «↩ Откатиться» (хранятся три);
  - откат к произвольному коммиту в той же карточке;
  - `sudo bash rollback.sh [версия]`, если программа не стартует.

  База при откате не меняется (ADR-03).
- **Docker.** `cp .env.example .env && docker compose up -d --build`.
- **Требует уточнения:** домен, IP, текущая ветка-источник и версия на сервере
  (Q-02, Q-14). Чек-лист продакшена — в конце DEPLOYMENT.md.

## Окружения

| Окружение | Назначение | Подтверждено |
|---|---|---|
| Рабочий VPS | рабочие данные (SQLite) | docs/DEPLOYMENT.md; параметры — требует уточнения |
| Локально / песочница | разработка, тесты, предпросмотр | README, development_environment.md |
| GitHub Actions | CI на каждый push: полный набор тестов, PostgreSQL 17 | `.github/workflows/ci.yml`, прогоны с 02.11.2026 |
| Supabase `chek.base` | read-only демо с синтетикой | supabase/README.md, проверка 2026-11-02 |
| Vercel `1c-chek` | превью-сборки на push; production из `main`; переменных нет; работоспособность не проверена | дашборд Vercel 2026-11-02; роль — требует уточнения (T-10) |

## Переменные окружения (только имена и назначение)

Источники: [`.env.example`](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/.env.example),
[`app/config.py`](https://github.com/Rimlin-UNC/1c_chek/blob/cd769234ebfc4d5c533a1df25fce31e8a9605212/app/config.py).
Значения не хранятся нигде, кроме `.env` на сервере.

| Имя | Назначение |
|---|---|
| `SECRET_KEY` | подпись JWT; в продакшене обязателен длинный случайный (deploy.sh генерирует сам) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | срок жизни токена входа |
| `DATABASE_URL` | БД приложения; по умолчанию SQLite `data/ymaster_check.db`, возможен PostgreSQL |
| `SUPABASE_DEMO_ENABLED`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | страница «Supabase демо»; только publishable key, не рабочая БД |
| `FRAME_ANCESTORS` | разрешение встраивания в iframe (по умолчанию `'none'`) |
| `FNS_API_BASE`, `FNS_MASTER_TOKEN`, `FNS_CLIENT_APP_ID` | Открытое API ФНС (мастер-токен и AppId выдаёт ФНС) |
| `FNS_DAILY_LIMIT`, `FNS_DAILY_LIMIT_ENABLED`, `FNS_CACHE_TTL_DAYS`, `FNS_TIMEOUT_SECONDS` | суточный лимит запросов к ФНС, кэш результатов, таймаут |
| `FNS_PROVIDER` | есть в `.env.example`, в коде не читается — **требует уточнения** (вероятно, устарела с v1.70.70) |
| `ONEC_API_TOKEN`, `ONEC_BATCH_SIZE` | обмен с 1С (`/onec/v1/*`), размер пакета |
| `CORS_ORIGINS` | кросс-доменные источники (по умолчанию пусто) |
| `TELEGRAM_PROXY`, `TELEGRAM_API_BASE` | доступ Telegram-бота сквозь блокировки |
| `MAX_UPLOAD_MB`, `PDF_MAX_MB`, `PAGE_MAX` | лимиты загрузки и пагинации |
| `RATE_LIMIT_LOGIN`, `RATE_LIMIT_REGISTER`, `RATE_LIMIT_ONEC`, `RATE_LIMIT_API` | пороги частоты запросов |
| `FIRST_RUN_SEED` | демо-данные при первом запуске |
| `YMASTER_BACKUP_EXTRA_DIR`, `TZ` | дополнительный каталог копий; часовой пояс |
| `YM_NO_REEXEC`, `YM_FETCHQ_OFF`, `YM_OSMON_OFF` | служебные выключатели: самоперезапуск, очередь загрузки, монитор OpenStack (тесты, QA) |
| `UNIFIED_POSTGRES_TEST_URL`, `YM_ASSESS_DATABASE_URL`, `YM_PLAYWRIGHT_CHROMIUM` | только для тестов и QA-скриптов |
| `FAKE_FNS_*` | имитатор ФНС `qa/fake_fns_api.py` |

Флаг `SELF_REEXEC` в `app/config.py` включает самоперезапуск после обновления;
его комментарий предлагает выключать через `.env`.
