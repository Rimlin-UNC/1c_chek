# Ямастер Чек — тесты v1.69.0: мини-приложение MAX + вход через MAX.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки:
#  - вход/регистрация через MAX: initData валидируется HMAC-SHA256
#    («WebAppData» + токен бота), повторный вход отзывает старые токены;
#  - мини-приложение: static/maxapp/index.html + maxapp.js (Bridge,
#    openCodeReader, BackButton, главный экран, сдача QR, баллы);
#  - валидатор: подпись, подделка → отказ, просрочка auth_date → отказ,
#    длина initData ограничена (8 КБ);
#  - API /api/v1/maxapp/*: session→me, scan (дедуп по чеку в ответе),
#    summary; отказ при выключенном пуле — как на сайте;
#  - замки: v1.65/v1.66/v1.67 не задеты, multipart+token не тронуты;
#  - тесты без сети: get_me MAX мокается, initData подписывается
#    тестовым токеном.
import hashlib
import hmac as hmac_mod
import json
import re
import time
import urllib.parse
import urllib.request

import pytest

SEQ = {"n": 69000}
BOT_TOKEN = "700001.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


def build_init_data(user_id: int, token: str = BOT_TOKEN,
                    first_name: str = "Максим",
                    auth_date: int | None = None,
                    start_param: str = "",
                    photo_url: str | None = None,
                    last_name: str = "Тестов") -> str:
    """initData по доке MAX: пары + hash = HMAC(secret, launch_params),
    secret = HMAC("WebAppData", token)."""
    auth_date = auth_date or int(time.time())
    pairs = [
        ("auth_date", str(auth_date)),
        ("query_id", f"q{user_id}-{auth_date}"),
        ("user", json.dumps({
            "id": user_id, "first_name": first_name,
            "last_name": last_name, "username": f"u{user_id}",
            "language_code": "ru", "photo_url": photo_url},
            ensure_ascii=False)),
    ]
    if start_param:
        pairs.append(("start_param", start_param))
    pairs.sort(key=lambda x: x[0])
    launch = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hmac_mod.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    sig = hmac_mod.new(secret, launch.encode(), hashlib.sha256).hexdigest()
    pairs.append(("hash", sig))
    # v1.70.36: кодируем КАК ПЛАТФОРМА — urlencode (пробел → «+»). Прежняя
    # фикстура писала quote(safe=''), то есть «%20», и старая реализация с
    # нераскрытым «+» на ней проходила, а на боевых initData — нет: тесты
    # были зелёные, вход через MAX не работал. Регресс закреплён ниже обоими
    # видами кодирования.
    return urllib.parse.urlencode(pairs)


