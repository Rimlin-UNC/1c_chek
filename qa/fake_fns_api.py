# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — поддельный сервер Открытых API ФНС для живой проверки.
#
# Зачем: настоящий сервис ФНС пускает только с согласованных IP
# (31.59.126.178, 94.183.236.179), поэтому из песочницы и с машины
# разработчика проверить обмен нельзя. Этот сервер повторяет контракт
# комплекта «API 2.0.9 от 10.12.2024»: адреса, SOAPAction, пространства
# имён, состояния PROCESSING → COMPLETED, код 200 с чеком в Ticket,
# отказ по IP, ошибку валидации и суточный лимит.
#
# Запуск:  python3 qa/fake_fns_api.py [порт]
# Проверка: FNS_API_BASE=http://127.0.0.1:8099 python3 qa/e2e_fns_check.py
#
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH_PATH = "/open-api/AuthService/0.1"
KKT_PATH = "/open-api/ais3/KktService/0.1"

MASTER_TOKEN = os.environ.get("FAKE_FNS_MASTER", "MASTER-TEST-TOKEN")
USER_TOKEN = "USER-1"
TOKEN = "TEMP-TOKEN-1"
DAILY_LIMIT = int(os.environ.get("FAKE_FNS_LIMIT", "1000"))
# сколько раз на один чек сервер отвечает «обрабатывается»
PROCESSING_TICKS = int(os.environ.get("FAKE_FNS_TICKS", "1"))
DENY_IP = os.environ.get("FAKE_FNS_DENY_IP", "") == "1"

# v1.70.72: чек максимально похож на настоящий ответ ФНС — со всеми
# полями, которые отдаёт GetTicket (продавец, касса, налоги, кассир,
# позиции с типом предмета расчёта, маркировкой и отраслевым реквизитом).
TICKET_OK = {
    "id": "ticket-1", "kind": "kkt", "ofdId": "ofd-1",
    "receipt": {
        "totalSum": 89000, "dateTime": "2026-10-01T09:08:00",
        "user": "ООО «Тест-Магазин»", "userInn": "7801234567",
        "userKpp": "780101001",
        "retailPlaceAddress": "198000, Санкт-Петербург, Невский пр., 1",
        "retailPlace": "Торговый зал",
        "operationType": 1, "taxationType": 1,
        "operator": "Иванова М. П.", "operatorInn": "780100000001",
        "kktRegId": "0001234567890123", "fiscalDriveNumber": "9988776655443322",
        "fiscalDocumentNumber": 77133, "fiscalSign": 1482926127,
        "shiftNumber": 285, "requestNumber": 12,
        "fiscalDocumentFormatVer": "1.05",
        "cashTotalSum": 0, "ecashTotalSum": 89000,
        "nds10": 7417, "nds18": 8100,
        "properties": [{"key": "Код заказа", "value": "G637"}],
        "items": [
            {"name": "Товар А", "price": 44500, "sum": 44500, "quantity": 1,
             "nds": "20", "ndsSum": 7417, "productType": 1, "paymentType": 4,
             "measurementUnit": "796"},
            {"name": "Товар Б", "price": 44500, "sum": 44500, "quantity": 1,
             "nds": "20", "ndsSum": 7417, "productType": 1, "paymentType": 4,
             "measurementUnit": "796",
             "itemsIndustryDetails": [
                 {"idFoiv": "020", "foundationDocDateTime": "14.12.2018",
                  "foundationDocNumber": "1556",
                  "industryPropValue": "RU-430302-0001",
                  "labelCodeProcesMode": 0,
                  "checkingLabeledProdResult": 15}],
             "productCodeData": [{"productIdType": 1,
                                  "gtin": "04601234567890",
                                  "serial": "ABC123"}]},
        ],
    },
}


def _envelope(body: str) -> str:
    return ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            'envelope/"><soap:Body>' + body + "</soap:Body></soap:Envelope>")


def _fault(text: str) -> bytes:
    return _envelope(f"<soap:Fault><faultstring>{text}</faultstring>"
                     "</soap:Fault>").encode("utf-8")


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0          # все обращения: как считает ФНС
        self.messages: dict[str, int] = {}
        self.next_id = 100


