package ru.gnivc.inplat.servin.openapi.soap.client.components;

import org.apache.camel.converter.IOConverter;
import org.w3c.dom.Document;
import org.xml.sax.SAXException;
import ru.business.data.testService.TestBusinessData;
import ru.business.data.testService.TestRequest;


import javax.xml.bind.*;
import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.parsers.ParserConfigurationException;
import java.io.IOException;
import java.io.StringWriter;

/**
 * Created by KuznetsovRA on 22.04.2019.
 * Вспомогательный класс для работы с бизнес-данными в soap-сообщении.
 */
public class BusinessMessageHelper {

    // Пример метода, в блоке бизнес-данных, при передачи которого, в soap-запрос добавляется цифровая подпись, на
    // часть документа, которая содержит блок данных <ns:PostRegistrationRequest>
    public final static String IDMZ_SIGNED_BUSINESS_MESSAGE = "<ns:PostRegistrationRequest" +
            " xmlns:ns=\"urn://x-artefacts-gnivc-ru/ExampleSystem/ExampleSubsystem/ExampleService/types/1.0\">" +
            "            <!-- Запрос на постановку налогоплательщика на учёт в ПП \"Самозанятые\" банком-партнером. -->" +
            "            </ns:PostRegistrationRequest>";

    public static StringWriter marshallerJaxbElement(Object jaxbElement, Class... classesToBeBound) throws JAXBException {

        StringWriter writer = new StringWriter();
        JAXBContext jaxbContext = JAXBContext.newInstance(classesToBeBound);
        Marshaller marshaller = jaxbContext.createMarshaller();
        marshaller.setProperty("com.sun.xml.bind.xmlDeclaration", Boolean.FALSE);
        marshaller.marshal(jaxbElement, writer);

        return writer;
    }

    public static Object unmarshallerDocument(Document document, Class... classesToBeBound) {

        try {

            JAXBContext jaxbContext = JAXBContext.newInstance(classesToBeBound);
            Unmarshaller unmarshaller = jaxbContext.createUnmarshaller();
            return unmarshaller.unmarshal(document);
        } catch (JAXBException e) {

            System.out.println("Произошла ошибка при демаршаллинге бизнес объекта: " + e.getMessage());
        }
        return null;
    }

    public static Document createDocument(String documentString) {

        try {

            DocumentBuilderFactory documentBuilderFactory = DocumentBuilderFactory.newInstance();
            documentBuilderFactory.setNamespaceAware(true);
            DocumentBuilder documentBuilder = documentBuilderFactory.newDocumentBuilder();
            Document businessDocument = documentBuilder.parse(IOConverter.toInputStream(documentString, null));
            businessDocument.getDocumentElement().normalize();

            return businessDocument;
        } catch (ParserConfigurationException | IOException | SAXException e) {

            System.out.println("Произошла ошибка при формировании бизнес данных: " + e.getMessage());
        }

        return null;
    }

    public static Document createBusinessRequest() {

        ru.business.data.testService.TestRequest testRequest = new ru.business.data.testService.TestRequest();
        TestBusinessData testBusinessData = new TestBusinessData();
        testBusinessData.setMessage("Бизнес-данные внешнего приложения.");
        testRequest.setTestBusinessData(testBusinessData);

        try {

            StringWriter stringWriter = marshallerJaxbElement(testRequest, TestRequest.class);
            return createDocument(stringWriter.toString());
        } catch (JAXBException e) {

            System.out.println("Произошла ошибка при маршаллинге бизнес объекта: " + e.getMessage());
        }
        return null;
    }
}