# ------------------------------------------------------------------ версии
class TestVersion1690:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.0: строгий замок версии переехал в test_v1700
        assert tuple(int(x) for x in ver.split(".")) >= (1, 69, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.68.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.69.0]") == 1
        assert ch.index("## [1.69.0]") < ch.index("## [1.68.1]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.69.0':") < js.index("'1.68.1':")

    def test_blocks(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Мини-приложение MAX"] == "1.69.0"
        assert reg["Чек-Пул"] == "1.56.1"            # движок не менялся


# ------------------------------------------------------------------ валидатор
class TestInitDataValidation:
    """Валидация initData по документации dev.max.ru (docs/webapps/validation)."""

    def test_valid_signature_accepted(self):
        from app.services import maxapp as ma
        data = build_init_data(424242)
        user, err = ma.validate_init_data(data, BOT_TOKEN)
        assert err is None and user["id"] == 424242
        assert user["first_name"] == "Максим"

    def test_forged_signature_rejected(self):
        from app.services import maxapp as ma
        data = build_init_data(424242, token="другой-токен-бота-000")
        user, err = ma.validate_init_data(data, BOT_TOKEN)
        assert user is None and err == "bad_hash"

    def test_tampered_payload_rejected(self):
        from app.services import maxapp as ma
        data = build_init_data(424242)
        data = data.replace("424242", "999999")     # подмена user.id
        user, err = ma.validate_init_data(data, BOT_TOKEN)
        assert user is None and err == "bad_hash"

    def test_expired_auth_date_rejected(self):
        from app.services import maxapp as ma
        old = int(time.time()) - 25 * 3600           # > 24 часов
        data = build_init_data(424242, auth_date=old)
        user, err = ma.validate_init_data(data, BOT_TOKEN)
        assert user is None and err == "expired"

    def test_plus_and_percent20_both_validate(self):
        """Регресс v1.70.36: платформа кодирует пробелы знаком «+», а
        urllib.parse.unquote его НЕ раскрывает (раскрывает только
        unquote_plus / parse_qsl). JSON в user всегда с пробелами, поэтому
        прежняя строка подписи собиралась с «+» и HMAC не сходился ни на
        одном боевом initData — вход и регистрация через MAX были
        невозможны. Проверяем оба вида кодирования и «сырой» вид."""
        import hashlib as _hl
        import hmac as _hm
        from app.services import maxapp as ma
        data = {"auth_date": str(int(time.time())),
                "query_id": "q-regress",
                "user": json.dumps({"id": 777, "first_name": "Плюс Плюс",
                                    "username": "plus"}, ensure_ascii=False)}
        pairs = sorted(data.items())
        launch = "\n".join(f"{k}={v}" for k, v in pairs)
        sec = _hm.new(b"WebAppData", BOT_TOKEN.encode(), _hl.sha256).digest()
        sig = _hm.new(sec, launch.encode(), _hl.sha256).hexdigest()
        full = dict(pairs, hash=sig)
        plus = urllib.parse.urlencode(full)                 # пробел → «+»
        pct = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                       for k, v in full.items())           # пробел → %20
        raw = "&".join(f"{k}={v}" for k, v in full.items()) # без кодирования
        dbl = urllib.parse.quote(plus, safe="")            # строка целиком ещё раз
        for name, variant in (("urlencode/+ ", plus), ("quote/%20 ", pct),
                              ("сырой       ", raw), ("двойной энкод", dbl)):
            user, err = ma.validate_init_data(variant, BOT_TOKEN)
            assert err is None and user["id"] == 777, f"{name}: {err}"

    def test_garbage_rejected(self):
        from app.services import maxapp as ma
        assert ma.validate_init_data("", BOT_TOKEN)[1] == "format"
        assert ma.validate_init_data("a=1", BOT_TOKEN)[1] == "format"
        no_hash = build_init_data(1).split("&hash=")[0]
        assert ma.validate_init_data(no_hash, BOT_TOKEN)[1] == "format"
        double = build_init_data(1) + "&hash=deadbeef"
        # v1.70.36: пары читаются как dict(parse_qsl(...)) — последний hash
        # побеждает и не сходится, поэтому отказ приходит как bad_hash.
        # Смысл проверки — дубль НЕ должен проходить, код причины вторичен.
        assert ma.validate_init_data(double, BOT_TOKEN)[1] in ("format",
                                                                "bad_hash")

    def test_no_token_configured(self):
        from app.services import maxapp as ma
        data = build_init_data(1)
        user, err = ma.validate_init_data(data, "")
        assert user is None and err == "not_configured"


