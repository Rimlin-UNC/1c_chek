# 🛠 Развёртывание «Ямастер Чек» на сервере Ubuntu 24.04

> **Разработчик и владелец идеи: ООО «Ямастер»** · [ymaster.ru](https://ymaster.ru) · info@ymaster.ru

Варианты установки — выберите свой:

| Способ | Кому подходит | Раздел |
|---|---|---|
| **deploy.sh из GitHub** ⭐ | продакшен: код тянется с вашего репозитория, всё настраивается автоматически | [1](#1-развёртывание-на-сервере-из-github-например-94183236179) |
| Docker Compose | контейнеры + PostgreSQL 17 | [2](#2-докер-docker-compose) |
| Ручной запуск | разработка и демо | [3](#3-ручной-запуск-для-разработки-и-демо) |

---

## 1. Развёртывание на сервере из GitHub (например, 94.183.236.179)

Код ставится **прямо из вашего репозитория** — одной командой на чистом Ubuntu 24.04.

### 1.1. Три команды на сервере

```bash
ssh root@94.183.236.179

# скачать скрипт развёртывания из вашего репозитория (ветка arena/01a0caaa-1c-chek):
curl -fsSL https://raw.githubusercontent.com/Rimlin-UNC/1c_chek/arena/01a0caaa-1c-chek/deploy.sh -o deploy.sh

# запустить:
sudo bash deploy.sh
```

Всё. Через 2–3 минуты система доступна по `http://94.183.236.179`.

### 1.2. Что делает deploy.sh

| Шаг | Действие |
|---|---|
| 1 | Устанавливает пакеты: `python3-venv, git, nginx, ufw, fail2ban, sqlite3` |
| 2 | Создаёт системного пользователя `ymaster`, каталог `/opt/ymaster-check` |
| 3 | **Клонирует ветку `arena/01a0caaa-1c-chek` с GitHub** (для приватного репозитория: `GITHUB_TOKEN=ghp_xxx sudo -E bash deploy.sh`) |
| 4 | Разворачивает venv, ставит зависимости из `requirements.txt` |
| 5 | Генерирует `SECRET_KEY` и токен 1С в `.env` (права 600) |
| 6 | Инициализирует БД и администратора (`admin` / `admin123`) |
| 7 | Ставит systemd-сервис с изоляцией (ProtectSystem=strict, NoNewPrivileges) |
| 8 | Настраивает Nginx (rate-limit, security headers, закрытые служебные пути) |
| 9 | Настраивает **UFW** (снаружи только 22/80/443) и **fail2ban** (бан за перебор) |

### 1.3. Опции скрипта

```bash
sudo bash deploy.sh --with-ssl=check.example.ru   # + Let's Encrypt (нужна A-запись DNS на сервер)
sudo bash deploy.sh --update                      # обновить код с GitHub и перезапустить
sudo bash deploy.sh --domain=check.example.ru     # указать server_name без SSL
sudo bash deploy.sh --no-nginx                    # без Nginx (не рекомендуется)
GITHUB_TOKEN=ghp_xxx sudo -E bash deploy.sh       # приватный репозиторий
BRANCH=main sudo -E bash deploy.sh                # другая ветка
```

### 1.4. Обязательные действия после установки

1. Откройте `http://94.183.236.179`, войдите `admin` / `admin123`;
2. **Система сама потребует сменить пароль** (администратор один, временный пароль сменяется при первом входе);
3. Создайте приглашения для сотрудников (см. руководство пользователя, раздел «Пользователи»);
4. Для сканирования с телефонов камеры нужен **HTTPS** — выпустите сертификат:
   ```bash
   sudo bash deploy.sh --update --with-ssl=ваш-домен.ru
   ```

### 1.5. Обновление версии

Все изменения пулаются в GitHub → на сервере одна команда:

```bash
sudo bash deploy.sh --update
```

### 1.6. Управление и диагностика

```bash
systemctl status ymaster-check        # статус
journalctl -u ymaster-check -f        # логи приложения
sudo fail2ban-client status ymaster-check   # кто забанен
sudo ufw status                       # файрвол
sqlite3 /opt/ymaster-check/data/ymaster_check.db ".tables"   # БД
```

---

## 2. Докер (Docker Compose)

```bash
git clone --branch arena/01a0caaa-1c-chek https://github.com/Rimlin-UNC/1c_chek.git
cd 1c_chek && cp .env.example .env    # заполните SECRET_KEY
docker compose up -d --build
```

Состав: `app` (FastAPI) + `db` (PostgreSQL 17). Данные — в volumes. Операции — `docker compose logs -f app`, бэкап — `docker compose exec db pg_dump -U ymaster ymaster_check > backup.sql`.

---

## 3. Ручной запуск (для разработки и демо)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.seed --demo
python3 run.py --port 8000
```

---

## 4. Архитектура безопасности (все механизмы — бесплатные)

Защита построена **в 6 слоёв** — от сети до приложения:

| Слой | Механизм | От чего защищает |
|---|---|---|
| 1. Сеть | **UFW**: снаружи открыты только 22 (limit), 80, 443; порт 8000 закрыт | прямой доступ к приложению в обход прокси |
| 2. Периметр | **fail2ban**: 12 ответов 401/429 за 2 минуты → бан IP на 15 мин | перебор паролей, сканеры, бот-бомбы |
| 3. Прокси | **Nginx limit_req/limit_conn**: вход 2 р/с, API 30 р/с, ≤40 соединений с IP | HTTP-flood, DDoS дешёвками |
| 4. Приложение | **Rate limiter** (скользящее окно): login 10/мин, регистрация 20/час, API 240/мин → 429 | подбор паролей, спам регистраций |
| 5. Логика | **LoginGuard**: 5 неудачных входов → блок (IP+логин) на 15 мин; администратор **всегда один**; регистрация **только по приглашениям**; временный пароль обязателен к замене | брутфорс, несанкционированные аккаунты, внутренние угрозы |
| 6. Данные | JWT (HS256, 12 ч), PBKDF2-260k, CSP/X-Frame-Options/nosniff, ORM (нет SQL-инъекций), `.env` вне git с правами 600, systemd-изоляция | XSS/кликджекинг, утечки, эскалация |

Дополнительно рекомендуется (по желанию):
- ограничить `/onec/` по IP сети 1С (готовый блок в конфиге Nginx);
- сменить порт SSH (`/etc/ssh/sshd_config`) — UFW `limit` уже смягчает брутфорс;
- включить автоматические обновления безопасности: `sudo apt install unattended-upgrades`.

### Чек-лист продакшена

- [ ] пароль `admin` сменён (система требует сама);
- [ ] `SECRET_KEY` сгенерирован (deploy.sh делает сам);
- [ ] HTTPS включён (`--with-ssl=домен`);
- [ ] приглашения создаются только под конкретных сотрудников (поле «Для кого»);
- [ ] токен 1С вставлен в модуль 1С и не публикуется;
- [ ] бэкап: крон `sqlite3 ... ".backup /var/backups/ymaster-$(date +\%F).db"`;
- [ ] (для реальных проверок) получен Мастер-токен ФНС.

---

## 5. Мониторинг и масштабирование

- `GET /health` — проверка живости (Zabbix/Prometheus blackbox);
- `/api/v1/dashboard/stats` — метрики по чекам (JWT);
- логи: systemd + Nginx access/error;
- масштабирование: `uvicorn app.main:app --workers 4`; для нескольких серверов — PostgreSQL (compose-профиль) за общим Nginx.

---

© 2026 **ООО «Ямастер»** — разработка и идея · [ymaster.ru](https://ymaster.ru) · info@ymaster.ru
