# 🛠 Руководство по развёртыванию «Ямастер Чек» (Ubuntu 24.04)

> **Разработчик и владелец идеи: ООО «Ямастер»** · [ymaster.ru](https://ymaster.ru) · info@ymaster.ru

Три способа установки — выберите свой:

| Способ | Кому подходит | Раздел |
|---|---|---|
| **install.sh** (рекомендуется) | продакшен на «голом» Ubuntu 24.04: systemd + Nginx + SSL | [1](#1-автоустановка-скриптом-installsh) |
| **Docker Compose** | продакшен в контейнерах, PostgreSQL 17 | [2](#2-докер-docker-compose) |
| **Ручной запуск** | разработка и быстрое демо | [3](#3-ручной-запуск-для-разработки-и-демо) |

---

## 1. Автоустановка скриптом install.sh

### 1.1. Требования

- Ubuntu 24.04 LTS (подойдут и 22.04+), 2 ГБ RAM, 10 ГБ диска;
- домен, указывающий на сервер (для SSL; без домена можно работать по IP:8000);
- открытые порты 80, 443 (и 8000, если без Nginx).

### 1.2. Запуск

```bash
# базовая установка (только приложение + systemd)
sudo bash install.sh

# с Nginx и доменом
sudo bash install.sh --with-nginx

# с Nginx + SSL Let's Encrypt
sudo bash install.sh --with-nginx --with-ssl=check.example.ru
```

Скрипт выполняет: установку python3/venv → пользователя `ymaster` и `/opt/ymaster-check` →
зависимости в venv → генерацию `SECRET_KEY` и токена 1С в `.env` → инициализацию БД и
администратора → systemd-сервис с автозапуском → Nginx/SSL (опционально).

### 1.3. После установки

| Что | Где |
|---|---|
| Веб-клиент | `http://<IP>:8000` (или ваш домен) |
| Swagger API | `http://<адрес>/api/docs` |
| Логи | `journalctl -u ymaster-check -f` |
| Конфигурация | `/opt/ymaster-check/.env` |
| База данных | `/opt/ymaster-check/data/ymaster_check.db` |

Обязательно: войдите (`admin` / `admin123`) и смените пароль.

### 1.4. Управление сервисом

```bash
sudo systemctl status ymaster-check
sudo systemctl restart ymaster-check
sudo systemctl stop ymaster-check
```

### 1.5. Резервное копирование

SQLite-база — один файл, достаточно файловой копии:

```bash
# крон: каждый день в 02:00
0 2 * * * sqlite3 /opt/ymaster-check/data/ymaster_check.db ".backup /var/backups/ymaster-$(date +\%F).db"
```

### 1.6. Обновление версии

```bash
cd 1c_chek && sudo bash install.sh   # повторный запуск обновит файлы и перезапустит сервис
```

### 1.7. Аварийный доступ (восстановление админа)

```bash
cd /opt/ymaster-check && sudo -u ymaster venv/bin/python - <<'EOF'
from app.database import SessionLocal, init_db
from app.models import User
from app.auth import hash_password
init_db()
db = SessionLocal()
db.add(User(username="recovery", full_name="Восстановление доступа",
            password_hash=hash_password("Temp#2026Pass"), role="admin"))
db.commit()
print("Создан admin recovery / Temp#2026Pass — смените пароль после входа!")
EOF
```

---

## 2. Докер (Docker Compose)

### 2.1. Запуск

```bash
cp .env.example .env
# заполните SECRET_KEY (python3 -c "import secrets; print(secrets.token_urlsafe(48))")
docker compose up -d --build
```

Состав: контейнер `app` (FastAPI) + контейнер `db` (PostgreSQL 17). Данные — в volumes
`app_data` и `pg_data`, конфигурация — из `.env`.

### 2.2. PostgreSQL

В compose приложение автоматически использует PostgreSQL
(`DATABASE_URL=postgresql+psycopg2://...`). Для ручной установки PostgreSQL раскомментируйте
`psycopg2-binary` в `requirements.txt` и укажите строку подключения в `.env`.

### 2.3. Операции

```bash
docker compose logs -f app        # логи
docker compose restart app        # перезапуск
docker compose down               # остановка (данные в volumes сохранятся)
docker compose exec db pg_dump -U ymaster ymaster_check > backup.sql   # бэкап
```

---

## 3. Ручной запуск (для разработки и демо)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 -m app.seed            # создать админа и маппинг
python3 -m app.seed --demo     # + демо-чеки (36 шт. за 14 дней)

python3 run.py --port 8000     # http://0.0.0.0:8000
```

Флаг `--reload` для разработки: `uvicorn app.main:app --reload --port 8000`.

---

## 4. Безопасность в продакшене (чек-лист)

- [ ] `SECRET_KEY` — уникальный (генерируется install.sh / указан в .env);
- [ ] пароль `admin` сменён, созданы персональные учётки;
- [ ] включён HTTPS (install.sh `--with-ssl=домен`, certbot сам обновляет сертификат);
- [ ] токен 1С перевыпущен в интерфейсе и вставлен в 1С;
- [ ] порт 8000 закрыт извне файрволом, если работаете через Nginx:
      `sudo ufw allow 80,443/tcp && sudo ufw deny 8000/tcp`;
- [ ] настроен бэкап базы (раздел 1.5);
- [ ] для реальных проверок подключён Мастер-токен ФНС
      ([docs/knowledge/fns_auth.md](knowledge/fns_auth.md)).

## 5. Мониторинг

- `GET /health` — лёгкая проверка живости (для Zabbix/Prometheus blackbox);
- `/api/v1/dashboard/stats` — метрики по чекам (требует JWT);
- журнал systemd + access/error логи Nginx.

## 6. Масштабирование

Запуск нескольких инстансов за Nginx:

```bash
uvicorn app.main:app --port 8000 --workers 4
```

Для горизонтального масштабирования переключитесь на PostgreSQL (общее хранилище для
всех инстансов). Очередь фоновых задач допускает вынос в Celery/Redis без изменения
API — точка расширения помечена в `app/routers/receipts.py`.

---

© 2026 **ООО «Ямастер»** — разработка и идея · [ymaster.ru](https://ymaster.ru) · info@ymaster.ru
