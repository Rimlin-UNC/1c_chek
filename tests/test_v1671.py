# Ямастер Чек — тесты v1.67.1: исправления аудита безопасности.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# XFF только от доверенного прокси · безопасный 422 · кап QR ·
# пул соединений и прагмы SQLite · воркеры без длинных сессий.
import inspect
import time

from fastapi.testclient import TestClient


class TestTrustedProxy:
    """X-Forwarded-For принимается только от доверенного соседа."""

    def _req(self, host, xff):
        from app.security import real_client_ip

        class _C:
            pass

        class _R:
            client = type("C", (), {"host": host})()
            headers = {"x-forwarded-for": xff} if xff else {}

        return real_client_ip(_R())

    def test_nginx_append_mode_rightmost(self):
        """v1.68.0: правый край XFF — что видел наш прокси; подделка
        слева игнорируется (nginx в режиме $proxy_add_x_forwarded_for)."""
        assert self._req("127.0.0.1", "1.2.3.4, 203.0.113.7") == "203.0.113.7"
        assert self._req("127.0.0.1", "203.0.113.7, 10.0.0.1") == "10.0.0.1"

    def test_private_proxy_trusted(self):
        assert self._req("10.1.1.5", "198.51.100.9") == "198.51.100.9"

    def test_direct_peer_xff_ignored(self):
        assert self._req("93.184.216.34", "1.2.3.4") == "93.184.216.34"

    def test_no_client(self):
        assert self._req("", "") == "unknown"

    def test_rotation_does_not_bypass_login_guard(self, client):
        """Аудит-кейс: 5 неудач + ротация XFF → блок остаётся на месте."""
        uname = f"bruteforce{int(time.time())}"
        codes = []
        for i in range(8):
            r = client.post("/api/v1/auth/login",
                            json={"username": uname, "password": "неверно"},
                            headers={"X-Forwarded-For": f"10.9.{i}.9"})
            codes.append(r.status_code)
        assert 429 in codes or 423 in codes, codes


class TestSafeValidation:
    """Бинарные тела не роняют обработчик валидации (было: 500)."""

    def test_multipart_into_json_endpoint_422(self, client):
        r = client.post(
            "/api/v1/auth/login",
            files={"f": ("x.bin", b"\xff" * 64, "application/octet-stream")})
        assert r.status_code == 422, r.status_code

    def test_binary_json_body_422(self, client):
        r = client.post("/api/v1/auth/login", content=b"\xff\xfe\x00g",
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.status_code


class TestQrCaps:
    """Кап длины QR: парсер и гостевой эндпоинт."""

    def test_parse_qr_max_length(self):
        from app.services.qr import QRParseError, parse_qr
        try:
            parse_qr("a=b&" * 30000)
            raise AssertionError("ожидали QRParseError")
        except QRParseError as e:
            assert "4000" in str(e)

    def test_parse_qr_normal_still_works(self):
        from app.services.qr import parse_qr
        p = parse_qr("t=20261012T0900&s=500.00&fn=9000000000017000"
                     "&i=123&fp=456&n=1")
        assert p.fn == "9000000000017000"

    def test_pool_check_body_capped(self):
        from app.pool.router_public import CheckBody
        from pydantic import ValidationError
        try:
            CheckBody(qr_text="x" * 5000)
            raise AssertionError("ожидали ValidationError")
        except ValidationError:
            pass
        ok = CheckBody(qr_text="t=1&s=2")
        assert ok.qr_text == "t=1&s=2"


class TestDbResilience:
    """Пул соединений расширен, SQLite ждёт блокировку, а не падает."""

    def test_pool_settings(self):
        from app.database import engine
        assert engine.pool.size() >= 20
        assert getattr(engine.pool, "_max_overflow", 0) >= 20
        assert engine.pool._timeout <= 15   # v1.68.1: 15 с

    def test_sqlite_pragmas(self):
        from app.database import engine
        from app.config import settings
        if not settings.DATABASE_URL.startswith("sqlite"):
            return
        raw = engine.raw_connection()
        try:
            cur = raw.connection.cursor()
            cur.execute("PRAGMA busy_timeout")
            assert cur.fetchone()[0] >= 5000
            cur.execute("PRAGMA journal_mode")
            assert cur.fetchone()[0].lower() == "wal"
            cur.close()
        finally:
            raw.close()


class TestWorkersShortSessions:
    """Воркеры ботов не держат сессию БД во время long-poll (v1.67.1)."""

    def test_poll_once_signature_no_db(self):
        from app.services import max_bot as mx
        from app.services import telegram_bot as tg
        for mod in (tg, mx):
            sig = inspect.signature(mod._poll_once)
            assert all(a != "db" for a in sig.parameters), mod.__name__

    def test_fetch_verify_bounded(self):
        """Фоновые задачи внешних источников ограничены семафором 4."""
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert "_FETCH_SEM = threading.Semaphore(4)" in src
        assert "_VERIFY_SEM = threading.Semaphore(4)" in src
        assert "for _VERIFY_SEM.release" not in src  # релиз только в finally

    def test_secret_weak_detector(self):
        from app.security import secret_is_weak
        assert secret_is_weak("")
        assert secret_is_weak("ymaster-dev-secret-change-me-in-production")
        assert secret_is_weak("короткий")
        assert not secret_is_weak("x" * 48)


class TestVersion1671:
    """Версионный замок патч-релиза."""

    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.68.0: строгий замок версии переехал в test_v1680
        assert tuple(int(x) for x in ver.split(".")) >= (1, 67, 1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.67.1]") == 1
        assert ch.index("## [1.67.1]") < ch.index("## [1.67.0]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Роли и доступ"].split(".")) >= (1, 67, 1)
        assert reg["Сканирование чеков"] == "1.67.1"
        assert reg["Чек-Пул"] == "1.56.1"          # блок не менялся
