# База знаний: Открытые API ФНС «Проверка чека ККТ»

> Ямастер Чек · ООО «Ямастер» · ymaster.ru · chek@ymaster.ru
>
> v1.70.70: документ сверен с официальным комплектом **«API 2.0.9 от
> 10.12.2024»** (папка `API 2.0.9 от 10.12.2024/` в репозитории:
> описание сервиса аутентификации, описание интерфейсов SOAP API,
> перечень ошибок, примеры запросов, WSDL и XSD из Java-клиента).
> Это единственный источник проверки чеков в программе.

## Доступ (Мастер-токен) и данные регистрации

Приложение регистрируется в ФНС; в ответ приходят **Мастер-токен** и
**AppId**. Наши реквизиты регистрации:

| Параметр | Значение |
|---|---|
| Юрлицо | 835, ООО «ЯМАСТЕР» |
| Brand / AppId | `YmasterAPI` (AppId вводится в программе) |
| ИНН | 4712030034 |
| Почта | chek@ymaster.ru |
| Телефоны | +7 812 988-80-80, +7 904 557-08-07 |
| Сайт | https://chek.ymaster.ru |
| Разрешённые IP | два адреса: машина разработки и рабочий сервер (значения — у владельца, в публичном репозитории не публикуются) |
| Срок действия | до 29.10.2027 |
| Лимит | 1000 запросов за 24 часа |

**Важно:** сервис проверяет IP-адрес. Запросы с любого другого адреса
получают `AccessDeniedException` («Доступ к сервису для переданного IP,
запрещен»). Мастер-токен — секрет: в репозиторий он не попадает, в
интерфейсе показывается маскированным.

## Аутентификация

`POST https://openapi.nalog.ru:8090/open-api/AuthService/0.1`

Заголовки: `Content-Type: text/xml; charset=UTF-8`,
`SOAPAction: "urn:GetMessageRequest"`.

Тело: конверт `OpenApiMessageConsumerService/types/1.0`, внутри
`GetMessageRequest → Message → AuthRequest` в пространстве имён
`urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0`:

```xml
<tns:AuthRequest xmlns:tns="urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0">
  <tns:AuthAppInfo>
    <tns:MasterToken>МАСТЕР-ТОКЕН</tns:MasterToken>
  </tns:AuthAppInfo>
</tns:AuthRequest>
```

Ответ: `Result/Token` и `Result/ExpireTime`.

**Срок жизни токена:** по документации — один час, при этом сервис
кэширует его 40 минут и рекомендует обновлять за 5–10 минут до истечения.
Программа держит токен 40 минут (`TOKEN_TTL_S` в `app/services/fns.py`).
Старый токен — частая причина ошибки «не верное значение token».

## Проверка чека = получение полного документа (GetTicket)

Отдельного «проверочного» REST-метода у ФНС нет. Чек проверяется тем,
что запрашивается его полный документ: если чек существует и реквизиты
совпали — приходят данные, иначе код ошибки.

`POST https://openapi.nalog.ru:8090/open-api/ais3/KktService/0.1`

Заголовки:

- `Content-Type: text/xml; charset=UTF-8`
- `SOAPAction: "urn:SendMessageRequest"` (отправка) и
  `"urn:GetMessageRequest"` (получение результата)
- `FNS-OpenApi-Token: <временный токен>`
- `FNS-OpenApi-UserToken: <base64 идентификатора пользователя>`

Тело отправки: конверт `OpenApiAsyncMessageConsumerService/types/1.0`,
внутри `SendMessageRequest → Message → GetTicketRequest` в пространстве
имён `urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0`:

```xml
<tns:GetTicketInfo>
  <tns:Sum>89000</tns:Sum>              <!-- копейки -->
  <tns:Date>2026-10-01T09:08:00</tns:Date>
  <tns:Fn>7381440700130934</tns:Fn>
  <tns:TypeOperation>1</tns:TypeOperation>
  <tns:FiscalDocumentId>11100</tns:FiscalDocumentId>
  <tns:FiscalSign>2060442762</tns:FiscalSign>
  <tns:RawData>true</tns:RawData>
</tns:GetTicketInfo>
```

Ответ отправки: `MessageId`. Далее опрос: `GetMessageRequest` с
`MessageId`; `ProcessingStatus` = `PROCESSING` (продолжаем опрашивать)
или `COMPLETED` (готово). При `COMPLETED` в `Result` приходят
`Code` (**200** — успех) и `Ticket` — JSON-строка с полным чеком
(`{"id":…,"ofdId":…,"receipt":{…позиции, ИНН, адрес…}}`).

## Лимит 1000 запросов за 24 часа и экономия квоты (v1.73.10)

Считаются **все** сетевые HTTP POST к ФНС: аутентификация (`AuthService`),
отправка запроса (`SendMessageRequest`) и каждый опрос результата
(`GetMessageRequest`). Учёт — в `app/services/fns_quota.py`
(таблица `fns_usage`, сутки по Москве):

