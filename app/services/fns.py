# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — единственный источник проверки чеков: Открытые API ФНС.
#
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru
#
# v1.70.70: сторонние провайдеры удалены — чек проверяется ТОЛЬКО в ФНС.
# Контракт сверен с официальным комплектом «API 2.0.9 от 10.12.2024»
# (папка в репозитории: документы «Описание сервиса Аутентификации»,
# «Описание интерфейсов Открытых SOAP API (Потребитель)», «Примеры
# запросов (Потребитель)» и WSDL/XSD из Java-клиента):
#
#   1) Аутентификация: POST {base}/open-api/AuthService/0.1
#      SOAPAction: "urn:GetMessageRequest",
#      конверт — OpenApiMessageConsumerService/types/1.0,
#      тело — AuthRequest в ais3/kkt/AuthService/types/1.0 с MasterToken.
#      Ответ: Result/Token + Result/ExpireTime.
#      По документации токен живёт 1 час и кэшируется сервисом 40 минут —
#      поэтому держим его 40 минут, а не дольше (иначе запросы шли бы с
#      просроченным токеном).
#
#   2) Проверка чека = получение полного документа (GetTicket):
#      POST {base}/open-api/ais3/KktService/0.1
#      заголовки FNS-OpenApi-Token (временный токен) и
#      FNS-OpenApi-UserToken (base64 идентификатора пользователя),
#      SOAPAction: "urn:SendMessageRequest",
#      конверт — OpenApiAsyncMessageConsumerService/types/1.0,
#      тело — GetTicketRequest в ais3/kkt/KktTicketService/types/1.0.
#      Ответ: MessageId.
#
#   3) Результат: тот же адрес, GetMessageRequest с MessageId
#      (SOAPAction "urn:GetMessageRequest", тот же асинхронный конверт).
#      ProcessingStatus: PROCESSING → опрос, COMPLETED → Result, где
#      Code = 200 (успех) и Ticket — JSON-строка с полным чеком.
#
# Лимит ФНС: 1000 запросов за 24 часа на приложение (каждый запрос,
# включая аутентификацию и опрос, считается) — учёт в fns_quota.py.
# IP-адреса сервера должны быть в списке, согласованном с ФНС, иначе
# сервис отвечает AccessDeniedException.
# ======================================================================
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..config import settings

log = logging.getLogger("ymaster")

# --- Адреса и пространства имён (строго по документации ФНС) -------------
FNS_BASE_DEFAULT = "https://openapi.nalog.ru:8090"
AUTH_PATH = "/open-api/AuthService/0.1"
KKT_PATH = "/open-api/ais3/KktService/0.1"

NS_SYNC = ("urn://x-artefacts-gnivc-ru/inplat/servin/"
           "OpenApiMessageConsumerService/types/1.0")
NS_ASYNC = ("urn://x-artefacts-gnivc-ru/inplat/servin/"
            "OpenApiAsyncMessageConsumerService/types/1.0")
NS_AUTH = "urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0"
NS_KKT = "urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0"

TOKEN_TTL_S = 40 * 60          # документация: токен кэшируется на 40 минут
POLL_DELAYS = (2, 3, 5, 8, 12)  # паузы опроса: экономия суточного лимита
SUCCESS_CODE = "200"           # Result/Code успешного GetTicket


@dataclass
class FnsResult:
    """Результат обращения к ФНС."""
    status: str          # valid | invalid | not_found | unknown | quota
    message: str
    raw: dict            # ответ ФНС (для raw_data и аудита)
    ok: bool             # технический успех обращения


class FnsQuotaExceeded(RuntimeError):
    """Суточный лимит запросов к ФНС исчерпан."""


# --------------------------------------------------------------------------
#  Безопасный разбор XML (v1.70.38: ответы ФНС — внешние данные)
# --------------------------------------------------------------------------
def safe_fromstring(xml_text: str):
    """Разбор XML без сущностей и внешних связей. Ошибка → ValueError."""
    try:
        from defusedxml.ElementTree import fromstring as _defused
    except ImportError:                                  # pragma: no cover
        if "<!ENTITY" in (xml_text or "")[:65536].upper():
            raise ValueError("XML с сущностями отклонён: нет defusedxml")
        import xml.etree.ElementTree as ET
        # nosec B314: запасная ветка без defusedxml; сущности отсечены выше
        return ET.fromstring(xml_text)  # nosec B314
    return _defused(xml_text, forbid_dtd=False,
                    forbid_entities=True, forbid_external=True)


