# Миграция ребренда: Ямастер Чек → DocVer

Дата: 2026-11-24  
Ветка: `arena/bc12beb3-1c-chek` (от `main@927350d2`)

## Что изменилось

| Область | Было | Стало |
|---|---|---|
| Продукт | Ямастер Чек | **DocVer — Документальная верификация** |
| Домен | `chek.ymaster.ru` | `docver.ru` |
| Почта | `chek@ymaster.ru` | `i@docver.ru` |
| Компания | ООО «Ямастер», `ymaster.ru` | **без изменений** |
| Cookie сессии | `ymaster_token` | `docver_token` (+ fallback чтения `ymaster_token`) |
| БД по умолчанию | `data/ymaster_check.db` | `data/docver.db` (+ fallback на legacy файл, если он есть) |
| Env-префикс | `YM_*` | `DOCVER_*` канонический, `YM_*` принимается как алиас |
| Деплой-артефакты | `ymaster-check.service`, `/opt/ymaster-check`, `/etc/ymaster-check` | `docver.service`, `/opt/docver`, `/etc/docver` (deploy.sh имеет compat-определение legacy) |

## Что уже сделано в коде (безопасная совместимость)

- **БД**: `app/config.py` — если `DATABASE_URL` не задан, `docver.db` отсутствует, а `ymaster_check.db` существует, используется legacy файл.
- **Сессии**: чтение `docver_token` или `ymaster_token`; установка `docver_token`; logout чистит обе cookie.
- **Env**: `_env()` в `app/config.py` и прямые чтения в `app/main.py`, `app/primary/inbox.py`, `app/primary/mailbox.py`, `app/routers/receipts.py`, `app/services/updater.py` принимают `DOCVER_*` и `YM_*`.
- **deploy.sh**: определяет legacy каталог/сервис/conf и продолжает работу на старых установках.
- **Бренд**: манифест, index, логотипы/SVG/PNG, brandbook, favicon, PWA-иконки обновлены.

## План миграции на проде (ручные шаги)

### 1. DNS / TLS / почта
- [ ] Создать/проверить A/AAAA/CNAME для `docver.ru` (и `www` при необходимости).
- [ ] Выпустить SSL (Let's Encrypt/certbot) для `docver.ru`.
- [ ] Настроить почтовый ящик `i@docver.ru`.
- [ ] Настроить SPF, DKIM, DMARC для домена `docver.ru`; проверить доставляемость.
- [ ] После проверки включить редирект `chek.ymaster.ru` → `docver.ru` (301) на переходный период.

### 2. Приложение / сервис
- [ ] Сделать бэкап БД и `.env` перед обновлением.
- [ ] Обновить код до релиза с ребрендом.
- [ ] Проверить `.env`: можно оставить `YM_*`, но предпочтительно постепенно перейти на `DOCVER_*`.
- [ ] Если используется SQLite по умолчанию и файл `data/ymaster_check.db` — ничего срочно не переименовывать: fallback поддерживается. Плановое переименование: остановить сервис → скопировать `ymaster_check.db` в `docver.db` → задать/проверить `DATABASE_URL` → старт → проверка → удалить старый файл после резервной выдержки.
- [ ] Если используется PostgreSQL — переименование БД/пользователя не обязательно; достаточно обновить `DATABASE_URL` при желании.

### 3. systemd / nginx / fail2ban (переименование артефактов)
Текущий `deploy.sh` умеет работать с legacy именами. Чистая миграция:
- [ ] Остановить старый сервис: `systemctl disable --now ymaster-check`.
- [ ] Установить новый unit: `deploy/systemd/docver.service` → `/etc/systemd/system/docver.service`.
- [ ] `systemctl daemon-reload && systemctl enable --now docver`.
- [ ] Обновить nginx vhost: `deploy/nginx/docver.conf`, домен `docver.ru`, SSL.
- [ ] Обновить fail2ban: `deploy/fail2ban/docver.conf`/`jail-docver.local`.
- [ ] Проверить `/health`, логин, загрузку статики, PWA-манифест.

### 4. Внешние интеграции
- [ ] **MAX-бот**: в коде целевой ник `docver.bot`; на платформе MAX нужно переименовать/создать бота и проверить webhook/miniapp.
- [ ] **FNS AppId**: `FNS_CLIENT_APP_ID` по умолчанию `DOCVER` — сверить с зарегистрированным значением; при отличии задать в env.
- [ ] **1C/форматы**: идентификаторы форматов (`ymaster-push-v1`, `ymaster-chek-package` и др.) не менялись механически там, где это риск совместимости; любое изменение делать через версионирование формата и двойную поддержку.
- [ ] **Telegram/AdGuard/другие сервисы**: проверить callback URL, домены, sudoers/helper пути на старых установках.

### 5. Проверка после миграции
- [ ] `GET /health` возвращает 200.
- [ ] Логин/логаут работают; старая сессия `ymaster_token` принимается до перелогина.
- [ ] PWA: манифест/иконки/название DocVer.
- [ ] Отправка писем с `i@docver.ru`.
- [ ] Загрузка/проверка чека, выгрузка 1С, API-эндпоинты.
- [ ] В логах нет ошибок по старым путям/именам.

## Риски и откат

- **Риск БД по умолчанию**: на нестандартной установке, где код рассчитывал на `ymaster_check.db` по пути по умолчанию, fallback покрывает типовой случай; при кастомном `DATABASE_URL` поведение не меняется.
- **Риск переименования сервисов**: делать только после проверки `docver.service` на стенде/в maintenance window; иметь готовый откат на `ymaster-check.service`.
- **Риск внешних ID**: FNS/MAX/1C — не менять «вслепую», подтверждать во внешних кабинетах.
- **Откат**: вернуть предыдущий релиз/ветку, восстановить `.env` и БД из бэкапа, включить старый systemd/nginx/conf. Cookie fallback и YM_* алиасы снижают риск при частичном откате.
