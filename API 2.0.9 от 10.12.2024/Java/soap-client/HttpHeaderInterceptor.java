package ru.gnivc.inplat.servin.openapi.soap.client.components;

import org.apache.cxf.interceptor.Fault;
import org.apache.cxf.message.Message;
import org.apache.cxf.phase.AbstractPhaseInterceptor;
import org.apache.cxf.phase.Phase;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import ru.gnivc.inplat.servin.openapi.soap.client.components.ClientHelper;

import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Created by KuznetsovRA on 19.04.2019.
 * Перехватчик, для добавления к http-запросу, всех необходимых заголовков.
 * Заголовки:
 * FNS-OpenApi-Token - служебный заголовок, содержит уникальное значение, которое определяет права доступа клиента к сервису.
 * Токен выдается на ограниченный период времени.
 * FNS-OpenApi-UserToken - служебный заголовок, содержит уникальное значение, определяемое клиентом и служит для ведения статистики частоты запросов.
 */
public class HttpHeaderInterceptor extends AbstractPhaseInterceptor<Message> {

    private static final Logger LOG = LoggerFactory.getLogger(HttpHeaderInterceptor.class);
    private final static String TOKEN_HEADER_NAME = "FNS-OpenApi-Token";
    private final static String USER_TOKEN_HEADER_NAME = "FNS-OpenApi-UserToken";
    private final String valueUserToken;
    private final String valueToken;


    public HttpHeaderInterceptor() {

        super(Phase.POST_PROTOCOL);
        this.valueToken = ClientHelper.VALUE_TOKEN;
        this.valueUserToken = ClientHelper.VALUE_USER_TOKEN;
    }

    @Override
    public void handleMessage(Message message) throws Fault {

        if (message.containsKey(Message.PROTOCOL_HEADERS)) {

            Map<String, List<String>> headers = (Map<String, List<String>>) message.get(Message.PROTOCOL_HEADERS);
            headers.put(TOKEN_HEADER_NAME, Collections.singletonList(valueToken));
            headers.put(USER_TOKEN_HEADER_NAME, Collections.singletonList(valueUserToken));
        } else {

            LOG.error("Заголовки пользователя не добавлены т.к в коллекции message не обнаружен ключ {}", Message.PROTOCOL_HEADERS);
        }
    }
}
