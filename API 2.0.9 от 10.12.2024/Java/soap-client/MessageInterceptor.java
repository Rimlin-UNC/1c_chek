package ru.gnivc.inplat.servin.openapi.soap.client.consumer.signature.asyncService;

import org.apache.cxf.binding.soap.SoapMessage;
import org.apache.cxf.binding.soap.saaj.SAAJOutInterceptor;
import org.apache.cxf.interceptor.Fault;
import org.apache.cxf.message.Message;
import org.apache.cxf.phase.AbstractPhaseInterceptor;
import org.apache.cxf.phase.Phase;
import org.apache.cxf.phase.PhaseInterceptor;
import org.apache.ws.security.WSSecurityException;
import org.apache.ws.security.message.WSSecHeader;
import org.apache.xml.security.transforms.TransformationException;
import org.apache.xml.security.transforms.Transforms;
import org.apache.xpath.XPathAPI;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;
import wss4j.utility.SpecUtility;

import javax.xml.crypto.MarshalException;
import javax.xml.crypto.dsig.*;
import javax.xml.crypto.dsig.dom.DOMSignContext;
import javax.xml.crypto.dsig.keyinfo.KeyInfo;
import javax.xml.crypto.dsig.keyinfo.KeyInfoFactory;
import javax.xml.crypto.dsig.keyinfo.X509Data;
import javax.xml.crypto.dsig.spec.C14NMethodParameterSpec;
import javax.xml.soap.SOAPException;
import javax.xml.soap.SOAPMessage;
import javax.xml.transform.TransformerException;
import java.io.IOException;
import java.security.*;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.List;

/**
 * Created by KuznetsovRA on 24.12.2018.
 * Перехватчик soap-сообщений в фреймворке CXF. Класс работает с низкоуровневыми данными - soap-сообщение в формате XML.
 * Уровень запуска перехватчика задается в момент передачи сформированного SOAP-сообщения на адрес web-сервиса.
 * Основная работа класса:
 * 1. в конструкторе произвести подключение к JCP хранилищу для доступа к контейнеру с сертификатом и закрытым ключом.
 * 2. Сформировать ЦП и поместить её в soap-сообщение.
 * 3. Передать сформированное сообщение потоку CXF.
 * Переменные уровня класса:
 * {@link DEFAULT_STORETYPE} - название корневого хранилища КриптоПро;
 * {@link extras} - список дополнительных обработчиков, на заданной фазе.
 * {@link samData} - массив сертификатов X509 из подключенного алиаса;
 * {@link xmlDSigProvider} - провайдера Java Security API, реализован КриптоПро.
 */
class MessageInterceptor extends AbstractPhaseInterceptor<SoapMessage> {

    private static final String DEFAULT_STORETYPE = "HDImageStore";
    private final List<PhaseInterceptor<? extends Message>> extras = new ArrayList<>(1);
    private Object[] samData = null;
    private Provider xmlDSigProvider;

    public MessageInterceptor() throws CertificateException, UnrecoverableKeyException, NoSuchAlgorithmException, KeyStoreException, IOException {

        // Задаем фазу срабатывания перехватчика.
        super(Phase.USER_PROTOCOL);
        // добавляем обработку SAAJOutInterceptor
        extras.add(new SAAJOutInterceptor());
        // Инициализация JCP хранилища КриптоПро.
        SpecUtility.initJCP();

        this.setSAMdata();
        this.xmlDSigProvider = new ru.CryptoPro.JCPxml.dsig.internal.dom.XMLDSigRI();
    }

    /**
     * Метод загружает сертификаты X509 из заданного alias {@link SpecUtility.DEFAULT_ALIAS}.
     */
    private void setSAMdata() throws KeyStoreException, NoSuchAlgorithmException, CertificateException, IOException, UnrecoverableKeyException {

        KeyStore keyStore = SpecUtility.loadKeyStore(DEFAULT_STORETYPE, null, SpecUtility.DEFAULT_PASSWORD);
        this.samData = new Object[]{keyStore.getCertificate(SpecUtility.DEFAULT_ALIAS), keyStore.getKey(SpecUtility.DEFAULT_ALIAS, SpecUtility.DEFAULT_PASSWORD)};
    }

    // Метод возвращает созданный перехватчик на стеке выполнения CXF.
    public Collection<PhaseInterceptor<? extends Message>> getAdditionalInterceptors() {

        return extras;
    }

    /**
     * Обработчик сообщений.
     *
     * @param message - коллекция из элементов soap.
     */
    @Override
    public void handleMessage(SoapMessage message) throws Fault {

        SOAPMessage soapMessage = message.getContent(SOAPMessage.class);


        try {

            // Передаем SOAP-сообщение на подписание.
            constructSecuredMessage(soapMessage);
        } catch (Exception e) {
            throw new Fault(e);
        }
    }

