package ru.gnivc.inplat.servin.openapi.client.download.service;

import org.apache.http.HttpResponse;
import org.apache.http.HttpStatus;
import org.apache.http.client.methods.HttpGet;
import org.apache.http.conn.ssl.NoopHostnameVerifier;
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
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;

/**
 * Created by KuznetsovRA on 25.04.2019.
 * Пример реализации http-клиента для загрузки файлов.
 * Внимание, сервис по загрузки файлов имеет настраиваемый параметр времени ожидания загрузки.
 */
public class HttpsClientDownloadFile {

    private final static String DOWNLOAD_FILE_URL = "downloadTestFile"; //  Каталог для загружаемых файлов
    private static final String URL_PROPERTY_SEPARATOR = "?";
    private final String httpUrl;


    public HttpsClientDownloadFile(String httpUrl) {

        this.httpUrl = httpUrl;
    }

    private String fileNameFromHttpUrl(String httpUrl) {

        String fileName = "defaultFileName";
        if (httpUrl != null && httpUrl.length() > 1) {
            int indexChar = httpUrl.indexOf("?") + 1;
            if (indexChar > 0) {
                fileName = httpUrl.substring(indexChar);
                indexChar = fileName.indexOf("/") + 1;
                if (indexChar > 0) {
                    return fileName.substring(indexChar);
                }

            }
        }
        return fileName;
    }

    private String getFileName(String fileLink) {

        return fileNameFromHttpUrl(httpUrl + URL_PROPERTY_SEPARATOR + fileLink);
    }

    public String startDownloadFile(String fileLink) {

        try {

            String encodeFileLinks = URLEncoder.encode(fileLink, StandardCharsets.UTF_8.toString());
            String fullHttpUrl = httpUrl + URL_PROPERTY_SEPARATOR + encodeFileLinks;
            SSLContext sslContext = new SSLContextBuilder().loadKeyMaterial(new File(ClientHelper.JKS_URL),
                    ClientHelper.KEY_STORE.toCharArray(),
                    ClientHelper.KEY_PASSWORD.toCharArray()).build();
            try (CloseableHttpClient closeableHttpClient =
                         HttpClients.custom().setSSLContext(sslContext)
                                 .setSSLHostnameVerifier(new NoopHostnameVerifier()).build()) {


                HttpResponse httpResponse = processingDownloadFile(closeableHttpClient, fullHttpUrl);
                if (httpResponse == null) {

                    return null;
                }

                String fileName = getFileName(fileLink);
                try (InputStream inputStream = httpResponse.getEntity().getContent()) {
                    String fileStorageAddress = String.format("%s/%s", DOWNLOAD_FILE_URL, fileName);
                    File targetFile = new File(fileStorageAddress);
                    Files.copy(inputStream, targetFile.toPath(), StandardCopyOption.REPLACE_EXISTING);
                    return fileStorageAddress;
                }
            }

        } catch (Exception e) {

            System.out.println("Произошла ошибка при загрузке файла: " + e.getMessage());
            return null;
        }
    }

    private HttpResponse processingDownloadFile(CloseableHttpClient closeableHttpClient, String fullHttpUrl) throws IOException {

        HttpGet httpGet = new HttpGet(fullHttpUrl);
        HttpResponse httpResponse = closeableHttpClient.execute(httpGet);
        int code = httpResponse.getStatusLine().getStatusCode();

        // Положительный код = 200, любое другое значение является ошибкой.
        // Код 429 - возвращается только тогда, когда клиент превысил лимит запросов к сервису в единицу времени (1 минута).
        if (code != HttpStatus.SC_OK) {
            System.out.println("Ошибка загрузки файла, код ошибки: " + code);
            return null;
        }

        return httpResponse;
    }
}
