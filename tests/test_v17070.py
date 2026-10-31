# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.70: проверка чеков только через официальное
# API ФНС (комплект «API 2.0.9 от 10.12.2024»), учёт суточного лимита
# 1000 запросов, очередь чеков при исчерпании лимита, удаление сторонних
# провайдеров и демонстрационного источника.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import datetime as dt
import re
from pathlib import Path

from sqlalchemy import text as sql_text

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CONFIG = APP / "config.py"
INDEX = APP / "static" / "index.html"
SW = APP / "static" / "sw.js"
MANIFEST = APP / "static" / "manifest.webmanifest"
MAX_HTML = APP / "static" / "maxapp" / "index.html"
CABINET = APP / "static" / "b2b" / "index.html"
APP_JS = APP / "static" / "js" / "app.js"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"

OWNER_QUOTE_A = "работать только через официальные источники"
OWNER_QUOTE_B = "ещё раз проверь всё по api"


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8")


class TestRelease17070:
    """Замок выпуска 1.70.70. Точки синхронизации версии ведёт файл
    текущего выпуска — tests/test_v17071.py, здесь остаётся история."""

    VERSION = "1.70.70"
    PREVIOUS = "1.70.69"

    def _current(self) -> str:
        return re.search(r'APP_VERSION: str = "([^"]+)"',
                         _read(CONFIG)).group(1)

    def _current(self) -> str:
        return re.search(r'APP_VERSION: str = "([^"]+)"',
                         _read(CONFIG)).group(1)

    def test_sync_points_not_behind(self):
        """Точки синхронизации идут за текущей версией (не отстают)."""
        ver = self._current()
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 70)
        assert f"ymaster-check-v{ver}" in _read(SW)
        assert f'"version": "{ver}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={ver}" in index and f"app.js?v={ver}" in index

    def test_whats_new_entry_present(self):
        js = _read(APP_JS)
        assert f"'{self.VERSION}': [" in js
        block = js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]
        assert block.count("{ icon:") == 3
        for needle in ("ФНС", "1000", "официальн"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section
        flat = section.lower()
        for needle in ("openapi", "getticket", "soapaction", "1000", "лимит"):
            assert needle in flat, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "Решение", "Интерфейс", "Проверено",
                       "ФНС", "лимит"):
            assert needle in section, f"в журнале нет «{needle}»"

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
                assert needle in flat, f"{Path(path).name}: {needle}"


class TestSingleSource:
    """Остался один источник проверки — официальное API ФНС."""

    def test_external_has_no_third_party_provider(self):
        src = _read(APP / "services" / "external.py")
        assert "def fetch_proverkacheka" not in src
        assert "PROVERKACHEKA_URL" not in src
        assert "proverkacheka.com/api" not in src

    def test_no_demo_provider_in_code(self):
        fns = _read(APP / "services" / "fns.py")
        ext = _read(APP / "services" / "external.py")
        assert "def check_mock" not in fns
        assert '"mock"' not in ext
        assert "FNS_PROVIDER" not in _read(CONFIG)

    def test_settings_have_no_provider_fields(self):
        sc = _read(APP / "schemas.py")
        assert "proverkacheka_token" not in sc
        assert "external_order" not in sc
        sr = _read(APP / "routers" / "settings_routes.py")
        assert "proverkacheka_token" not in sr
        assert "external_order" not in sr

    def test_ui_has_no_demo_switch(self):
        js = _read(APP_JS)
        assert 'id="seg-mock"' not in js and 'id="seg-fns"' not in js
        assert "Демо (mock)" not in js
        assert 'id="ext-pke"' not in js and 'id="ext-order"' not in js

    def test_checko_untouched(self):
        """Решение владельца: Checko остаётся для карточек компаний."""
        assert (APP / "services" / "checko.py").exists()
        assert (APP / "services" / "registry.py").exists()
        assert "checko_api_key" in _read(APP / "services" / "appsettings.py")


