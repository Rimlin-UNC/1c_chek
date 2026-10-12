# Ямастер Чек — тесты v1.68.0: устойчивость (рекомендации аудита).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# Очередь автозагрузки · правый край XFF · мониторинг вебхуков ·
# статус SECRET_KEY · runbook восстановления.
import re
import time

from fastapi.testclient import TestClient


# ------------------------------------------------------------------ версии
class TestVersion1680:
    """Строгий версионный замок релиза v1.68.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.68.1: строгий замок версии переехал в test_v1681
        assert tuple(int(x) for x in ver.split(".")) >= (1, 68, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.67.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.68.0]") == 1
        assert ch.index("## [1.68.0]") < ch.index("## [1.67.1]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.68.0':") < js.index("'1.67.1':")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Проверка чеков (ФНС и источники)"] == "1.68.0"
        assert reg["Роли и доступ"] == "1.68.0"
        assert reg["Чек-Пул"] == "1.56.1"            # не менялся


# ------------------------------------------------------------------ очередь
class TestFetchQueue:
    """Очередь автозагрузки: дедуп, идемпотентный старт, демоны живут."""

    def test_source_wiring(self):
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert "_enqueue_fetch(receipt.id)" in src
        assert "import queue" in src
        assert "_FETCH_SEM = threading.Semaphore(4)" in src   # замок v1.67.1
        main = open("app/main.py", encoding="utf-8").read()
        assert "start_fetch_workers" in main

    def test_enqueue_dedup_and_drain(self):
        from app.routers import receipts as rc
        before = rc.fetch_queue_stats()["queued"]
        assert rc._enqueue_fetch("audit-no-such-1") is True
        assert rc._enqueue_fetch("audit-no-such-1") is False   # дедуп
        assert rc.fetch_queue_stats()["queued"] == before + 1
        # ручной слив без воркеров (в тестах очередь off — детерминизм)
        rid = rc._FETCHQ.get()
        with rc._FETCHQ_LOCK:
            rc._FETCHQ_IDS.discard(rid)
        rc._FETCHQ.task_done()
        assert rc._FETCHQ.qsize() == before
        assert "audit-no-such-1" not in rc._FETCHQ_IDS

    def test_start_workers_idempotent(self, monkeypatch):
        from app.routers import receipts as rc
        monkeypatch.setenv("YM_FETCHQ_OFF", "0")
        spawned = []

        class _FakeT:
            def __init__(self, target=None, daemon=None, name=None):
                pass
            def start(self):
                spawned.append(1)
            def is_alive(self):
                return True

        monkeypatch.setattr(rc.threading, "Thread", _FakeT)
        assert rc.start_fetch_workers(2) == 2
        assert rc.start_fetch_workers(2) == 0        # уже «живут»
        assert len(spawned) == 2

    def test_env_off_disables(self, monkeypatch):
        from app.routers import receipts as rc
        monkeypatch.setenv("YM_FETCHQ_OFF", "1")
        assert rc.start_fetch_workers(2) == 0


# ------------------------------------------------------------------ XFF
class TestRightmostXff:
    """Правый край XFF: подделка слева игнорируется, правый — наш прокси."""

    def _req(self, host, xff):
        from app.security import real_client_ip

        class _R:
            client = type("C", (), {"host": host})()
            headers = {"x-forwarded-for": xff} if xff else {}

        return real_client_ip(_R())

    def test_append_mode_spoof_ignored(self):
        assert self._req("127.0.0.1", "1.2.3.4, 203.0.113.7") == "203.0.113.7"

    def test_replace_mode_single(self):
        assert self._req("10.1.1.5", "198.51.100.9") == "198.51.100.9"

    def test_direct_peer_ignores_header(self):
        assert self._req("93.184.216.34", "1.2.3.4") == "93.184.216.34"

    def test_integration_direct_no_bypass(self, client):
        """Атака аудита: ротация XFF напрямую не обходит LoginGuard."""
        uname = f"rot{int(time.time())}"
        codes = [client.post(
            "/api/v1/auth/login",
            json={"username": uname, "password": "неверно"},
            headers={"X-Forwarded-For": f"10.9.{i}.9"}).status_code
            for i in range(8)]
        assert 429 in codes or 423 in codes, codes


# ------------------------------------------------------------------ система
class TestSystemIndicators:
    """Статус ключа и глубина очереди — в системной сводке админа."""

    def test_admin_system_fields(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        d = client.get("/api/v1/admin/system", headers=H).json()
        assert isinstance(d["secret_key_ok"], bool)
        assert d["secret_key_ok"] is False        # в тестах ключ дефолтный
        assert "queued" in d["fetch_queue"]


# ------------------------------------------------------------------ вебхуки
class TestWebhookMonitoring:
    """Застрявшие вебхуки видны: счётчики + строка в дайджесте."""

    def test_counts_and_digest_line(self):
        from app.database import SessionLocal
        from app.models import WebhookLog
        from app.services import webhooks as wh
        from app.services import max_bot as mx
        s = SessionLocal()
        try:
            row = WebhookLog(event_type="receipt_verified", url="http://x/",
                             payload="{}", attempt=wh.MAX_ATTEMPTS,
                             delivered_at=None)
            s.add(row)
            s.commit()
            rid = row.id
            s.expire_all()
            assert wh.stuck_count(s) >= 1
            text = mx.digest_text(s)
            assert "🪝" in text and "недоставлено" in text
        finally:
            s.rollback()
            row_q = s.query(WebhookLog).get(rid)
            if row_q:
                s.delete(row_q)
                s.commit()
            s.close()

    def test_worker_warn_wiring(self):
        src = open("app/services/webhooks.py", encoding="utf-8").read()
        assert "_warn_stuck_once_daily(db)" in src
        assert "stuck_count" in src


# ------------------------------------------------------------------ docs
class TestRunbook:
    """Квартальная тренировка восстановления: инструкция на месте."""

    def test_restore_drill_exists(self):
        txt = open("docs/restore_drill.md", encoding="utf-8").read()
        assert "восстановлени" in txt.lower()
        assert "systemctl" in txt and "integrity_check" in txt

    def test_audit_report_updated(self):
        txt = open("docs/audit_v1671.md", encoding="utf-8").read()
        assert "v1.68.0" in txt
