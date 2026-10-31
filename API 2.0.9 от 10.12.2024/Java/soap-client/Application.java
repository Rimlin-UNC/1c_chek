package ru.gnivc.inplat.servin.openapi.client;

import ru.gnivc.inplat.servin.openapi.client.download.service.HttpsClientDownloadFile;
import ru.gnivc.inplat.servin.openapi.client.upload.service.HttpsClientUploadFile;

/**
 * Created by KuznetsovRA on 23.05.2019.
 * Класс демонстрирует работу с сервисами загрузки/выгрузки файлов.
 * Замените значение в переменных uploadFileServiceUrl, downloadFileServiceUrl в соответствии с предоставленными адресами
 * сервиса.
 */
public class Application {


    public static void main(String[] args) {

        final String uploadFileServiceUrl = "https://localhost:8443/open-api/ais3/KktService/0.1/upload";
        final String downloadFileServiceUrl = "https://localhost:8443/open-api/ais3/KktService/0.1/download";

        HttpsClientUploadFile httpsClientUploadFile = new HttpsClientUploadFile(uploadFileServiceUrl);
        String responseUploadFile = httpsClientUploadFile.startUploadFile();
        System.out.println("Upload file link: " + responseUploadFile);

        HttpsClientDownloadFile httpsClientDownloadFile = new HttpsClientDownloadFile(downloadFileServiceUrl);
        String downloadFileUrl = httpsClientDownloadFile.startDownloadFile(responseUploadFile);
        System.out.println("Download file folder/fileName: " + downloadFileUrl);


    }
}
