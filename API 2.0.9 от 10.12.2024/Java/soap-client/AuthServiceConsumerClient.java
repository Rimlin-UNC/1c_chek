package ru.gnivc.inplat.servin.openapi.soap.client.consumer.authService;

import org.apache.cxf.frontend.ClientFactoryBean;
import org.apache.cxf.interceptor.LoggingInInterceptor;
import org.apache.cxf.interceptor.LoggingOutInterceptor;
import org.apache.cxf.jaxws.JaxWsProxyFactoryBean;
import org.w3c.dom.Document;
import ru.business.data.authService.AuthAppInfo;
import ru.business.data.authService.AuthRequest;
import ru.business.data.authService.AuthResponse;
import ru.gnivc.artefacts.x.inplat.servin.openapimessageconsumerservice._1.AuthenticationException;
import ru.gnivc.artefacts.x.inplat.servin.openapimessageconsumerservice._1.OpenApiMessageConsumerServicePortType;
import ru.gnivc.artefacts.x.inplat.servin.openapimessageconsumerservice.types._1.GetMessageRequest;
import ru.gnivc.artefacts.x.inplat.servin.openapimessageconsumerservice.types._1.GetMessageResponse;
import ru.gnivc.inplat.servin.openapi.soap.client.components.BusinessMessageHelper;
import ru.gnivc.inplat.servin.openapi.soap.client.components.ClientHelper;
import javax.xml.bind.JAXBException;
import javax.xml.ws.soap.SOAPFaultException;
import java.io.StringWriter;

/**
 * Created by KuznetsovRA on 08.05.2019.
 * Класс реализует soap-клиента web-сервиса авторизации: {@link OpenApiMessageConsumerServicePortType}
 * Клиент формирует soap-запросы и передает их на заданный url-адрес сервиса.
 */
public class AuthServiceConsumerClient {

    // Декларация клиента из контракта WSDL.
    private final OpenApiMessageConsumerServicePortType client;

    public AuthServiceConsumerClient(String serviceUrl) {

        // Перехватчики для логирования сообщений: soap-request/soap-response
        LoggingInInterceptor loggingInInterceptor = new LoggingInInterceptor();
        LoggingOutInterceptor loggingOutInterceptor = new LoggingOutInterceptor();
        // Создаем клиента
        JaxWsProxyFactoryBean factoryBean = ClientHelper.createJaxWsProxyFactoryBean(OpenApiMessageConsumerServicePortType.class, serviceUrl);
        ClientFactoryBean clientBean = factoryBean.getClientFactoryBean();
        clientBean.getInInterceptors().add(loggingInInterceptor);
        clientBean.getOutInterceptors().add(loggingOutInterceptor);
        clientBean.setBus(factoryBean.getBus());
        this.client = (OpenApiMessageConsumerServicePortType) factoryBean.create();
    }


    public AuthResponse callMethodGetMessage(String masterToken) {

        // Создаем запрос
        GetMessageRequest getMessageRequest = new GetMessageRequest();
        GetMessageRequest.Message businessMessage = new GetMessageRequest.Message();
        // Создаем документ с бизнес-данными.
        Document document = createBusinessData(masterToken);
        if (document == null) {
            return null;
        }

        // Помещаем документ в запрос
        businessMessage.setAny(document.getDocumentElement());
        getMessageRequest.setMessage(businessMessage);

        try {
            /*
            Сервис авторизации работает в синхронном режиме, соединение которое установил клиент, будет закрыто
            сервисом, когда сессионный токен будет выделен или когда время ожидания обработки истечет.
            Время ожидания задается на стороне сервиса.
            В случае если клиент в одностороннем порядке разорвет соединение до получения ответа от сервиса, то
            запросить результат обработки по ранее переданным данным будет невозможно. Любое, вновь установленное
            соединение, рассматривается сервисом, как новый запрос на авторизацию.
             */
            GetMessageResponse getMessageResponse = client.getMessage(getMessageRequest);
            // Извлекаем бизнес-данные из ответа
            Document responseDocument = getMessageResponse.getMessage().getAny().getOwnerDocument();
            // Производим демаршаллинг бизнес-данных.
            return  (AuthResponse) BusinessMessageHelper.unmarshallerDocument(responseDocument, AuthResponse.class);
        } catch (AuthenticationException | SOAPFaultException e) {

            /*
           1. Типизированная ошибка:
           1.1 AuthenticationException - клиент обращается к сервису с неразрешенного ip-адреса.
           2. Остальные ошибки, которые не предполагают наличия обработки программной логикой, и могут сигнализировать о наличии
           проблем на стороне сервиса, или о несоблюдении правил клиентским кодом, передаются при помощи SOAPFaultException.
            */
            System.out.println("Произошла ошибка при вызове метода GetMessage: " + e.getMessage());
        }
        return null;
    }

    private static Document createBusinessData(String masterToken) {

        AuthRequest authRequest = new AuthRequest();
        AuthAppInfo authAppInfo = new AuthAppInfo();
        authAppInfo.setMasterToken(masterToken);
        authRequest.setAuthAppInfo(authAppInfo);

        try {

            StringWriter stringWriter = BusinessMessageHelper.marshallerJaxbElement(authRequest, AuthRequest.class);
            return BusinessMessageHelper.createDocument(stringWriter.toString());
        } catch (JAXBException e) {

            System.out.println("Произошла ошибка при маршаллинге бизнес объекта: " + e.getMessage());
        }
        return null;
    }
}
