# Устойчивый доступ к Telegram Bot API через Cloudflare Worker (v1.70.68)

Комплект для сервера в Москве, когда прямые запросы к `api.telegram.org`
блокируются или нестабильны. Второй зарубежный сервер не используется и
не нужен — единственный внешний посредник — Cloudflare Worker на
бесплатном тарифе.

Выбор решения обоснован в отчёте `docs/telegram_proxy_audit_v17068.md`
(пакет `tgwsapiproxy`, репозиторий winchanii/TgWsApiProxy, лицензия MIT).

## Как это работает

```
Ямастер Чек ──HTTP CONNECT──▶ tgwsapiproxy ──WebSocket──▶ Cloudflare Worker ──TCP──▶ api.telegram.org
 (httpx, proxy=…)              127.0.0.1:8080              имя-abc.workers.dev          порт 443
```

* Блокировщик (DPI) на участке сервер → Cloudflare видит только
  `SNI=имя-abc.workers.dev`; `SNI=api.telegram.org` не покидает туннель.
* TLS остаётся сквозным: рукопожатие с `api.telegram.org` выполняет сам
  сервер, поэтому Worker ретранслирует шифрованные байты и НЕ видит ни
  токен бота, ни содержимое сообщений. (В схемах «реверс-прокси» токен
  виден воркеру — поэтому они отвергнуты.)
* Локальный прокси слушает только `127.0.0.1` — снаружи им воспользоваться
  нельзя.

## Шаг 1. Развернуть Worker (бесплатный аккаунт Cloudflare)

### Вариант А — через дашборд (проще)

1. `dash.cloudflare.com` → войти (или зарегистрировать бесплатный
   аккаунт; домен покупать не нужно).
2. `Compute` → `Workers & Pages` → `Create application` →
   `Start with Hello World!` → `Deploy`.
3. `Edit code` → заменить содержение редактора на файл `worker.js` из
   этой папки → `Deploy`.
4. Скопировать домен вида `имя-abc123.ваш-аккаунт.workers.dev`.

### Вариант Б — через Wrangler CLI

```bash
npm install -g wrangler
wrangler login
cd deploy/tg-cf-worker
wrangler deploy worker.js --name ymaster-tg-relay --compatibility-date 2025-01-01
```

### Проверка Worker

```bash
curl https://ВАШ-ДОМЕН.workers.dev/healthz     # → ok
curl https://ВАШ-ДОМЕН.workers.dev/diag        # → диагностика connect()/fetch()
```

**БЕЛЫЙ СПИСОК НЕ УБИРАТЬ.** В `worker.js` жёстко заданы `ALLOWED_HOSTS`
(только `api.telegram.org` и его IP) и `ALLOWED_PORTS` (только 443). Без
ограничения Worker превращается в open-proxy на инфраструктуре Cloudflare
— через него смогут сканировать порты, и Cloudflare забанит аккаунт.

## Шаг 2. Установить локальный прокси (Ubuntu 24.04)

```bash
cd deploy/tg-cf-worker
sudo ./install.sh ВАШ-ДОМЕН.workers.dev
```

Установщик:

* ставит `python3-venv` и `git` (пакет `tgwsapiproxy` имеет НУЛЕВЫЕ
  зависимости — весь код на стандартной библиотеке, поэтому ставится из
  Git даже когда PyPI недоступен из РФ);
* создаёт служебного пользователя `tgproxy` и окружение
  `/opt/tgwsapiproxy/venv`;
* ставит службу systemd `tgwsapiproxy.service` с `Restart=always`
  (graceful restart при падении через 3 секунды) и усилением
  (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
  `PrivateTmp`);
* поднимает слушатели `127.0.0.1:1080` (SOCKS5) и `127.0.0.1:8080`
  (HTTP CONNECT — его использует «Ямастер Чек»).

## Шаг 3. Настроить программу

Клиентский код менять НЕ нужно: поддержка прокси встроена с v1.29.0 —
`httpx` в `app/services/telegram_bot.py` уже передаёт прокси во все
вызовы (отправка, long-poll, диагностика-пинг).

* В веб-интерфейсе: `Настройки → Чек-Пул и интеграции → Telegram-бот →
  Прокси` = `http://127.0.0.1:8080` → сохранить; либо
* в `.env`: `TELEGRAM_PROXY=http://127.0.0.1:8080` и перезапуск службы
  программы.

## Как проверить, что трафик идёт через прокси

1. `./check_proxy.sh ВАШ-ДОМЕН.workers.dev` — служба активна, порты
   слушают только на локальном интерфейсе, `api.telegram.org` отвечает
   через прокси, Worker жив (`/healthz`, `/diag`).
2. В программе: `Настройки → Telegram-бот → Диагностика` — пинг
   (`ping_ms`) измеряется ровно тем путём, которым ходит бот (через
   настроенный прокси).
3. Журнал прокси: `journalctl -u tgwsapiproxy -f` — во время отправки
   уведомления видны соединения.
4. Контрольный опыт: очистить поле прокси → диагностика падает (если
   сеть блокирует Telegram), вернуть `http://127.0.0.1:8080` → работает.

## Как обновлять Worker

1. Взять свежий `worker.js` (из этой папки или из апстрима
   winchanii/TgWsApiProxy).
2. Дашборд: `Edit code` → вставить → `Deploy`; либо
   `wrangler deploy worker.js --name ymaster-tg-relay`.
3. Перепроверить: `curl https://ВАШ-ДОМЕН.workers.dev/healthz`.
4. Локальный прокси перезапускать не нужно — он обращается к Worker по
   тому же домену.

## Штатные ситуации

| Симптом | Действие |
|---|---|
| `check_proxy.sh`: FAIL на шаге 1 | `systemctl status tgwsapiproxy`, `journalctl -u tgwsapiproxy -e` |
| Прокси отвечает, но Worker `/diag` показывает FAIL `connect()` | тариф аккаунта не даёт сырые сокеты или адрес запрещён — см. вывод `/diag` |
| Порт 8080 занят | сменить `--http-port` в юните и адрес в настройках программы |
| Нужен SOCKS5 для других клиентов | `socks5://127.0.0.1:1080` уже поднят тем же юнитом |
| PyPI недоступен | не мешает: пакет ставится из Git, зависимостей нет |

## Соотношение с другими схемами программы

* `deploy/tg-bridge` (v1.70.12) — мост на ВТОРОЙ машине; при запрете
  зарубежных VPS неприменим, оставлен как историческая альтернатива.
* AdGuard VPN «TUN + selective» (v1.70.24) — в туннель уходит весь
  разрешённый трафик сервера; настоящая схема точечная: только Telegram,
  только бесплатный Worker, без VPN-подписок.
* Поле «Адрес API» (`TELEGRAM_API_BASE`) — для реверс-прокси схем; с
  настоящим комплектом не используется (остаётся `https://api.telegram.org`).