STATE = _State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- служебное ---------------------------------------------------
    def log_message(self, fmt, *args):        # тихий журнал
        pass

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                          # healthcheck
        self._send(200, b"fake fns api: ok")

    # --- запросы -----------------------------------------------------
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        with STATE.lock:
            STATE.requests += 1
            over_limit = STATE.requests > DAILY_LIMIT
        if DENY_IP:
            return self._send(500, _fault(
                "AccessDeniedException: Доступ к сервису для переданного "
                "IP, запрещен"))
        if over_limit:
            return self._send(500, _fault(
                "Превышен общий дневной лимит запросов, приложением "
                "YmasterAPI"))

        if self.path == AUTH_PATH:
            return self._auth(body)
        if self.path == KKT_PATH:
            action = (self.headers.get("SOAPAction") or "").strip('"')
            if action == "urn:SendMessageRequest":
                return self._send_message(body)
            if action == "urn:GetMessageRequest":
                return self._get_message(body)
            return self._send(500, _fault(
                "UnmarshallingFailureException: unexpected SOAPAction"))
        return self._send(404, _fault("MessageNotFoundException: путь"))

    # --- аутентификация ----------------------------------------------
    def _auth(self, body: str):
        if MASTER_TOKEN not in body:
            return self._send(500, _fault(
                "Отказ в доступе: не верное значение master-токена"))
        payload = _envelope(
            '<ns2:GetMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
            'inplat/servin/OpenApiMessageConsumerService/types/1.0">'
            "<ns2:Message><ns2:Result>"
            f"<ns2:Token>{TOKEN}</ns2:Token>"
            "<ns2:ExpireTime>2026-10-31T18:40:00+03:00</ns2:ExpireTime>"
            "</ns2:Result></ns2:Message></ns2:GetMessageResponse>")
        self._send(200, payload.encode("utf-8"))

    # --- отправка запроса на чек -------------------------------------
    def _send_message(self, body: str):
        if self.headers.get("FNS-OpenApi-Token") != TOKEN:
            return self._send(401, _fault("Отказ в доступе: token"))
        if not self.headers.get("FNS-OpenApi-UserToken"):
            return self._send(500, _fault(
                "UnmarshallingFailureException: FNS-OpenApi-UserToken"))
        for tag in ("Sum", "Date", "Fn", "TypeOperation", "FiscalDocumentId",
                    "FiscalSign"):
            if f"<tns:{tag}>" not in body and f":{tag}>" not in body:
                return self._send(500, _fault(
                    f"UnmarshallingFailureException: unexpected element {tag}"))
        # Чек «7381440700130934» есть в базе, любой другой — нет
        found = "7381440700130934" in body
        with STATE.lock:
            STATE.next_id += 1
            message_id = f"msg-{STATE.next_id}"
            STATE.messages[message_id] = (
                PROCESSING_TICKS if found else -1)     # -1 = «не найден»
        payload = _envelope(
            '<ns2:SendMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
            'inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">'
            f"<ns2:MessageId>{message_id}</ns2:MessageId>"
            "</ns2:SendMessageResponse>")
        self._send(200, payload.encode("utf-8"))

    # --- результат ---------------------------------------------------
    def _get_message(self, body: str):
        if self.headers.get("FNS-OpenApi-Token") != TOKEN:
            return self._send(401, _fault("Отказ в доступе: token"))
        message_id = ""
        if "msg-" in body:
            start = body.index("msg-")
            message_id = body[start:start + 20]
            for ch in ("<", "&", '"'):
                message_id = message_id.split(ch)[0]
        with STATE.lock:
            if message_id not in STATE.messages:
                return self._send(500, _fault(
                    "MessageNotFoundException: Сообщение не найдено"))
            left = STATE.messages[message_id]
            if left > 0:
                STATE.messages[message_id] = left - 1
                status, code, extra = "PROCESSING", "", ""
            elif left == 0:
                STATE.messages[message_id] = -2
                status = "COMPLETED"
                code = "<ns3:Code xmlns:ns3=\"urn://x-artefacts-gnivc-ru/" \
                       "ais3/kkt/KktTicketService/types/1.0\">200</ns3:Code>"
                extra = ("<ns3:Ticket xmlns:ns3=\"urn://x-artefacts-gnivc-ru/"
                         "ais3/kkt/KktTicketService/types/1.0\">"
                         + json.dumps(TICKET_OK, ensure_ascii=False)
                         + "</ns3:Ticket>")
            else:
                STATE.messages[message_id] = -2
                status = "COMPLETED"
                code = "<ns3:Code xmlns:ns3=\"urn://x-artefacts-gnivc-ru/" \
                       "ais3/kkt/KktTicketService/types/1.0\">404</ns3:Code>"
                extra = ("<ns3:Message xmlns:ns3=\"urn://x-artefacts-gnivc-ru/"
                         "ais3/kkt/KktTicketService/types/1.0\">"
                         "Чек не найден</ns3:Message>")
        payload = _envelope(
            '<ns2:GetMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
            'inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">'
            "<ns2:Message>"
            f"<ns2:ProcessingStatus>{status}</ns2:ProcessingStatus>"
            f"<ns2:Result>{code}{extra}</ns2:Result>"
            "</ns2:Message></ns2:GetMessageResponse>")
        self._send(200, payload.encode("utf-8"))


def serve(port: int = 8099) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = serve(port)
    print(f"поддельный API ФНС слушает http://127.0.0.1:{port}")
    print(f"мастер-токен: {MASTER_TOKEN}, лимит: {DAILY_LIMIT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