- **Сначала проверка локальной БД (`find_cached_in_db`) и предполётный фильтр (`validate_ticket_request`)**:
  нецифровые ФН/ФД/ФП, нулевая сумма, недопустимый признак расчёта или дата
  в будущем отсекаются за **0 запросов к ФНС**. Перед любым сетевым вызовом
  проверяются `receipts`, `unified_documents` + `unified_items` (включая
  ранее удалённые чеки), `pool_receipts` + `pool_items` и полный архив
  `fns_log` (до 250 КБ валидного JSON без обрезки на 4 КБ). Статус `invalid`
  кэшируется по отпечатку реквизитов до первой правки суммы/даты/операции,
  `not_found` выдерживает возрастную лестницу ОФД (15 мин / 2 ч / 12 ч / 30 дн),
  а при исчерпанной квоте для знакомого 16-значного ФН оффлайн подставляется
  продавец/ИНН/адрес ККТ (`fn_registry_hint`) без ложной отметки `valid`.
- **Сессионный токен до ~53 минут по `<ExpireTime>`, Keep-Alive и singleflight**:
  токен `AuthService` удерживается до `ExpireTime − 7 мин` (при отсутствии —
  40 минут `TOKEN_TTL_S`), шифруется в `app_settings` для переживания рестартов
  и автоматически обновляется при HTTP 401/403 или HTTP 500 `AuthenticationFault`.
- **Пакетный опрос `GetMessagesRequest` (до 20 `MessageId` за 1 вызов) и сохранение `MessageId`**:
  при параллельных проверках и пакетной достройке опрос объединяется в
  официальный `GetMessagesRequest` (`1 + 1/20 = 1.05` запроса на чек вместо `2`,
  с адаптивным фолбэком на `GetMessageRequest`), а при таймауте или минутном
  `RateLimitingFault (429)` оплаченный `MessageId` доопрашивается за **1 запрос**
  вместо повторного `SendMessageRequest`. Для живых пользователей удерживается
  интерактивный резерв квоты (`background_budget`), а при `AppRequestLimitException`
  счётчик суток немедленно синхронизируется (`mark_exhausted_today`).
- **Все 4 типа расчёта (`TypeOperation = 1..4`) и 100 % реквизитов ФФД 1.05/1.1/1.2 в БД**:
  из одного ответа `GetTicketResponse (RawData=true)` в `Receipt`, `ReceiptItem`,
  `UnifiedDocument`, `UnifiedItem`, `PoolReceipt`, `PoolItem`, `fns_log` и
  кандидатов справочника «Контрагенты» сохраняются обёртка `Ticket` (`id`, `kind`,
  `ofdId`), все ставки НДС 2025–2026 гг. (`nds5`, `nds7`, `nds22`, расчётные
  `5/105`, `7/107`, `10/110`, `20/120`, `22/122`), `userKpp`, `operatorInn`,
  `ofdInn`, `sellerAddress`, `fnsSite`, `internetSign`, `machineNumber`, `buyerInn`,
  реквизиты коррекции, `properties` / `propertiesUser` / `propertiesData`,
  маркировка (`productCodeData` и `productCodeNew`), поставщик (`supplierInfo` +
  `providerInn`), агент, ФОИВ, ГТД, акциз и дробное количество.
- на 80 % администратор получает предупреждение (баннер + запись в журнал);
- при исчерпании чек остаётся в статусе «ожидает проверки» и проверяется
  автоматически после сброса лимита (фоновый воркер).

## Ошибки (по «Перечню ошибок Открытых SOAP API»)

| Ответ | Причина | Что делает программа |
|---|---|---|
| `AccessDeniedException` | IP не в списке разрешённых | пишет прямую причину, просит добавить IP |
| «не верное значение token» | токен просрочен/неверен | получает новый токен и повторяет один раз |
| «Превышен общий дневной лимит запросов» | исчерпан лимит | статус quota → чек в очередь |
| `UnmarshallingFailureException` | запрос не прошёл проверку по XSD | сообщает, что запрос не прошёл схему |
| `MessageNotFoundException` | результат не найден/истёк | чек помечается «не найден» |
| `TimeOutResponseException` | сервис не ответил вовремя | повтор позже |

## Включение в «Ямастер Чек»

**Настройки → Проверка чеков (ФНС)**: Мастер-токен и AppId → сохранить.
Там же видно, сколько запросов израсходовано за сутки. Через `.env`
(до старта) задаются те же значения:

```
FNS_MASTER_TOKEN=…
FNS_CLIENT_APP_ID=YmasterAPI
FNS_API_BASE=https://openapi.nalog.ru:8090
FNS_DAILY_LIMIT=1000
```

## Известные особенности

- Данные чека от ОФД поступают в ФНС с задержкой (обычно до 3 суток) —
  «не найден» для свежего чека не означает подделку: повторите позже.
- QR-код некоторых касс содержит тег `fpd` вместо `fp` и разделитель `;` —
  парсер системы обрабатывает эти случаи автоматически.
- Проверка обмена без доступа к ФНС: `qa/fake_fns_api.py` (поддельный
  SOAP-сервер) и `qa/e2e_fns_check.py` (сквозной прогон клиента).
