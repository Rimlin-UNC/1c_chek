# Почтовый сервис на своём сервере — chek@chek.ymaster.ru

ООО «Ямастер» · https://ymaster.ru · info@ymaster.ru

Отправка писем «Ямастер Чек» (подтверждение e-mail, вход по ссылке,
рассылки Почтового центра) идёт **с вашего сервера** от ящика
`chek@chek.ymaster.ru`. Настраивается одной командой — `deploy.sh
--setup-mail` — и тремя записями в DNS.

## 1. Команда на сервере

```bash
ssh root@94.183.236.179
curl -fsSL https://raw.githubusercontent.com/Rimlin-UNC/1c_chek/arena/01a0caaa-1c-chek/deploy.sh -o deploy.sh
sudo bash deploy.sh --setup-mail
```

Скрипт (idempotent — можно повторять):
- ставит **Postfix** (только отправка), **OpenDKIM** (RSA 2048, selector `mail`), mailutils;
- создаёт системного получателя `chek` (вход в систему отключён);
- слушает **только 127.0.0.1** — снаружи нет ни 25, ни 587 порта,
  значит нет ни перебора паролей, ни риска открытого релея (UFW не меняется);
- генерирует DKIM-ключ и файл **`/root/mail-dns-records.txt`**
  с готовыми значениями для DNS.

## 2. DNS-записи (у держателя DNS зоны ymaster.ru)

| Тип | Имя | Значение |
|---|---|---|
| TXT | `chek.ymaster.ru` | `"v=spf1 a ip4:94.183.236.179 ~all"` |
| TXT | `mail._domainkey.chek.ymaster.ru` | `"v=DKIM1; …; p=<из файла на сервере>"` |
| TXT | `_dmarc.chek.ymaster.ru` | `"v=DMARC1; p=quarantine; rua=mailto:info@ymaster.ru; adkim=s; aspf=s"` |
| A | `chek.ymaster.ru` | `94.183.236.179` (уже есть — сайт работает) |
| MX *(необязательно)* | `chek.ymaster.ru` | `10 chek.ymaster.ru.` — только для **приёма** писем |

Точное значение DKIM — в `/root/mail-dns-records.txt` на сервере.

Проверка после добавления записей (5–30 минут на расстворение):

```bash
dig +short TXT chek.ymaster.ru
dig +short TXT mail._domainkey.chek.ymaster.ru
opendkim-testkey -d chek.ymaster.ru -s mail -vvv   # ждём: "key OK"
```

## 3. Значения в «Почтовом центре» приложения

Настройки → «✉️ Почтовый центр»:

- SMTP-сервер: **127.0.0.1**, порт **25**;
- логин и пароль: **пусто** (локальный сервер доверенный);
- STARTTLS: **выключен**; ящик From: **chek@chek.ymaster.ru**;
- адрес сайта для ссылок: `https://chek.ymaster.ru`.

Затем кнопка «✉ Тестовое письмо» себе на адрес.

## 4. Если провайдер закрыл исходящий порт 25

Домашние/офисные интернет-провайдеры в РФ часто блокируют исходящий
25/tcp — скрипт сообщит об этом. Решение — **smarthost**: Postfix
отдаёт письма на SMTP-сервер провайдера (или Яндекса/Mail.ru с вашим
аккаунтом). В `/etc/postfix/main.cf`:

```
relayhost = [smtp.isp.ru]:587
smtp_sasl_auth_enable = yes
smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd
smtp_sasl_security_options = noanonymous
smtp_tls_security_level = encrypt
```

`/etc/postfix/sasl_passwd` (затем `postmap /etc/postfix/sasl_passwd`
и `systemctl restart postfix`):

```
[smtp.isp.ru]:587 логин:пароль
```

DKIM при релее через своего провайдера сохраняется (подпись ставится
до передачи). Если релей чужой — уточните, не перезаписывает ли он
заголовки.

## 5. Приём писем (необязательно)

Для получения ответов на `chek@chek.ymaster.ru` добавьте MX-запись
(таблица выше) — Postfix положит письма в `/var/mail/chek`
(читать: `sudo mail -u chek`). Удобнее альтернатива: пересылка на
ваш корпоративный ящик —

```bash
echo "info@ymaster.ru" >> /etc/aliases && newaliases
```

## 6. Порядок в системе (152-ФЗ)

- журнал отправок приложения — ≤ 90 дней (чистится автоматически);
- DKIM-ключ хранится только на сервере (`/etc/dkimkeys`, права 600);
- паролей почты в приложении нет (локальный доверенный SMTP).
