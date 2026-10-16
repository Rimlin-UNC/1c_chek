# Telegram API-мост (v1.70.12)

Если сервер с «Ямастер Чек» не видит `api.telegram.org` (типично для
сетей РФ), а брать внешний socks5-прокси негде — поднимите крошечный
мост на любой машине, где Telegram доступен (недорогой VPS, домашний
компьютер, сервер знакомых). Мост — один файл на чистом Python без
зависимостей, без ключей и без базы данных.

## Как это работает

    Ямастер Чек ──(HTTP)──▶ мост ──▶ api.telegram.org ──▶ Telegram

В настройках программы («Настройки → Чек-Пул и интеграции →
Telegram-бот → Адрес API») вместо `https://api.telegram.org` указывается
адрес моста — и весь трафик бота (long-poll, напоминания, диагностика)
идёт через него. Токен бота остаётся зашифрованным на вашем сервере:
мост ретранслирует те же запросы `/bot…/method`, что и любой узел сети.

## Вариант А (рекомендуется): мост на второй машине + SSH-туннель

Порты наружу не открываем вовсе.

1. На второй машине поставьте мост как службу:
   ```
   scp deploy/tg-bridge/tg_bridge.py user@ВТОРАЯ_МАШИНА:/tmp/
   scp deploy/tg-bridge/tg-bridge.service user@ВТОРАЯ_МАШИНА:/tmp/
   ssh user@ВТОРАЯ_МАШИНА
   sudo mkdir -p /opt/tg-bridge && sudo mv /tmp/tg_bridge.py /opt/tg-bridge/
   sudo mv /tmp/tg-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now tg-bridge
   curl http://127.0.0.1:8077/health      # → {"ok": true}
   ```
2. Настройте SSH-ключи с основного сервера (один раз):
   ```
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519   # если ключа ещё нет
   ssh-copy-id user@ВТОРАЯ_МАШИНА
   ```
3. На основном сервере поднимите туннель:
   ```
   sudo tee /etc/systemd/system/tg-bridge-tunnel.service <<'EOF'
   [Unit]
   Description=SSH tunnel to Telegram bridge
   After=network-online.target

   [Service]
   ExecStart=/usr/bin/ssh -N -L 127.0.0.1:8077:127.0.0.1:8077 -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes user@ВТОРАЯ_МАШИНА
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   EOF
   sudo systemctl daemon-reload && sudo systemctl enable --now tg-bridge-tunnel
   ```
4. В программе: «Адрес API» = `http://127.0.0.1:8077`, прокси — убрать
   («-»). Нажмите «🔍 Диагностика» — через адрес API должно быть ✅.
   Затем «💾 Сохранить и запустить».

## Вариант Б: мост со своим доменом и HTTPS

Если туннель не подходит — выдайте второй машине домен, поставьте nginx
с TLS (certbot) перед мостом и в «Адрес API» укажите
`https://мост.example.ru`. Порт 8077 наружу по-прежнему закрыт.

## Безопасность

- Мост — «тупой» ретранслятор: не хранит токен, не пишет на диск,
  в systemd работает без привилегий, файловая система только для чтения.
- Белый список хостов (`ALLOWED_HOSTS`) не позволяет использовать мост
  как открытый прокси для произвольных сайтов.
- Порт моста держите закрытым от интернета (ufw/туннель): мост должен
  принимать запросы только с вашего сервера.

## Диагностика

- `curl http://127.0.0.1:8077/health` → `{"ok": true}` — мост жив;
- в программе «🔍 Диагностика» показывает три пути: напрямую, через
  прокси и через адрес API;
- журналы моста: `journalctl -u tg-bridge -f`.