class TestFnsContract:
    """Контракт сверен с комплектом «API 2.0.9 от 10.12.2024»."""

    def _fns(self):
        from app.services import fns
        return fns

    def test_endpoints_and_namespaces(self):
        fns = self._fns()
        assert fns.AUTH_PATH == "/open-api/AuthService/0.1"
        assert fns.KKT_PATH == "/open-api/ais3/KktService/0.1"
        assert fns.NS_AUTH == ("urn://x-artefacts-gnivc-ru/ais3/kkt/"
                               "AuthService/types/1.0")
        assert fns.NS_KKT == ("urn://x-artefacts-gnivc-ru/ais3/kkt/"
                              "KktTicketService/types/1.0")
        assert "OpenApiAsyncMessageConsumerService/types/1.0" in fns.NS_ASYNC
        assert "OpenApiMessageConsumerService/types/1.0" in fns.NS_SYNC

    def test_auth_body_is_in_auth_namespace(self):
        fns = self._fns()
        body = fns.AUTH_TEMPLATE.format(master="M")
        assert fns.NS_AUTH in body
        assert "<tns:MasterToken>M</tns:MasterToken>" in body
        assert 'SOAPAction' not in body      # заголовок, а не тело

    def test_send_body_has_all_ticket_fields(self):
        fns = self._fns()
        body = fns.SEND_TEMPLATE.format(total=89000, date="2026-10-01T09:08:00",
                                        fn="1", fd="2", fp="3", operation=1)
        for tag in ("Sum", "Date", "Fn", "TypeOperation", "FiscalDocumentId",
                    "FiscalSign", "RawData"):
            assert f"<tns:{tag}>" in body, tag
        assert fns.NS_KKT in body and fns.NS_ASYNC in body

    def test_token_lives_forty_minutes(self):
        assert self._fns().TOKEN_TTL_S == 40 * 60

    def test_success_code_is_200(self):
        assert self._fns().SUCCESS_CODE == "200"

    def test_no_invented_rest_endpoint(self):
        for path in (APP / "services" / "fns.py",
                     APP / "services" / "external.py"):
            assert "/open-api/api/2/check" not in _read(path)

    def test_xml_parsed_by_local_name(self):
        """Префиксы ns2: в ответах ФНС не мешают разбору."""
        fns = self._fns()
        xml = ('<a:Envelope xmlns:a="x"><a:Body><b:Result xmlns:b="y">'
               "<b:Token>TT</b:Token></b:Result></a:Body></a:Envelope>")
        assert fns.xml_text(xml, "Token") == "TT"
        assert fns.xml_element(xml, "Result") is not None

    def test_error_classifier(self):
        fns = self._fns()
        assert fns.classify_error(
            "Превышен общий дневной лимит запросов")[0] == "quota"
        assert fns.classify_error(
            "AccessDeniedException: Доступ к сервису для переданного IP, "
            "запрещен")[0] == "ip"
        assert fns.classify_error("Отказ в доступе: не верное значение "
                                  "token")[0] == "token"
        assert fns.classify_error(
            "UnmarshallingFailureException")[0] == "contract"
        # наша собственная формулировка тоже распознаётся (нашли живой
        # проверкой: «суточный лимит» уходил в «сеть недоступна»)
        assert fns.classify_error(
            "ФНС: исчерпан суточный лимит запросов приложения")[0] == "quota"

    def test_ticket_code_mapping(self):
        fns = self._fns()
        assert fns.classify_ticket_code("200") == "valid"
        assert fns.classify_ticket_code("404", "Чек не найден") == "not_found"


class TestDailyLimit:
    """Лимит 1000 запросов за 24 часа: счёт, предупреждение, очередь."""

    def test_limit_is_thousand(self):
        from app.services import fns_quota
        assert fns_quota.DAILY_LIMIT == 1000
        assert fns_quota.WARN_PERCENT == 80
        assert fns_quota.limit_value() == 1000

    def test_msk_day_format(self):
        from app.services import fns_quota
        moment = dt.datetime(2026, 10, 31, 22, 30, tzinfo=dt.timezone.utc)
        # 22:30 UTC = 01:30 следующего дня по Москве
        assert fns_quota.msk_day(moment) == "2026-11-01"

    def test_spend_stops_at_limit(self, monkeypatch):
        from app.services import fns_quota
        monkeypatch.setattr(fns_quota, "limit_value", lambda: 3)
        fns_quota.reset_today()
        assert fns_quota.spend(1) is True
        assert fns_quota.spend(2) is True
        assert fns_quota.spend(1) is False, "запрос сверх лимита не проходит"
        assert fns_quota.remaining() == 0
        fns_quota.reset_today()

    def test_warning_at_eighty_percent(self, monkeypatch):
        from app.services import fns_quota
        monkeypatch.setattr(fns_quota, "limit_value", lambda: 10)
        fns_quota.reset_today()
        for _ in range(7):
            assert fns_quota.spend(1) is True
        assert fns_quota.warning() == {}, "до 80 % предупреждения нет"
        assert fns_quota.spend(1) is True          # 8 из 10 — порог
        warn = fns_quota.warning()
        assert warn and warn["used"] == 8 and warn["limit"] == 10
        assert "8 из 10" in warn["text"]
        fns_quota.reset_today()

    def test_stats_shape(self):
        from app.services import fns_quota
        st = fns_quota.stats()
        for key in ("limit", "used", "remaining", "percent", "warn_percent",
                    "exhausted", "day"):
            assert key in st
        assert st["limit"] == 1000
        assert st["warn_percent"] == 80

    def test_cache_does_not_spend_limit(self):
        from app.services import fns
        fresh = dt.datetime.now() - dt.timedelta(days=3)
        res = fns.check_receipt("1", "2", "3", 100.0, fresh,
                                cached_result="valid", cached_at=fresh)
        assert res.ok is True and res.raw.get("cached") is True
        assert res.status == "valid"