def _local(tag: str) -> str:
    return (tag or "").rsplit("}", 1)[-1]


def xml_text(xml_text: str, local_name: str) -> str | None:
    """Текст ПЕРВОГО элемента с заданным локальным именем (без NS).

    v1.70.70: именно так разбираются ответы ФНС — в документации элементы
    приходят с префиксами (`<ns2:Token>`, `<ns2:Ticket>`), поэтому поиск
    подстрокой «<Token>» не находил ничего и токен считался неполученным.
    """
    import xml.etree.ElementTree as ET
    try:
        root = safe_fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    for el in root.iter():
        if _local(el.tag) == local_name and (el.text or "").strip():
            return el.text.strip()
    return None


def xml_element(xml_text: str, local_name: str):
    """Первый элемент с заданным локальным именем или None."""
    import xml.etree.ElementTree as ET
    try:
        root = safe_fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    for el in root.iter():
        if _local(el.tag) == local_name:
            return el
    return None


def _fault_text(text: str) -> str:
    """Текст ошибки SOAP (faultstring) или пустая строка."""
    return (xml_text(text, "faultstring")
            or xml_text(text, "Message") or "").strip()


def error_text(resp) -> str:
    """Текст ошибки из ответа ФНС: faultstring, иначе начало тела."""
    body = getattr(resp, "text", "") or ""
    fault = xml_text(body, "faultstring") or xml_text(body, "Message")
    return (fault or body[:400]).strip()


def classify_error(text: str, http_status: int = 0) -> tuple[str, str]:
    """Человеческое объяснение ошибки ФНС: (класс, текст).

    Классы: quota | ip | token | contract | not_found | network | unknown.
    Тексты — по «Перечню ошибок Открытых SOAP API».
    """
    low = (text or "").lower()
    # «Превышен общий дневной лимит…» — формулировка ФНС; наши собственные
    # сообщения говорят «суточный лимит», их тоже узнаём
    if "лимит" in low and ("дневн" in low or "суточн" in low
                           or "превышен" in low):
        return ("quota", "ФНС: исчерпан суточный лимит запросов приложения "
                         "(1000 в сутки) — проверка возобновится после "
                         "сброса лимита")
    if "accessdenied" in low or "доступ к сервису для переданного ip" in low:
        return ("ip", "ФНС: доступ с этого IP запрещён — добавьте адрес "
                      "сервера в список разрешённых в ФНС")
    if "master" in low:
        return ("master", "ФНС: мастер-токен не принят. Проверьте, что он "
                          "скопирован целиком и без лишних пробелов, и что "
                          "срок действия приложения не истёк")
    if (("token" in low or "токен" in low)
            and ("не верн" in low or "истек" in low
                 or "отказ в доступе" in low or "недействит" in low)):
        return ("token", "ФНС: временный токен не принят — он будет получен "
                         "заново")
    if "unmarshalling" in low or "валидац" in low:
        return ("contract", "ФНС: запрос не прошёл проверку по схеме "
                            f"({text[:160]})")
    if "сообщение не найдено" in low or "messagenotfound" in low:
        return ("not_found", "ФНС: сообщение с результатом не найдено "
                             "(истекло время ожидания)")
    if http_status == 401 or http_status == 403:
        return ("token", "ФНС: доступ запрещён (HTTP %d)" % http_status)
    if http_status:
        return ("unknown", f"ФНС: HTTP {http_status}")
    return ("network", "ФНС: сеть недоступна")


def classify_ticket_code(code: str, message: str = "") -> str:
    """Код из Result/Code → статус проверки (valid | invalid | not_found)."""
    code = str(code or "").strip()
    low = (message or "").lower()
    if code == SUCCESS_CODE:
        return "valid"
    if "не найден" in low or "отсутств" in low or code in ("404", "204"):
        return "not_found"
    if "некоррект" in low or "не совпад" in low or "чек" in low and "недейств" in low:
        return "invalid"
    return "not_found"


