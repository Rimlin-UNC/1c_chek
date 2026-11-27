"""Замок выпуска 1.73.10: защита суточного лимита ФНС (1000 запросов/сутки)
и обязательная предварительная проверка чека по локальной БД до обращения к ФНС.

Покрывает:
  1. Полную синхронизацию версии 1.73.10, кэша статики, реестра блоков и КБ.
  2. Устранение 6-кратного списания лимита ФНС на один чек:
     • первый новый чек (холодный токен AuthService) → ровно 3 запроса
       (1 Auth + 1 SendMessage + 1 GetMessage), а не 6;
     • второй новый чек (тёплый 40-минутный токен) → ровно 2 запроса
       (0 Auth + 1 SendMessage + 1 GetMessage);
     • повторная проверка / загрузка деталей того же чека → 0 запросов к ФНС.
  3. Предварительную проверку локальной БД (`receipts`, `unified_documents`
     включая удалённые из реестра чеки, `pool_receipts`, `fns_log`) до любого
     обращения к ФНС — 0 сетевых запросов для уже известных чеков.
  4. Очистку контактных ПДн покупателя (152-ФЗ / ADR-20) при переиспользовании
     документа из локальной БД и отказ кэша при несовпадении суммы/операции.
  5. Singleflight-защиту параллельных запросов на одну тройку (ФН, ФД, ФП),
     проброс `TypeOperation=2` для чеков возврата прихода (`n=2`) и уважение
     выключенной настройки `auto_verify=False`.
"""
from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import FnsLog, Receipt
from app.pool.models import PoolItem, PoolReceipt
from app.services import fns, fns_quota, updater
from app.services.external import engine as external_engine
from app.unified import copies, service as unified
from app.unified.models import UnifiedDocument
from tests.conftest import login

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.10"
PREV = "1.73.9"
BUMPED_BLOCKS = (
    "Сканирование чеков",
    "Проверка чеков (ФНС и источники)",
    "Чек-Пул",
    "Настройки",
    "Обновления",
    "Единый реестр чеков",
    "Контрагенты",
)


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _start_counting_fns_server():
    """Локальный SOAP-сервер ФНС (API 2.0.9) со счётчиками по типам запросов."""
    stats = {
        "lock": threading.Lock(),
        "total": 0,
        "auth": 0,
        "send": 0,
        "poll": 0,
        "poll_batch": 0,
        "send_bodies": [],
        "messages": {},
        "seq": 0,
        "custom_ticket": None,
        "processing_ticks": {},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002
            return

        def _reply(self, xml_body: str, status: int = 200) -> None:
            payload = (
                '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
                f"<soap:Body>{xml_body}</soap:Body></soap:Envelope>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            action = (self.headers.get("SOAPAction") or "").strip('"')

            with stats["lock"]:
                stats["total"] += 1
                if self.path == fns.AUTH_PATH:
                    stats["auth"] += 1
                    self._reply(
                        '<ns2:GetMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
                        'inplat/servin/OpenApiMessageConsumerService/types/1.0">'
                        "<ns2:Message><tns:AuthResponse xmlns:tns="
                        '"urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0">'
                        "<tns:Result><tns:Token>TEMP-SESSION-TOKEN-40M</tns:Token>"
                        "<tns:ExpireTime>2099-01-01T00:00:00Z</tns:ExpireTime>"
                        "</tns:Result></tns:AuthResponse></ns2:Message>"
                        "</ns2:GetMessageResponse>"
                    )
                    return

                if "SendMessageRequest" in action:
                    stats["send"] += 1
                    stats["send_bodies"].append(body)
                    stats["seq"] += 1
                    msg_id = f"MSG-{stats['seq']}"
                    fn_val = fns.xml_text(body, "Fn") or "7381440700130934"
                    fd_val = fns.xml_text(body, "FiscalDocumentId") or "1"
                    fp_val = fns.xml_text(body, "FiscalSign") or "1"
                    sum_val = int(fns.xml_text(body, "Sum") or "10000")
                    dt_val = fns.xml_text(body, "Date") or "2026-11-27T12:00:00"
                    op_val = int(fns.xml_text(body, "TypeOperation") or "1")
                    if isinstance(stats.get("custom_ticket"), dict):
                        stats["messages"][msg_id] = stats["custom_ticket"]
                    else:
                        stats["messages"][msg_id] = {
                            "id": f"ticket-{msg_id}",
                            "kind": "kkt",
                            "ofdId": "ofd-test",
                            "receipt": {
                                "totalSum": sum_val,
                                "dateTime": dt_val,
                                "operationType": op_val,
                                "taxationType": 1,
                                "user": "ООО «Проверка Квоты»",
                                "userInn": "7701234567",
                                "retailPlaceAddress": "г. Москва, ул. Проверочная, д. 10",
                                "operator": "Кассир Тестова",
                                "cashTotalSum": 0,
                                "ecashTotalSum": sum_val,
                                "fiscalDriveNumber": fn_val,
                                "fiscalDocumentNumber": int(fd_val) if fd_val.isdigit() else fd_val,
                                "fiscalSign": int(fp_val) if fp_val.isdigit() else fp_val,
                                "buyerPhoneOrAddress": "+79991112233",
                                "email": "secret-buyer@example.ru",
                                "items": [
                                    {
                                        "name": "Бумага офисная А4",
                                        "price": sum_val,
                                        "quantity": 1,
                                        "sum": sum_val,
                                        "nds": "20",
                                        "ndsSum": int(round(sum_val * 20 / 120)),
                                        "productType": 1,
                                        "paymentType": 4,
                                        "itemsQuantityMeasure": 0,
                                    }
                                ],
                            },
                        }
                    self._reply(
                        '<ns2:SendMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
                        'inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">'
                        f"<ns2:MessageId>{msg_id}</ns2:MessageId>"
                        "</ns2:SendMessageResponse>"
                    )
                    return

                if "GetMessagesRequest" in action:
                    import re as _re
                    stats["poll"] += 1
                    stats["poll_batch"] += 1
                    mids = _re.findall(r"MessageId>\s*(MSG-\d+)\s*<", body)
                    msg_nodes = []
                    for mid in mids:
                        ticks_left = stats["processing_ticks"].get(mid, 0)
                        if ticks_left > 0:
                            stats["processing_ticks"][mid] = ticks_left - 1
                            msg_nodes.append(
                                f"<ns2:Message><ns2:MessageId>{mid}</ns2:MessageId>"
                                "<ns2:ProcessingStatus>PROCESSING</ns2:ProcessingStatus>"
                                "</ns2:Message>"
                            )
                            continue
                        t_dict = stats["messages"].get(mid, {})
                        t_esc = (
                            json.dumps(t_dict, ensure_ascii=False)
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                        )
                        msg_nodes.append(
                            f"<ns2:Message><ns2:MessageId>{mid}</ns2:MessageId>"
                            "<ns2:ProcessingStatus>COMPLETED</ns2:ProcessingStatus>"
                            "<ns2:Result><tns:GetTicketResponse xmlns:tns="
                            '"urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0">'
                            "<tns:Result><tns:Code>200</tns:Code>"
                            f"<tns:Ticket>{t_esc}</tns:Ticket>"
                            "</tns:Result></tns:GetTicketResponse></ns2:Result>"
                            "</ns2:Message>"
                        )
                    self._reply(
                        '<ns2:GetMessagesResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
                        'inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">'
                        f"<ns2:Messages>{''.join(msg_nodes)}</ns2:Messages>"
                        "</ns2:GetMessagesResponse>"
                    )
                    return

                stats["poll"] += 1
                msg_id = fns.xml_text(body, "MessageId") or ""
                ticks_left = stats["processing_ticks"].get(msg_id, 0)
                if ticks_left > 0:
                    stats["processing_ticks"][msg_id] = ticks_left - 1
                    self._reply(
                        '<ns2:GetMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
                        'inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">'
                        "<ns2:ProcessingStatus>PROCESSING</ns2:ProcessingStatus>"
                        "</ns2:GetMessageResponse>"
                    )
                    return
                ticket_dict = stats["messages"].get(msg_id, {})
                ticket_esc = (
                    json.dumps(ticket_dict, ensure_ascii=False)
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                self._reply(
                    '<ns2:GetMessageResponse xmlns:ns2="urn://x-artefacts-gnivc-ru/'
                    'inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">'
                    "<ns2:ProcessingStatus>COMPLETED</ns2:ProcessingStatus>"
                    "<ns2:Message><tns:GetTicketResponse xmlns:tns="
                    '"urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0">'
                    f"<tns:Result><tns:Code>200</tns:Code><tns:Ticket>{ticket_esc}"
                    "</tns:Ticket></tns:Result></tns:GetTicketResponse></ns2:Message>"
                    "</ns2:GetMessageResponse>"
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url, stats


def _wait_receipt_settled(client: TestClient, hdr: dict, rid: str,
                          timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
        if last.get("status") != "verifying" and last.get("fns_status") in (
            "valid", "invalid", "not_found", "pending"
        ):
            return last
        time.sleep(0.05)
    return last


class TestRelease17310:
    def test_version_and_static_sync(self):
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        assert updater._is_newer(cur, PREV) is True
        sw = read("app/static/sw.js")
        assert f'APP_VERSION: str = "{cur}"' in read("app/config.py")
        assert f"ymaster-check-v{cur}" in sw and sw.count(f"?v={cur}") == 5
        assert read("app/static/index.html").count(f"?v={cur}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={cur}") == 2
        assert f"/maxapp/maxapp.js?v={cur}" in read("app/static/maxapp/index.html")
        assert f'"version": "{cur}"' in read("app/static/manifest.webmanifest")
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/osm.js")
        assert f"server_monitor.js?v={cur}" in read("app/static/js/app.js")
        about = TestClient(app).get("/api/v1/about")
        assert about.status_code == 200 and about.json()["version"] == cur

    def test_blocks_and_knowledge_synced(self):
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for block in BUMPED_BLOCKS:
            assert _ver(reg[block]) >= _ver(VERSION), (block, reg[block])
        assert f"## [{VERSION}]" in read("CHANGELOG.md")
        assert f"v{VERSION}" in read("README.md")
        assert f"v{VERSION}" in read("docs/kb/README.md")
        assert f"## v{VERSION}" in read("docs/ui_journal.md")
        assert f"'{VERSION}':" in read("app/static/js/app.js")
        assert "find_cached_in_db" in read("docs/knowledge/fns_auth.md")
        rows = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert rows[-1]["Версия"] == settings.APP_VERSION


class TestFnsQuotaAndCallMath:
    """Точная арифметика запросов к ФНС: 3 (холодный токен) → 2 (тёплый) → 0 (в БД)."""

    def test_scan_spends_3_then_2_then_0_instead_of_6(self, client, monkeypatch):
        srv, base_url, stats = _start_counting_fns_server()
        try:
            import app.config as cfg
            monkeypatch.setattr(cfg.settings, "FNS_API_BASE", base_url)
            monkeypatch.setattr(fns, "POLL_DELAYS", (0.01, 0.01))
            monkeypatch.setattr(external_engine, "_polite_wait", lambda _p: None)
            fns.reset_shared_clients()

            hdr = login(client, "admin", "admin123")
            client.put(
                "/api/v1/settings/fns",
                json={"master_token": "MASTER-QUOTA-17310", "client_app_id": "YmasterAPI"},
                headers=hdr,
            )
            quota_before = fns_quota.used_today()

            # 1) Первый уникальный чек (холодный токен): 1 Auth + 1 Send + 1 Poll = 3 (НЕ 6!)
            qr1 = "t=20261127T1015&s=1450.00&fn=7381440700991731&i=91001&fp=3910011731&n=1"
            r1 = client.post("/api/v1/receipts/scan", json={"qr_data": qr1}, headers=hdr)
            assert r1.status_code == 200, r1.text
            rid1 = r1.json()["receipt"]["id"]
            rec1 = _wait_receipt_settled(client, hdr, rid1)

            assert rec1["fns_status"] == "valid"
            assert rec1["status"] == "verified"
            assert rec1["full_data"] is True
            assert rec1["merchant_inn"] == "7701234567"
            assert rec1["items_count"] == 1
            assert stats["auth"] == 1
            assert stats["send"] == 1
            assert stats["poll"] == 1
            assert stats["total"] == 3, f"ожидалось 3 запроса на холодный токен, ушло {stats['total']}"
            assert fns_quota.used_today() - quota_before == 3

            # 2) Второй уникальный чек (тёплый 40-минутный токен): 0 Auth + 1 Send + 1 Poll = +2!
            qr2 = "t=20261127T1020&s=2300.00&fn=7381440700991731&i=91002&fp=3910021731&n=1"
            r2 = client.post("/api/v1/receipts/scan", json={"qr_data": qr2}, headers=hdr)
            assert r2.status_code == 200, r2.text
            rid2 = r2.json()["receipt"]["id"]
            rec2 = _wait_receipt_settled(client, hdr, rid2)

            assert rec2["fns_status"] == "valid" and rec2["full_data"] is True
            assert stats["auth"] == 1, "40-минутный токен AuthService должен переиспользоваться"
            assert stats["send"] == 2
            assert stats["poll"] == 2
            assert stats["total"] == 5, f"ожидалось 3+2=5 запросов за 2 чека, ушло {stats['total']}"
            assert fns_quota.used_today() - quota_before == 5

            # 3) Повторный скан, повторная кнопка «Проверить» и «Получить данные» → +0 запросов!
            dup = client.post("/api/v1/receipts/scan", json={"qr_data": qr1}, headers=hdr)
            assert dup.status_code == 200 and dup.json()["duplicate"] is True
            v_again = client.post(f"/api/v1/receipts/{rid1}/verify", json={}, headers=hdr)
            assert v_again.status_code == 200
            f_again = client.post(f"/api/v1/receipts/{rid1}/fetch-details", headers=hdr)
            assert f_again.status_code == 200
            time.sleep(0.15)

            assert stats["total"] == 5, "повторные операции по известному чеку не должны ходить в ФНС"
            assert fns_quota.used_today() - quota_before == 5
        finally:
            srv.shutdown()
            fns.reset_shared_clients()


class TestLocalDbPrecheckAndSafety:
    """Предварительный поиск в локальной БД, перескан после удаления и очистка ПДн."""

    def test_delete_and_rescan_hydrates_from_local_db_with_zero_fns_calls(
        self, client, monkeypatch
    ):
        srv, base_url, stats = _start_counting_fns_server()
        try:
            import app.config as cfg
            monkeypatch.setattr(cfg.settings, "FNS_API_BASE", base_url)
            monkeypatch.setattr(fns, "POLL_DELAYS", (0.01, 0.01))
            fns.reset_shared_clients()

            hdr = login(client, "admin", "admin123")
            client.put(
                "/api/v1/settings/fns",
                json={"master_token": "MASTER-RESCAN-17310", "client_app_id": "YmasterAPI"},
                headers=hdr,
            )

            # Первичный скан чека с временем без секунд в QR (в ответе ФНС будут секунды :42)
            qr = "t=20261127T1130&s=980.00&fn=7381440700991732&i=92001&fp=3920011732&n=1"
            r = client.post("/api/v1/receipts/scan", json={"qr_data": qr}, headers=hdr)
            rid = r.json()["receipt"]["id"]
            rec = _wait_receipt_settled(client, hdr, rid)
            assert rec["fns_status"] == "valid" and rec["items_count"] == 1
            calls_after_first = stats["total"]
            assert calls_after_first == 3

            # Уточним секунды в единой записи (как возвращает реальная касса ФНС: 11:30:42)
            with SessionLocal() as db:
                doc = unified.document_for_source(db, unified.REGISTRY, rid)
                assert doc is not None
                doc_id = doc.id
                r_row = db.get(Receipt, rid)
                r_row.receipt_date = datetime(2026, 11, 27, 11, 30, 42)
                unified.sync_registry(db, r_row)
                db.commit()

            # Удаляем чек из реестра (единая запись переходит в deleted_at IS NOT NULL)
            d = client.delete(f"/api/v1/receipts/{rid}", headers=hdr)
            assert d.status_code == 200
            with SessionLocal() as db:
                assert db.get(UnifiedDocument, doc_id).deleted_at is not None

            # Повторно сканируем тот же QR (без секунд!) → 0 запросов к ФНС, мгновенно verified!
            r_rescan = client.post("/api/v1/receipts/scan", json={"qr_data": qr}, headers=hdr)
            assert r_rescan.status_code == 200, r_rescan.text
            rescan_rec = r_rescan.json()["receipt"]
            assert stats["total"] == calls_after_first, "перескан известного чека должен стоить 0 запросов к ФНС"
            assert rescan_rec["status"] == "verified"
            assert rescan_rec["fns_status"] == "valid"
            assert rescan_rec["full_data"] is True
            assert rescan_rec["merchant_inn"] == "7701234567"
            assert len(rescan_rec["items"]) == 1

            # Владелец в UnifiedOwnership восстановлен, расхождений копий нет, ПДн покупателя удалены
            with SessionLocal() as db:
                doc_after = db.get(UnifiedDocument, doc_id)
                assert doc_after.deleted_at is None
                assert doc_after.ownership is not None
                assert doc_after.ownership.company_id == rescan_rec["company_id"]
                assert copies.reconcile_copies(db)["errors"] == 0
                saved_raw = json.loads(db.get(Receipt, rescan_rec["id"]).raw_data or "{}")
                raw_str = json.dumps(saved_raw, ensure_ascii=False)
                assert "+79991112233" not in raw_str
                assert "secret-buyer@example.ru" not in raw_str
        finally:
            srv.shutdown()
            fns.reset_shared_clients()

    def test_pool_and_fns_log_cache_hits_and_mismatch_guard(self):
        with SessionLocal() as db:
            # 1) Попадание по Чек-Пулу (0 сетевых вызовов, даже без мастер-токена)
            pool = PoolReceipt(
                fn="7381440700991733",
                fd="93001",
                fp="3930011733",
                receipt_date=datetime(2026, 11, 27, 14, 0, 0),
                total_sum=550.0,
                operation=1,
                merchant_name="ООО «Пул-Кэш»",
                merchant_inn="7811223344",
                merchant_address="СПб, Лиговский пр., 5",
                cashier="Смирнов И.И.",
                status="verified",
                fns_confirmed=True,
                fns_checked_at=datetime(2026, 11, 27, 14, 5, 0),
            )
            db.add(pool)
            db.flush()
            db.add(PoolItem(
                receipt_id=pool.id,
                name="Кофе зерновой",
                quantity=1.0,
                price=550.0,
                total=550.0,
                vat_rate="20",
                vat_sum=91.67,
            ))
            # 2) Попадание по журналу fns_log (с ПДн покупателя внутри ответа)
            nid = (db.query(FnsLog.id).order_by(FnsLog.id.desc()).first() or [0])[0] + 1
            db.add(FnsLog(
                id=nid,
                receipt_id=None,
                request_data=json.dumps(
                    {"fn": "7381440700991734", "fd": "94001", "fp": "3940011734"},
                    ensure_ascii=False,
                ),
                response_data=json.dumps({
                    "provider": "fns",
                    "code": "200",
                    "ticket": {
                        "receipt": {
                            "totalSum": 77000,
                            "dateTime": "2026-11-27T15:10:00",
                            "operationType": 1,
                            "user": "ООО «Журнал-Кэш»",
                            "userInn": "7722334455",
                            "buyerPhoneOrAddress": "+79005554433",
                            "properties": [
                                {"propertyName": "Телефон покупателя", "propertyValue": "+79005554433"},
                                {"propertyName": "Номер заказа", "propertyValue": "ORD-77"},
                            ],
                            "items": [{"name": "Тетрадь", "price": 77000, "quantity": 1, "sum": 77000}],
                        }
                    },
                }, ensure_ascii=False),
                provider="fns",
                ok=True,
                duration_ms=120,
            ))
            db.commit()

            try:
                hit_pool = fns.find_cached_in_db(
                    db, "7381440700991733", "93001", "3930011733",
                    550.0, datetime(2026, 11, 27, 14, 0, 0), 1, require_items=True,
                )
                assert hit_pool is not None and hit_pool.status == "valid"
                assert hit_pool.raw["cache_source"] == "pool"

                # Несовпадающая сумма или операция НЕ должны ложно подтверждаться из БД
                mismatch_sum = fns.find_cached_in_db(
                    db, "7381440700991733", "93001", "3930011733",
                    9999.0, datetime(2026, 11, 27, 14, 0, 0), 1,
                )
                mismatch_op = fns.find_cached_in_db(
                    db, "7381440700991733", "93001", "3930011733",
                    550.0, datetime(2026, 11, 27, 14, 0, 0), 2,
                )
                assert mismatch_sum is None
                assert mismatch_op is None

                hit_log = fns.find_cached_in_db(
                    db, "7381440700991734", "94001", "3940011734",
                    770.0, datetime(2026, 11, 27, 15, 10, 0), 1, require_items=True,
                )
                assert hit_log is not None and hit_log.status == "valid"
                assert hit_log.raw["cache_source"] == "fns_log"
                cleaned_dump = json.dumps(hit_log.raw["ticket"], ensure_ascii=False)
                assert "+79005554433" not in cleaned_dump
                assert "ORD-77" in cleaned_dump
            finally:
                db.query(PoolItem).filter_by(receipt_id=pool.id).delete()
                db.query(PoolReceipt).filter_by(id=pool.id).delete()
                db.query(FnsLog).filter_by(id=nid).delete()
                db.commit()


class TestSingleflightRefundAndAutoVerifySetting:
    def test_refund_operation_2_and_disabled_auto_verify(self, client, monkeypatch):
        srv, base_url, stats = _start_counting_fns_server()
        try:
            import app.config as cfg
            monkeypatch.setattr(cfg.settings, "FNS_API_BASE", base_url)
            monkeypatch.setattr(fns, "POLL_DELAYS", (0.01, 0.01))
            fns.reset_shared_clients()

            hdr = login(client, "admin", "admin123")
            client.put(
                "/api/v1/settings/fns",
                json={"master_token": "MASTER-OP2-17310", "client_app_id": "YmasterAPI"},
                headers=hdr,
            )

            # 1) Выключаем auto_verify → при скане 0 запросов к ФНС
            client.put(
                "/api/v1/settings/app",
                json={"auto_verify": False, "advance_deadline_days": 10},
                headers=hdr,
            )
            qr_refund = "t=20261127T1610&s=640.00&fn=7381440700991735&i=95001&fp=3950011735&n=2"
            r = client.post("/api/v1/receipts/scan", json={"qr_data": qr_refund}, headers=hdr)
            assert r.status_code == 200 and r.json()["auto_verify"] is False
            rid = r.json()["receipt"]["id"]
            time.sleep(0.1)
            assert stats["total"] == 0, "при auto_verify=False скан не должен обращаться к ФНС"

            # 2) Нажимаем проверку вручную → в SOAP уходит <tns:TypeOperation>2</tns:TypeOperation>
            client.post(f"/api/v1/receipts/{rid}/verify", json={}, headers=hdr)
            rec = _wait_receipt_settled(client, hdr, rid)
            assert rec["fns_status"] == "valid"
            assert rec["operation"] == 2
            assert stats["send"] == 1
            assert "<tns:TypeOperation>2</tns:TypeOperation>" in stats["send_bodies"][0]
        finally:
            srv.shutdown()
            fns.reset_shared_clients()

    def test_concurrent_singleflight_prevents_parallel_duplicate_fns_calls(
        self, client, monkeypatch
    ):
        """Одновременный запуск verify + fetch-details по новому чеку делает только 1 запрос в ФНС."""
        srv, base_url, stats = _start_counting_fns_server()
        try:
            import app.config as cfg
            import app.routers.receipts as rr
            monkeypatch.setattr(cfg.settings, "FNS_API_BASE", base_url)
            monkeypatch.setattr(fns, "POLL_DELAYS", (0.04, 0.04))
            monkeypatch.setattr(external_engine, "_polite_wait", lambda _p: None)
            fns.reset_shared_clients()

            hdr = login(client, "admin", "admin123")
            client.put(
                "/api/v1/settings/fns",
                json={"master_token": "MASTER-SF-17310", "client_app_id": "YmasterAPI"},
                headers=hdr,
            )
            client.put(
                "/api/v1/settings/app",
                json={"auto_verify": False, "advance_deadline_days": 10},
                headers=hdr,
            )

            qr = "t=20261127T1715&s=1120.00&fn=7381440700991736&i=96001&fp=3960011736&n=1"
            r = client.post("/api/v1/receipts/scan", json={"qr_data": qr}, headers=hdr)
            rid = r.json()["receipt"]["id"]
            assert stats["total"] == 0

            barrier = threading.Barrier(2)

            def run_verify():
                barrier.wait(timeout=5)
                rr._verify_one_sync(rid)

            def run_fetch():
                barrier.wait(timeout=5)
                rr._run_external_fetch([rid])

            t1 = threading.Thread(target=run_verify)
            t2 = threading.Thread(target=run_fetch)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert stats["send"] == 1, f"singleflight должен пропустить только 1 SendMessage, ушло {stats['send']}"
            assert stats["total"] == 3, f"ожидалось ровно 3 запроса (1 Auth + 1 Send + 1 Poll), ушло {stats['total']}"
            rec = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
            assert rec["fns_status"] == "valid"
            assert rec["full_data"] is True
        finally:
            srv.shutdown()
            fns.reset_shared_clients()


class TestFnsProtocolFloorAndMaxPersistence:
    """Глубокая проверка протокола API ФНС 2.0.9 (минимум запросов + 100 % данных ФФД)."""

    def test_preflight_validation_and_code_classifiers_zero_network_calls(
        self, client, monkeypatch
    ):
        """Предполётный фильтр отсекает заведомо битые реквизиты за 0 сетевых вызовов."""
        srv, base_url, stats = _start_counting_fns_server()
        try:
            cli = fns.FnsClient(api_base=base_url, master_token="MASTER-PREFLIGHT")
            now = datetime(2026, 11, 27, 12, 0, 0)

            r_bad_digits = cli.get_ticket("7381ABC", "10", "20", 100.0, now, 1)
            assert r_bad_digits.status == "invalid" and r_bad_digits.raw.get("preflight")
            r_zero_sum = cli.get_ticket("7381440700999901", "10", "20", 0.0, now, 1)
            assert r_zero_sum.status == "invalid" and r_zero_sum.raw.get("preflight")
            r_bad_op = cli.get_ticket("7381440700999901", "10", "20", 100.0, now, 9)
            assert r_bad_op.status == "invalid" and r_bad_op.raw.get("preflight")
            r_future = cli.get_ticket(
                "7381440700999901", "10", "20", 100.0,
                datetime(2099, 1, 1, 0, 0, 0), 1,
            )
            assert r_future.status == "invalid" and r_future.raw.get("preflight")
            assert stats["total"] == 0, "ни один битый запрос не должен уходить в сеть ФНС"

            # Коды ответа KktTicketService и классификация ошибок API 2.0.9
            assert fns.classify_ticket_code("200") == "valid"
            for nf in ("404", "204", "454"):
                assert fns.classify_ticket_code(nf) == "not_found"
            for inv in ("400", "406", "453"):
                assert fns.classify_ticket_code(inv) == "invalid"
            for tmp in ("529", "532"):
                assert fns.classify_ticket_code(tmp) == "unknown"

            assert fns.classify_error("RateLimitingFault: Слишком много запросов", 429)[0] == "rate_limit"
            assert fns.classify_error("AuthenticationFault: Ошибка аутентификации", 500)[0] == "token"
            assert fns.classify_error("AppRequestLimitException", 500)[0] == "quota"

            # Резерв квоты для интерактивных пользователей и синхронизация исчерпания
            monkeypatch.setattr(fns_quota, "limit_value", lambda: 1000)
            fns_quota.reset_today()
            assert fns_quota.interactive_reserve() == 50
            assert fns_quota.background_budget() == 950
            fns_quota.mark_exhausted_today()
            assert fns_quota.remaining() == 0
            assert fns_quota.background_budget() == 0
            fns_quota.reset_today()
        finally:
            cli.close()
            srv.shutdown()

    def test_batch_get_messages_request_and_pending_message_id_recovery(
        self, client, monkeypatch
    ):
        """GetMessagesRequest опрашивает пачку за 1 вызов (1 + 1/K на чек), а MessageId не теряется при таймауте."""
        srv, base_url, stats = _start_counting_fns_server()
        cli = fns.FnsClient(api_base=base_url, master_token="MASTER-BATCH-17310")
        try:
            monkeypatch.setattr(fns, "POLL_DELAYS", (0.01, 0.01))
            fns_quota.reset_today()

            # Пачка №1 (холодный токен): 5 новых чеков, включая расход n=3 и возврат расхода n=4
            batch1 = [
                {
                    "fn": "7381440700880001",
                    "fd": str(8000 + i),
                    "fp": str(3800000000 + i),
                    "total_sum": 150.0 + i * 10,
                    "receipt_date": datetime(2026, 11, 27, 10, i, 0),
                    "operation": (i % 4) + 1,
                }
                for i in range(5)
            ]
            res1 = cli.get_tickets_batch(batch1)
            assert all(r.status == "valid" for r in res1)
            assert stats["auth"] == 1
            assert stats["send"] == 5
            assert stats["poll_batch"] == 1
            assert stats["total"] == 7, f"ожидалось 1 Auth + 5 Send + 1 BatchPoll = 7, было {stats['total']}"
            assert any("<tns:TypeOperation>3</tns:TypeOperation>" in b for b in stats["send_bodies"])
            assert any("<tns:TypeOperation>4</tns:TypeOperation>" in b for b in stats["send_bodies"])

            # Пачка №2 (тёплый 60-мин токен по ExpireTime): 5 новых чеков = ровно 6 запросов (1.2/чек вместо 10)
            batch2 = [
                {
                    "fn": "7381440700880002",
                    "fd": str(9000 + i),
                    "fp": str(3900000000 + i),
                    "total_sum": 250.0 + i * 10,
                    "receipt_date": datetime(2026, 11, 27, 11, i, 0),
                    "operation": 1,
                }
                for i in range(5)
            ]
            res2 = cli.get_tickets_batch(batch2)
            assert all(r.status == "valid" for r in res2)
            assert stats["auth"] == 1, "сессионный токен не должен запрашиваться повторно"
            assert stats["send"] == 10
            assert stats["poll_batch"] == 2
            assert stats["total"] == 13, "вторая пачка из 5 чеков должна стоить ровно 6 запросов"

            # Восстановление оплаченного MessageId после таймаута (0 новых SendMessageRequest!)
            next_msg_id = f"MSG-{stats['seq'] + 1}"
            stats["processing_ticks"][next_msg_id] = 2  # оба POLL_DELAYS вернут PROCESSING → timeout
            slow_dt = datetime(2026, 11, 27, 12, 30, 0)
            r_timeout = cli.get_ticket(
                "7381440700880099", "9901", "3990100099", 500.0, slow_dt, 1
            )
            assert r_timeout.status == "unknown"
            assert r_timeout.raw.get("pending_message_id") == next_msg_id
            sends_before_resume = stats["send"]

            # Повторный вызов доопрашивает УЖЕ оплаченный MessageId за 1 запрос вместо 2!
            total_before_resume = stats["total"]
            r_resumed = cli.get_ticket(
                "7381440700880099", "9901", "3990100099", 500.0, slow_dt, 1
            )
            assert r_resumed.status == "valid"
            assert stats["send"] == sends_before_resume, "повторный SendMessageRequest не должен отправляться!"
            assert stats["total"] - total_before_resume == 1, "доопрос готового MessageId должен стоить 1 запрос!"
        finally:
            cli.close()
            srv.shutdown()
            fns.reset_shared_clients()

    def test_invalid_fingerprint_cache_and_fn_offline_merchant_hint_on_quota(self, client):
        """Кэш invalid сбрасывается при правке суммы, а при quota ФН-справочник даёт продавца за 0 запросов."""
        with SessionLocal() as db:
            known_fn = "7381440700776655"
            dt_val = datetime(2026, 11, 27, 13, 15, 0)
            fp_orig = fns.request_fingerprint(known_fn, "7001", "3700100001", 999.0, dt_val, 1)

            rec_inv = Receipt(
                qr_data=f"t=20261127T1315&s=999.00&fn={known_fn}&i=7001&fp=3700100001&n=1",
                fn=known_fn,
                fd="7001",
                fp="3700100001",
                receipt_date=dt_val,
                total_sum=999.0,
                operation=1,
                status="failed",
                fns_status="invalid",
                fns_message="Сумма не совпадает",
                fns_checked_at=datetime(2026, 11, 27, 13, 20, 0),
                raw_data=json.dumps({"fns": {"provider": "fns", "checked_fingerprint": fp_orig}}),
            )
            rec_valid_same_fn = Receipt(
                qr_data=f"t=20261127T1000&s=420.00&fn={known_fn}&i=6999&fp=3699900001&n=1",
                fn=known_fn,
                fd="6999",
                fp="3699900001",
                receipt_date=datetime(2026, 11, 27, 10, 0, 0),
                total_sum=420.0,
                operation=1,
                status="verified",
                fns_status="valid",
                fns_checked_at=datetime(2026, 11, 27, 10, 5, 0),
                merchant_name="ООО «Касса Известного ФН»",
                merchant_inn="7810998877",
                merchant_address="191025, Санкт-Петербург, ул. Марата, д. 5",
                region_code="78",
                region_name="Санкт-Петербург",
                region_city="Санкт-Петербург",
            )
            rec_pending_quota = Receipt(
                qr_data=f"t=20261127T1400&s=850.00&fn={known_fn}&i=7002&fp=3700200002&n=1",
                fn=known_fn,
                fd="7002",
                fp="3700200002",
                receipt_date=datetime(2026, 11, 27, 14, 0, 0),
                total_sum=850.0,
                operation=1,
                status="new",
                fns_status="pending",
            )
            db.add_all([rec_inv, rec_valid_same_fn, rec_pending_quota])
            db.commit()
            try:
                # 1) Те же реквизиты → мгновенный invalid из локальной БД (0 запросов)
                hit_same = fns.find_cached_in_db(db, known_fn, "7001", "3700100001", 999.0, dt_val, 1)
                assert hit_same is not None and hit_same.status == "invalid"

                # 2) Бухгалтер исправил опечатку в сумме (999.00 → 1099.00) → кэш пропускает на проверку!
                hit_edited = fns.find_cached_in_db(db, known_fn, "7001", "3700100001", 1099.0, dt_val, 1)
                assert hit_edited is None

                # 3) При исчерпанном лимите ФНС новый чек той же кассы получает продавца оффлайн
                assert fns.apply_fn_merchant_hint(db, rec_pending_quota) is True
                assert rec_pending_quota.merchant_name == "ООО «Касса Известного ФН»"
                assert rec_pending_quota.merchant_inn == "7810998877"
                assert rec_pending_quota.region_code == "78"
                assert rec_pending_quota.details_source == "fn_registry_hint"
                assert rec_pending_quota.fns_status == "pending", "статус проверки ФНС подменять нельзя!"
            finally:
                for r_del in (rec_inv, rec_valid_same_fn, rec_pending_quota):
                    db.delete(r_del)
                db.commit()

    def test_full_ffd_12_ticket_persisted_across_all_db_tables_and_counterparties(
        self, client, monkeypatch
    ):
        """100 % полей ответа ФНС (ФФД 1.2, НДС 22 %, маркировка, поставщик, ОФД, КПП) сохраняются во все таблицы БД."""
        srv, base_url, stats = _start_counting_fns_server()
        rich_fn = "7381440700665544"
        rich_fd = "55001"
        rich_fp = "3550016655"
        # Генерируем богатый чек ФФД 1.2 (> 5 КБ JSON, чтобы проверить отсутствие обрезки 4 КБ в fns_log!)
        extra_items = [
            {
                "name": f"Кабель силовой ВВГнг(А)-LS 3х2.5 бухта №{idx}",
                "price": 12200,
                "quantity": 1,
                "sum": 12200,
                "nds": 11,  # ФФД 2026: код 11 = НДС 22 %
                "ndsSum": 2200,
                "productType": 2,
                "paymentType": 4,
                "itemsQuantityMeasure": 0,
                "productCodeNew": {
                    "gs1m": {
                        "gtin": "04607001234567",
                        "sernum": f"SN2026-{idx:03d}",
                        "productIdType": 6,
                        "rawProductCode": f"010460700123456721SN2026-{idx:03d}",
                    }
                },
                "providerInn": "7733445566",
                "supplierInfo": {"name": "АО «Кабельный Завод Поставщик»", "phones": ["+74951112233"]},
                "paymentAgentByProductType": 64,
                "countryOfOriginCode": "643",
                "customsDeclaration": "10131010/011126/0001234",
                "excise": 1500,
                "fractionalQuantity": {"numerator": 1, "denominator": 2},
                "propertiesItem": [{"propertyName": "Партия", "propertyValue": f"LOT-{idx}"}],
                "itemsIndustryDetails": [
                    {
                        "idFoiv": "020",
                        "foundationDocDateTime": "28.02.2024",
                        "foundationDocNumber": "1944",
                        "industryPropValue": f"UUID=7788-{idx:03d}&Time=1732700000",
                    }
                ],
            }
            for idx in range(1, 16)
        ]
        total_kopeks = 12200 * len(extra_items)
        total_rub = round(total_kopeks / 100.0, 2)
        stats["custom_ticket"] = {
            "id": "9988776655443322110",
            "kind": "kkt",
            "ofdId": "ofd-platforma",
            "receipt": {
                "totalSum": total_kopeks,
                "dateTime": "2026-11-27T18:45:39",
                "operationType": 1,
                "taxationType": 0,
                "user": "ООО «СтройМаркет ФФД-1.2»",
                "userInn": "7708991122",
                "userKpp": "770801001",
                "retailPlace": "Гипермаркет Север",
                "retailPlaceAddress": "125040, г. Москва, Ленинградский пр-т, д. 15",
                "sellerAddress": "ofd-noreply@stroymarket.example.ru",
                "senderAddress": "check@ofd.example.ru",
                "fnsSite": "www.nalog.gov.ru",
                "operator": "Старший кассир Петрова Е.В.",
                "operatorInn": "770812345678",
                "buyer": "ООО «Заказчик СМР»",
                "buyerInn": "7714556677",
                "kktRegId": "0005556667778889",
                "numberKkt": "001099008877",
                "fiscalDriveNumber": rich_fn,
                "fiscalDocumentNumber": int(rich_fd),
                "fiscalSign": int(rich_fp),
                "shiftNumber": 142,
                "requestNumber": 87,
                "fiscalDocumentFormatVer": "4",
                "receiptCode": 3,
                "machineNumber": "AUTO-KKT-07",
                "internetSign": 1,
                "ofdInn": "9715260691",
                "ofdName": "ООО «Эвотор ОФД»",
                "cashTotalSum": 0,
                "ecashTotalSum": total_kopeks,
                "prepaidSum": 10000,
                "nds22": 15 * 2200,
                "nds22122": 2200,
                "properties": [{"key": "Заказ ИМ", "value": "IM-2026-909"}],
                "propertiesUser": [{"propertyName": "Договор поставки", "propertyValue": "ДП-44/26"}],
                "propertiesData": "WS-TOKEN-778899",
                "items": extra_items,
            },
        }
        try:
            import app.config as cfg
            monkeypatch.setattr(cfg.settings, "FNS_API_BASE", base_url)
            monkeypatch.setattr(fns, "POLL_DELAYS", (0.01, 0.01))
            monkeypatch.setattr(external_engine, "_polite_wait", lambda _p: None)
            fns.reset_shared_clients()
            fns_quota.reset_today()

            hdr = login(client, "admin", "admin123")
            client.put(
                "/api/v1/settings/fns",
                json={"master_token": "MASTER-RICH-17310", "client_app_id": "YmasterAPI"},
                headers=hdr,
            )
            client.put(
                "/api/v1/settings/app",
                json={"auto_verify": True, "advance_deadline_days": 10},
                headers=hdr,
            )

            qr = f"t=20261127T1845&s={total_rub:.2f}&fn={rich_fn}&i={rich_fd}&fp={rich_fp}&n=1"
            scan_resp = client.post("/api/v1/receipts/scan", json={"qr_data": qr}, headers=hdr)
            assert scan_resp.status_code == 200, scan_resp.text
            rid = scan_resp.json()["receipt"]["id"]
            settled = _wait_receipt_settled(client, hdr, rid)
            assert settled["fns_status"] == "valid"
            assert len(settled["items"]) == 15

            # 1) Проверяем карточку чека: НДС 22 % расшифрован, все реквизиты ФФД 1.2 на месте
            card = client.get(f"/api/v1/receipts/{rid}/card", headers=hdr).json()
            doc = card["document"]
            assert doc["seller"]["kpp"] == "770801001"
            assert doc["seller"]["email"] == "ofd-noreply@stroymarket.example.ru"
            assert doc["kkt"]["ofd_inn"] == "9715260691"
            assert doc["kkt"]["machine_number"] == "AUTO-KKT-07"
            assert doc["kkt"]["internet_sign"] == 1
            assert doc["buyer"]["inn"] == "7714556677"
            assert doc["items"][0]["nds_name"] == "22 %"
            assert doc["items"][0]["marking"][0]["gtin"] == "04607001234567"
            assert doc["items"][0]["supplier"]["inn"] == "7733445566"
            assert doc["items"][0]["supplier"]["name"] == "АО «Кабельный Завод Поставщик»"
            assert doc["items"][0]["fractional_quantity"] == {"numerator": 1, "denominator": 2}
            prop_names = {p["name"] for p in doc["properties"]}
            assert {"Заказ ИМ", "Договор поставки", "Доп. реквизит чека (1192)"} <= prop_names

            # 2) Проверяем БД: Receipt.ext_json, UnifiedItem.extra_json и необрезанный FnsLog (> 5 КБ JSON!)
            with SessionLocal() as db:
                rec_db = db.get(Receipt, rid)
                ext = json.loads(rec_db.ext_json or "{}")
                assert ext["ticket_id"] == "9988776655443322110"
                assert ext["ofd_id"] == "ofd-platforma"
                assert ext["ofd_inn"] == "9715260691"
                assert ext["seller_kpp"] == "770801001"
                assert ext["cashier_inn"] == "770812345678"
                assert ext["buyer_inn"] == "7714556677"
                assert ext["fns_date_time"] == "2026-11-27T18:45:39"

                ukey = unified.identity_key(rich_fn, rich_fd, rich_fp)
                udoc = db.query(UnifiedDocument).filter_by(identity_key=ukey).first()
                assert udoc is not None and len(udoc.items) == 15
                uitem0_extra = json.loads(udoc.items[0].extra_json or "{}")
                assert uitem0_extra["vat_canonical"] == "22"
                assert uitem0_extra["supplier"]["inn"] == "7733445566"
                assert uitem0_extra["marking"][0]["serial"] == "SN2026-001"

                flog = db.query(FnsLog).filter_by(receipt_id=rid).order_by(FnsLog.id.desc()).first()
                assert flog is not None
                assert len(flog.response_data) > 5000, "длинный ответ ФНС больше не должен обрезаться на 4000 символах!"
                parsed_log = json.loads(flog.response_data)
                assert len(parsed_log["ticket"]["receipt"]["items"]) == 15

            # 3) Справочник «Контрагенты» видит и продавца чека с КПП, и поставщика позиции из ответа ФНС!
            cands = client.get("/api/v1/counterparties/candidates", headers=hdr).json()
            by_inn = {(c["inn"], c["source"]): c for c in cands}
            assert ("7708991122", "receipt") in by_inn
            assert by_inn[("7708991122", "receipt")]["kpp"] == "770801001"
            assert ("7733445566", "receipt") in by_inn
            assert "Кабельный Завод" in by_inn[("7733445566", "receipt")]["name"]
        finally:
            srv.shutdown()
            fns.reset_shared_clients()


class TestSystemdSelfUpdateAndPhaseProgress:
    """Исправление самообновления под ProtectSystem=strict и прогресса фаз в окне обновления."""

    def test_systemd_unit_allows_self_update_and_offapp_backups(self):
        unit = read("deploy/systemd/ymaster-check.service")
        assert "ProtectSystem=strict" in unit
        assert "ProtectHome=true" in unit
        assert "ReadWritePaths=/opt/ymaster-check -/var/backups/ymaster-check" in unit
        assert "ReadWritePaths=/opt/ymaster-check/data\n" not in unit
        assert "Environment=HOME=/opt/ymaster-check/data" in unit
        assert "--no-cache-dir" in updater._pip_cmd()

    def test_writable_check_preflight_and_apply_409_on_readonly_fs(
        self, client, tmp_path, monkeypatch
    ):
        import errno as _errno

        monkeypatch.setattr(updater, "APP_DIR", str(tmp_path))
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "ymaster_check.db").write_bytes(b"sqlite")
        (tmp_path / ".git").mkdir(exist_ok=True)

        # Штатный каталог доступен на запись
        ok_w, err_w = updater._writable_check()
        assert ok_w is True and err_w == ""

        # Эмулируем EROFS (Read-only file system от старого systemd ReadWritePaths)
        def fake_open_erofs(path, _flags, _mode=0o600):
            raise OSError(_errno.EROFS, "Read-only file system", path)

        monkeypatch.setattr(updater.os, "open", fake_open_erofs)
        monkeypatch.setattr(updater, "_restart_mode", lambda: (True, "sudoers"))
        monkeypatch.setattr(
            updater,
            "_remote_version",
            lambda _repo, _branch: {"version": "9.9.9", "checked_at": "2026-11-28T00:00:00Z"},
        )

        hdr = login(client, "admin", "admin123")
        pf = client.get("/api/v1/admin/update/preflight", headers=hdr).json()
        assert pf["writable_ok"] is False
        assert pf["ready"] is False
        assert "deploy.sh --update" in pf["writable_error"]

        resp = client.post("/api/v1/admin/update/apply", headers=hdr)
        assert resp.status_code == 409
        assert "deploy.sh --update" in resp.json()["detail"]

    def test_fetch_error_marks_failed_phase_without_opening_install_or_fake_rollback(
        self, tmp_path, monkeypatch
    ):
        """Регрессия скриншота 02:16:28: сбой на git fetch закрывает фазу с ✗ и не зажигает «Установка файлов»."""
        monkeypatch.setattr(updater, "APP_DIR", str(tmp_path))
        monkeypatch.setattr(updater, "_local_commit", lambda: "abc1234")
        monkeypatch.setattr(updater, "_writable_check", lambda: (True, ""))
        monkeypatch.setattr(updater, "_ensure_git_repo", lambda _repo, _branch: (True, "git-репозиторий в порядке"))
        monkeypatch.setattr("app.services.backups.create_backup", lambda _kind: "")
        monkeypatch.setattr(updater, "_append_history", lambda *a, **k: None)

        fetch_calls = {"n": 0}

        def fail_erofs_fetch(_branch):
            fetch_calls["n"] += 1
            return 1, "error: cannot open '.git/FETCH_HEAD': Read-only file system", 0

        reset_calls = {"n": 0}

        def track_git(args, _timeout=180):
            if args and args[0] == "reset":
                reset_calls["n"] += 1
            return 1, "Read-only file system"

        monkeypatch.setattr(updater, "_git_fetch_progress", fail_erofs_fetch)
        monkeypatch.setattr(updater, "_git", track_git)

        updater.job.prepare("update", to_version="1.73.10")
        updater._do_apply("1.73.10", "Rimlin-UNC/1c_chek", "main")
        st = updater.job.public()

        assert st["finished"] is True and st["success"] is False
        # При Read-only file system не делаем бесполезный второй fetch и не трогаем git reset
        assert fetch_calls["n"] == 1
        assert reset_calls["n"] == 0
        assert st["rolled_back"] is False
        assert "deploy.sh --update" in st["error"]
        # Ни одна фаза не осталась «active», а «install» («Установка файлов») вообще не открывалась!
        active_phases = [k for k, v in st["phases"].items() if v.get("status") == "active"]
        assert active_phases == [], f"остались зависшие active-фазы: {active_phases}"
        assert st["phases"]["fetch"]["status"] == "failed"
        assert "install" not in st["phases"]
        updater.job.reset()

    def test_ui_update_modal_has_writable_chip_and_no_active_spinner_when_finished(self):
        js = read("app/static/js/app.js")
        assert "chip('Запись файлов'" in js
        assert 'if (st.finished) return `<div class="form-error">✗ ${title}${dtxt}</div>`;' in js
        assert "обновление до v${esc(st.to_version || '')} не применено" in js
