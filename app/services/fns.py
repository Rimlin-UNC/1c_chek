# ======================================================================
# Ямастер Чек — единственный источник проверки чеков: Открытые API ФНС.
#
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru
#
# v1.70.70+: сторонние провайдеры удалены — чек проверяется ТОЛЬКО в ФНС.
# Контракт сверен с официальным комплектом «API 2.0.9 от 10.12.2024»
# (папка в репозитории: документы «Описание сервиса Аутентификации»,
# «Описание интерфейсов Открытых SOAP API (Потребитель)», «Примеры
# запросов (Потребитель)», «Перечень ошибок Открытых SOAP API» и
# WSDL/XSD из Java-клиента):
#
#   1) Аутентификация: POST {base}/open-api/AuthService/0.1
#      SOAPAction: "urn:GetMessageRequest",
#      конверт — OpenApiMessageConsumerService/types/1.0,
#      тело — AuthRequest в ais3/kkt/AuthService/types/1.0 с MasterToken.
#      Ответ: Result/Token + Result/ExpireTime (срок действия 60 минут).
#      При наличии ExpireTime удерживаем токен до (ExpireTime − 7 мин),
#      без ExpireTime — базовые 40 минут (TOKEN_TTL_S), плюс сохраняем
#      зашифрованно в app_settings для переживания рестартов сервера.
#
#   2) Проверка чека = получение полного документа (GetTicket):
#      POST {base}/open-api/ais3/KktService/0.1
#      заголовки FNS-OpenApi-Token (временный токен) и
#      FNS-OpenApi-UserToken (base64 идентификатора пользователя),
#      SOAPAction: "urn:SendMessageRequest",
#      конверт — OpenApiAsyncMessageConsumerService/types/1.0,
#      тело — GetTicketRequest в ais3/kkt/KktTicketService/types/1.0
#      (RawData=true, TypeOperation=1..4). Ответ: MessageId.
#
#   3) Результат:
#      • одиночный чек — GetMessageRequest (SOAPAction "urn:GetMessageRequest");
#      • конкурентный поток / пачка до 20 чеков — пакетный GetMessagesRequest
#        (SOAPAction "urn:GetMessagesRequest", до 20 MessageId в одном вызове
#        по OpenApiAsyncMessageConsumerService-v0.1.wsdl, расход 1 + 1/K
#        запросов на чек вместо 2 и соблюдение порога 5 запросов/мин).
#      ProcessingStatus: PROCESSING → опрос, COMPLETED → Result, где
#      Code = 200 (успех) и Ticket — JSON-строка с полным чеком ФФД.
#
# Лимит ФНС: 1000 запросов за 24 часа на приложение (каждый запрос,
# включая аутентификацию и опрос, считается) — учёт в fns_quota.py.
# ======================================================================
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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

TOKEN_TTL_S = 40 * 60             # базовый TTL без ExpireTime (40 минут)
TOKEN_EXPIRE_SAFETY_S = 7 * 60    # запас 7 мин до ExpireTime (60-мин токен)
POLL_DELAYS = (2, 3, 5, 8, 12)    # паузы опроса: экономия суточного лимита
MAX_BATCH_POLL_IDS = 20           # WSDL GetMessagesRequest: до 20 MessageId
PENDING_MSG_TTL_S = 25 * 60       # сколько живёт оплаченный MessageId у ФНС
SUCCESS_CODE = "200"              # Result/Code успешного GetTicket
NOT_FOUND_RETRY_MINUTES = 15      # свежий чек (< 2 ч) может ещё идти из ОФД
NOT_FOUND_FINAL_AGE_DAYS = 14     # старше 14 дней not_found кэшируется штатно
PERSISTED_TOKEN_KEY = "fns_cached_session_token"
MAX_FNS_LOG_BYTES = 250_000       # полный JSON в fns_log без обрезки на 4 КБ

# ПДн покупателя из электронного чека (152-ФЗ / ADR-20): при повторном
# использовании фискального документа из локальной БД контакты покупателя
# никогда не переносятся между записями и организациями.
_BUYER_PII_KEYS = frozenset({
    "buyerphoneoraddress", "buyeraddress", "clientinfo", "buyerinn",
    "buyer", "usercontact", "telephone", "email", "buyeremail", "buyerphone",
})


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
    """Текст ПЕРВОГО элемента с заданным локальным именем (без NS)."""
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


def _child_text(el, local_name: str) -> str:
    if el is None:
        return ""
    for ch in el.iter():
        if _local(ch.tag) == local_name and (ch.text or "").strip():
            return ch.text.strip()
    return ""