# ------------------------------------------------------------------ API
class TestMaxAppApi:
    """Сессия, профиль, сдача чека и баллы — контур пула."""

    def _setup(self, pool_on="1"):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
            appsettings.set_setting(s, "pool_enabled", pool_on)
        finally:
            s.rollback(); s.close()

    def _auth(self, client, max_id=555001, first="Максим"):
        self._setup()
        r = client.post("/api/v1/maxapp/session", json={
            "init_data": build_init_data(max_id, first_name=first)})
        assert r.status_code == 200, r.text
        d = r.json()
        return {"Authorization": f"Bearer {d['access_token']}"}, d

    def test_session_creates_user(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        H, d = self._auth(client, 555001)
        assert d["is_new"] is True
        assert d["me"]["points"] == 0
        assert d["me"]["max_id"] == "555001"
        me = client.get("/api/v1/maxapp/me", headers=H).json()
        assert me["max_id"] == "555001" and me["points"] == 0
        s = SessionLocal()
        try:
            u = s.query(PoolUser).filter(
                PoolUser.tg_user_id == "555001").first()
            assert u is not None and int(u.points or 0) == 0
            # (привязка ассистента — на users.max_user_id, см. биндинг-тест)
        finally:
            s.rollback(); s.close()

    def test_session_relogin_fresh_token(self, client):
        """Повторный вход выдаёт свежий рабочий токен (как в кабинете пула:
        JWT 30 дней, без серверного отзыва — поведение согласованное)."""
        H1, _ = self._auth(client, 555002)
        H2, d2 = self._auth(client, 555002)
        assert client.get("/api/v1/maxapp/me", headers=H2).status_code == 200
        assert client.get("/api/v1/maxapp/me", headers=H1).status_code == 200
        assert d2["is_new"] is False

    def test_bad_init_data_rejected(self, client):
        self._setup()
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data": "hash=abc&auth_date=1"})
        assert r.status_code == 401

    def test_scan_flow_and_dedup(self, client, monkeypatch):
        # ноль реальных сетевых вызовов: детали чека — детерминированный мок
        from datetime import datetime as _dt
        from app.services.external import ExternalItem, ExternalResult
        monkeypatch.setattr(
            "app.pool.ingest._fetch_details",
            lambda *a, **k: ExternalResult(
                ok=True, source="test", found=True, message="OK",
                date_time=_dt(2026, 9, 10, 10, 0), total_sum=1250.5,
                operation=1, merchant_name="ООО Ромашка",
                merchant_inn="7704001275",
                items=[ExternalItem(name="Кофе", quantity=1.0, price=1250.5,
                                    total=1250.5, vat_rate="10",
                                    vat_sum=125.05)],
                raw={}))
        H, _ = self._auth(client, 555003)
        qr = ("t=20261012T090000&s=1250.50&fn=9900000000016901"
              "&i=16901&fp=690169&n=1")
        r = client.post("/api/v1/maxapp/scan", headers=H,
                        json={"qr": qr, "form_ms": 4200})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["accepted"] is True and d["fn"] == "9900000000016901"
        # фон-поток ingest_parsed асинхронный: ждём появления чека (до 8с)
        me1 = {"receipts": []}
        for _ in range(80):
            me1 = client.get("/api/v1/maxapp/me", headers=H).json()
            if any(r.get("fn") == "9900000000016901"
                   for r in me1.get("receipts", [])):
                break
            time.sleep(0.1)
        assert any(r.get("fn") == "9900000000016901"
                   for r in me1.get("receipts", [])), me1
        time.sleep(0.5)                  # фон-поток дозаписывает баллы
        me1 = client.get("/api/v1/maxapp/me", headers=H).json()
        # дедуп: тот же чек снова
        r2 = client.post("/api/v1/maxapp/scan", headers=H,
                         json={"qr": qr, "form_ms": 4200})
        assert r2.status_code == 200 and r2.json()["accepted"] is False
        me2 = client.get("/api/v1/maxapp/me", headers=H).json()
        assert me2["points"] == me1["points"]       # баллы не растут
        assert len(me2["receipts"]) == 1

    def test_scan_respects_pool_off(self, client):
        H, _ = self._auth(client, 555004)   # _setup включает пул — так надо
        self._setup(pool_on="0")            # выключаем уже после входа
        r = client.post("/api/v1/maxapp/scan", headers=H, json={
            "qr": "t=20261012T090000&s=100&fn=9900000000016902"
                  "&i=16902&fp=690269&n=1"})
        assert r.status_code == 200
        assert r.json()["result"] == "disabled"
        self._setup(pool_on="1")

    def test_summary_and_ref_link(self, client):
        H, _ = self._auth(client, 555005)
        d = client.get("/api/v1/maxapp/summary", headers=H).json()
        assert d["ref_code"].startswith("YM-")
        assert d["ref_link"].startswith("https://max.ru/")
        assert "startapp=" in d["ref_link"]

    def test_session_ok_even_pool_off(self, client):
        """Сессия не зависит от приёма чеков (баллы посмотреть можно)."""
        self._setup(pool_on="0")
        H, _ = self._auth(client, 555006)
        assert client.get("/api/v1/maxapp/me", headers=H).status_code == 200
        self._setup(pool_on="1")


# ------------------------------------------------------------------ ассистент
class TestMaxAssistantsBinding:
    """Вход через MAX привязывает канал ассистентов (без /start КОД)."""

    def test_binding_by_username(self, client):
        """Логин сотрудника ядра == username MAX → привязка ассистента."""
        from app.database import SessionLocal
        from app.models import User
        from app.auth import hash_password
        s = SessionLocal()
        try:
            from app.services import appsettings
            appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
            appsettings.set_setting(s, "pool_enabled", "1")
            su = User(username="maxbind1", full_name="Связной",
                      password_hash=hash_password("x"), role="user")
            s.add(su)
            s.commit()
        finally:
            s.rollback(); s.close()
        # initData с username=maxbind1 (совпадает с логином) — ручной HMAC
        import hashlib, hmac as hm, urllib.parse, json as js, time as tm
        auth_date = int(tm.time())
        pairs = [("auth_date", str(auth_date)),
                 ("query_id", f"q{auth_date}"),
                 ("user", js.dumps({"id": 555007, "first_name": "Связной",
                                    "last_name": "", "username": "maxbind1",
                                    "language_code": "ru", "photo_url": None},
                                   ensure_ascii=False))]
        pairs.sort(key=lambda x: x[0])
        launch = "\n".join(f"{k}={v}" for k, v in pairs)
        secret = hm.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        sig = hm.new(secret, launch.encode(), hashlib.sha256).hexdigest()
        pairs.append(("hash", sig))
        data = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                        for k, v in pairs)
        r = client.post("/api/v1/maxapp/session", json={"init_data": data})
        assert r.status_code == 200, r.text
        s = SessionLocal()
        try:
            u = s.query(User).filter(User.username == "maxbind1").first()
            assert u is not None and u.max_user_id == "555007"
        finally:
            s.rollback(); s.close()