# --------------------------------------------------------------------------
#  Клиент ФНС
# --------------------------------------------------------------------------
class FnsClient:
    """Клиент Открытых API ФНС: аутентификация и GetTicket."""

    def __init__(self, api_base: str | None = None,
                 master_token: str | None = None,
                 client_app_id: str | None = None,
                 timeout: int | None = None):
        self.api_base = (api_base or settings.FNS_API_BASE
                         or FNS_BASE_DEFAULT).rstrip("/")
        self.master_token = (master_token or _db_setting("fns_master_token")
                             or settings.FNS_MASTER_TOKEN or "").strip()
        self.client_app_id = (client_app_id
                              or _db_setting("fns_client_app_id")
                              or settings.FNS_CLIENT_APP_ID or "").strip()
        self.timeout = timeout or settings.FNS_TIMEOUT_SECONDS
        self._token: str | None = None
        self._token_at: float = 0.0
        self._lock = threading.Lock()
        self.requests_made = 0          # сколько запросов ушло в этой проверке

    # --- низкий уровень: один запрос к ФНС с учётом лимита ---------------
    def _post(self, path: str, body: str, headers: dict) -> httpx.Response:
        from . import fns_quota
        if not fns_quota.spend(1):
            raise FnsQuotaExceeded(fns_quota.exhausted_message())
        self.requests_made += 1
        full = dict(headers)
        if self.client_app_id:
            full["ClientAppId"] = self.client_app_id
        resp = httpx.post(self.api_base + path, content=body.encode("utf-8"),
                          headers=full, timeout=self.timeout)
        # v1.70.70: в журнале видно каждый обмен с ФНС (без токенов в теле)
        log.info("📩 ФНС %s: HTTP %s, ответ %d байт", path, resp.status_code,
                 len(resp.text or ""))
        if resp.status_code != 200:
            log.warning("⚠️ ФНС %s: %s", path,
                        (resp.text or "")[:400].replace("\n", " "))
        return resp

    # --- 1. временный токен ---------------------------------------------
    def token(self, force: bool = False) -> str:
        """Временный токен ФНС (кэш 40 минут, как в документации)."""
        with self._lock:
            fresh = (self._token
                     and (time.time() - self._token_at) < TOKEN_TTL_S)
            if fresh and not force:
                return self._token or ""
            if not self.master_token:
                raise RuntimeError("Не задан Мастер-токен ФНС")
            body = AUTH_TEMPLATE.format(master=self.master_token)
            resp = self._post(AUTH_PATH, body, {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '"urn:GetMessageRequest"',
            })
            if resp.status_code != 200:
                raw = error_text(resp)
                kind, text = classify_error(raw, resp.status_code)
                if kind == "unknown" and raw and "Exception" in raw:
                    text = f"ФНС: {raw[:200]}"
                raise RuntimeError(text)
            token = xml_text(resp.text, "Token")
            fault = _fault_text(resp.text)
            if not token:
                kind, text = classify_error(fault or resp.text)
                raise RuntimeError(text if fault else
                                   "ФНС: в ответе аутентификации нет токена")
            self._token = token
            self._token_at = time.time()
            return token

    def _headers(self, token: str, action: str,
                 user_id: str) -> dict:
        return {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"urn:{action}"',
            "FNS-OpenApi-Token": token,
            "FNS-OpenApi-UserToken": base64.b64encode(
                (user_id or "ymaster-check").encode("utf-8")).decode("ascii"),
        }

    # --- 2. проверка чека = GetTicket ------------------------------------
    def get_ticket(self, fn: str, fd: str, fp: str, total_sum: float,
                   receipt_date: datetime, operation: int = 1,
                   user_id: str = "ymaster-check") -> FnsResult:
        """Полный документ чека из ФНС (единственный официальный метод).

        Успех означает: чек существует в базе ФНС и реквизиты совпали —
        иначе сервис вернул бы код ошибки. Возвращаем и полные данные
        (позиции, ИНН, адрес расчётов) для карточки чека.
        """
        if not self.master_token:
            return FnsResult("unknown", "Не задан Мастер-токен ФНС",
                             {"provider": "fns", "error": "no_master_token"},
                             False)
        send_body = SEND_TEMPLATE.format(
            total=int(round((total_sum or 0) * 100)),
            date=(receipt_date or _now_naive()).strftime(
                "%Y-%m-%dT%H:%M:%S"),
            fn=_esc(fn), fd=_esc(fd), fp=_esc(fp),
            operation=2 if operation == 2 else 1,
        )
        try:
            token = self.token()
            resp = self._post(KKT_PATH, send_body,
                              self._headers(token, "SendMessageRequest",
                                            user_id))
            if resp.status_code in (401, 403):
                # токен протух раньше срока — берём новый и повторяем один раз
                token = self.token(force=True)
                resp = self._post(KKT_PATH, send_body,
                                  self._headers(token, "SendMessageRequest",
                                                user_id))
            if resp.status_code != 200:
                raw = error_text(resp)
                kind, text = classify_error(raw, resp.status_code)
                if kind == "unknown" and raw and "Exception" in raw:
                    text = f"ФНС: {raw[:200]}"
                if kind == "token":
                    self._token = None
                return FnsResult(
                    "quota" if kind == "quota" else "unknown", text,
                    {"provider": "fns", "kind": kind,
                     "raw": resp.text[:400]}, False)
            message_id = xml_text(resp.text, "MessageId")
            if not message_id:
                kind, text = classify_error(_fault_text(resp.text)
                                            or resp.text)
                return FnsResult("unknown",
                                 text or "ФНС: в ответе нет MessageId",
                                 {"provider": "fns", "kind": kind,
                                  "raw": resp.text[:400]}, False)

            # --- 3. опрос результата -------------------------------------
            for delay in POLL_DELAYS:
                time.sleep(delay)
                poll = self._post(KKT_PATH,
                                  POLL_TEMPLATE.format(message_id=message_id),
                                  self._headers(token, "GetMessageRequest",
                                                user_id))
                if poll.status_code != 200:
                    kind, text = classify_error(error_text(poll),
                                                poll.status_code)
                    if kind == "not_found":
                        return FnsResult("not_found", text,
                                         {"provider": "fns", "kind": kind}, True)
                    return FnsResult(
                        "quota" if kind == "quota" else "unknown", text,
                        {"provider": "fns", "kind": kind}, False)
                status = (xml_text(poll.text, "ProcessingStatus") or "").upper()
                if status == "PROCESSING":
                    continue
                if status and status != "COMPLETED":
                    return FnsResult("unknown",
                                     f"ФНС: состояние обработки {status}",
                                     {"provider": "fns", "status": status},
                                     False)
                result_el = xml_element(poll.text, "Result")
                if result_el is None:
                    kind, text = classify_error(_fault_text(poll.text))
                    return FnsResult("unknown",
                                     text or "ФНС: в ответе нет Result",
                                     {"provider": "fns", "kind": kind}, False)
                code = xml_text(_et_text(result_el), "Code") or ""
                message = xml_text(_et_text(result_el), "Message") or ""
                ticket_raw = xml_text(_et_text(result_el), "Ticket") or ""
                status_kind = classify_ticket_code(code, message)
                ticket: dict = {}
                if ticket_raw:
                    try:
                        ticket = json.loads(ticket_raw)
                    except ValueError:
                        ticket = {"raw": ticket_raw[:400]}
                if status_kind == "valid":
                    return FnsResult(
                        "valid", "Чек найден в ФНС, реквизиты совпадают",
                        {"provider": "fns", "code": code, "ticket": ticket},
                        True)
                return FnsResult(
                    status_kind,
                    message or ("Чек не найден в базе ФНС" if status_kind ==
                                "not_found" else "Чек отклонён ФНС"),
                    {"provider": "fns", "code": code, "message": message,
                     "ticket": ticket}, True)
            return FnsResult("unknown",
                             "ФНС: время ожидания результата истекло — "
                             "повторите проверку позже",
                             {"provider": "fns", "timeout": True}, False)
        except FnsQuotaExceeded as e:
            return FnsResult("quota", str(e),
                             {"provider": "fns", "kind": "quota"}, False)
        except httpx.HTTPError as e:
            return FnsResult("unknown",
                             f"ФНС: сеть недоступна ({e.__class__.__name__})",
                             {"provider": "fns", "kind": "network"}, False)
        except (ValueError, KeyError) as e:
            return FnsResult("unknown", f"ФНС: разбор ответа не удался ({e})",
                             {"provider": "fns"}, False)
        except RuntimeError as e:                     # нет токена / отказ ФНС
            text = str(e)
            kind, human = classify_error(text)
            if kind == "quota":
                return FnsResult("quota", human,
                                 {"provider": "fns", "kind": "quota"}, False)
            return FnsResult("unknown", human, {"provider": "fns",
                                                "kind": kind}, False)

    # --- проверка чека (тот же метод; имя оставлено для совместимости) ---
    def check_receipt(self, fn: str, fd: str, fp: str, total_sum: float,
                      receipt_date: datetime, operation: int = 1) -> FnsResult:
        return self.get_ticket(fn, fd, fp, total_sum, receipt_date, operation)

    # --- проверка связи без расхода лимита на чек -----------------------
    def probe(self) -> tuple[bool, str]:
        """Проверить доступ: мастер-токен + адрес + IP (только auth-запрос)."""
        if not self.master_token:
            return False, "Мастер-токен ФНС не задан"
        try:
            self.token(force=True)
            return True, "ФНС отвечает: токен получен"
        except FnsQuotaExceeded as e:
            return False, str(e)
        except httpx.HTTPError as e:
            return False, f"Сеть недоступна: {e.__class__.__name__}"
        except RuntimeError as e:
            return False, str(e)