def _child_element(el, local_name: str):
    if el is None:
        return None
    for ch in el.iter():
        if ch is not el and _local(ch.tag) == local_name:
            return ch
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

    Классы: quota | rate_limit | ip | master | token | contract |
            not_found | network | unknown.
    Тексты — по «Перечню ошибок Открытых SOAP API» (API 2.0.9).
    """
    low = (text or "").lower()
    if ("apprequestlimit" in low
            or ("лимит" in low and ("дневн" in low or "суточн" in low
                                    or "превышен" in low))):
        return ("quota", "ФНС: исчерпан суточный лимит запросов приложения "
                         "(1000 в сутки) — проверка возобновится после "
                         "сброса лимита")
    if (http_status == 429 or "ratelimiting" in low
            or "слишком много запросов" in low
            or "too many requests" in low
            or ("частот" in low and "запрос" in low)):
        return ("rate_limit", "ФНС: превышена минутная частота опроса "
                              "(не более 5 в минуту) — выдерживаем паузу")
    if "accessdenied" in low or "доступ к сервису для переданного ip" in low:
        return ("ip", "ФНС: доступ с этого IP запрещён — добавьте адрес "
                      "сервера в список разрешённых в ФНС")
    if "master" in low:
        return ("master", "ФНС: мастер-токен не принят. Проверьте, что он "
                          "скопирован целиком и без лишних пробелов, и что "
                          "срок действия приложения не истёк")
    if ("authenticationfault" in low
            or (("token" in low or "токен" in low or "аутентиф" in low)
                and ("не верн" in low or "истек" in low
                     or "не принят" in low
                     or "отказ в доступе" in low or "недействит" in low
                     or "ошибка аутентификац" in low))):
        return ("token", "ФНС: временный токен не принят — он будет получен "
                         "заново")
    if "unmarshalling" in low or "валидац" in low:
        return ("contract", "ФНС: запрос не прошёл проверку по схеме "
                            f"({text[:160]})")
    if "сообщение не найдено" in low or "messagenotfound" in low:
        return ("not_found", "ФНС: сообщение с результатом не найдено "
                             "(истекло время ожидания)")
    if http_status in (401, 403):
        return ("token", "ФНС: доступ запрещён (HTTP %d)" % http_status)
    if http_status:
        return ("unknown", f"ФНС: HTTP {http_status}")
    return ("network", "ФНС: сеть недоступна")


def classify_ticket_code(code: str, message: str = "") -> str:
    """Код из Result/Code → статус проверки (valid | invalid | not_found | unknown).

    Коды KktTicketService по спецификации ФНС:
      • 200             — чек найден, реквизиты совпадают (valid);
      • 404, 204, 454   — чек пока отсутствует в хранилище ФНС (not_found);
      • 400, 406, 453   — несовпадение реквизитов / недействительный чек (invalid);
      • 529, 532        — временная перегрузка подсистемы ККТ (unknown, повторим).
    """
    code = str(code or "").strip()
    low = (message or "").lower()
    if code == SUCCESS_CODE:
        return "valid"
    if code in ("529", "532") or "перегруж" in low or "временно недоступ" in low:
        return "unknown"
    if "не найден" in low or "отсутств" in low or code in ("404", "204", "454"):
        return "not_found"
    if (code in ("400", "406", "453")
            or "некоррект" in low or "не совпад" in low
            or "не прошл" in low
            or ("чек" in low and "недейств" in low)):
        return "invalid"
    return "not_found"


# --------------------------------------------------------------------------
#  Предполётный фильтр и отпечаток реквизитов (0 запросов на битые данные)
# --------------------------------------------------------------------------
def normalize_operation(operation: int | None) -> int:
    """Признак расчёта по тегу 1054 ФФД: 1..4 (иначе 1 — приход)."""
    try:
        op = int(operation or 1)
    except (TypeError, ValueError):
        return 1
    return op if op in (1, 2, 3, 4) else 1


def request_fingerprint(fn: str, fd: str, fp: str,
                        total_sum: float | None,
                        receipt_date: datetime | None,
                        operation: int | None = 1) -> str:
    """Отпечаток реквизитов запроса GetTicketInfo для кэша и MessageId."""
    kopeks = int(round(float(total_sum or 0.0) * 100))
    dt_part = (receipt_date.strftime("%Y-%m-%dT%H:%M")
               if isinstance(receipt_date, datetime) else "")
    op = normalize_operation(operation)
    return f"{str(fn or '').strip()}|{str(fd or '').strip()}|{str(fp or '').strip()}|{kopeks}|{dt_part}|{op}"


def validate_ticket_request(fn: str, fd: str, fp: str,
                            total_sum: float | None,
                            receipt_date: datetime | None,
                            operation: int | None = 1) -> FnsResult | None:
    """Отсечь заведомо невалидные реквизиты за 0 сетевых запросов к ФНС.

    Если реквизиты не могут пройти XSD/ФФД проверку KktTicketService
    (нецифровые ФН/ФД/ФП, нулевая/отрицательная сумма или дата в будущем
    дальше 24 часов), возвращает готовый FnsResult('invalid', ...).
    """
    fn_s = str(fn or "").strip()
    fd_s = str(fd or "").strip()
    fp_s = str(fp or "").strip()
    if (not fn_s or not fd_s or not fp_s
            or not fn_s.isdigit() or not fd_s.isdigit() or not fp_s.isdigit()
            or len(fn_s) > 20 or len(fd_s) > 12 or len(fp_s) > 12):
        return FnsResult(
            "invalid",
            "Реквизиты ФН, ФД и ФП должны содержать только цифры — запрос в ФНС не отправлялся",
            {"provider": "fns", "preflight": True, "reason": "bad_fiscal_digits"},
            False,
        )
    try:
        kopeks = int(round(float(total_sum or 0.0) * 100))
    except (TypeError, ValueError):
        kopeks = 0
    if kopeks <= 0:
        return FnsResult(
            "invalid",
            "Сумма чека должна быть больше нуля — запрос в ФНС не отправлялся",
            {"provider": "fns", "preflight": True, "reason": "non_positive_sum"},
            False,
        )
    if operation is not None:
        try:
            op_int = int(operation)
        except (TypeError, ValueError):
            op_int = 0
        if op_int not in (1, 2, 3, 4):
            return FnsResult(
                "invalid",
                "Недопустимый признак расчёта (допустимы 1..4 по ФФД) — запрос в ФНС не отправлялся",
                {"provider": "fns", "preflight": True, "reason": "bad_operation"},
                False,
            )
    if isinstance(receipt_date, datetime):
        cmp_dt = (receipt_date.astimezone(timezone.utc).replace(tzinfo=None)
                  if receipt_date.tzinfo is not None else receipt_date)
        if cmp_dt > _now_naive() + timedelta(hours=24):
            return FnsResult(
                "invalid",
                "Дата расчёта находится в будущем — запрос в ФНС не отправлялся",
                {"provider": "fns", "preflight": True, "reason": "future_date"},
                False,
            )
    return None


def _parse_expire_epoch(expire_text: str | None) -> float | None:
    """Разобрать <ExpireTime> из AuthResponse в эпоху UTC (с запасом 7 мин)."""
    if not expire_text:
        return None
    txt = expire_text.strip()
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        parsed = datetime.fromisoformat(txt)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        exp_ts = parsed.timestamp() - TOKEN_EXPIRE_SAFETY_S
        now = time.time()
        # Защита от рассинхрона часов: не меньше 5 мин и не больше 58 мин
        if now + 300 <= exp_ts <= now + 58 * 60:
            return exp_ts
    except ValueError:
        log.debug("fns: не удалось разобрать ExpireTime=%r", expire_text)
    return None


def _httpx_post_is_native() -> bool:
    """True, если httpx.post не подменён тестовым monkeypatch."""
    mod = getattr(httpx.post, "__module__", "") or ""
    return mod.startswith("httpx")


# --------------------------------------------------------------------------
#  Координатор пакетного опроса GetMessagesRequest (до 20 MessageId)
# --------------------------------------------------------------------------
@dataclass
class _PollWaiter:
    message_id: str
    user_id: str
    event: threading.Event
    outcome: tuple[str, FnsResult | None] | None = None  # ("done"|"processing", res)


class _BatchPollCoordinator:
    """Коалесинг конкурентных опросов в GetMessagesRequest (до 20 MessageId).

    • Если в поллинге только 1 поток — выполняет обычный GetMessageRequest
      без дополнительного ожидания (0 мс задержки, 100 % совместимость).
    • Если параллельно опрашиваются 2..20 чеков с одним user_id — ведущий
      поток собирает их MessageId в один GetMessagesRequest, тратя 1 запрос
      из 1000/сутки на всю пачку вместо N отдельных запросов.
    """

    def __init__(self, client: "FnsClient"):
        self._client = client
        self._guard = threading.Lock()
        self._active: set[tuple[str, str]] = set()
        self._queue: dict[str, list[_PollWaiter]] = {}
        self._leader_busy: set[str] = set()

    def register(self, message_id: str, user_id: str) -> None:
        with self._guard:
            self._active.add((user_id, message_id))

    def unregister(self, message_id: str, user_id: str) -> None:
        with self._guard:
            self._active.discard((user_id, message_id))

    def active_count(self, user_id: str) -> int:
        with self._guard:
            return sum(1 for uid, _ in self._active if uid == user_id)

    def poll_once(self, token: str, message_id: str,
                  user_id: str) -> tuple[str, FnsResult | None]:
        # Одиночный запрос ИЛИ стенд без поддержки GetMessagesRequest →
        # прямой GetMessageRequest без ожидания попутчиков.
        if (not self._client._batch_poll_supported
                or self.active_count(user_id) <= 1):
            return self._client._poll_single_once(token, message_id, user_id)

        waiter = _PollWaiter(message_id=message_id, user_id=user_id,
                             event=threading.Event())
        become_leader = False
        with self._guard:
            bucket = self._queue.setdefault(user_id, [])
            bucket.append(waiter)
            if user_id not in self._leader_busy:
                self._leader_busy.add(user_id)
                become_leader = True

        if become_leader:
            try:
                # Короткое окно 60 мс, чтобы соседние воркеры пачки успели
                # положить свои MessageId в общий конверт GetMessagesRequest.
                time.sleep(0.06)
                while True:
                    with self._guard:
                        bucket = self._queue.get(user_id) or []
                        if not bucket:
                            self._leader_busy.discard(user_id)
                            break
                        batch = bucket[:MAX_BATCH_POLL_IDS]
                        del bucket[:len(batch)]
                    self._dispatch_batch(token, user_id, batch)
            finally:
                with self._guard:
                    remaining = self._queue.pop(user_id, [])
                    self._leader_busy.discard(user_id)
                for w in remaining:
                    if w.outcome is None:
                        w.outcome = self._client._poll_single_once(
                            token, w.message_id, w.user_id)
                    w.event.set()

        waiter.event.wait(timeout=max(5.0, float(self._client.timeout) + 5.0))
        if waiter.outcome is None:
            return self._client._poll_single_once(token, message_id, user_id)
        return waiter.outcome

    def _dispatch_batch(self, token: str, user_id: str,
                        batch: list[_PollWaiter]) -> None:
        unique_ids = list(dict.fromkeys(w.message_id for w in batch))
        if len(unique_ids) == 1 or not self._client._batch_poll_supported:
            outcomes = {
                mid: self._client._poll_single_once(token, mid, user_id)
                for mid in unique_ids
            }
        else:
            outcomes = self._client._poll_many_once(token, unique_ids, user_id)
            if outcomes is None:
                # Стенд не поддерживает GetMessagesRequest — переключаемся
                self._client._batch_poll_supported = False
                outcomes = {
                    mid: self._client._poll_single_once(token, mid, user_id)
                    for mid in unique_ids
                }
        for w in batch:
            w.outcome = outcomes.get(
                w.message_id,
                ("processing", None),
            )
            w.event.set()


# --------------------------------------------------------------------------
#  Клиент ФНС
# --------------------------------------------------------------------------
class FnsClient:
    """Клиент Открытых API ФНС: аутентификация, GetTicket и пакетный опрос."""

    def __init__(self, api_base: str | None = None,
                 master_token: str | None = None,
                 client_app_id: str | None = None,
                 timeout: int | None = None):
        self.api_base = (api_base or _db_setting("fns_api_base")
                         or settings.FNS_API_BASE
                         or FNS_BASE_DEFAULT).rstrip("/")
        self.master_token = (master_token or _db_setting("fns_master_token")
                             or settings.FNS_MASTER_TOKEN or "").strip()
        self.client_app_id = (client_app_id
                              or _db_setting("fns_client_app_id")
                              or settings.FNS_CLIENT_APP_ID or "").strip()
        self.timeout = timeout or settings.FNS_TIMEOUT_SECONDS
        self._token: str | None = None
        self._token_at: float = 0.0
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()
        self._http_lock = threading.Lock()
        self._http: httpx.Client | None = None
        self._batch_poll_supported: bool = True
        self._pending_lock = threading.Lock()
        self._pending_messages: dict[str, tuple[str, str, float]] = {}
        self._coordinator = _BatchPollCoordinator(self)
        self.requests_made = 0          # сколько запросов ушло в этой проверке

    def _remember_pending(self, fp_key: str, message_id: str, user_id: str) -> None:
        now = time.time()
        with self._pending_lock:
            if len(self._pending_messages) >= 1024:
                expired = [k for k, v in self._pending_messages.items()
                           if now - v[2] >= PENDING_MSG_TTL_S]
                for k in expired:
                    self._pending_messages.pop(k, None)
                if len(self._pending_messages) >= 1024:
                    self._pending_messages.clear()
            self._pending_messages[fp_key] = (message_id, user_id, now)

    def _peek_pending(self, fp_key: str) -> tuple[str, str] | None:
        now = time.time()
        with self._pending_lock:
            entry = self._pending_messages.get(fp_key)
            if not entry:
                return None
            mid, uid, created = entry
            if now - created >= PENDING_MSG_TTL_S:
                self._pending_messages.pop(fp_key, None)
                return None
            return mid, uid

    def _forget_pending(self, fp_key: str) -> None:
        with self._pending_lock:
            self._pending_messages.pop(fp_key, None)

    def _cred_fingerprint(self) -> str:
        raw = f"{self.api_base}|{self.master_token}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _load_persisted_token(self) -> tuple[str, float] | None:
        """Восстановить ещё живой токен AuthService из зашифрованных настроек."""
        if not self.master_token or not _httpx_post_is_native():
            return None
        try:
            raw = _db_setting(PERSISTED_TOKEN_KEY)
            if not raw:
                return None
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            if data.get("cred") != self._cred_fingerprint():
                return None
            tok = str(data.get("token") or "").strip()
            exp = float(data.get("expires_at") or 0.0)
            if tok and exp > time.time() + 30:
                return tok, exp
        except Exception:                                # noqa: BLE001
            log.debug("fns: чтение сохранённого сессионного токена пропущено",
                      exc_info=True)
        return None

    def _save_persisted_token(self, token: str, expires_at: float) -> None:
        if not token or not self.master_token or not _httpx_post_is_native():
            return
        try:
            from ..database import SessionLocal
            from .appsettings import set_setting
            db = SessionLocal()
            try:
                payload = json.dumps({
                    "cred": self._cred_fingerprint(),
                    "token": token,
                    "expires_at": round(expires_at, 3),
                }, ensure_ascii=False)
                set_setting(db, PERSISTED_TOKEN_KEY, payload, encrypt=True)
                db.commit()
            finally:
                db.close()
        except Exception:                                # noqa: BLE001
            log.debug("fns: сохранение сессионного токена пропущено",
                      exc_info=True)

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_at = 0.0
        self._token_expires_at = 0.0

    # --- низкий уровень: один запрос к ФНС с учётом лимита и Keep-Alive ---
    def _post(self, path: str, body: str, headers: dict) -> httpx.Response:
        from . import fns_quota
        if not fns_quota.spend(1):
            raise FnsQuotaExceeded(fns_quota.exhausted_message())
        self.requests_made += 1
        full = dict(headers)
        if self.client_app_id:
            full["ClientAppId"] = self.client_app_id
        url = self.api_base + path
        payload = body.encode("utf-8")
        if _httpx_post_is_native():
            with self._http_lock:
                if self._http is None:
                    self._http = httpx.Client(
                        timeout=self.timeout,
                        limits=httpx.Limits(max_keepalive_connections=4,
                                            max_connections=8),
                    )
                client = self._http
            resp = client.post(url, content=payload, headers=full,
                               timeout=self.timeout)
        else:
            resp = httpx.post(url, content=payload, headers=full,
                              timeout=self.timeout)
        log.info("📩 ФНС %s: HTTP %s, ответ %d байт", path, resp.status_code,
                 len(resp.text or ""))
        if resp.status_code != 200:
            raw_err = error_text(resp)
            kind, _ = classify_error(raw_err, resp.status_code)
            if kind == "quota" and _httpx_post_is_native():
                fns_quota.mark_exhausted_today()
            log.warning("⚠️ ФНС %s: %s", path,
                        (resp.text or "")[:400].replace("\n", " "))
        return resp

    def close(self) -> None:
        with self._http_lock:
            if self._http is not None:
                try:
                    self._http.close()
                except Exception:                        # noqa: BLE001
                    log.debug("fns http close skipped", exc_info=True)
                self._http = None

    # --- 1. временный токен (60 мин по ExpireTime / 40 мин базово) --------
    def token(self, force: bool = False) -> str:
        """Временный токен ФНС (по ExpireTime до ~53 мин, иначе 40 мин)."""
        with self._lock:
            now = time.time()
            expires_at = (self._token_expires_at
                          if self._token_expires_at > 0
                          else (self._token_at + TOKEN_TTL_S
                                if self._token_at > 0 else 0.0))
            if self._token and now < expires_at and not force:
                return self._token
            if not force:
                persisted = self._load_persisted_token()
                if persisted is not None:
                    tok, exp = persisted
                    self._token = tok
                    self._token_at = now
                    self._token_expires_at = exp
                    return tok
            if not self.master_token:
                raise RuntimeError("Не задан Мастер-токен ФНС")
            body = AUTH_TEMPLATE.format(master=_esc(self.master_token))
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
                if kind == "quota" and _httpx_post_is_native():
                    from . import fns_quota
                    fns_quota.mark_exhausted_today()
                raise RuntimeError(text if fault else
                                   "ФНС: в ответе аутентификации нет токена")
            got_at = time.time()
            expire_epoch = (_parse_expire_epoch(xml_text(resp.text, "ExpireTime"))
                            or (got_at + TOKEN_TTL_S))
            self._token = token
            self._token_at = got_at
            self._token_expires_at = expire_epoch
            self._save_persisted_token(token, expire_epoch)
            return token

    def _user_token_b64(self, user_id: str) -> str:
        return base64.b64encode(
            (user_id or "ymaster-check").encode("utf-8")).decode("ascii")

    def _headers(self, token: str, action: str,
                 user_id: str) -> dict:
        return {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"urn:{action}"',
            "FNS-OpenApi-Token": token,
            "FNS-OpenApi-UserToken": self._user_token_b64(user_id),
        }

    def _post_with_token_retry(self, path: str, body: str,
                               action: str, user_id: str) -> tuple[str, httpx.Response]:
        """Отправить запрос к KKT_PATH; при протухшем токене обновить 1 раз."""
        token = self.token()
        resp = self._post(path, body, self._headers(token, action, user_id))
        if resp.status_code in (401, 403):
            self._invalidate_token()
            token = self.token(force=True)
            resp = self._post(path, body, self._headers(token, action, user_id))
        elif resp.status_code != 200:
            kind, _ = classify_error(error_text(resp), resp.status_code)
            if kind == "token":
                self._invalidate_token()
                token = self.token(force=True)
                resp = self._post(path, body, self._headers(token, action, user_id))
        return token, resp

    @staticmethod
    def _decode_result_element(result_el) -> FnsResult:
        res_xml = _et_text(result_el)
        code = xml_text(res_xml, "Code") or ""
        message = xml_text(res_xml, "Message") or ""
        ticket_raw = xml_text(res_xml, "Ticket") or ""
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
                True,
            )
        if status_kind == "unknown":
            return FnsResult(
                "unknown",
                message or f"ФНС временно перегружена (код {code}) — повторите позже",
                {"provider": "fns", "code": code, "message": message},
                False,
            )
        return FnsResult(
            status_kind,
            message or ("Чек не найден в базе ФНС"
                        if status_kind == "not_found"
                        else "Чек отклонён ФНС"),
            {"provider": "fns", "code": code, "message": message,
             "ticket": ticket},
            True,
        )

    def _poll_single_once(self, token: str, message_id: str,
                          user_id: str) -> tuple[str, FnsResult | None]:
        """Один шаг одиночного опроса GetMessageRequest."""
        poll = self._post(
            KKT_PATH,
            POLL_TEMPLATE.format(message_id=_esc(message_id)),
            self._headers(token, "GetMessageRequest", user_id),
        )
        if poll.status_code in (401, 403):
            self._invalidate_token()
            token = self.token(force=True)
            poll = self._post(
                KKT_PATH,
                POLL_TEMPLATE.format(message_id=_esc(message_id)),
                self._headers(token, "GetMessageRequest", user_id),
            )
        elif poll.status_code != 200:
            kind_tok, _ = classify_error(error_text(poll), poll.status_code)
            if kind_tok == "token":
                self._invalidate_token()
                token = self.token(force=True)
                poll = self._post(
                    KKT_PATH,
                    POLL_TEMPLATE.format(message_id=_esc(message_id)),
                    self._headers(token, "GetMessageRequest", user_id),
                )
        if poll.status_code != 200:
            kind, text = classify_error(error_text(poll), poll.status_code)
            if kind == "rate_limit":
                return "rate_limit", None
            if kind == "not_found":
                return "lost_message", FnsResult(
                    "not_found", text,
                    {"provider": "fns", "kind": kind}, True)
            return "done", FnsResult(
                "quota" if kind == "quota" else "unknown", text,
                {"provider": "fns", "kind": kind}, False)
        status = (xml_text(poll.text, "ProcessingStatus") or "").upper()
        if status == "PROCESSING":
            return "processing", None
        if status and status != "COMPLETED":
            return "done", FnsResult(
                "unknown", f"ФНС: состояние обработки {status}",
                {"provider": "fns", "status": status}, False)
        result_el = xml_element(poll.text, "Result")
        if result_el is None:
            fault = _fault_text(poll.text)
            kind, text = classify_error(fault)
            if kind == "rate_limit":
                return "rate_limit", None
            if kind == "not_found":
                return "lost_message", FnsResult(
                    "not_found", text,
                    {"provider": "fns", "kind": kind}, True)
            return "done", FnsResult(
                "quota" if kind == "quota" else "unknown",
                text or "ФНС: в ответе нет Result",
                {"provider": "fns", "kind": kind}, False)
        return "done", self._decode_result_element(result_el)

    def _poll_many_once(
        self, token: str, message_ids: list[str], user_id: str,
    ) -> dict[str, tuple[str, FnsResult | None]] | None:
        """Пакетный опрос до 20 MessageId за 1 HTTP-вызов (GetMessagesRequest).

        Возвращает None, если стенд не поддерживает GetMessagesRequest (чтобы
        координатор прозрачно переключился на GetMessageRequest).
        """
        ids_xml = "".join(
            f"<ns:MessageId>{_esc(mid)}</ns:MessageId>"
            for mid in message_ids[:MAX_BATCH_POLL_IDS]
        )
        body = POLL_BATCH_TEMPLATE.format(
            message_ids_xml=ids_xml,
            user_token=_esc(self._user_token_b64(user_id)),
        )
        poll = self._post(
            KKT_PATH, body,
            self._headers(token, "GetMessagesRequest", user_id),
        )
        if poll.status_code in (401, 403):
            self._invalidate_token()
            token = self.token(force=True)
            poll = self._post(
                KKT_PATH, body,
                self._headers(token, "GetMessagesRequest", user_id),
            )
        if poll.status_code != 200:
            raw_err = error_text(poll)
            kind, text = classify_error(raw_err, poll.status_code)
            if kind == "token":
                self._invalidate_token()
                token = self.token(force=True)
                poll = self._post(
                    KKT_PATH, body,
                    self._headers(token, "GetMessagesRequest", user_id),
                )
                if poll.status_code != 200:
                    kind, text = classify_error(error_text(poll),
                                                poll.status_code)
            if poll.status_code != 200:
                if kind in ("contract", "unknown"):
                    return None
                if kind == "rate_limit":
                    return {mid: ("rate_limit", None) for mid in message_ids}
                err_res = FnsResult(
                    "quota" if kind == "quota" else "unknown", text,
                    {"provider": "fns", "kind": kind}, False)
                return {mid: ("done", err_res) for mid in message_ids}

        import xml.etree.ElementTree as ET
        try:
            root = safe_fromstring(poll.text)
        except (ET.ParseError, ValueError):
            return None

        messages_el = None
        for el in root.iter():
            if _local(el.tag) == "Messages":
                messages_el = el
                break
        if messages_el is None:
            return None

        outcomes: dict[str, tuple[str, FnsResult | None]] = {}
        for item_el in list(messages_el):
            if _local(item_el.tag) != "Message":
                continue
            mid = _child_text(item_el, "MessageId")
            if not mid:
                continue
            p_status = _child_text(item_el, "ProcessingStatus").upper()
            if p_status == "PROCESSING":
                outcomes[mid] = ("processing", None)
                continue
            fault_el = _child_element(item_el, "Fault")
            if fault_el is not None:
                ftxt = (_child_text(fault_el, "Message")
                        or _child_text(fault_el, "faultstring")
                        or _et_text(fault_el))
                kind, human = classify_error(ftxt)
                if kind == "not_found":
                    outcomes[mid] = ("lost_message", FnsResult(
                        "not_found", human,
                        {"provider": "fns", "kind": kind}, True))
                elif kind == "rate_limit":
                    outcomes[mid] = ("rate_limit", None)
                else:
                    outcomes[mid] = ("done", FnsResult(
                        "quota" if kind == "quota" else "unknown", human,
                        {"provider": "fns", "kind": kind}, False))
                continue
            res_el = _child_element(item_el, "Result")
            if res_el is not None:
                outcomes[mid] = ("done", self._decode_result_element(res_el))
            elif p_status and p_status != "COMPLETED":
                outcomes[mid] = ("done", FnsResult(
                    "unknown", f"ФНС: состояние обработки {p_status}",
                    {"provider": "fns", "status": p_status}, False))
            else:
                outcomes[mid] = ("processing", None)
        if not outcomes:
            return None
        for mid in message_ids:
            outcomes.setdefault(mid, ("processing", None))
        return outcomes

    def _poll_until_done(self, token: str, message_id: str,
                         user_id: str, fp_key: str) -> tuple[str, FnsResult]:
        """Опрос существующего MessageId с паузами и поддержкой коалесинга."""
        self._coordinator.register(message_id, user_id)
        try:
            for delay in POLL_DELAYS:
                time.sleep(delay)
                state, result = self._coordinator.poll_once(
                    token, message_id, user_id)
                if state == "processing":
                    continue
                if state == "rate_limit":
                    # Минутный троттлинг (5 запросов/мин) — ждём дольше, но
                    # НЕ теряем уже оплаченный MessageId!
                    time.sleep(min(12, delay + 4))
                    continue
                if state == "lost_message":
                    self._forget_pending(fp_key)
                    return "lost_message", result or FnsResult(
                        "not_found",
                        "ФНС: сообщение с результатом не найдено",
                        {"provider": "fns", "kind": "not_found"}, True)
                if result is not None:
                    if result.status in ("valid", "invalid", "not_found"):
                        self._forget_pending(fp_key)
                    return "done", result
            # Таймаут ожидания: сохраняем MessageId для доопроса без новой отправки
            self._remember_pending(fp_key, message_id, user_id)
            return "timeout", FnsResult(
                "unknown",
                "ФНС: время ожидания результата истекло — повторите проверку позже",
                {"provider": "fns", "timeout": True,
                 "pending_message_id": message_id},
                False,
            )
        finally:
            self._coordinator.unregister(message_id, user_id)

    def _send_ticket_message(self, fn: str, fd: str, fp: str,
                             total_sum: float, receipt_date: datetime | None,
                             operation: int, user_id: str) -> tuple[str, str | None, FnsResult | None]:
        """Отправить SendMessageRequest → (token, message_id, err_result)."""
        op_code = normalize_operation(operation)
        send_body = SEND_TEMPLATE.format(
            total=int(round((total_sum or 0) * 100)),
            date=(receipt_date or _now_naive()).strftime("%Y-%m-%dT%H:%M:%S"),
            fn=_esc(fn), fd=_esc(fd), fp=_esc(fp),
            operation=op_code,
        )
        token, resp = self._post_with_token_retry(
            KKT_PATH, send_body, "SendMessageRequest", user_id)
        if resp.status_code != 200:
            raw = error_text(resp)
            kind, text = classify_error(raw, resp.status_code)
            if kind == "unknown" and raw and "Exception" in raw:
                text = f"ФНС: {raw[:200]}"
            if kind == "token":
                self._invalidate_token()
            return token, None, FnsResult(
                "quota" if kind == "quota" else "unknown", text,
                {"provider": "fns", "kind": kind,
                 "raw": resp.text[:400]}, False)
        message_id = xml_text(resp.text, "MessageId")
        if not message_id:
            kind, text = classify_error(_fault_text(resp.text) or resp.text)
            return token, None, FnsResult(
                "quota" if kind == "quota" else "unknown",
                text or "ФНС: в ответе нет MessageId",
                {"provider": "fns", "kind": kind,
                 "raw": resp.text[:400]}, False)
        return token, message_id, None

    # --- 2. проверка чека = GetTicket ------------------------------------
    def get_ticket(self, fn: str, fd: str, fp: str, total_sum: float,
                   receipt_date: datetime, operation: int = 1,
                   user_id: str = "ymaster-check") -> FnsResult:
        """Полный документ чека из ФНС (единственный официальный метод).

        • Предполётный фильтр отсекает некорректные реквизиты за 0 запросов.
        • Если есть живой оплаченный MessageId по этим же реквизитам — сначала
          доопрашивает его за 1 запрос вместо повторного SendMessageRequest.
        • При параллельных проверках опрос автоматически объединяется в
          пакетный GetMessagesRequest (до 20 MessageId в 1 HTTP-вызове).
        """
        preflight = validate_ticket_request(
            fn, fd, fp, total_sum, receipt_date, operation)
        if preflight is not None:
            return preflight
        if not self.master_token:
            return FnsResult("unknown", "Не задан Мастер-токен ФНС",
                             {"provider": "fns", "error": "no_master_token"},
                             False)

        fp_key = request_fingerprint(
            fn, fd, fp, total_sum, receipt_date, operation)
        try:
            # Попытка №0: доопросить уже оплаченный MessageId (1 запрос вместо 2)
            saved_pending = self._peek_pending(fp_key)
            if saved_pending is not None:
                saved_mid, saved_uid = saved_pending
                token = self.token()
                state, resumed = self._poll_until_done(
                    token, saved_mid, saved_uid, fp_key)
                if state != "lost_message":
                    if isinstance(resumed.raw, dict):
                        resumed.raw["checked_fingerprint"] = fp_key
                    return resumed

            token, message_id, send_err = self._send_ticket_message(
                fn, fd, fp, total_sum, receipt_date, operation, user_id)
            if send_err is not None or not message_id:
                return send_err or FnsResult(
                    "unknown", "ФНС: в ответе нет MessageId",
                    {"provider": "fns"}, False)

            self._remember_pending(fp_key, message_id, user_id)
            _, final_res = self._poll_until_done(
                token, message_id, user_id, fp_key)
            if isinstance(final_res.raw, dict):
                final_res.raw["checked_fingerprint"] = fp_key
            return final_res
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
        except RuntimeError as e:
            text = str(e)
            kind, human = classify_error(text)
            if kind == "quota":
                return FnsResult("quota", human,
                                 {"provider": "fns", "kind": "quota"}, False)
            return FnsResult("unknown", human,
                             {"provider": "fns", "kind": kind}, False)

    def get_tickets_batch(
        self,
        items: list[dict],
        *,
        user_id: str = "ymaster-check",
    ) -> list[FnsResult]:
        """Пакетное получение полных чеков: N SendMessage + 1 GetMessages на 20 шт.

        Каждый элемент `items` — словарь с ключами `fn`, `fd`, `fp`,
        `total_sum`, `receipt_date`, `operation` (1..4).
        Расход квоты на пачку из 20 новых чеков при тёплом токене:
        20 SendMessageRequest + 1 GetMessagesRequest = 21 запрос (1.05/чек).
        """
        if not items:
            return []
        results: list[FnsResult | None] = [None] * len(items)
        pending_slots: list[tuple[int, str, str]] = []  # (idx, message_id, fp_key)

        try:
            token: str | None = None
            for idx, spec in enumerate(items):
                fn = str(spec.get("fn") or "")
                fd = str(spec.get("fd") or "")
                fp = str(spec.get("fp") or "")
                total_sum = float(spec.get("total_sum") or 0.0)
                r_dt = spec.get("receipt_date")
                op = normalize_operation(spec.get("operation"))

                pre = validate_ticket_request(fn, fd, fp, total_sum, r_dt, op)
                if pre is not None:
                    results[idx] = pre
                    continue
                if not self.master_token:
                    results[idx] = FnsResult(
                        "unknown", "Не задан Мастер-токен ФНС",
                        {"provider": "fns", "error": "no_master_token"}, False)
                    continue

                fp_key = request_fingerprint(fn, fd, fp, total_sum, r_dt, op)
                saved = self._peek_pending(fp_key)
                if saved is not None:
                    if token is None:
                        token = self.token()
                    pending_slots.append((idx, saved[0], fp_key))
                    continue

                token, mid, send_err = self._send_ticket_message(
                    fn, fd, fp, total_sum, r_dt, op, user_id)
                if send_err is not None or not mid:
                    results[idx] = send_err or FnsResult(
                        "unknown", "ФНС: в ответе нет MessageId",
                        {"provider": "fns"}, False)
                    if send_err is not None and send_err.status == "quota":
                        for rest in range(idx + 1, len(items)):
                            results[rest] = send_err
                        break
                    continue
                self._remember_pending(fp_key, mid, user_id)
                pending_slots.append((idx, mid, fp_key))

            if pending_slots and token is not None:
                unresolved = dict(
                    (idx, (mid, fp_key)) for idx, mid, fp_key in pending_slots
                )
                for delay in POLL_DELAYS:
                    if not unresolved:
                        break
                    time.sleep(delay)
                    mids = list(dict.fromkeys(
                        mid for mid, _ in unresolved.values()))
                    outcomes = None
                    if len(mids) > 1 and self._batch_poll_supported:
                        outcomes = {}
                        for start in range(0, len(mids), MAX_BATCH_POLL_IDS):
                            chunk = mids[start:start + MAX_BATCH_POLL_IDS]
                            part = self._poll_many_once(token, chunk, user_id)
                            if part is None:
                                self._batch_poll_supported = False
                                outcomes = None
                                break
                            outcomes.update(part)
                    if outcomes is None:
                        outcomes = {
                            mid: self._poll_single_once(token, mid, user_id)
                            for mid in mids
                        }
                    hit_rate_limit = False
                    for idx, (mid, fp_key) in list(unresolved.items()):
                        state, res = outcomes.get(mid, ("processing", None))
                        if state == "processing":
                            continue
                        if state == "rate_limit":
                            hit_rate_limit = True
                            continue
                        self._forget_pending(fp_key)
                        if res is not None and isinstance(res.raw, dict):
                            res.raw["checked_fingerprint"] = fp_key
                        results[idx] = res or FnsResult(
                            "unknown", "ФНС: пустой ответ опроса",
                            {"provider": "fns"}, False)
                        unresolved.pop(idx, None)
                    if hit_rate_limit and unresolved:
                        time.sleep(min(12, delay + 4))

                for idx, (mid, fp_key) in unresolved.items():
                    self._remember_pending(fp_key, mid, user_id)
                    results[idx] = FnsResult(
                        "unknown",
                        "ФНС: время ожидания результата истекло — повторите проверку позже",
                        {"provider": "fns", "timeout": True,
                         "pending_message_id": mid,
                         "checked_fingerprint": fp_key},
                        False,
                    )
        except FnsQuotaExceeded as e:
            q_res = FnsResult("quota", str(e),
                              {"provider": "fns", "kind": "quota"}, False)
            for i in range(len(results)):
                if results[i] is None:
                    results[i] = q_res
        except httpx.HTTPError as e:
            n_res = FnsResult("unknown",
                              f"ФНС: сеть недоступна ({e.__class__.__name__})",
                              {"provider": "fns", "kind": "network"}, False)
            for i in range(len(results)):
                if results[i] is None:
                    results[i] = n_res
        except RuntimeError as e:
            kind, human = classify_error(str(e))
            r_res = FnsResult(
                "quota" if kind == "quota" else "unknown", human,
                {"provider": "fns", "kind": kind}, False)
            for i in range(len(results)):
                if results[i] is None:
                    results[i] = r_res

        return [
            r if r is not None else FnsResult(
                "unknown", "Проверка не завершена", {"provider": "fns"}, False)
            for r in results
        ]

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
    """Пошаговая диагностика связи с ФНС (v1.70.71)."""
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

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addr = sorted({i[4][0] for i in infos})
        step("DNS", True, f"{host} → {', '.join(addr[:3])}")
    except OSError as e:
        step("DNS", False, f"Имя {host} не разрешается: {e}")
        return {"ok": False, "steps": steps,
                "verdict": "Сервер не видит адрес ФНС — проверьте DNS"}

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

    client = FnsClient(api_base=base, master_token=token, timeout=int(timeout))
    try:
        got, message = client.probe()
    finally:
        client.close()
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
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
#  Шаблоны SOAP (по «Примерам запросов (Потребитель)» и WSDL API 2.0.9)
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

POLL_BATCH_TEMPLATE = (
    '<soapenv:Envelope xmlns:soapenv='
    '"http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:ns="{ns}">'
    "<soapenv:Header/><soapenv:Body>"
    "<ns:GetMessagesRequest>"
    "<ns:MessageIds>{message_ids_xml}</ns:MessageIds>"
    "<ns:UserToken>{user_token}</ns:UserToken>"
    "</ns:GetMessagesRequest>"
    "</soapenv:Body></soapenv:Envelope>"
).format(ns=NS_ASYNC, message_ids_xml="{message_ids_xml}",
         user_token="{user_token}")


def _read_setting(db, key: str) -> str:
    """Прочитать настройку через переданную сессию или короткую свою."""
    if db is not None:
        try:
            from .appsettings import get_setting
            return (get_setting(db, key, "") or "").strip()
        except Exception:                                # noqa: BLE001
            log.debug("read setting %s failed", key, exc_info=True)
    return _db_setting(key)


# --------------------------------------------------------------------------
#  Общий клиент ФНС (кэш сессионного токена AuthService между проверками)
#  и замок singleflight по фискальной тройке (ФН, ФД, ФП)
# --------------------------------------------------------------------------
_DEFAULT_FNS_CLIENT_CLS = FnsClient
_SHARED_LOCK = threading.Lock()
_SHARED_CLIENTS: dict[tuple[str, str, str], FnsClient] = {}

_INFLIGHT_GUARD = threading.Lock()
_INFLIGHT_LOCKS: dict[tuple[str, str, str], threading.RLock] = {}


def reset_shared_clients(clear_persisted: bool = False) -> None:
    """Сбросить кэшированные клиенты, токены и ожидающие MessageId."""
    with _SHARED_LOCK:
        old = list(_SHARED_CLIENTS.values())
        _SHARED_CLIENTS.clear()
    for cli in old:
        cli.close()
    if clear_persisted:
        try:
            from ..database import SessionLocal
            from .appsettings import set_setting
            db = SessionLocal()
            try:
                set_setting(db, PERSISTED_TOKEN_KEY, "", encrypt=False)
                db.commit()
            finally:
                db.close()
        except Exception:                                # noqa: BLE001
            log.debug("fns persisted token reset skipped", exc_info=True)


def shared_client(*, api_base: str | None = None,
                  master_token: str | None = None,
                  client_app_id: str | None = None,
                  timeout: int | None = None,
                  db=None) -> FnsClient:
    """Общий потокобезопасный FnsClient для актуальных реквизитов доступа."""
    resolved_base = (api_base or _read_setting(db, "fns_api_base")
                     or settings.FNS_API_BASE or FNS_BASE_DEFAULT).rstrip("/")
    resolved_token = (master_token or _read_setting(db, "fns_master_token")
                      or settings.FNS_MASTER_TOKEN or "").strip()
    resolved_app = (client_app_id or _read_setting(db, "fns_client_app_id")
                    or settings.FNS_CLIENT_APP_ID or "").strip()
    resolved_timeout = timeout or settings.FNS_TIMEOUT_SECONDS
    if FnsClient is not _DEFAULT_FNS_CLIENT_CLS:
        return FnsClient(api_base=resolved_base,
                         master_token=resolved_token,
                         client_app_id=resolved_app,
                         timeout=resolved_timeout)
    key = (resolved_base, resolved_token, resolved_app)
    with _SHARED_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            if len(_SHARED_CLIENTS) >= 8:
                oldest = next(iter(_SHARED_CLIENTS))
                evicted = _SHARED_CLIENTS.pop(oldest, None)
                if evicted is not None:
                    evicted.close()
            client = FnsClient(api_base=resolved_base,
                               master_token=resolved_token,
                               client_app_id=resolved_app,
                               timeout=resolved_timeout)
            _SHARED_CLIENTS[key] = client
        else:
            client.timeout = resolved_timeout
        return client


def inflight_lock(fn: str, fd: str, fp: str) -> threading.RLock:
    """Реентерабельный замок на одну фискальную тройку (ФН, ФД, ФП)."""
    key = (str(fn or "").strip(), str(fd or "").strip(), str(fp or "").strip())
    with _INFLIGHT_GUARD:
        lock = _INFLIGHT_LOCKS.get(key)
        if lock is None:
            if len(_INFLIGHT_LOCKS) >= 2048:
                _INFLIGHT_LOCKS.clear()
            lock = threading.RLock()
            _INFLIGHT_LOCKS[key] = lock
        return lock


# --------------------------------------------------------------------------
#  Запись полного ответа ФНС в архивный журнал fns_log (без обрезки на 4 КБ)
# --------------------------------------------------------------------------
def record_fns_log(db, *, receipt_id: str | None, fn: str, fd: str, fp: str,
                   result: FnsResult, duration_ms: int = 0) -> None:
    """Сохранить реальный ответ ФНС в fns_log без обрезки JSON посередине.

    Попадания в локальный кэш (cached=True) или предполётный отказ не пишутся,
    а каждый оплаченный квотой ответ сохраняется валидным JSON (с очищенными
    ПДн покупателя), чтобы даже после удаления чека не платить за него дважды.
    """
    if db is None or not isinstance(result, FnsResult):
        return
    if isinstance(result.raw, dict) and (result.raw.get("cached")
                                         or result.raw.get("preflight")):
        return
    try:
        from sqlalchemy import func
        from sqlalchemy.exc import IntegrityError

        from ..models import FnsLog

        req_json = json.dumps(
            {"fn": str(fn or "").strip(),
             "fd": str(fd or "").strip(),
             "fp": str(fp or "").strip()},
            ensure_ascii=False,
        )
        raw_copy: dict = {}
        if isinstance(result.raw, dict):
            raw_copy = dict(result.raw)
            if isinstance(raw_copy.get("ticket"), dict):
                raw_copy["ticket"] = sanitize_cached_ticket(raw_copy["ticket"])
        resp_json = json.dumps(raw_copy, ensure_ascii=False)
        if len(resp_json) > MAX_FNS_LOG_BYTES:
            slim = {k: v for k, v in raw_copy.items() if k != "ticket"}
            slim["truncated"] = True
            resp_json = json.dumps(slim, ensure_ascii=False)
        prov = str(raw_copy.get("provider") or "fns")[:50]

        for _ in range(5):
            nid = (db.query(func.coalesce(func.max(FnsLog.id), 0)).scalar()
                   or 0) + 1
            db.add(FnsLog(
                id=nid,
                receipt_id=receipt_id,
                request_data=req_json,
                response_data=resp_json,
                provider=prov,
                ok=bool(result.ok),
                duration_ms=int(duration_ms or 0),
            ))
            try:
                db.commit()
                break
            except IntegrityError:
                db.rollback()
    except Exception:                                    # noqa: BLE001
        log.debug("fns_log write skipped", exc_info=True)
        try:
            db.rollback()
        except Exception:                                # noqa: BLE001
            log.debug("fns_log rollback skipped", exc_info=True)


# --------------------------------------------------------------------------
#  Локальный поиск уже проверенного чека в нашей БД (до обращения к ФНС)
# --------------------------------------------------------------------------
def sanitize_cached_ticket(ticket: dict | None) -> dict:
    """Вернуть копию фискального документа без контактных данных покупателя."""
    if not isinstance(ticket, dict):
        return {}

    def _clean(obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kl = str(k).strip().lower()
                if kl in _BUYER_PII_KEYS:
                    continue
                if kl in ("properties", "propertiesuser") and isinstance(v, list):
                    kept = []
                    for prop in v:
                        if isinstance(prop, dict):
                            pname = str(prop.get("propertyName")
                                        or prop.get("key")
                                        or prop.get("name") or "").lower()
                            if any(tag in pname for tag in (
                                    "phone", "email", "mail", "телефон",
                                    "почт", "покупател", "клиент", "contact")):
                                continue
                        kept.append(_clean(prop))
                    out[k] = kept
                else:
                    out[k] = _clean(v)
            return out
        if isinstance(obj, list):
            return [_clean(x) for x in obj]
        return obj

    return _clean(ticket)


def _ticket_body(ticket: dict | None) -> dict:
    """Извлечь словарь реквизитов чека из обёртки Ticket."""
    if not isinstance(ticket, dict) or not ticket:
        return {}
    if isinstance(ticket.get("receipt"), dict):
        return ticket["receipt"]
    doc = ticket.get("document")
    if isinstance(doc, dict) and isinstance(doc.get("receipt"), dict):
        return doc["receipt"]
    return ticket


def _ticket_has_items(ticket: dict | None) -> bool:
    body = _ticket_body(ticket)
    items = body.get("items") if isinstance(body, dict) else None
    return isinstance(items, list) and len(items) > 0


def _build_ticket_from_fields(*, fn: str, fd: str, fp: str,
                              total_sum: float | None,
                              receipt_date: datetime | None,
                              operation: int | None,
                              merchant_name: str = "",
                              merchant_inn: str = "",
                              merchant_address: str = "",
                              cashier: str = "",
                              cash_sum: float | None = None,
                              ecash_sum: float | None = None,
                              taxation: str = "",
                              ext_json: str = "",
                              items=None) -> dict:
    """Собрать совместимый словарь Ticket из колонок, ext_json и позиций БД."""
    rec: dict = {
        "fiscalDriveNumber": str(fn or ""),
        "fiscalDocumentNumber": int(fd) if str(fd or "").isdigit() else str(fd or ""),
        "fiscalSign": int(fp) if str(fp or "").isdigit() else str(fp or ""),
        "totalSum": int(round(float(total_sum or 0.0) * 100)),
        "operationType": normalize_operation(operation),
    }
    if isinstance(receipt_date, datetime):
        rec["dateTime"] = receipt_date.strftime("%Y-%m-%dT%H:%M:%S")
    if merchant_name:
        rec["user"] = merchant_name
    if merchant_inn:
        rec["userInn"] = merchant_inn
    if merchant_address:
        rec["retailPlaceAddress"] = merchant_address
    if cashier:
        rec["operator"] = cashier
    if cash_sum:
        rec["cashTotalSum"] = int(round(float(cash_sum) * 100))
    if ecash_sum:
        rec["ecashTotalSum"] = int(round(float(ecash_sum) * 100))
    if str(taxation or "").isdigit():
        rec["taxationType"] = int(taxation)

    outer: dict = {}
    if ext_json:
        try:
            ext = json.loads(ext_json)
        except ValueError:
            ext = {}
        if isinstance(ext, dict):
            if ext.get("seller_kpp"):
                rec["userKpp"] = ext["seller_kpp"]
            if ext.get("retail_place"):
                rec["retailPlace"] = ext["retail_place"]
            if ext.get("kkt_reg_id"):
                rec["kktRegId"] = ext["kkt_reg_id"]
            if ext.get("shift_number") is not None:
                rec["shiftNumber"] = ext["shift_number"]
            if ext.get("request_number") is not None:
                rec["requestNumber"] = ext["request_number"]
            if ext.get("ffd_version"):
                rec["fiscalDocumentFormatVer"] = ext["ffd_version"]
            if ext.get("cashier_inn"):
                rec["operatorInn"] = ext["cashier_inn"]
            if ext.get("ofd_inn"):
                rec["ofdInn"] = ext["ofd_inn"]
            if ext.get("internet_sign") is not None:
                rec["internetSign"] = ext["internet_sign"]
            if ext.get("seller_email"):
                rec["sellerAddress"] = ext["seller_email"]
            if ext.get("fns_site"):
                rec["fnsSite"] = ext["fns_site"]
            if ext.get("machine_number"):
                rec["machineNumber"] = ext["machine_number"]
            if ext.get("properties") and isinstance(ext["properties"], list):
                rec["properties"] = [
                    {"propertyName": str(p.get("name") or ""),
                     "propertyValue": str(p.get("value") or "")}
                    for p in ext["properties"] if isinstance(p, dict)
                ]
            if ext.get("ofd_id"):
                outer["ofdId"] = ext["ofd_id"]
            if ext.get("ticket_id"):
                outer["id"] = ext["ticket_id"]
            if ext.get("ticket_kind"):
                outer["kind"] = ext["ticket_kind"]

    built_items = []
    for it in (items or []):
        entry: dict = {
            "name": str(getattr(it, "name", "") or "Позиция")[:1000],
            "price": int(round(float(getattr(it, "price", 0.0) or 0.0) * 100)),
            "quantity": float(getattr(it, "quantity", 1.0) or 1.0),
            "sum": int(round(float(getattr(it, "total", 0.0) or 0.0) * 100)),
            "nds": str(getattr(it, "vat_rate", "none") or "none"),
            "ndsSum": int(round(float(getattr(it, "vat_sum", 0.0) or 0.0) * 100)),
        }
        pt = getattr(it, "product_type", None)
        if pt is not None:
            entry["productType"] = pt
        pmt = getattr(it, "payment_type", None)
        if pmt is not None:
            entry["paymentType"] = pmt
        extra_raw = getattr(it, "extra_json", "") or ""
        if extra_raw:
            try:
                extra_obj = json.loads(extra_raw)
            except ValueError:
                extra_obj = {}
            if isinstance(extra_obj, dict):
                if extra_obj.get("measure_code") not in (None, ""):
                    entry["itemsQuantityMeasure"] = extra_obj["measure_code"]
                if extra_obj.get("marking"):
                    entry["productCodeData"] = extra_obj["marking"]
                if extra_obj.get("supplier"):
                    entry["supplierInfo"] = extra_obj["supplier"]
                if extra_obj.get("agent"):
                    entry["agentInfo"] = extra_obj["agent"]
                if extra_obj.get("industry_details"):
                    entry["itemsIndustryDetails"] = extra_obj["industry_details"]
                if extra_obj.get("country"):
                    entry["countryOfOriginCode"] = extra_obj["country"]
                if extra_obj.get("customs"):
                    entry["customsDeclaration"] = extra_obj["customs"]
                if extra_obj.get("excise"):
                    entry["excise"] = int(round(float(extra_obj["excise"]) * 100))
        built_items.append(entry)
    if built_items:
        rec["items"] = built_items
    outer["receipt"] = rec
    return outer


def _matches_request(*, req_total: float | None, req_date: datetime | None,
                     req_op: int | None, row_total: float | None,
                     row_date: datetime | None, row_op: int | None) -> bool:
    """Совпадают ли контрольные реквизиты (сумма, операция 1..4, время ±2 мин)."""
    if req_total is not None and row_total is not None:
        if req_total > 0 and row_total > 0:
            if abs(float(row_total) - float(req_total)) > 0.02:
                return False
    if req_op in (1, 2, 3, 4) and row_op in (1, 2, 3, 4) and int(req_op) != int(row_op):
        return False
    if isinstance(req_date, datetime) and isinstance(row_date, datetime):
        d1 = (req_date.astimezone(timezone.utc).replace(tzinfo=None)
              if req_date.tzinfo is not None else req_date)
        d2 = (row_date.astimezone(timezone.utc).replace(tzinfo=None)
              if row_date.tzinfo is not None else row_date)
        if abs((d2 - d1).total_seconds()) > 120:
            return False
    return True


def _extract_saved_ticket(raw_json: str | None) -> dict:
    """Достать сохранённый JSON документа ФНС из raw_data чека/пула."""
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    fns_block = parsed.get("fns")
    if isinstance(fns_block, dict) and isinstance(fns_block.get("ticket"), dict):
        t = fns_block["ticket"]
        if _ticket_body(t):
            return sanitize_cached_ticket(t)
    if isinstance(parsed.get("ticket"), dict) and _ticket_body(parsed["ticket"]):
        return sanitize_cached_ticket(parsed["ticket"])
    if isinstance(parsed.get("_fns_ticket"), dict) and _ticket_body(parsed["_fns_ticket"]):
        return sanitize_cached_ticket(parsed["_fns_ticket"])
    if isinstance(parsed.get("receipt"), dict) and _ticket_body(parsed):
        return sanitize_cached_ticket(parsed)
    if any(k in parsed for k in ("totalSum", "fiscalDriveNumber", "items")):
        return sanitize_cached_ticket({"receipt": parsed})
    return {}


def _find_ticket_in_fns_log(db, fn: str, fd: str, fp: str) -> dict:
    """Попробовать достать полный Ticket из журнала fns_log."""
    from ..models import FnsLog
    exact_req = json.dumps({"fn": fn, "fd": fd, "fp": fp}, ensure_ascii=False)
    rows = (db.query(FnsLog)
            .filter(FnsLog.ok.is_(True), FnsLog.request_data == exact_req)
            .order_by(FnsLog.id.desc())
            .limit(5).all())
    for row in rows:
        if not row.response_data:
            continue
        try:
            resp = json.loads(row.response_data)
        except ValueError:
            continue
        if not isinstance(resp, dict):
            continue
        ticket = resp.get("ticket")
        if isinstance(ticket, dict) and _ticket_body(ticket):
            code = str(resp.get("code") or "")
            if not code or code == SUCCESS_CODE:
                return sanitize_cached_ticket(ticket)
    return {}


def _not_found_ttl_seconds(receipt_date: datetime | None) -> float:
    """Возрастная лестница кэша not_found: не долбить ОФД-задержку каждую минуту.

    • чек моложе 2 часов     → пауза 15 минут (ОФД ещё везёт пакет);
    • чек от 2 до 24 часов   → пауза 2 часа;
    • чек от 1 до 14 суток   → пауза 12 часов;
    • чек старше 14 суток    → штатный FNS_CACHE_TTL_DAYS (30 суток).
    """
    if not isinstance(receipt_date, datetime):
        return float(settings.FNS_CACHE_TTL_DAYS * 86400)
    r_dt = (receipt_date.astimezone(timezone.utc).replace(tzinfo=None)
            if receipt_date.tzinfo is not None else receipt_date)
    age_s = max(0.0, (_now_naive() - r_dt).total_seconds())
    if age_s < 2 * 3600:
        return float(NOT_FOUND_RETRY_MINUTES * 60)
    if age_s < 24 * 3600:
        return 2 * 3600.0
    if age_s < NOT_FOUND_FINAL_AGE_DAYS * 86400:
        return 12 * 3600.0
    return float(settings.FNS_CACHE_TTL_DAYS * 86400)


def find_cached_in_db(db, fn: str, fd: str, fp: str,
                      total_sum: float | None = None,
                      receipt_date: datetime | None = None,
                      operation: int | None = None,
                      *,
                      require_items: bool = False,
                      exclude_receipt_id: str | None = None) -> FnsResult | None:
    """Проверить, есть ли уже проверенный чек в нашей локальной БД (0 запросов).

    Порядок поиска:
      1. `receipts` (включая отпечаток реквизитов для invalid и лестницу not_found);
      2. `unified_documents` + `unified_items` (включая ранее удалённые из реестра);
      3. `pool_receipts` + `pool_items` (Чек-Пул);
      4. `fns_log` (архив оплаченных квотой ответов ФНС).
    """
    fn_s = str(fn or "").strip()
    fd_s = str(fd or "").strip()
    fp_s = str(fp or "").strip()
    if db is None or not (fn_s and fd_s and fp_s):
        return None

    try:
        from ..models import Receipt
        from ..pool.models import PoolItem, PoolReceipt
        from ..unified.models import (
            REVIEW_BLOCKING,
            REVIEW_VERIFICATION,
            UnifiedDocument,
        )
        from ..unified.service import identity_key

        now = _now_naive()
        req_fp = request_fingerprint(
            fn_s, fd_s, fp_s, total_sum, receipt_date, operation)

        # --- 1. Основной реестр (receipts) -------------------------------
        q = db.query(Receipt).filter(
            Receipt.fn == fn_s, Receipt.fd == fd_s, Receipt.fp == fp_s)
        if exclude_receipt_id:
            q = q.filter(Receipt.id != exclude_receipt_id)
        for rec in q.all():
            if not _matches_request(
                    req_total=total_sum, req_date=receipt_date, req_op=operation,
                    row_total=rec.total_sum, row_date=rec.receipt_date,
                    row_op=rec.operation):
                continue
            if rec.fns_status == "valid":
                ticket = _extract_saved_ticket(rec.raw_data)
                if not _ticket_has_items(ticket) and rec.items:
                    ticket = _build_ticket_from_fields(
                        fn=fn_s, fd=fd_s, fp=fp_s,
                        total_sum=rec.total_sum, receipt_date=rec.receipt_date,
                        operation=rec.operation,
                        merchant_name=rec.merchant_name or "",
                        merchant_inn=rec.merchant_inn or "",
                        merchant_address=rec.merchant_address or "",
                        cashier=rec.cashier or "",
                        cash_sum=rec.cash_sum, ecash_sum=rec.ecash_sum,
                        taxation=getattr(rec, "taxation", "") or "",
                        ext_json=getattr(rec, "ext_json", "") or "",
                        items=rec.items,
                    )
                elif not ticket:
                    ticket = _build_ticket_from_fields(
                        fn=fn_s, fd=fd_s, fp=fp_s,
                        total_sum=rec.total_sum, receipt_date=rec.receipt_date,
                        operation=rec.operation,
                        merchant_name=rec.merchant_name or "",
                        merchant_inn=rec.merchant_inn or "",
                        merchant_address=rec.merchant_address or "",
                        cashier=rec.cashier or "",
                        cash_sum=rec.cash_sum, ecash_sum=rec.ecash_sum,
                        taxation=getattr(rec, "taxation", "") or "",
                        ext_json=getattr(rec, "ext_json", "") or "",
                        items=rec.items,
                    )
                if require_items and not _ticket_has_items(ticket):
                    continue
                log.info("♻️ ФНС [fn=%s i=%s fp=%s]: взят из локальной БД "
                         "(receipts, 0 запросов к ФНС)", fn_s, fd_s, fp_s)
                return FnsResult(
                    "valid",
                    "Чек подтверждён по локальной базе (повторный запрос в ФНС не требовался)",
                    {"provider": "fns", "code": SUCCESS_CODE, "cached": True,
                     "cache_source": "receipts", "ticket": ticket},
                    True,
                )
            if not require_items and rec.fns_checked_at is not None:
                if (rec.fns_status == "invalid"
                        and (now - rec.fns_checked_at).days < settings.FNS_CACHE_TTL_DAYS):
                    saved_fp = ""
                    try:
                        r_data = json.loads(rec.raw_data or "{}")
                        if isinstance(r_data.get("fns"), dict):
                            saved_fp = str(r_data["fns"].get("checked_fingerprint") or "")
                    except ValueError:
                        saved_fp = ""
                    # Если был сохранён отпечаток и пользователь уже исправил
                    # сумму/дату/признак — не блокируем перепроверку!
                    if not saved_fp or saved_fp == req_fp:
                        return FnsResult(
                            "invalid",
                            rec.fns_message or "Чек отклонён ФНС (из локальной базы)",
                            {"provider": "fns", "cached": True,
                             "cache_source": "receipts",
                             "checked_fingerprint": req_fp},
                            True,
                        )
                if rec.fns_status == "not_found":
                    ttl_s = _not_found_ttl_seconds(receipt_date or rec.receipt_date)
                    if (now - rec.fns_checked_at).total_seconds() < ttl_s:
                        return FnsResult(
                            "not_found",
                            rec.fns_message or "Чек пока не найден в ФНС (выдерживаем паузу ОФД)",
                            {"provider": "fns", "cached": True,
                             "cache_source": "receipts"},
                            True,
                        )

        # --- 2. Единая база (unified_documents, включая удалённые из реестра)
        ukey = identity_key(fn_s, fd_s, fp_s)
        doc = None
        if ukey:
            doc = (db.query(UnifiedDocument)
                   .filter(UnifiedDocument.identity_key == ukey).first())
        if doc is None:
            doc = (db.query(UnifiedDocument)
                   .filter(UnifiedDocument.fn == fn_s,
                           UnifiedDocument.fd == fd_s,
                           UnifiedDocument.fp == fp_s).first())
        skip_flags = REVIEW_BLOCKING | REVIEW_VERIFICATION
        if (doc is not None and not ((doc.review_flags or 0) & skip_flags)
                and _matches_request(
                    req_total=total_sum, req_date=receipt_date, req_op=operation,
                    row_total=doc.total_sum, row_date=doc.receipt_date,
                    row_op=doc.operation)):
            if doc.fns_status == "valid":
                ticket = _find_ticket_in_fns_log(db, fn_s, fd_s, fp_s)
                if not _ticket_has_items(ticket) and doc.items:
                    ticket = _build_ticket_from_fields(
                        fn=fn_s, fd=fd_s, fp=fp_s,
                        total_sum=doc.total_sum, receipt_date=doc.receipt_date,
                        operation=doc.operation,
                        merchant_name=doc.merchant_name or "",
                        merchant_inn=doc.merchant_inn or "",
                        merchant_address=doc.merchant_address or "",
                        cashier=doc.cashier or "",
                        items=doc.items,
                    )
                elif not ticket:
                    ticket = _build_ticket_from_fields(
                        fn=fn_s, fd=fd_s, fp=fp_s,
                        total_sum=doc.total_sum, receipt_date=doc.receipt_date,
                        operation=doc.operation,
                        merchant_name=doc.merchant_name or "",
                        merchant_inn=doc.merchant_inn or "",
                        merchant_address=doc.merchant_address or "",
                        cashier=doc.cashier or "",
                        items=doc.items,
                    )
                if not require_items or _ticket_has_items(ticket):
                    log.info("♻️ ФНС [fn=%s i=%s fp=%s]: взят из единой базы "
                             "(unified_documents, 0 запросов к ФНС)", fn_s, fd_s, fp_s)
                    return FnsResult(
                        "valid",
                        "Чек подтверждён по единой базе (повторный запрос в ФНС не требовался)",
                        {"provider": "fns", "code": SUCCESS_CODE, "cached": True,
                         "cache_source": "unified", "ticket": ticket},
                        True,
                    )

        # --- 3. Чек-Пул (pool_receipts) ----------------------------------
        pool = (db.query(PoolReceipt)
                .filter(PoolReceipt.fn == fn_s,
                        PoolReceipt.fd == fd_s,
                        PoolReceipt.fp == fp_s).first())
        if (pool is not None and (pool.fns_confirmed or pool.status == "verified")
                and _matches_request(
                    req_total=total_sum, req_date=receipt_date, req_op=operation,
                    row_total=pool.total_sum, row_date=pool.receipt_date,
                    row_op=pool.operation)):
            pool_items = (db.query(PoolItem)
                          .filter(PoolItem.receipt_id == pool.id)
                          .order_by(PoolItem.id.asc()).all())
            ticket = _extract_saved_ticket(pool.raw)
            if not _ticket_has_items(ticket) and pool_items:
                ticket = _build_ticket_from_fields(
                    fn=fn_s, fd=fd_s, fp=fp_s,
                    total_sum=pool.total_sum, receipt_date=pool.receipt_date,
                    operation=pool.operation,
                    merchant_name=pool.merchant_name or "",
                    merchant_inn=pool.merchant_inn or "",
                    merchant_address=pool.merchant_address or "",
                    cashier=pool.cashier or "",
                    ext_json=getattr(pool, "extra_json", "") or "",
                    items=pool_items,
                )
            elif not ticket:
                ticket = _build_ticket_from_fields(
                    fn=fn_s, fd=fd_s, fp=fp_s,
                    total_sum=pool.total_sum, receipt_date=pool.receipt_date,
                    operation=pool.operation,
                    merchant_name=pool.merchant_name or "",
                    merchant_inn=pool.merchant_inn or "",
                    merchant_address=pool.merchant_address or "",
                    cashier=pool.cashier or "",
                    ext_json=getattr(pool, "extra_json", "") or "",
                    items=pool_items,
                )
            if not require_items or _ticket_has_items(ticket):
                log.info("♻️ ФНС [fn=%s i=%s fp=%s]: взят из базы Чек-Пула "
                         "(0 запросов к ФНС)", fn_s, fd_s, fp_s)
                return FnsResult(
                    "valid",
                    "Чек подтверждён по базе Чек-Пула (повторный запрос в ФНС не требовался)",
                    {"provider": "fns", "code": SUCCESS_CODE, "cached": True,
                     "cache_source": "pool", "ticket": ticket},
                    True,
                )

        # --- 4. Журнал проверок (fns_log) --------------------------------
        log_ticket = _find_ticket_in_fns_log(db, fn_s, fd_s, fp_s)
        if log_ticket:
            body = _ticket_body(log_ticket)
            raw_sum = body.get("totalSum")
            t_sum = None
            if isinstance(raw_sum, (int, float)):
                t_sum = float(raw_sum) / 100.0
            t_op = body.get("operationType")
            t_op_int = int(t_op) if str(t_op or "").isdigit() else None
            t_dt = None
            dt_str = body.get("dateTime")
            if isinstance(dt_str, str) and len(dt_str) >= 16:
                try:
                    t_dt = datetime.fromisoformat(dt_str.replace("Z", ""))
                except ValueError:
                    t_dt = None
            if (_matches_request(
                    req_total=total_sum, req_date=receipt_date, req_op=operation,
                    row_total=t_sum, row_date=t_dt, row_op=t_op_int)
                    and (not require_items or _ticket_has_items(log_ticket))):
                log.info("♻️ ФНС [fn=%s i=%s fp=%s]: взят из журнала fns_log "
                         "(0 запросов к ФНС)", fn_s, fd_s, fp_s)
                return FnsResult(
                    "valid",
                    "Чек подтверждён по журналу проверок (повторный запрос в ФНС не требовался)",
                    {"provider": "fns", "code": SUCCESS_CODE, "cached": True,
                     "cache_source": "fns_log", "ticket": log_ticket},
                    True,
                )
    except Exception:                                    # noqa: BLE001
        log.debug("fns local db cache lookup skipped", exc_info=True)
    return None


def find_known_fn_merchant(db, fn: str) -> dict:
    """Оффлайн-справочник продавца по заводскому номеру 16-значного ФН (0 запросов).

    По 54-ФЗ фискальный накопитель жёстко регистрируется на конкретный ИНН и
    карточку ККТ. Если в нашей БД уже есть подтверждённый ФНС чек с тем же ФН,
    мы мгновенно знаем продавца, ИНН, адрес расчётов и регион даже при
    исчерпанной суточной квоте ФНС — НЕ помечая новый чек как проверенный ФНС.
    """
    fn_s = str(fn or "").strip()
    if db is None or len(fn_s) < 14 or not fn_s.isdigit():
        return {}
    try:
        from ..models import Receipt
        from ..unified.models import UnifiedDocument

        rec = (db.query(Receipt)
               .filter(Receipt.fn == fn_s,
                       Receipt.fns_status == "valid",
                       Receipt.merchant_inn != "")
               .order_by(Receipt.fns_checked_at.desc())
               .first())
        if rec is not None and (rec.merchant_name or rec.merchant_inn):
            return {
                "merchant_name": rec.merchant_name or "",
                "merchant_inn": rec.merchant_inn or "",
                "merchant_address": rec.merchant_address or "",
                "region_code": getattr(rec, "region_code", "") or "",
                "region_name": getattr(rec, "region_name", "") or "",
                "region_city": getattr(rec, "region_city", "") or "",
                "taxation": getattr(rec, "taxation", "") or "",
                "taxation_name": getattr(rec, "taxation_name", "") or "",
            }
        udoc = (db.query(UnifiedDocument)
                .filter(UnifiedDocument.fn == fn_s,
                        UnifiedDocument.fns_status == "valid",
                        UnifiedDocument.merchant_inn != "")
                .order_by(UnifiedDocument.fns_checked_at.desc())
                .first())
        if udoc is not None and (udoc.merchant_name or udoc.merchant_inn):
            return {
                "merchant_name": udoc.merchant_name or "",
                "merchant_inn": udoc.merchant_inn or "",
                "merchant_address": udoc.merchant_address or "",
                "region_code": udoc.region_code or "",
                "region_name": "",
                "region_city": udoc.region_city or "",
                "taxation": "",
                "taxation_name": "",
            }
    except Exception:                                    # noqa: BLE001
        log.debug("fn merchant hint lookup skipped", exc_info=True)
    return {}


def apply_fn_merchant_hint(db, receipt) -> bool:
    """Заполнить пустые поля продавца по известному ФН ККТ (без смены fns_status)."""
    if db is None or receipt is None:
        return False
    if receipt.merchant_name and receipt.merchant_inn:
        return False
    hint = find_known_fn_merchant(db, getattr(receipt, "fn", ""))
    if not hint:
        return False
    changed = False
    for attr in ("merchant_name", "merchant_inn", "merchant_address",
                 "region_code", "region_name", "region_city",
                 "taxation", "taxation_name"):
        val = hint.get(attr)
        if val and not getattr(receipt, attr, ""):
            setattr(receipt, attr, val)
            changed = True
    if changed:
        if not getattr(receipt, "details_source", ""):
            receipt.details_source = "fn_registry_hint"
        from ..unified import service as unified
        unified.sync_registry_safely(db, receipt, "fn_hint")
    return changed


def _is_cached_status_fresh(cached_result: str | None,
                            cached_at: datetime | None,
                            receipt_date: datetime | None = None) -> bool:
    """Актуален ли сохранённый статус проверки без нового запроса к ФНС."""
    if cached_at is None or cached_result not in ("valid", "invalid", "not_found"):
        return False
    now = _now_naive()
    if cached_result == "valid":
        return True
    if cached_result == "invalid":
        return (now - cached_at).days < settings.FNS_CACHE_TTL_DAYS
    ttl_s = _not_found_ttl_seconds(receipt_date)
    return (now - cached_at).total_seconds() < ttl_s


# --------------------------------------------------------------------------
#  Проверка чека с кэшем и предварительным поиском по локальной БД
# --------------------------------------------------------------------------
def check_receipt(fn: str, fd: str, fp: str, total_sum: float,
                  receipt_date: datetime,
                  cached_result: str | None = None,
                  cached_at: datetime | None = None,
                  *,
                  operation: int = 1,
                  db=None,
                  force: bool = False) -> FnsResult:
    """Проверка чека в ФНС с обязательной предварительной проверкой локальной БД."""
    preflight = validate_ticket_request(
        fn, fd, fp, total_sum, receipt_date, operation)
    if preflight is not None:
        return preflight

    op_code = normalize_operation(operation)
    if not force:
        if db is not None:
            hit = find_cached_in_db(db, fn, fd, fp, total_sum, receipt_date,
                                    op_code)
            if hit is not None:
                return hit
        if _is_cached_status_fresh(cached_result, cached_at, receipt_date):
            return FnsResult(
                cached_result or "valid",
                "Результат из кэша (повторная проверка не требовалась)",
                {"provider": "fns", "cached": True},
                True,
            )

    with inflight_lock(fn, fd, fp):
        if not force:
            try:
                from ..database import SessionLocal
                fresh_db = SessionLocal()
                try:
                    hit = find_cached_in_db(fresh_db, fn, fd, fp, total_sum,
                                            receipt_date, op_code)
                    if hit is not None:
                        return hit
                finally:
                    fresh_db.close()
            except Exception:                            # noqa: BLE001
                log.debug("check_receipt local db lookup skipped", exc_info=True)
        client = shared_client(db=db)
        return client.get_ticket(fn, fd, fp, total_sum, receipt_date,
                                 operation=op_code)


def check_receipts_batch(items: list[dict], *, db=None,
                         force: bool = False) -> list[FnsResult]:
    """Пакетная проверка списка чеков с локальным БД-фильтром и GetMessagesRequest."""
    if not items:
        return []
    out: list[FnsResult | None] = [None] * len(items)
    to_query: list[tuple[int, dict]] = []

    for idx, spec in enumerate(items):
        fn = str(spec.get("fn") or "")
        fd = str(spec.get("fd") or "")
        fp = str(spec.get("fp") or "")
        total_sum = float(spec.get("total_sum") or 0.0)
        r_dt = spec.get("receipt_date")
        op = normalize_operation(spec.get("operation"))

        pre = validate_ticket_request(fn, fd, fp, total_sum, r_dt, op)
        if pre is not None:
            out[idx] = pre
            continue
        if not force and db is not None:
            hit = find_cached_in_db(db, fn, fd, fp, total_sum, r_dt, op)
            if hit is not None:
                out[idx] = hit
                continue
        to_query.append((idx, {
            "fn": fn, "fd": fd, "fp": fp,
            "total_sum": total_sum, "receipt_date": r_dt, "operation": op,
        }))

    if to_query:
        client = shared_client(db=db)
        fetched = client.get_tickets_batch([spec for _, spec in to_query])
        for (idx, _), res in zip(to_query, fetched):
            out[idx] = res

    return [
        r if r is not None else FnsResult(
            "unknown", "Проверка не завершена", {"provider": "fns"}, False)
        for r in out
    ]