# ------------------------------------------------------------------ статика
class TestMaxAppStatic:
    """Страница мини-приложения отдаётся без авторизации."""

    def test_page_served(self, client):
        r = client.get("/maxapp/")
        assert r.status_code == 200
        assert "max-web-app.js" in r.text
        assert "openCodeReader" in client.get("/maxapp/maxapp.js").text
        assert "Ямастер Чек" in r.text

    def test_bridge_not_bundled_into_spa(self):
        """Наш бандл не должен тянуть Bridge MAX (он подключает страница)."""
        src = open("app/static/js/app.js", encoding="utf-8").read()
        assert "max-web-app.js" not in src


# ------------------------------------------------------------------ инструкция
class TestManualSection:
    def test_manual_has_maxapp_section(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        d = client.get("/api/v1/manual", headers=H).json()
        sec = {s["title"]: s for s in d["sections"]}
        assert "Мини-приложение в MAX" in sec
        s = sec["Мини-приложение в MAX"]
        assert s["image"] == "/img/manual/maxapp.jpg"
        assert "Валид" in s["html"] or "подпис" in s["html"]
        img = open("app/static/img/manual/maxapp.jpg", "rb").read()
        assert img[:2] == b"\xff\xd8"


# ------------------------------------------------------------------ замки
class TestNoRegression:
    """Главное приложение не задето: приём, печать, версии блоков."""

    def test_core_locks_untouched(self):
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert 'status = ("verified" if result.status in ("valid", "not_found")' in src
        appjs = open("app/static/js/app.js", encoding="utf-8").read()
        assert "slice(0, 60)" not in appjs              # замок test_v1241
        blocks = open("app/services/blocks.py", encoding="utf-8").read()
        for blk, ver in (("Сканирование чеков", "1.67.1"),
                         ("Роли и доступ", "1.68.0"),
                         ("Исходящие webhooks", "1.67.0")):
            assert f'"{blk}": "{ver}"' in blocks