def diagnose(api_base: str | None = None, master_token: str | None = None,
             timeout: float = 10.0) -> dict:
    """Пошаговая диагностика связи с ФНС (v1.70.71).

    Отвечает на вопрос «почему не работает», разбирая путь по шагам:
    настройки → DNS → TCP до порта 8090 → TLS → запрос аутентификации.
    Первый же неудачный шаг показывает причину и что делать.
    Стоимость: один запрос из суточного лимита (только при успешном DNS/TCP).
    """
    import socket
    import ssl
    from urllib.parse import urlparse

    base = (api_base or settings.FNS_API_BASE or FNS_BASE_DEFAULT).rstrip("/")
    parsed = urlparse(base)
    host = parsed.hostname or "openapi.nalog.ru"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    token = (master_token or _db_setting("fns_master_token")
             or settings.FNS_MASTER_TOKEN or "").strip()
    steps: list[dict] = []

    def step(name: str, ok: bool | None, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    step("Настройки", bool(token),
         "Мастер-токен задан" if token else
         "Мастер-токен НЕ задан — впишите его в поле выше и нажмите "
         "«Сохранить настройки ФНС»")
    step("Адрес сервиса", True, base)
    if not token:
        return {"ok": False, "steps": steps,
                "verdict": "Не задан мастер-токен ФНС"}

    # --- DNS -------------------------------------------------------------
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addr = sorted({i[4][0] for i in infos})
        step("DNS", True, f"{host} → {', '.join(addr[:3])}")
    except OSError as e:
        step("DNS", False, f"Имя {host} не разрешается: {e}")
        return {"ok": False, "steps": steps,
                "verdict": "Сервер не видит адрес ФНС — проверьте DNS"}

    # --- TCP -------------------------------------------------------------
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        step("Соединение", True, f"порт {port} открыт")
    except OSError as e:
        step("Соединение", False,
             f"Не удалось подключиться к {host}:{port} — {e}")
        return {"ok": False, "steps": steps,
                "verdict": "Порт 8090 закрыт: его блокирует хостинг или "
                           "фаервол сервера. Разрешите исходящие соединения "
                           "на openapi.nalog.ru:8090"}

    # --- TLS -------------------------------------------------------------
    if parsed.scheme == "https":
        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                step("Шифрование (TLS)", True,
                     f"сертификат {tls.version()}")
        except (ssl.SSLError, OSError) as e:
            step("Шифрование (TLS)", False, f"Ошибка TLS: {e}")
            return {"ok": False, "steps": steps,
                    "verdict": "Не удалось установить защищённое соединение"}
    else:
        sock.close()

    # --- запрос аутентификации ------------------------------------------
    client = FnsClient(api_base=base, master_token=token, timeout=int(timeout))
    got, message = client.probe()
    step("Мастер-токен принят", got, message)
    from . import fns_quota
    return {"ok": got, "steps": steps,
            "verdict": "Связь с ФНС работает" if got else message,
            "quota": fns_quota.stats() if got else None}


def _et_text(el) -> str:
    import xml.etree.ElementTree as ET
    return ET.tostring(el, encoding="unicode")


def _now_naive() -> datetime:
    """Текущее время без пояса (совместимо с хранением в базе)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _esc(value) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _db_setting(key: str) -> str:
    """Значение настройки из БД (админка), иначе пустая строка."""
    try:
        from ..database import SessionLocal
        from .appsettings import get_setting
        db = SessionLocal()
        try:
            return (get_setting(db, key, "") or "").strip()
        finally:
            db.close()
    except Exception:                                    # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
#  Шаблоны SOAP (по «Примерам запросов (Потребитель)»)
# --------------------------------------------------------------------------
AUTH_TEMPLATE = (
    '<soapenv:Envelope xmlns:soapenv='
    '"http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:ns="{ns}">'
    "<soapenv:Header/><soapenv:Body>"
    "<ns:GetMessageRequest><ns:Message>"
    '<tns:AuthRequest xmlns:tns="{auth}">'
    "<tns:AuthAppInfo><tns:MasterToken>{master}</tns:MasterToken>"
    "</tns:AuthAppInfo></tns:AuthRequest>"
    "</ns:Message></ns:GetMessageRequest>"
    "</soapenv:Body></soapenv:Envelope>"
).format(ns=NS_SYNC, auth=NS_AUTH, master="{master}")

SEND_TEMPLATE = (
    '<soapenv:Envelope xmlns:soapenv='
    '"http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:ns="{ns}">'
    "<soapenv:Header/><soapenv:Body>"
    "<ns:SendMessageRequest><ns:Message>"
    '<tns:GetTicketRequest xmlns:tns="{kkt}">'
    "<tns:GetTicketInfo>"
    "<tns:Sum>{total}</tns:Sum>"
    "<tns:Date>{date}</tns:Date>"
    "<tns:Fn>{fn}</tns:Fn>"
    "<tns:TypeOperation>{operation}</tns:TypeOperation>"
    "<tns:FiscalDocumentId>{fd}</tns:FiscalDocumentId>"
    "<tns:FiscalSign>{fp}</tns:FiscalSign>"
    "<tns:RawData>true</tns:RawData>"
    "</tns:GetTicketInfo></tns:GetTicketRequest>"
    "</ns:Message></ns:SendMessageRequest>"
    "</soapenv:Body></soapenv:Envelope>"
).format(ns=NS_ASYNC, kkt=NS_KKT, total="{total}", date="{date}", fn="{fn}",
         operation="{operation}", fd="{fd}", fp="{fp}")

POLL_TEMPLATE = (
    '<soapenv:Envelope xmlns:soapenv='
    '"http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:ns="{ns}">'
    "<soapenv:Header/><soapenv:Body>"
    "<ns:GetMessageRequest><ns:MessageId>{message_id}</ns:MessageId>"
    "</ns:GetMessageRequest>"
    "</soapenv:Body></soapenv:Envelope>"
).format(ns=NS_ASYNC, message_id="{message_id}")


# --------------------------------------------------------------------------
#  Проверка чека с кэшем (совместимый вход для старых вызовов)
# --------------------------------------------------------------------------
def check_receipt(fn: str, fd: str, fp: str, total_sum: float,
                  receipt_date: datetime,
                  cached_result: str | None = None,
                  cached_at: datetime | None = None) -> FnsResult:
    """Проверка чека в ФНС с учётом кэша результатов.

    Свежий успешный результат (в пределах FNS_CACHE_TTL_DAYS) не
    отправляется повторно — это экономит суточный лимит 1000 запросов.
    """
    ttl_ok = (
        cached_result in ("valid", "invalid", "not_found")
        and cached_at is not None
        and (datetime.utcnow() - cached_at).days < settings.FNS_CACHE_TTL_DAYS
    )
    if ttl_ok:
        return FnsResult(cached_result,
                         "Результат из кэша (повторная проверка не требовалась)",
                         {"provider": "fns", "cached": True}, True)
    return FnsClient().get_ticket(fn, fd, fp, total_sum, receipt_date)
