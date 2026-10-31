### Реализация soap-клиента для передачи сообщений с цифровой подписью.
##### Внедрение ЦП в запросы, требуется только в тех случаях, когда это требует бизнес-взаимодействие.
#### Структура проекта
###### `consumer-async-service-signature-client/src` - содержит реализацию soap-клиента, который формирует и передает вместе с запросом цифровую подпись. Ознакомьтесь с реализацией клиента.
###### `consumer-async-service-signature-client/ContainerForCryptoPro` контейнер с тестовым ключом и сертификатом для КриптоПро;
###### `consumer-async-service-signature-client/CryptoProLib` библиотеки для JRE;
###### `consumer-async-service-signature-client/data/WebContent/crypto.properties` конфигурация для запуска JCP;
###### `consumer-async-service-signature-client/doc/exampleRequest` - примеры подписанного soap-запросе. Подпись получена по ГОСТ алгоритму 2012 года. Длина ключа: 256, 512;
###### `consumer-async-service-signature-client/doc/КриптоПро JCP_КраткоеРукаводство.docx` - руководство КриптоПро по установке;
#### Подготовка хранилища HDImage для подписания soap-сообщения
1. Установить дистрибутив КриптоПро на компьютер разработчика.
2. В командной строке windows выполнить команду:
`ControlPane.bat "c:\Program Files (x86)\Java\jdk1.8.X_X"`
    * Файл `ControloPane.bat` находится в каталоге с дистрибутивом КриптоПро
    * `c:\Program Files (x86)\Java\jdk1.8.X_X` - адрес расположения JDK, куда был инсталлирован
    дистрибутив КриптоПро. В результате откроется окно "CryptoPro JCP setting".
3. По умолчанию хранилище HDImage в Windows находится по адресу: `${user.home}\Local Settings\Application Data\Crypto Pro\`
Скопируйте по данному адресу каталог `test2012.000` из корня проекта:  `ContainerForCryptoPro\test2012.000`
Перезагрузите окно "CryptoPro JCP setting", перейдите на вкладку "Keys and certificates stores", 
откройте структуру папки "HDImageStore" и проверьте, что в структуре есть узел *test2012_256*
4. Наименование корневого хранилища с пользовательским ключом и сертификатом,
задается в файле: `data\WebContent\crypto.properties`, параметр: `org.apache.ws.security.crypto.merlin.keystore.alias`

#### Подготовка JRE, для запуска проекта.
1. В каталог с JRE `c:\Program Files (x86)\Java\jdk1.8.X_X\jre\lib\ext`, куда был установлен дистрибутив КриптоПро, 
поместить все библиотеки из каталога проекта  `CryptoProLib`.

2. Если по каким либо причинам вы не можете импортировать сертификаты SSL в корневое хранилище JRE при помощи 
утилиты **keytool**, то поместите файл: `jre_lib_security/cacert` в каталог 
`c:\Program Files (x86)\Java\jdk1.8.X_X\jre\lib\security`.
3. При запуске проекта, выбрать необходимую JRE, для этого в Intellij IDEA: Run/Debug configurations, в параметре *JRE*, установить:
`c:\Program Files (x86)\Java\jdk1.8.X_X`
4. В классе Application.java, в методе main, отредактируйте URL-адрес web-сервиса. Пример: `http://localhost:8095/open-api/ais3/AsyncService/0.1`
Запустить на выполнение, класс Application.java
5. После выполнения программы, в консоли будут распечатаны soap-запрос/soap-ответ, а так же краткая
отладочная информация о результате обработки на заданном этапе.

*Внимание, не удаляйте библиотеки: `CryptoProLib\samples.jar`, `CryptoProLib\XMLDSigRI.jar` из проекта. Классы из этих
библиотек используются в реализации soap-клиента*.

#### Сборка и запуск проекта
**Сервис open-api, должен быть доступен c вашего ПК и перед выполнением сборки, необходимо отредактировать значение в переменной `consumerServiceUrl`, 
в классе `ru.gnivc.inplat.servin.openapi.soap.client.consumer.signature.asyncService.Application`.**
В консоли выполнить команды:
1. `mvn clean -U`
2. `mvn package -P consumer-signature-async-client`
3. Запустите файл, командой: `java -jar consumer-async-service-signature-client-1.0-executable-file.jar`
Обратите внимание, что java, должна быть из JDK, куда были установлены библиотеки КриптоПро.


#### WS-Security
*BinarySecurityToken* - содержит открытую версия сертификата X.509, в виде шифрованных данных. Каким способом зашифрованы данных о сертификате указано в теге EncodingType, в блоке BinarySecurityToken

*DigestValue* - содержит хэш-сумму элемента сообщения, ссылка на элемент, установлена в теге Reference. Хэш-алгоритм задан в параметре DigestMethod.

*SignatureValue* - содержит результат подписания блока SignatureInfo, фактически это означает подписание дайджеста данных вместе с ссылкой на данные. Алгоритм
подписания задается в параметре SignatureMethod.

*KeyInfo* - задает тип сертификата в блоке SecurityTokenReferenc.

**Статьи про ws-security**

`http://www.vbnet.ru/articles/showarticle.aspx?id=144`

`http://xmlhack.ru/texts/03/wssecurity/wssecurity.html`