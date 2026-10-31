package ru.gnivc.inplat.servin.openapi.soap.client.consumer.signature.asyncService;

import org.apache.cxf.frontend.ClientFactoryBean;
import org.apache.cxf.interceptor.LoggingInInterceptor;
import org.apache.cxf.interceptor.LoggingOutInterceptor;
import org.apache.cxf.jaxws.JaxWsProxyFactoryBean;
import org.w3c.dom.Document;
import ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice._1.AuthenticationException;
import ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice._1.FileNotFoundException;
import ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice._1.MessageNotFoundException;
import ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice.types._1.GetMessageRequest;
import ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice.types._1.SendMessageRequest;
import ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice.types._1.SendMessageResponse;
import ru.gnivc.inplat.servin.openapi.soap.client.components.BusinessMessageHelper;
import ru.gnivc.inplat.servin.openapi.soap.client.components.ClientHelper;
import ru.gnivc.inplat.servin.openapi.soap.client.components.HttpHeaderInterceptor;

import javax.xml.ws.soap.SOAPFaultException;

/**
 * Created by KuznetsovRA on 26.04.2019.
 */
class AsyncServiceConsumerClient {

    // Клиент для передачи сообщений SendMessageRequest c цифровой подписью
    private final ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice._1.OpenApiAsyncMessageConsumerServicePortType
            digitalSignedClient;


    public AsyncServiceConsumerClient(String serviceUrl, MessageInterceptor messageInterceptor, HttpHeaderInterceptor httpHeaderInterceptor) {

        // Перехватчики для логирования сообщений: soap-request/soap-response
        LoggingInInterceptor loggingInInterceptor = new LoggingInInterceptor();
        LoggingOutInterceptor loggingOutInterceptor = new LoggingOutInterceptor();

        // Создаем клиента для передачи soap-запросов, с цифровой подписью.
        JaxWsProxyFactoryBean signedFactoryBean = ClientHelper.createJaxWsProxyFactoryBean(
                ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice._1.OpenApiAsyncMessageConsumerServicePortType.class,
                serviceUrl);
        ClientFactoryBean signedClientBean = signedFactoryBean.getClientFactoryBean();
        signedClientBean.getOutInterceptors().add(messageInterceptor);
        signedClientBean.getOutInterceptors().add(httpHeaderInterceptor);
        signedClientBean.getOutInterceptors().add(loggingOutInterceptor);
        signedClientBean.getInInterceptors().add(loggingInInterceptor);
        signedClientBean.setBus(signedFactoryBean.getBus());
        this.digitalSignedClient =
                (ru.gnivc.artefacts.x.inplat.servin.openapiasyncmessageconsumerservice._1.OpenApiAsyncMessageConsumerServicePortType)
                        signedFactoryBean.create();
    }

    public String callMethodSendMessageSignedMessage() {

        final String resultMessage = "Передан подписанный запрос sendMessageRequest";
        try {

            SendMessageRequest.Message message = new SendMessageRequest.Message();
            Document document = BusinessMessageHelper.createDocument(BusinessMessageHelper.IDMZ_SIGNED_BUSINESS_MESSAGE);
            if (document == null) {
                return null;
            }
            message.setAny(document.getDocumentElement());
            SendMessageRequest sendMessageRequest = new SendMessageRequest();
            sendMessageRequest.setMessage(message);
            SendMessageResponse sendMessageResponse = digitalSignedClient.sendMessage(sendMessageRequest);
            System.out.println(resultMessage + " MessageId: " + sendMessageResponse.getMessageId());
            return sendMessageResponse.getMessageId();
        } catch (FileNotFoundException | AuthenticationException | SOAPFaultException e) {
        /*
           1. Типизированные:
           1.1 FileNotFoundException - в запросе была передана ссылка на файл, но сам файл предварительно не был загружен
           на сервер или ссылка на файл указана не верно.
           1.2 AuthenticationException - в запросе не были переданы заголовки аутентификации: FNS-OpenApi-Token, FNS-OpenApi-UserToken
           или доступ для переданного токена запрещен.
           2. Остальные ошибки, которые не предполагают наличия обработки программной логикой, и могут сигнализировать о наличии
           проблем в сервере, или о несоблюдении правил клиентским кодом, передаются при помощи SOAPFaultException.
         */
            System.out.println("При передачи запроса sendMessageRequest, возникла ошибка: " + e.getMessage());
        }

        return null;
    }

    public void callMethodGetMessage(String messageId) {

        GetMessageRequest getMessageRequest = new GetMessageRequest();
        getMessageRequest.setMessageId(messageId);
        try {

            digitalSignedClient.getMessage(getMessageRequest);
        } catch (MessageNotFoundException | AuthenticationException | SOAPFaultException e) {
        /*
           1. Типизированные:
           1.1 MessageNotFoundException - в запросе была передан некорректный messageId или время ожидания ответа для
           переданного messageId истекло.
           1.2 AuthenticationException - в запросе не были переданы заголовки аутентификации: FNS-OpenApi-Token, FNS-OpenApi-UserToken
           или доступ для переданного токена запрещен.
           2. Остальные ошибки, которые не предполагают наличия обработки программной логикой, и могут сигнализировать о наличии
           проблем на стороне сервиса, или о несоблюдении правил клиентским кодом, передаются при помощи SOAPFaultException.
         */
            System.out.println(
                    "При передачи запроса getMessageRequest, возникла ошибка: " + e.getMessage()
            );
        }
    }
}
