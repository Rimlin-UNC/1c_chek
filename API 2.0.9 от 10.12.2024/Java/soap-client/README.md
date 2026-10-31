### Проект с реализацией клиентов сервиса open-api

#### Структура проекта
###### `authService-xsd` xsd-схема сервиса авторизации. По данной схеме составляются запросы и разбираются ответы от сервиса
авторизации soap-клиентом из модуля consumer-auth-service-client.
###### `business-data-shcema-xsd` пример xsd-схемы бизнес данных, которые пересылают клиенты: consumer-async-service-client.
###### `downloadTestFile` каталог для загруженных файлов, используется в работе клиента из модуля: upload-downloadFile-service-client
###### `uploadTestFile` каталог с файлами для передачи на сервер, используется в работе клиента из модуля: upload-downloadFile-service-client
###### `wsdl` - каталог с wsdl файлами, сервисов open-api. Контракты используются при реализации soap-клиентов.
###### `consumer-async-service-client` - soap-клиент асинхронного soap-сервиса.
###### `consumer-async-service-signature-client` - soap-клиент асинхронного soap-сервиса-потребитель, формирует soap-запросы с ЦП.
###### `consumer-auth-service-client` - soap-клиент сервиса авторизации.
###### `soap-client-components` - модуль общих компонентов.
###### `upload-downloadFile-service-client` - реализация клиентов загрузки/выгрузки файлов.

### Сборка всех модулей проекта
В консоли выполнить команды:
1. `mvn clean -U`
2. `mvn package`