    /**
     * Метод формирует подпись в сообщении
     * Комментарий: KEY_SIZE_512
     * Если сообщение подписано ключом длина которого 512 символов, то строки под комментарием KEY_SIZE_512 раскомментировать,
     * а строки под комментарием KEY_SIZE_256 закомментировать.
     *
     * @param soapMessageXml: сформированное soap-сообщение без подписи.
     */
    private void constructSecuredMessage(SOAPMessage soapMessageXml)
            throws SOAPException, WSSecurityException, InvalidAlgorithmParameterException, NoSuchAlgorithmException, TransformationException, MarshalException, XMLSignatureException, TransformerException {

        if (soapMessageXml == null) {
            return;
        }

        final Object[] obj = samData.clone();

        // Добавляем к сообщению необходимые заголовки.
        soapMessageXml.getSOAPPart().getEnvelope().addNamespaceDeclaration("wsse",
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd");
        soapMessageXml.getSOAPPart().getEnvelope().addNamespaceDeclaration("wsu",
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd");
        soapMessageXml.getSOAPPart().getEnvelope().addNamespaceDeclaration("ds",
                "http://www.w3.org/2000/09/xmldsig#");
        Element element = (Element) soapMessageXml.getSOAPBody().getFirstChild().getFirstChild().getFirstChild();
        element.setAttributeNS(
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
                "wsu:Id", "PostRegistrationRequest");

        WSSecHeader header = new WSSecHeader();
        header.setActor("urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0");
        header.setMustUnderstand(false);

        Element sec = header.insertSecurityHeader(soapMessageXml.getSOAPPart());
        Document doc = soapMessageXml.getSOAPPart().getEnvelope().getOwnerDocument();

        Element token = (Element) sec.appendChild(doc.createElementNS(
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
                "wsse:BinarySecurityToken"));
        token.setAttribute("EncodingType",
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary");
        token.setAttribute("ValueType",
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3");
        token.setAttribute("wsu:Id", "CertId");
        header.getSecurityHeader().appendChild(token);

        // Формируем подпись провайдера.
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM", xmlDSigProvider);

        // KEY_SIZE_256
        Reference ref = fac.newReference("#PostRegistrationRequest",
                fac.newDigestMethod("urn:ietf:params:xml:ns:cpxmlsec:algorithms:gostr34112012-256", null));
        // END_KEY_SIZE_256

        // KEY_SIZE_512
        /*Reference ref = fac.newReference("#PostRegistrationRequest",
                fac.newDigestMethod("urn:ietf:params:xml:ns:cpxmlsec:algorithms:gostr34112012-512", null));*/
        // END_KEY_SIZE_512

        // Добавляем ссылку на элемент подписи.
        // KEY_SIZE_256
        SignedInfo si = fac.newSignedInfo(fac.newCanonicalizationMethod(CanonicalizationMethod.INCLUSIVE_WITH_COMMENTS,
                (C14NMethodParameterSpec) null),
                fac.newSignatureMethod(
                        "urn:ietf:params:xml:ns:cpxmlsec:algorithms:gostr34102012-gostr34112012-256", null),
                Collections.singletonList(ref));
        // END_KEY_SIZE_256

        // KEY_SIZE_512
        /*SignedInfo si = fac.newSignedInfo(fac.newCanonicalizationMethod(CanonicalizationMethod.INCLUSIVE_WITH_COMMENTS,
                (C14NMethodParameterSpec) null),
                fac.newSignatureMethod(
                        "urn:ietf:params:xml:ns:cpxmlsec:algorithms:gostr34102012-gostr34112012-512", null),
                Collections.singletonList(ref));*/
        // END_KEY_SIZE_512

        final Transforms transforms = new Transforms(doc);
        transforms.addTransform(Transforms.TRANSFORM_C14N_EXCL_OMIT_COMMENTS);

         /* Формируется информация о сертификате и закрытом ключе.
         Информация необходима для проверки подписи получателем. */
        KeyInfoFactory kif = fac.getKeyInfoFactory();
        X509Data x509d = kif.newX509Data(Collections.singletonList((X509Certificate) obj[0]));
        KeyInfo ki = kif.newKeyInfo(Collections.singletonList(x509d));

        // На основе полученных, промежуточных данных, создаем подпись и подписываем ее закрытым ключом.
        javax.xml.crypto.dsig.XMLSignature sig = fac.newXMLSignature(si, ki);
        DOMSignContext signContext = new DOMSignContext((Key) obj[1], token);
        sig.sign(signContext);

        // Помещаем подпись в запрос.
        Element sigE = (Element) XPathAPI.selectSingleNode(signContext.getParent(), "//ds:Signature");
        Node keyE = XPathAPI.selectSingleNode(sigE, "//ds:KeyInfo", sigE);
        token.appendChild(doc.createTextNode(XPathAPI.selectSingleNode(keyE,
                "//ds:X509Certificate", keyE).getFirstChild().getNodeValue()));
        keyE.removeChild(XPathAPI.selectSingleNode(keyE, "//ds:X509Data", keyE));
        NodeList chl = keyE.getChildNodes();

        for (int i = 0; i < chl.getLength(); i++) {
            keyE.removeChild(chl.item(i));
        }

        Node str = keyE.appendChild(doc.createElementNS(
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
                "wsse:SecurityTokenReference"));
        Element strRef = (Element) str.appendChild(doc.createElementNS(
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
                "wsse:Reference"));

        strRef.setAttribute("ValueType",
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3");
        strRef.setAttribute("URI", "#CertId");
        header.getSecurityHeader().appendChild(sigE);
    }
}