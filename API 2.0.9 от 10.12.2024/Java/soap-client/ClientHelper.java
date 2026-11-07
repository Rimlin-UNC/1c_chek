package ru.gnivc.inplat.servin.openapi.soap.client.components;

import org.apache.cxf.jaxws.JaxWsProxyFactoryBean;

/**
 * Created by KuznetsovRA on 26.04.2019.
 * Вспомогательный класс.
 */
public class ClientHelper {

    /*
    Данные аутентификации. Значение токена используется для определения доступа клиента на вызов заданного
    сервиса и передачу определенного метода в блоке бизнес данных. Значение UserToken используется для ведения
    статистики и доступа к результату обработки бизнес данных при вызове метода GetMessages сервиса в IDMZ.
    Значения передаются в http-заголовках. При формировании запроса GetMessagesRequest, к сервису в IDMZ,
    значение UserToken передаются в теле soap-запроса.
     */
    public final static String VALUE_TOKEN = "4541";
    public final static String VALUE_USER_TOKEN = "UserToken1";
    // Параметры доступа к сервису Upload, Download по протоколу https.
    public final static String JKS_URL = "ssl/keystore.jks";
    public final static String KEY_STORE = "openapi";
    public final static String KEY_PASSWORD = "openapi";

    // Метод создает JAX-WS proxy для работы с объектами, которые были получены из xsd схемы бизнес-данных.
    public static JaxWsProxyFactoryBean createJaxWsProxyFactoryBean(Class<?> serviceClass, String serviceUrl) {

        JaxWsProxyFactoryBean factoryBean = new JaxWsProxyFactoryBean();
        factoryBean.setServiceClass(serviceClass);
        factoryBean.setAddress(serviceUrl);
        return factoryBean;
    }
}
