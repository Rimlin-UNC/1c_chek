package ru.gnivc.inplat.servin.openapi.client.upload.service;

import org.apache.commons.io.IOUtils;
import org.apache.http.HttpEntity;
import org.apache.http.HttpResponse;
import org.apache.http.HttpStatus;
import org.apache.http.client.methods.HttpPost;
import org.apache.http.conn.ssl.NoopHostnameVerifier;
import org.apache.http.entity.ContentType;
import org.apache.http.entity.mime.HttpMultipartMode;
import org.apache.http.entity.mime.MultipartEntityBuilder;
import org.apache.http.impl.client.CloseableHttpClient;
import org.apache.http.impl.client.HttpClients;
import org.apache.http.ssl.SSLContextBuilder;
import ru.gnivc.inplat.servin.openapi.soap.client.components.ClientHelper;

import javax.net.ssl.SSLContext;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * Created by KuznetsovRA on 25.04.2019.
 * Пример реализации http-клиента для выгрузки файлов на сервер.
 * Внимание, сервис по приему файлов имеет настраиваемые параметры на максимальный размер загружаемого файла и время
 * ожидания выгрузки файла.
 */
public class HttpsClientUploadFile {

    private final static String UPLOAD_FILE_URL = "uploadTestFile/testFile"; // Каталог для выгружаемымых файлоы
    private final String httpUrl;

    public HttpsClientUploadFile(String httpUrl) {

        this.httpUrl = httpUrl;
    }


    private HttpResponse processingUploadFile(CloseableHttpClient httpClient, File file) throws IOException {

        MultipartEntityBuilder multipartEntityBuilder = MultipartEntityBuilder.create();
        multipartEntityBuilder.setMode(HttpMultipartMode.BROWSER_COMPATIBLE);

        String fileName = file.getName();
        fileName = URLEncoder.encode(fileName, StandardCharsets.UTF_8.toString());

        ContentType contentType = ContentType.create(ContentType.DEFAULT_BINARY.toString(), StandardCharsets.UTF_8.toString());
        multipartEntityBuilder.addBinaryBody(fileName, file, contentType, fileName);
        HttpEntity httpEntity = multipartEntityBuilder.build();

        HttpPost httpPost = new HttpPost(httpUrl);
        httpPost.setEntity(httpEntity);
        return httpClient.execute(httpPost);
    }

    public String startUploadFile() {

        try {

            File file = new File(UPLOAD_FILE_URL);
            SSLContext sslContext = new SSLContextBuilder().loadKeyMaterial(new File(ClientHelper.JKS_URL),
                    ClientHelper.KEY_STORE.toCharArray(),
                    ClientHelper.KEY_PASSWORD.toCharArray()).build();

            try (CloseableHttpClient closeableHttpClient =
                         HttpClients.custom().setSSLContext(sslContext)
                                 .setSSLHostnameVerifier(new NoopHostnameVerifier()).build()) {


                HttpResponse httpResponse = processingUploadFile(closeableHttpClient, file);

                int code = httpResponse.getStatusLine().getStatusCode();
                // Положительный код = 200, любое другое значение является ошибкой.
                // Код 429 - возвращается только тогда, когда клиент превысил лимит запросов к сервису в единицу времени (1 минута).
                if (code == HttpStatus.SC_OK) {

                    try (InputStream inputStream = httpResponse.getEntity().getContent()) {

                        return IOUtils.toString(inputStream, StandardCharsets.UTF_8);
                    }
                } else {

                    String errorResponse = String.format("http-код ошибки: %s", code);
                    throw new Exception(errorResponse);
                }
            }
        } catch (Exception e) {

            System.out.println("Произошла ошибка при выгрузки файла: " + e.getMessage());
            return null;
        }
    }
}