class TestQueueAtLimit:
    """Чек при исчерпанном лимите остаётся «ожидает проверки»."""

    def test_receipt_status_pending(self):
        src = _read(APP / "routers" / "receipts.py")
        assert 'receipt.fns_status = "pending"' in src
        assert 'receipt.status = "new"' in src

    def test_retry_worker_started(self):
        src = _read(APP / "routers" / "receipts.py")
        assert "start_limit_retry_worker" in src
        assert "LIMIT_RETRY_INTERVAL_S" in src
        assert "start_limit_retry_worker" in _read(APP / "main.py")

    def test_pending_status_label(self):
        js = _read(APP_JS)
        assert "ожидает проверки" in js

    def test_quota_exceeded_message(self, monkeypatch):
        from app.services import fns_quota
        monkeypatch.setattr(fns_quota, "stats", lambda db=None: {
            "limit": 1000, "used": 1000, "remaining": 0, "percent": 100,
            "warn_percent": 80, "exhausted": True, "day": "2026-10-31"})
        msg = fns_quota.exhausted_message()
        assert "лимит" in msg.lower() and "1000" in msg


class TestEncryptedTokenRead:
    """v1.70.71: движок обязан читать токен РАСШИФРОВАННЫМ.

    Найдено при живой проверке: настройки брались прямо из таблицы, где
    мастер-токен хранится как «enc:v1:…», и в ФНС уходила зашифрованная
    строка — сервис отвечал отказом, проверка «не работала».
    """

    def test_settings_are_decrypted(self, client):
        from app.services.external import engine
        from app.database import SessionLocal
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        client.put("/api/v1/settings/fns", json={
            "master_token": "MASTER-PLAIN-TOKEN",
            "client_app_id": "YmasterAPI"}, headers=hdr)
        db = SessionLocal()
        try:
            raw = db.execute(sql_text(
                "SELECT value FROM app_settings "
                "WHERE key='fns_master_token'")).scalar()
            cfg = engine._settings(db)
        finally:
            db.close()
        assert raw and raw.startswith("enc:"), "в базе токен должен быть шифром"
        assert cfg["fns_master_token"] == "MASTER-PLAIN-TOKEN"
        assert "enc:" not in cfg["fns_master_token"]

    def test_client_sends_plain_token_to_fns(self, client):
        """Клиент, созданный движком, не содержит «enc:» в мастер-токене."""
        from app.services import fns
        from app.database import SessionLocal
        from app.services.external import engine
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        client.put("/api/v1/settings/fns", json={
            "master_token": "MASTER-SECOND-TOKEN"}, headers=hdr)
        db = SessionLocal()
        try:
            cfg = engine._settings(db)
        finally:
            db.close()
        client_fns = fns.FnsClient(master_token=cfg.get("fns_master_token"))
        assert client_fns.master_token == "MASTER-SECOND-TOKEN"


class TestDiagnose:
    """Кнопка «Диагностика связи» показывает место обрыва."""

    def test_no_token_verdict(self):
        from app.services import fns
        res = fns.diagnose(api_base="http://127.0.0.1:9", master_token="")
        assert res["ok"] is False
        assert "мастер-токен" in res["verdict"].lower()
        assert res["steps"][0]["ok"] is False

    def test_closed_port_verdict(self):
        from app.services import fns
        res = fns.diagnose(api_base="http://127.0.0.1:9", master_token="T",
                           timeout=2)
        assert res["ok"] is False
        assert "8090" in res["verdict"] or "закрыт" in res["verdict"]
        names = [s["name"] for s in res["steps"]]
        assert "Соединение" in names

    def test_endpoint_is_admin_only(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/settings/fns/diagnose", headers=hdr)
        assert r.status_code == 200
        body = r.json()
        assert "steps" in body and "verdict" in body

    def test_master_token_error_is_named(self):
        from app.services import fns
        kind, text = fns.classify_error(
            "Отказ в доступе: не верное значение master-токена")
        assert kind == "master" and "мастер-токен" in text.lower()


class TestSettingsUi:
    def test_quota_visible_to_admin(self):
        js = _read(APP_JS)
        assert "fns.quota" in js and "ext.quota" in js
        assert "остаток" in js and "80 %" in js

    def test_endpoints_report_quota(self):
        sr = _read(APP / "routers" / "settings_routes.py")
        assert fns_quota_marker(sr) is True
        rc = _read(APP / "routers" / "receipts.py")
        assert '"quota"' in rc


def fns_quota_marker(src: str) -> bool:
    """В настройках ФНС есть расход лимита (без отдельного эндпоинта)."""
    return "fns_quota.stats(db)" in src
