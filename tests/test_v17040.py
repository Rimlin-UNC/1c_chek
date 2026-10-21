# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.40: сдача чеков пачкой (до 100 за сессию).
#
# Требование владельца: «при сканирование можно сканировать сразу до 100
# чеков далее появляется всплывает окно вы отсканировал 100 чеков
# продолжаем или закончим? … проверяем сразу чек если он есть в нашей базе
# то пишем уведомление что такой чек уже есть в базе, и не принимаем его …
# есть счётчик сколько добавлено (синий на модерации) и сколько не принято
# (красный …), если всё ок после модерации (оранжевый корпаративный стиль)
# … реализуй такой механизм у тех кто добавляет чеки».
#
# Что закреплено:
#   1) суточный лимит 100 и прозрачность остатка;
#   2) сервис статусов пачки: чужого не показываем, мусор терпим, потолок;
#   3) новые маршруты batch-status (сайт/кабинет и мини-приложение MAX);
#   4) СОГЛАСОВАНИЕ порогов защиты: прежний FLOOD_PER_HOUR=20 помечал
#      риском каждую честную пачку, а IP-порог 20 давал 429 на 21-м чеке;
#   5) настоящая клиентская логика (счётчики, итог, ключи, экранирование)
#      прогоняется в node — а не поиск подстрок в исходнике;
#   6) непрерывность камеры пачки и сохранность одноразовой камеры;
#   7) два исправленных дефекта приёма: pushRecent(null) и тишина при
#      дубликате;
#   8) цвета, живая область, слои и уважение к «меньше движения»;
#   9) ритуал выпуска и отсутствие посторонних письмен в новых текстах.
#
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
import inspect
import json
import py_compile
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from app.database import SessionLocal, engine
from app.pool import accounts, antifraud, batch, ingest
from app.pool import router_public as public
from app.pool.models import PoolReceipt, PoolUser, ensure_pool_schema

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/static/js/app.js"
MAX_JS = ROOT / "app/static/maxapp/maxapp.js"
MAX_HTML = ROOT / "app/static/maxapp/index.html"
CSS = ROOT / "app/static/css/app.css"

ensure_pool_schema(engine)      # добавляющая миграция (idempotent) до тестов

_SEQ = {"n": 40000}


def _triple():
    """Уникальная тройка ФН/ФД/ФП: тестовая БД персистентна между прогонами,
    а UNIQUE-индекс пула глобальный (fn, fd, fp)."""
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return f"999907890{k:06d}"[:20], str(k)[:10], f"780{k:06d}"[:20]


def _read(path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p.read_text(encoding="utf-8")


def _js_block(src: str, marker: str) -> str:
    """Вырезать функцию/литерал по первой фигурной скобке (со счётом вложенности).

    Регулярным выражением это не берётся: внутри client-кода тысячи вложенных
    скобок и шаблонных строк.
    """
    start = src.index(marker)
    i = src.index("{", start + len(marker) - 1)
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[start:k + 1]
    raise AssertionError("не нашлась закрывающая скобка")


def _func_code(fn) -> str:
    """Исходник функции без docstring: в пояснении законно упомянуть соседа,
    чей побочный эффект мы намеренно не используем."""
    import ast
    src = inspect.getsource(fn)
    tree = ast.parse(textwrap.dedent(src))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        first, last = body[0].lineno, body[0].end_lineno
        keep = [ln for i, ln in enumerate(src.splitlines(), 1)
                if not (first <= i <= last)]
        return "\n".join(keep)
    return src


def _node(script: str) -> str:
    """Прогон НАСТОЯЩЕГО клиентского кода в node с минимальным окружением."""
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node недоступен на этой машине")
    path = Path(tempfile.mkdtemp()) / "t.mjs"
    path.write_text(script, encoding="utf-8")
    r = subprocess.run([node, str(path)], capture_output=True, text=True,
                       timeout=60, check=False)
    assert r.returncode == 0, (r.stdout + r.stderr)[:2000]
    return r.stdout.strip()


# ======================================================================
# 1. Суточный лимит 100 и его прозрачность
# ======================================================================
class TestDailyLimit:
    def test_limit_is_100(self):
        """Решение владельца: лимит поднят константой, без настройки в админке."""
        assert ingest.DAILY_LIMIT == 100

    def test_session_cap_matches_daily_limit(self):
        """Рубеж сессии совпадает с суточным лимитом: честная пачка из 100
        чеков не упирается в отказ «Лимит: не больше N чеков в сутки»."""
        assert batch.SESSION_CAP == ingest.DAILY_LIMIT

    def test_limit_info_without_user(self):
        db = SessionLocal()
        try:
            assert ingest.today_count(db, None) == 0
            assert ingest.limit_info(db, None) == {
                "today": 0, "limit": ingest.DAILY_LIMIT,
                "left": ingest.DAILY_LIMIT}
        finally:
            db.close()

    def test_limit_info_counts_own_receipts_of_today(self):
        db = SessionLocal()
        try:
            u = PoolUser(vid=f"v17040-limit-{_SEQ['n']}")
            db.add(u)
            db.commit()
            db.refresh(u)
            fn, fd, fp = _triple()
            db.add(PoolReceipt(pool_user_id=u.id, fn=fn, fd=fd, fp=fp,
                               status="pending", total_sum=10.0))
            db.commit()
            info = ingest.limit_info(db, u)
            assert info["today"] == 1
            assert info["left"] == ingest.DAILY_LIMIT - 1
        finally:
            db.rollback()
            db.close()

    def test_flood_refusal_mentions_real_limit(self):
        """Текст отказа обязан показывать действительный лимит, а не 50."""
        src = _read("app/pool/ingest.py")
        assert 'f"Лимит: не больше {DAILY_LIMIT} чеков в сутки"' in src


# ======================================================================
# 2. Сервис статусов пачки
# ======================================================================
class TestBatchService:
    def test_poll_contract(self):
        assert batch.POLL_INTERVAL_MS == 2500
        assert batch.POLL_MAX_TRIES == 40
        assert batch.KEYS_MAX == 200

    def test_norm_key_strips(self):
        assert batch.norm_key(" 99 ", "12", " 7 ") == "99|12|7"
        assert batch.norm_key(None, None, None) == "||"

    def test_clean_keys_tolerates_garbage(self):
        """Публичный эндпоинт: кривой ввод не роняет запрос, а отбрасывается."""
        out = batch.clean_keys([
            {"fn": "1", "fd": "2", "fp": "3"},
            "мусор",                                   # не словарь
            None,
            {"fn": "", "fd": "2", "fp": "3"},          # неполная тройка
            {"fn": "1", "fd": "2", "fp": "3", "x": 1},  # лишнее поле
            {"fn": "1", "fd": "2", "fp": "3"},          # повтор
        ])
        assert out == [("1", "2", "3")]

    def test_clean_keys_empty_input(self):
        assert batch.clean_keys(None) == []
        assert batch.clean_keys([]) == []

    def test_clean_keys_capped(self):
        many = [{"fn": f"9{i:015d}"[:20], "fd": str(i)[:10], "fp": str(i)[:20]}
                for i in range(batch.KEYS_MAX + 50)]
        assert len(batch.clean_keys(many)) == batch.KEYS_MAX

    def test_clean_keys_truncates_like_model(self):
        """Длины как в PoolReceipt: fn 20, fd 10, fp 20."""
        out = batch.clean_keys([{"fn": "9" * 40, "fd": "1" * 30, "fp": "7" * 40}])
        assert [len(x) for x in out[0]] == [20, 10, 20]

    def test_statuses_without_user_shows_nothing(self):
        """Чужого не показываем: без участника статусов нет вовсе."""
        db = SessionLocal()
        try:
            res = batch.statuses(db, None, [{"fn": "1", "fd": "2", "fp": "3"}])
            assert res["items"] == []
            assert res["limit"] == ingest.DAILY_LIMIT
            assert res["session_cap"] == batch.SESSION_CAP
            assert res["poll_interval_ms"] == batch.POLL_INTERVAL_MS
            assert res["poll_max_tries"] == batch.POLL_MAX_TRIES
        finally:
            db.close()

    def test_statuses_of_another_user_are_not_disclosed(self):
        """Дубликат, сданный кем-то раньше, не превращается в «проверенный»."""
        db = SessionLocal()
        try:
            mine = PoolUser(vid=f"v17040-mine-{_SEQ['n']}")
            other = PoolUser(vid=f"v17040-other-{_SEQ['n']}")
            db.add_all([mine, other])
            db.commit()
            db.refresh(mine)
            db.refresh(other)
            fn, fd, fp = _triple()
            db.add(PoolReceipt(pool_user_id=other.id, fn=fn, fd=fd, fp=fp,
                               status="verified", total_sum=100.0,
                               points_awarded=1))
            db.commit()
            res = batch.statuses(db, mine, [{"fn": fn, "fd": fd, "fp": fp}])
            assert len(res["items"]) == 1
            assert res["items"][0]["found"] is False
            assert res["items"][0]["status"] == ""
            assert res["items"][0]["points"] == 0
            # счётчики собирает маршрут; здесь проверяем ту же функцию сервиса
            assert batch.count_by_status(res["items"])["ok"] == 0
        finally:
            db.rollback()
            db.close()

    def test_statuses_returns_own_receipt_with_points(self):
        db = SessionLocal()
        try:
            u = PoolUser(vid=f"v17040-own-{_SEQ['n']}")
            db.add(u)
            db.commit()
            db.refresh(u)
            fn, fd, fp = _triple()
            db.add(PoolReceipt(pool_user_id=u.id, fn=fn, fd=fd, fp=fp,
                               status="verified", total_sum=1250.0,
                               points_awarded=1, status_message="Принят в пул"))
            db.commit()
            res = batch.statuses(db, u, [{"fn": fn, "fd": fd, "fp": fp}])
            it = res["items"][0]
            assert it["found"] is True and it["status"] == "verified"
            assert it["sum"] == 1250.0 and it["points"] == 1
            assert batch.count_by_status(res["items"]) == {
                "ok": 1, "moderation": 0, "bad": 1 - 1, "points": 1}
        finally:
            db.rollback()
            db.close()

    def test_count_by_status(self):
        items = [{"status": "verified", "points": 1},
                 {"status": "pending", "points": 0},
                 {"status": "rejected", "points": 0},
                 {"status": "", "points": 0}]
        assert batch.count_by_status(items) == {
            "ok": 1, "moderation": 1, "bad": 1, "points": 1}

    def test_service_is_read_only(self):
        """Сервис статусов не пишет в БД: приём чека не должен ломаться."""
        src = _read("app/pool/batch.py")
        assert "db.commit" not in src
        assert "db.add" not in src
        assert "db.delete" not in src


# ======================================================================
# 3. Маршруты статусов пачки
# ======================================================================
class TestBatchStatusEndpoints:
    def test_public_guest_gets_contract(self, client):
        r = client.post("/api/v1/public/pool/batch-status", json={"keys": []})
        assert r.status_code == 200
        d = r.json()
        assert d["limit"] == ingest.DAILY_LIMIT
        assert d["session_cap"] == batch.SESSION_CAP
        assert d["poll_interval_ms"] == batch.POLL_INTERVAL_MS
        assert d["poll_max_tries"] == batch.POLL_MAX_TRIES
        assert set(d["counts"]) == {"ok", "moderation", "bad", "points"}
        for key in ("today", "left", "user_points", "items"):
            assert key in d, key

    def test_public_unknown_key_is_not_found(self, client):
        fn, fd, fp = _triple()
        r = client.post("/api/v1/public/pool/batch-status",
                        json={"keys": [{"fn": fn, "fd": fd, "fp": fp}]})
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["found"] is False

    def test_public_empty_triple_is_dropped(self, client):
        r = client.post("/api/v1/public/pool/batch-status",
                        json={"keys": [{"fn": "", "fd": "", "fp": ""}]})
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_public_oversized_keys_rejected(self, client):
        keys = [{"fn": "9" * 20, "fd": str(i % 100), "fp": str(i)}
                for i in range(batch.KEYS_MAX + 1)]
        r = client.post("/api/v1/public/pool/batch-status", json={"keys": keys})
        assert r.status_code == 422

    def test_public_garbage_key_type_rejected(self, client):
        """Строгая схема: строка вместо объекта — 422 до сервиса."""
        r = client.post("/api/v1/public/pool/batch-status",
                        json={"keys": ["мусор"]})
        assert r.status_code == 422

    def test_public_too_long_field_rejected(self, client):
        r = client.post("/api/v1/public/pool/batch-status",
                        json={"keys": [{"fn": "9" * 21, "fd": "1", "fp": "1"}]})
        assert r.status_code == 422

    def test_maxapp_requires_token(self, client):
        r = client.post("/api/v1/maxapp/batch-status", json={"keys": []})
        assert r.status_code == 401

    def test_maxapp_with_pool_token(self, client):
        db = SessionLocal()
        try:
            u = PoolUser(vid=f"v17040-max-{_SEQ['n']}")
            db.add(u)
            db.commit()
            db.refresh(u)
            token = accounts.issue_pool_token(u.id)
        finally:
            db.close()
        r = client.post("/api/v1/maxapp/batch-status", json={"keys": []},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        d = r.json()
        assert d["user_points"] == 0
        assert d["limit"] == ingest.DAILY_LIMIT

    def test_maxapp_rejects_core_token(self, client):
        """Токен ядра (typ≠pool) не даёт доступа к данным пула."""
        from tests.conftest import login
        headers = login(client, "admin", "admin123")
        core = headers["Authorization"].split(" ", 1)[1]
        r = client.post("/api/v1/maxapp/batch-status", json={"keys": []},
                        headers={"Authorization": f"Bearer {core}"})
        assert r.status_code == 401

    def test_routes_declared_in_openapi(self, client):
        r = client.get("/api/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/api/v1/public/pool/batch-status" in paths
        assert "/api/v1/maxapp/batch-status" in paths


# ======================================================================
# 4. Согласование порогов защиты с пачкой
# ======================================================================
class TestThresholdAlignment:
    def test_flood_threshold_derived_from_daily_limit(self):
        """Прежние 20 в час помечали риском КАЖДУЮ честную пачку из 100 чеков."""
        assert antifraud.FLOOD_PER_HOUR == ingest.DAILY_LIMIT

    def test_flood_signal_never_quarantines_alone(self):
        """Правило файла: «ложные блокировки на честных недопустимы».
        Один сигнал потока обязан быть ниже порога карантина."""
        _level, weight, _desc = antifraud.SIGNALS["RECEIPT_FLOOD"]
        assert weight < antifraud.QUARANTINE_RISK

    def test_signal_description_stays_in_sync(self):
        """Описание собирается f-строкой из порога и не может разъехаться."""
        _level, _weight, desc = antifraud.SIGNALS["RECEIPT_FLOOD"]
        assert str(antifraud.FLOOD_PER_HOUR) in desc

    def test_anonymous_ip_limit_unchanged(self):
        """20 в час для анонима — единственный тормоз против подмены метки
        посетителя: поднимать его глобально нельзя."""
        assert public.WEB_IP_HOURLY_LIMIT == 20

    def test_authed_ip_limit_covers_full_batch(self):
        """Вошедший участник сдаёт 100 чеков; запас нужен, потому что счётчик
        расходуют и честные повторы с отказами."""
        assert public.WEB_IP_HOURLY_LIMIT_AUTH == ingest.DAILY_LIMIT + 60
        assert public.WEB_IP_HOURLY_LIMIT_AUTH > ingest.DAILY_LIMIT

    def test_authed_lookup_is_pure(self):
        """До отказа 429 записи быть не должно: _request_pool_account ещё и
        приливает историю гостя к аккаунту — поэтому отдельная чистая функция."""
        code = _func_code(public._authed_account)
        assert "_request_pool_account" not in code
        assert "merge_guest_into_account" not in code
        assert "db.commit" not in code and "db.add" not in code
        assert "pool_user_from_token" in code

    def test_authed_lookup_without_bearer(self):
        from starlette.requests import Request
        req = Request({"type": "http", "method": "POST", "path": "/",
                       "query_string": b"", "headers": []})
        db = SessionLocal()
        try:
            assert public._authed_account(db, req) is None
        finally:
            db.close()

    def test_ip_allow_default_is_anonymous_limit(self):
        sig = inspect.signature(public._ip_allow)
        assert sig.parameters["limit"].default == public.WEB_IP_HOURLY_LIMIT

    def test_ip_allow_blocks_after_anonymous_limit(self):
        ip = "203.0.113.40"
        public._IP_HITS.pop(ip, None)
        try:
            for _ in range(public.WEB_IP_HOURLY_LIMIT):
                assert public._ip_allow(ip) is True
            assert public._ip_allow(ip) is False
        finally:
            public._IP_HITS.pop(ip, None)

    def test_ip_allow_authed_limit_lets_full_batch_through(self):
        ip = "203.0.113.41"
        public._IP_HITS.pop(ip, None)
        try:
            n = ingest.DAILY_LIMIT + 10
            hits = sum(1 for _ in range(n)
                       if public._ip_allow(ip, public.WEB_IP_HOURLY_LIMIT_AUTH))
            assert hits == n
        finally:
            public._IP_HITS.pop(ip, None)

    def test_honeypot_threshold_untouched(self):
        """Порог «быстрой отправки» не поднимали: пачка отправляет время жизни
        формы, а не дельту одного чека."""
        assert public.HONEYPOT_MS == 800


# ======================================================================
# 4б. Три дефекта приёма, найденные живым прогоном пачки (v1.70.41)
# ======================================================================
def _enable_pool(db):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1")


def _mk_user(db, tag, **kw):
    _SEQ["n"] += 1
    u = PoolUser(vid=f"v17040-{tag}-{_SEQ['n']}", **kw)
    db.add(u)
    db.flush()
    return u


def _reset_auth_hits():
    """Сброс счётчика попыток регистрации/входа.

    В полном прогоне воркер накапливает попытки соседних тестов, и без сброса
    своя регистрация получает честный 429 вместо проверяемого поведения
    (так же поступают фикстуры test_v1310/test_v1320).
    """
    from app.pool import router_auth
    router_auth._HITS.clear()


def _wait_bg(name="pool-web-ingest", timeout=25):
    """Ждёт завершения фоновых потоков приёма (приём идёт не в запросе)."""
    import threading
    import time
    end = time.time() + timeout
    while time.time() < end:
        if not [t for t in threading.enumerate() if t.name == name]:
            return True
        time.sleep(0.05)
    return False


class TestGuestMergeIntegrity:
    """Дефект 22: слияние гостевой истории падало на FOREIGN KEY.

    `merge_guest_into_account` переносила только чеки и баллы, после чего
    удаляла строку гостя. Но на `pool_users.id` ссылаются десять таблиц:
    согласия 152-ФЗ, одноразовые токены, отпечатки устройств, журнал IP,
    сигналы антифрода, рефералы, достижения и выводы средств. Любой гость,
    успевший получить сигнал антифрода (например REPEAT_SUMS после пяти
    одинаковых сумм подряд) или достижение, при регистрации давал
    `sqlite3.IntegrityError: FOREIGN KEY constraint failed`: регистрация,
    вход и сдача чека из кабинета отвечали 500 — то есть обрывался ровно тот
    путь, который обещан в шапке модуля аккаунтов.
    """

    def test_merge_moves_every_dependent_table(self):
        from app.pool.models import (
            PoolAchievement,
            PoolConsent,
            PoolFingerprint,
            PoolIpLog,
            PoolPoint,
            PoolReferral,
            PoolSignal,
            PoolToken,
            PoolWithdrawal,
        )
        db = SessionLocal()
        try:
            guest = _mk_user(db, "m22g", points=3)
            acc = _mk_user(db, "m22a")
            ref = _mk_user(db, "m22r")
            other = _mk_user(db, "m22o")
            fn, fd, fp = _triple()
            db.add(PoolReceipt(pool_user_id=guest.id, fn=fn, fd=fd, fp=fp,
                               status="verified", total_sum=10.0,
                               points_awarded=1))
            for row in (
                PoolPoint(user_id=guest.id, delta=1, reason="receipt"),
                PoolConsent(user_id=guest.id, vid=guest.vid,
                            offerta_version="2026-10"),
                PoolToken(user_id=guest.id, token_hash="h22", purpose="magic",
                          email="m22@test.ru"),
                PoolFingerprint(user_id=guest.id, visitor_hash="vh22",
                                ua_hash="uh22"),
                PoolIpLog(user_id=guest.id, ip="ip22", ip24="24", kind="check"),
                PoolSignal(user_id=guest.id, code="REPEAT_SUMS", severity=2,
                           points=20, status="open"),
                PoolReferral(referrer_id=ref.id, referred_id=guest.id),
                PoolReferral(referrer_id=guest.id, referred_id=other.id),
                PoolAchievement(user_id=guest.id, code="first_receipt"),
                PoolWithdrawal(user_id=guest.id, points=5, status="new"),
            ):
                db.add(row)
            db.commit()
            gvid = guest.vid

            moved = accounts.merge_guest_into_account(db, acc, gvid)
            db.commit()

            assert moved == 1, "число перенесённых чеков нужно для письма"
            assert db.query(PoolUser).filter(PoolUser.vid == gvid).count() == 0
            for model, col in ((PoolReceipt, "pool_user_id"),
                               (PoolPoint, "user_id"), (PoolConsent, "user_id"),
                               (PoolToken, "user_id"),
                               (PoolFingerprint, "user_id"),
                               (PoolIpLog, "user_id"), (PoolSignal, "user_id"),
                               (PoolAchievement, "user_id"),
                               (PoolWithdrawal, "user_id")):
                left = (db.query(model)
                        .filter(getattr(model, col) == guest.id).count())
                assert left == 0, f"{model.__tablename__}: строки гостя не перенесены"
            # антифрод-история переживает слияние, а не стирается
            assert (db.query(PoolSignal)
                    .filter(PoolSignal.user_id == acc.id).count()) == 1
            assert (db.query(PoolReferral)
                    .filter(PoolReferral.referred_id == acc.id).count()) == 1
            assert (db.query(PoolReferral)
                    .filter(PoolReferral.referrer_id == acc.id).count()) == 1
            assert acc.points == 3, "баллы гостя прибавлены к аккаунту"
        finally:
            db.rollback()
            db.close()

    def test_merge_resolves_achievement_code_conflict(self):
        """UNIQUE(user_id, code): уже полученное аккаунтом не дублируется."""
        from app.pool.models import PoolAchievement
        db = SessionLocal()
        try:
            guest = _mk_user(db, "m22ach-g")
            acc = _mk_user(db, "m22ach-a")
            db.add(PoolAchievement(user_id=acc.id, code="first_receipt"))
            db.add(PoolAchievement(user_id=guest.id, code="first_receipt"))
            db.add(PoolAchievement(user_id=guest.id, code="hundred"))
            db.commit()
            gvid = guest.vid

            accounts.merge_guest_into_account(db, acc, gvid)
            db.commit()

            codes = sorted(a.code for a in db.query(PoolAchievement)
                           .filter(PoolAchievement.user_id == acc.id).all())
            assert codes == ["first_receipt", "hundred"], codes
        finally:
            db.rollback()
            db.close()

    def test_merge_resolves_referral_conflict(self):
        """UNIQUE(referred_id): привязка аккаунта побеждает гостевую."""
        from app.pool.models import PoolReferral
        db = SessionLocal()
        try:
            guest = _mk_user(db, "m22ref-g")
            acc = _mk_user(db, "m22ref-a")
            first, second = _mk_user(db, "m22ref-1"), _mk_user(db, "m22ref-2")
            db.add(PoolReferral(referrer_id=first.id, referred_id=acc.id))
            db.add(PoolReferral(referrer_id=second.id, referred_id=guest.id))
            db.commit()
            gvid = guest.vid

            accounts.merge_guest_into_account(db, acc, gvid)
            db.commit()

            rows = (db.query(PoolReferral)
                    .filter(PoolReferral.referred_id == acc.id).all())
            assert len(rows) == 1, "гостевая привязка не должна затереть свою"
            assert rows[0].referrer_id == first.id
        finally:
            db.rollback()
            db.close()

    def test_merge_is_wrapped_in_savepoint(self):
        """Сбой слияния не имеет права ломать регистрацию/вход/приём чека."""
        src = _func_code(accounts.merge_guest_into_account)
        assert "db.begin_nested()" in src, "слияние не в SAVEPOINT"
        assert "sp.rollback()" in src
        assert "sp.commit()" in src
        assert "db.flush()" in src, "FK-конфликт проявляется только на сбросе"
        assert "log.warning" in src, "причина откладывания слияния должна быть в журнале"

    def test_register_with_antifraud_history_is_not_500(self, client):
        from app.pool.models import PoolSignal
        db = SessionLocal()
        try:
            _enable_pool(db)
            guest = _mk_user(db, "m22api")
            db.add(PoolSignal(user_id=guest.id, code="REPEAT_SUMS", severity=2,
                              points=20, status="open"))
            db.commit()
            cookie = {"pool_vid": f"{guest.vid}.{public._sign(guest.vid)}"}
            public._IP_HITS.clear()
            _reset_auth_hits()
            _SEQ["n"] += 1
            r = client.post("/api/v1/pool-auth/register",
                            json={"email": f"m22-{_SEQ['n']}@test.ru",
                                  "password": "parol-1234"},
                            cookies=cookie)
            assert r.status_code == 200, r.text[:300]
            assert "token" in r.json()
        finally:
            db.rollback()
            db.close()

    def test_login_with_achievement_history_is_not_500(self, client):
        from app.auth import hash_password
        from app.pool.models import PoolAchievement
        db = SessionLocal()
        try:
            _SEQ["n"] += 1
            email = f"m22log-{_SEQ['n']}@test.ru"
            acc = _mk_user(db, "m22login", email=email)
            acc.password_hash = hash_password("parol-1234")
            guest = _mk_user(db, "m22loging")
            db.add(PoolAchievement(user_id=guest.id, code="first_receipt"))
            db.commit()
            cookie = {"pool_vid": f"{guest.vid}.{public._sign(guest.vid)}"}
            _reset_auth_hits()
            r = client.post("/api/v1/pool-auth/login",
                            json={"email": email, "password": "parol-1234"},
                            cookies=cookie)
            assert r.status_code == 200, r.text[:300]
        finally:
            db.rollback()
            db.close()


class TestMergeRace:
    """Дефект 23: чек терялся, если донор регистрировался во время проверки.

    Приём отвечает «Чек принят — проверяем» сразу, а проверка идёт секунды в
    фоновом потоке. Если в это окно донор регистрировался, слияние переносило
    историю и удаляло строку гостя — поток либо падал на FOREIGN KEY, либо
    получал ObjectDeletedError при перечитывании владельца, и чек исчезал:
    счётчик донора навсегда оставался синим.
    """

    def test_merge_inherits_vid(self):
        """У аккаунта, созданного регистрацией, своего vid нет — он наследует
        гостевой: именно по нему фоновый приём потом находит владельца."""
        db = SessionLocal()
        try:
            guest = _mk_user(db, "m23g")
            acc = _mk_user(db, "m23a")
            acc.vid = None
            db.commit()
            gvid = guest.vid
            accounts.merge_guest_into_account(db, acc, gvid)
            db.commit()
            assert acc.vid == gvid, "vid должен наследоваться аккаунтом"
            assert db.query(PoolUser).filter(PoolUser.vid == gvid).count() == 1
        finally:
            db.rollback()
            db.close()

    def test_merge_keeps_account_vid_when_it_already_has_one(self):
        """Свой vid аккаунта не затирается, а гостевой освобождается: vid
        уникален, и слияние не имеет права падать на этом ограничении."""
        db = SessionLocal()
        try:
            guest = _mk_user(db, "m23g2")
            acc = _mk_user(db, "m23a2")
            own, gvid = acc.vid, guest.vid
            accounts.merge_guest_into_account(db, acc, gvid)
            db.commit()
            assert acc.vid == own, "чужой vid затёр собственный"
            assert db.query(PoolUser).filter(PoolUser.vid == gvid).count() == 0
        finally:
            db.rollback()
            db.close()

    def test_receipt_survives_registration_during_verification(
            self, client, monkeypatch):
        import time
        from datetime import datetime

        from app.services.external import ExternalItem, ExternalResult

        slow = {"calls": 0}

        def _slow_fetch(*_a, **_k):
            slow["calls"] += 1
            time.sleep(0.9)     # окно гонки: регистрация успевает пройти раньше
            return ExternalResult(
                ok=True, source="test", found=True, message="OK",
                date_time=datetime(2026, 9, 5, 14, 30), total_sum=1250.0,
                operation=1, merchant_name="ООО Ромашка",
                merchant_inn="7704001275", merchant_address="г. Тихвин",
                items=[ExternalItem(name="Кофе", quantity=1.0, price=1250.0,
                                    total=1250.0, vat_rate="20", vat_sum=208.33)],
                raw={"document": {"receipt": {"totalSum": 125000}}})

        monkeypatch.setattr(ingest, "_fetch_details", _slow_fetch)
        db = SessionLocal()
        try:
            _enable_pool(db)
            db.commit()
            fn, fd, fp = _triple()
            qr = f"t=20260905T1430&s=1250.00&fn={fn}&i={fd}&fp={fp}&n=1"
            public._IP_HITS.clear()
            client.cookies.clear()   # сессионный клиент хранит чужие cookie
            r = client.post("/api/v1/public/pool/check",
                            json={"qr_text": qr, "offerta": True, "hp": "",
                                  "form_ms": 5000})
            assert r.status_code == 200 and r.json().get("accepted"), r.text[:200]
            raw = client.cookies.get("pool_vid") or ""
            assert "." in raw, f"сервер не выдал подписанный vid: {raw!r}"

            _SEQ["n"] += 1
            reg = client.post("/api/v1/pool-auth/register",
                              json={"email": f"m23-{_SEQ['n']}@test.ru",
                                    "password": "parol-1234"},
                              cookies={"pool_vid": raw})   # подпись обязательна
            assert reg.status_code == 200, reg.text[:300]
            acc_id = None
            tok = reg.json()["token"]
            import jwt as _jwt

            from app.config import settings as _st
            acc_id = _jwt.decode(tok, _st.SECRET_KEY,
                                 algorithms=["HS256"]).get("pu")

            _wait_bg()
            db.expire_all()
            rows = db.query(PoolReceipt).filter(PoolReceipt.fn == fn).all()
            assert len(rows) == 1, f"чек потерян или задвоен: {len(rows)}"
            assert rows[0].pool_user_id == acc_id, \
                "чек должен принадлежать аккаунту, поглотившему гостя"
            assert slow["calls"] >= 2, "приём не был повторён по актуальному владельцу"
        finally:
            db.rollback()
            db.close()

    def test_background_reresolves_owner_by_vid(self):
        """Замок на сам механизм повтора: оба проявления гонки пойманы."""
        src = _read("app/pool/router_public.py")
        start = src.index("    def _bg():")
        end = src.index('threading.Thread(target=_bg, name="pool-web-ingest"',
                        start)
        body = src[start:end]
        assert "except (IntegrityError, ObjectDeletedError):" in body
        assert "s.rollback()" in body
        assert "PoolUser.vid == vid" in body
        assert "stale_id = u.id" in body, \
            "id надо фиксировать до rollback: после него строка гостя мертва"
        assert body.count("ingest.ingest_parsed(") == 2, \
            "повтор приёма обязан быть ровно один"


class TestAtomicPoints:
    """Дефект 24: баллы терялись при параллельном приёме пачки.

    `add_points` прибавляла баллы чтением-изменением-записью в памяти сессии.
    При поточной сдаче каждый чек проверяется своим фоновым потоком со своей
    сессией: два потока читали одно значение, и одно начисление терялось.
    Замер живого прогона: 10 принятых чеков MAX дали 9 баллов на счёте при
    10 начислениях в самих чеках.
    """

    def test_add_points_is_atomic_update(self):
        src = _func_code(ingest.add_points)
        assert "func.coalesce(PoolUser.points, 0) + int(delta)" in src
        assert 'synchronize_session="fetch"' in src, \
            "без синхронизации ответ user_points останется неточным"
        assert "if not changed:" in src, "нужен запасной путь для незаписанной строки"

    def test_concurrent_add_points_loses_nothing(self):
        import threading
        db = SessionLocal()
        try:
            u = _mk_user(db, "m24", points=0)
            db.commit()
            uid = u.id
            n = 12
            errors = []

            def _one():
                s = SessionLocal()
                try:
                    from app.pool.models import PoolUser as _PU
                    owner = s.get(_PU, uid)
                    ingest.add_points(s, owner, 1, "receipt", "r24")
                    s.commit()
                except Exception as e:                        # noqa: BLE001
                    errors.append(repr(e))
                    s.rollback()
                finally:
                    s.close()

            threads = [threading.Thread(target=_one) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not errors, errors[:2]
            db.expire_all()
            from app.pool.models import PoolPoint
            from app.pool.models import PoolUser as _PU
            owner = db.get(_PU, uid)
            ledger = (db.query(PoolPoint)
                      .filter(PoolPoint.user_id == uid,
                              PoolPoint.reason == "receipt").count())
            assert owner.points == n, f"потеряно {n - owner.points} баллов из {n}"
            assert ledger >= n, "журнал начислений обязан совпадать со счётом"
        finally:
            db.rollback()
            db.close()

    def test_add_points_writes_ledger_and_zero_is_noop(self):
        from app.pool.models import PoolPoint
        db = SessionLocal()
        try:
            u = _mk_user(db, "m24b", points=0)
            db.commit()
            ingest.add_points(db, u, 0, "receipt")
            db.commit()
            assert u.points == 0
            assert (db.query(PoolPoint)
                    .filter(PoolPoint.user_id == u.id).count()) == 0
            ingest.add_points(db, u, 2, "receipt", "ref24")
            db.commit()
            row = (db.query(PoolPoint)
                   .filter(PoolPoint.user_id == u.id).one())
            assert (row.delta, row.reason, row.ref_id) == (2, "receipt", "ref24")
            assert u.points == 2
        finally:
            db.rollback()
            db.close()


# ======================================================================
# 5. Настоящая клиентская логика сайта (прогон в node)
# ======================================================================
WEB_HELPERS = ("pubBatchKey", "pubBatchState", "pubBatchLabel", "pubBatchCount",
               "pubBatchRowHTML", "pubBatchDayText", "pubBatchSummaryText")
SCAN_HELPERS = ("scanSessKey", "scanSessStateOf", "scanSessLabel",
                "scanSessCount", "scanSessRowHTML", "scanSessSummaryText")

_STUBS = (
    "const esc = (s) => String(s).replace(/[&<>\"']/g,"
    " (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n"
    "const fmtSum = (n) => Number(n).toFixed(2) + ' ₽';\n"
    "const PUB_BATCH_CAP = 100;\n"
)


def _helpers(names, source: Path) -> str:
    js = _read(source)
    return _STUBS + "\n".join(_js_block(js, f"function {n}(") for n in names)


class TestWebBatchLogic:
    def test_key_is_fiscal_triple(self):
        out = _node(_helpers(WEB_HELPERS, APP_JS) + """
console.log(pubBatchKey('99', '12', '7'));
console.log(pubBatchKey(undefined, '12', '7'));
""")
        a, b = out.split("\n")
        assert a == "99|12|7"
        assert b == "|12|7"

    def test_status_mapping(self):
        out = _node(_helpers(WEB_HELPERS, APP_JS) + """
console.log(['verified', 'pending', 'rejected', ''].map((s) => pubBatchState(s)).join(','));
console.log(['ok', 'pend', 'bad'].map((s) => pubBatchLabel(s)).join(','));
""")
        states, labels = out.split("\n")
        assert states == "ok,pend,bad,pend"
        assert labels == "принят,на модерации,не принят"

    def test_counters_and_points(self):
        out = _node(_helpers(WEB_HELPERS, APP_JS) + """
const items = [{state: 'ok', points: 1}, {state: 'pend', points: 0},
               {state: 'bad', points: 0}, {state: 'pend', points: 0}];
console.log(JSON.stringify(pubBatchCount(items)));
console.log(JSON.stringify(pubBatchCount([])));
console.log(JSON.stringify(pubBatchCount(null)));
""")
        a, b, c = out.split("\n")
        assert json.loads(a) == {"ok": 1, "pend": 2, "bad": 1, "points": 1}
        assert json.loads(b) == {"ok": 0, "pend": 0, "bad": 0, "points": 0}
        assert json.loads(c) == {"ok": 0, "pend": 0, "bad": 0, "points": 0}

    def test_day_text_shows_left_and_exhausted(self):
        out = _node(_helpers(WEB_HELPERS, APP_JS) + """
console.log(pubBatchDayText({today: 3, limit: 100, left: 97}));
console.log(pubBatchDayText({today: 100, limit: 100, left: 0}));
console.log(pubBatchDayText(null));
""")
        a, b, c = out.split("\n")
        assert "Сегодня сдано 3 из 100" in a and "осталось 97" in a
        assert "лимит исчерпан" in b and "осталось" not in b
        assert c.startswith("Сегодня сдано 0 из 100")

    def test_summary_lists_three_counters_points_and_time(self):
        out = _node(_helpers(WEB_HELPERS, APP_JS) + """
const items = [{state: 'ok', points: 1}, {state: 'bad', points: 0}];
console.log(pubBatchSummaryText(items, 90000));
console.log(pubBatchSummaryText([], 500));
""")
        a, b = out.split("\n")
        for part in ("принято 1", "на модерации 0", "не принято 1",
                     "баллов +1", "1 мин 30 с"):
            assert part in a, part
        assert "0 с" not in b and "1 с" in b      # округление вверх до секунды

    def test_row_marks_state_and_escapes(self):
        out = _node(_helpers(WEB_HELPERS, APP_JS) + """
console.log(pubBatchRowHTML({state: 'bad', fd: '', sum: 0, who: '<img src=x>'}));
console.log(pubBatchRowHTML({state: 'ok', fd: '12', sum: 1250}));
""")
        bad, ok = out.split("\n")
        assert 'class="brow s-bad"' in bad
        assert "<img" not in bad and "&lt;img" in bad
        assert 'class="brow s-ok"' in ok
        assert "ФД 12" in ok and "1250.00 ₽" in ok


class TestEmployeeSessionLogic:
    def test_receipt_status_mapping(self):
        out = _node(_helpers(SCAN_HELPERS, APP_JS) + """
const cases = [{status: 'verified'}, {status: 'verified', fns_status: 'invalid'},
               {status: 'failed'}, {status: 'new'}, {status: 'verifying'}, {}];
console.log(cases.map((r) => scanSessStateOf(r)).join(','));
console.log(['ok', 'pend', 'bad'].map((s) => scanSessLabel(s)).join(','));
""")
        states, labels = out.split("\n")
        assert states == "ok,bad,bad,pend,pend,pend"
        assert labels == "принят,на проверке,не принят"

    def test_counters_include_money(self):
        out = _node(_helpers(SCAN_HELPERS, APP_JS) + """
const items = [{state: 'ok', sum: 100}, {state: 'pend', sum: 50.5},
               {state: 'bad', sum: 0}];
console.log(JSON.stringify(scanSessCount(items)));
""")
        assert json.loads(out) == {"ok": 1, "pend": 1, "bad": 1, "sum": 150.5}

    def test_summary_mentions_money_and_duration(self):
        out = _node(_helpers(SCAN_HELPERS, APP_JS) + """
const items = [{state: 'ok', sum: 1250}, {state: 'bad', sum: 0}];
console.log(scanSessSummaryText(items, 65000));
""")
        for part in ("принято 1", "на проверке 0", "не принято 1",
                     "1250.00 ₽", "1 мин 5 с"):
            assert part in out, part

    def test_row_escapes_and_marks_state(self):
        out = _node(_helpers(SCAN_HELPERS, APP_JS) + """
console.log(scanSessRowHTML({state: 'pend', fd: '77', sum: 0}));
console.log(scanSessRowHTML({state: 'bad', fd: '', who: '<script>x</script>'}));
""")
        pend, bad = out.split("\n")
        assert 'class="brow s-pend"' in pend and "ФД 77" in pend
        assert "<script>" not in bad and "&lt;script&gt;" in bad


class TestMaxBatchLogic:
    def _script(self, extra: str) -> str:
        js = _read(MAX_JS)
        head = ("function esc(s) { return String(s); }\n"
                "const BATCH_CAP = 100;\n"
                + _js_block(js, "const BATCH = {") + "\n"
                + _js_block(js, "function batchKey(") + "\n"
                + _js_block(js, "function batchStateRow(") + "\n"
                + _js_block(js, "function batchSummaryText()") + "\n")
        return head + extra

    def test_key_and_row(self):
        out = _node(self._script("""
console.log(batchKey('1', '2', '3'));
console.log(batchStateRow({state: 'ok', fd: '12', sum: 10}));
console.log(batchStateRow({state: 'bad', fd: '', who: 'дубликат'}));
"""))
        key, ok, bad = out.split("\n")
        assert key == "1|2|3"
        assert 's-ok' in ok and "принят" in ok and "ФД 12" in ok and "10.00 ₽" in ok
        assert 's-bad' in bad and "не принят" in bad and "дубликат" in bad

    def test_summary_of_session(self):
        out = _node(self._script("""
BATCH.counts = {ok: 2, pend: 1, bad: 3};
BATCH.items = [{points: 2}];
BATCH.day = {today: 6, limit: 100, left: 94};
BATCH.t0 = Date.now() - 5000;
console.log(batchSummaryText());
"""))
        for part in ("принято 2", "на модерации 1", "не принято 3",
                     "баллов +2", "5 с", "Сегодня сдано 6 из 100"):
            assert part in out, part

    def test_cap_is_100(self):
        assert "const BATCH_CAP = 100;" in _read(MAX_JS)


# ======================================================================
# 6. Поток пачки на сайте: непрерывность, рубеж, форма
# ======================================================================
class TestWebBatchFlow:
    def test_markup_ids(self):
        js = _read(APP_JS)
        for needle in ('id="pub-batch-start"', 'id="pub-batch"',
                       'id="pub-batch-prog"', 'id="pub-bc-ok"',
                       'id="pub-bc-pend"', 'id="pub-bc-bad"',
                       'id="pub-batch-day"', 'id="pub-batch-list"',
                       'id="pub-batch-sum"', 'id="pub-batch-stop"'):
            assert needle in js, needle

    def test_constants(self):
        js = _read(APP_JS)
        assert "const PUB_BATCH_CAP = 100;" in js
        assert "const PUB_BATCH_POLL_MS = 2500;" in js
        assert "const PUB_BATCH_POLL_MAX = 40;" in js
        assert "const PUB_BATCH_ROWS = 8;" in js

    def test_batch_camera_is_continuous(self):
        """Главное отличие от одноразовой камеры: после чека она НЕ закрывается."""
        fn = _js_block(_read(APP_JS), "function openPoolCameraBatch(")
        cb = fn.split("new CameraScanner(video, (text) => {", 1)[1]
        cb = cb.split("});", 1)[0]
        assert "onText(text, parsed)" in cb
        assert "close()" not in cb, "камера пачки закрывается после чека"
        assert "cam.stop()" not in cb, "камера пачки останавливается после чека"

    def test_one_shot_camera_still_one_shot(self):
        """Одноразовая камера пула не тронута — её использует одиночная сдача."""
        fn = _js_block(_read(APP_JS), "function openPoolCameraScan(")
        assert "close()" in fn and "cam.stop()" in fn

    def test_non_receipt_qr_is_not_counted(self):
        """Чужой QR (не чек) не должен попадать в счётчики пачки."""
        fn = _js_block(_read(APP_JS), "function openPoolCameraBatch(")
        cb = fn.split("new CameraScanner(video, (text) => {", 1)[1]
        assert "if (!parsed)" in cb

    def test_checkpoint_at_cap(self):
        js = _read(APP_JS)
        assert "PB.items.length >= PB.cap" in js
        assert "pbAskMore" in js
        assert "Продолжить — ещё" in js
        assert "Закончить и посмотреть итог" in js

    def test_checkpoint_moves_cap_by_100(self):
        js = _read(APP_JS)
        assert "PB.cap += PUB_BATCH_CAP" in js

    def test_form_ms_is_form_lifetime_not_per_receipt(self):
        """HONEYPOT_MS=800 и третий слой антифрода пометили бы честного донора
        как бота, если бы отправлялась дельта одного скана."""
        js = _read(APP_JS)
        block = js.split("const pbHandle = async (text) => {", 1)[1]
        block = block.split("const pbStart", 1)[0]
        assert "form_ms: Date.now() - t0" in block
        assert "Date.now() - PB.t0" not in block.split("form_ms")[1][:80]

    def test_t0_is_form_open_time(self):
        js = _read(APP_JS)
        head = js.split("function bindPoolForm(root, opts) {", 1)[1][:200]
        assert "const t0 = Date.now();" in head

    def test_offerta_gate_before_batch(self):
        """Согласие с офертой обязательно и ДО начала пачки (152-ФЗ, v1.70.37)."""
        js = _read(APP_JS)
        block = js.split("const pbStart = (resume) => {", 1)[1]
        block = block.split("const pbStartBtn", 1)[0]
        assert "pub-offerta" in block and "checked" in block

    def test_single_submit_routes_into_batch(self):
        js = _read(APP_JS)
        block = js.split("if (submit) submit.onclick = async () => {", 1)[1][:500]
        assert "PB.active" in block and "pbHandle" in block

    def test_polls_batch_status(self):
        assert "/api/v1/public/pool/batch-status" in _read(APP_JS)

    def test_intra_session_duplicate_does_not_hit_server(self):
        js = _read(APP_JS)
        block = js.split("const pbHandle = async (text) => {", 1)[1]
        block = block.split("const pbStart", 1)[0]
        seen_at = block.index("PB.seen[norm]")
        post_at = block.index("'/api/v1/public/pool/check'")
        assert seen_at < post_at, "повтор сессии уходит на сервер"

    def test_flood_refusal_stops_batch(self):
        js = _read(APP_JS)
        assert "if (d.result === 'flood') pbStop('flood')" in js


# ======================================================================
# 7. Экран «Сканирование»: сессия, дубликат, WS
# ======================================================================
class TestEmployeeScanSession:
    def test_push_recent_tolerates_null(self):
        """Дефект 20: дубликат чужой организации приходит с receipt: null,
        а pushRecent обращался к r.fn — TypeError тонул в async-обработчике."""
        fn = _js_block(_read(APP_JS), "function pushRecent(")
        assert "if (!r || typeof r !== 'object') return;" in fn
        assert "duplicate: duplicate === true" in fn

    def test_duplicate_flag_is_passed(self):
        assert "pushRecent(rec, true)" in _read(APP_JS)

    def test_duplicate_gets_feedback(self):
        """Дефект 21: раньше единственным откликом был beep и только для
        НЕдубликата — повторный скан выглядел как «ничего не произошло»."""
        fn = _js_block(_read(APP_JS), "function scanSessNoteServer(")
        assert "r.duplicate" in fn
        assert "toast(" in fn
        assert fn.index("r.duplicate") < fn.index("beep()")

    def test_session_panel_ids(self):
        js = _read(APP_JS)
        for needle in ("scan-bc-ok", "scan-bc-pend", "scan-bc-bad",
                       "scan-batch-prog", "scan-batch-list", "scan-batch-sum",
                       "btn-scan-sess-reset"):
            assert needle in js, needle

    def test_camera_hooks_for_checkpoint(self):
        """Окно рубежа обязано выключать камеру без перерисовки экрана,
        иначе счётчики пропадают из-под окна."""
        js = _read(APP_JS)
        assert "scanCamStop = () => doStop(false);" in js
        assert "scanCamStart = async ()" in js
        assert "stopBtn.onclick = () => doStop(true);" in js

    def test_ws_updates_session_state(self):
        js = _read(APP_JS)
        block = js.split("case 'receipt_verified': {", 1)[1][:240]
        assert "scanSessNote(p.id" in block

    def test_checkpoint_uses_cap(self):
        js = _read(APP_JS)
        assert "const SCAN_CAP = 100;" in js
        assert "scanSess.items.length < scanSess.cap" in js

    def test_all_intake_paths_counted(self):
        """Сессия считается по всем способам приёма: камера, фото, PDF."""
        js = _read(APP_JS)
        assert js.count("scanSessNoteServer(r, file.name)") == 2

    def test_session_state_is_module_level(self):
        """Панель живёт между перерисовками экрана — состояние не локальное."""
        js = _read(APP_JS)
        assert "const scanSess = {" in js


# ======================================================================
# 8. Мини-приложение MAX
# ======================================================================
class TestMaxBatchSource:
    def test_constants(self):
        js = _read(MAX_JS)
        assert "const BATCH_POLL_MS = 2500;" in js
        assert "const BATCH_POLL_MAX = 40;" in js
        assert "const BATCH_ROWS = 8;" in js

    def test_submit_routes_into_batch(self):
        fn = _js_block(_read(MAX_JS), "async function submit(")
        assert "BATCH.active" in fn and "batchHandle(text)" in fn

    def test_native_scanner_preferred(self):
        fn = _js_block(_read(MAX_JS), "async function batchScanOnce(")
        assert "typeof app.openCodeReader === 'function'" in fn

    def test_empty_scan_means_cancel_not_error(self):
        """Пустой результат сканера MAX — это отмена, а не ошибка: цикл
        завершается спокойно, иначе донор получал бы ложное сообщение."""
        js = _read(MAX_JS)
        block = js.split("async function startBatch(", 1)[1]
        block = block.split("\n}", 1)[0]
        assert "stopBatch('done')" in block

    def test_scanner_locks_still_hold(self):
        """Замки test_v1707/test_v1702 проверены после вставки пачки."""
        js = _read(MAX_JS)
        assert js.index("openCodeReader") < js.index("scanCamera")
        found = re.findall(r"if \(!app \|\| !app\.openCodeReader\)[^}]*}", js)
        assert len(found) == 1 and "scanCamera" in found[0]
        assert "waitForBridge" in js and "mx-retry" in js

    def test_batch_code_goes_after_scanner_symbols(self):
        """Вставка после openCodeReader/scanCamera — иначе замок порядка падает."""
        js = _read(MAX_JS)
        assert js.index("function scanCamera") < js.index("const BATCH_CAP")

    def test_markup_controls(self):
        html = _read(MAX_HTML)
        for needle in ("btn-batch", "batch-card", "batch-modal",
                       "btn-batch-more", "btn-batch-finish", "btn-batch-stop",
                       "batch-prog", "batch-day", "batch-list", "batch-sum"):
            assert needle in html, needle

    def test_polls_batch_status(self):
        assert "/api/v1/maxapp/batch-status" in _read(MAX_JS)

    def test_buttons_disabled_during_batch(self):
        """Одиночное сканирование во время пачки выключается — два пути приёма
        не должны соревноваться за один счётчик."""
        js = _read(MAX_JS)
        fn = _js_block(js, "function batchSetButtons(")
        for needle in ("btn-scan", "btn-cam", "btn-photo"):
            assert needle in fn, needle


# ======================================================================
# 9. Стили: цвета бренда, слои, живая область, «меньше движения»
# ======================================================================
class TestBatchCss:
    def test_panel_classes_exist(self):
        css = _read(CSS)
        for needle in (".batch-panel", ".batch-head", ".batch-title",
                       ".batch-prog", ".batch-counts", ".bcount", ".batch-day",
                       ".batch-list", ".brow", ".batch-sum",
                       ".batch-cam-counts", ".fly-chip"):
            assert needle in css, needle

    def test_keyframes_exist(self):
        css = _read(CSS)
        for needle in ("@keyframes batchFly", "@keyframes batchPulse",
                       "@keyframes batchIn"):
            assert needle in css, needle

    def test_counter_colours(self):
        """Синий — на модерации, терракота — не принят, оранжевый — принят."""
        css = _read(CSS)
        ok = css.split(".bcount.b-ok", 1)[1].split("}", 1)[0]
        pend = css.split(".bcount.b-pend", 1)[1].split("}", 1)[0]
        bad = css.split(".bcount.b-bad", 1)[1].split("}", 1)[0]
        assert "var(--accent)" in ok and "var(--accent-text)" in ok
        assert "var(--info)" in pend and "var(--info-text)" in pend
        assert "var(--bad)" in bad and "var(--bad-text)" in bad

    def test_row_state_colours(self):
        css = _read(CSS)
        for sel, token in ((".brow.s-ok", "--accent"), (".brow.s-pend", "--info"),
                           (".brow.s-bad", "--bad")):
            assert sel in css, sel
            block = css.split(sel + " .bst", 1)[1].split("}", 1)[0]
            assert f"var({token}-text)" in block, sel

    def test_no_raw_hex_outside_palette(self):
        """Новые стили не вводят своих цветов — только токены палитры."""
        css = _read(CSS)
        start = css.index(".batch-panel")
        end = css.index("@keyframes batchIn")
        block = css[start:end]
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", block)
        assert not hexes, hexes

    def test_reduced_motion_respected(self):
        assert "prefers-reduced-motion: reduce" in _read(CSS)

    def test_fly_chip_layer_between_drawer_and_modal(self):
        """Значок летит выше ящика и оверлея камеры, но ниже модального окна
        и тостов: окно рубежа и уведомления он перекрывать не должен."""
        css = _read(CSS)
        block = css.split(".fly-chip {", 1)[1].split("}", 1)[0]
        m = re.search(r"z-index:\s*(\d+)", block)
        assert m, "у .fly-chip нет z-index"
        assert 70 < int(m.group(1)) < 80

    def test_hidden_pair_exists(self):
        """`.hidden{display:none}` объявлен раньше: у окна пачки должна быть
        парная запись, иначе панель не скрывается."""
        html = _read(MAX_HTML)
        assert ".batch-modal.hidden" in html


# ======================================================================
# 10. Разметка и доступность
# ======================================================================
class TestMarkupAccessibility:
    def test_counters_are_polite_live_region(self):
        """Скринридер сообщает изменение счётчика, но не кричит на каждый чек."""
        assert 'role="status" aria-live="polite"' in _read(APP_JS)
        assert 'aria-live="polite"' in _read(MAX_HTML)

    def test_max_batch_buttons_are_type_button(self):
        html = _read(MAX_HTML)
        for bid in ("btn-batch", "btn-batch-stop", "btn-batch-more",
                    "btn-batch-finish"):
            i = html.index(f'id="{bid}"')
            tag = html[html.rindex("<button", 0, i):html.index(">", i)]
            assert 'type="button"' in tag, bid

    def test_site_batch_buttons_are_type_button(self):
        js = _read(APP_JS)
        for bid in ("pub-batch-start", "pub-batch-stop", "pub-batch-hide"):
            i = js.index(f'id="{bid}"')
            tag = js[js.rindex("<button", 0, i):js.index(">", i)]
            assert 'type="button"' in tag, bid

    def test_panel_has_titles_for_counters(self):
        """У каждого счётчика подпись — цвет не единственный носитель смысла."""
        js = _read(APP_JS)
        block = js.split('id="pub-batch"', 1)[1].split("</div>\n  <div", 1)[0]
        for label in ("принято", "на модерации", "не принято"):
            assert label in block, label


# ======================================================================
# 11. Синтаксис и собираемость
# ======================================================================
class TestSyntax:
    def test_app_js_parses_as_module(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("node недоступен")
        path = Path(tempfile.mkdtemp()) / "app.mjs"
        path.write_text(_read(APP_JS), encoding="utf-8")
        r = subprocess.run([node, "--check", str(path)], capture_output=True,
                           text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stderr[:800]

    def test_maxapp_js_syntax(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("node недоступен")
        r = subprocess.run([node, "--check", str(MAX_JS)], capture_output=True,
                           text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stderr[:800]

    def test_python_modules_compile(self):
        for f in ("app/pool/batch.py", "app/pool/ingest.py",
                  "app/pool/antifraud.py", "app/pool/router_public.py",
                  "app/routers/maxapp.py"):
            py_compile.compile(str(ROOT / f), doraise=True)

    def test_no_stray_scripts_in_texts(self):
        """В русских текстах не должно быть иероглифов/каны: вставка чужого
        письма уже случалась (и в разметке, и в журнале) — страж обязателен."""
        for path in (APP_JS, MAX_JS, CSS, MAX_HTML, "app/pool/batch.py",
                     "app/pool/antifraud.py", "app/pool/ingest.py",
                     "app/pool/router_public.py", "app/routers/maxapp.py",
                     "CHANGELOG.md", "docs/ui_journal.md"):
            bad = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]+", _read(path))
            assert not bad, (path, bad[:3])


# ======================================================================
# 12. Документы: отступление зафиксировано, а не переписано молча
# ======================================================================
class TestDocsDeviation:
    def test_concept_docs_keep_vendor_threshold(self):
        """docs/plan.md и docs/dev_plan_checkpool.md — концептуальные
        документы: порог «20+/час» в них остался."""
        assert "20+" in _read("docs/plan.md")
        assert "20+" in _read("docs/dev_plan_checkpool.md")

    def test_deviation_recorded_in_changelog_and_journal(self):
        assert "FLOOD_PER_HOUR" in _read("CHANGELOG.md")
        assert "FLOOD_PER_HOUR" in _read("docs/ui_journal.md")
        assert "WEB_IP_HOURLY_LIMIT_AUTH" in _read("CHANGELOG.md")

    def test_manual_mentions_batch(self):
        assert "пачк" in _read("app/services/manual_content.py").lower()


# ======================================================================
# 13. Ритуал выпуска
# ======================================================================
class TestRelease17040:
    """v1.70.41: выпуск 1.70.40 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17041, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38 и 1.70.39).
    """

    VERSION = "1.70.40"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read("app/config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 40)

    def test_release_record_stays_in_history(self):
        assert f"## [{self.VERSION}]" in _read("CHANGELOG.md")
        assert f"'{self.VERSION}': [" in _read(APP_JS)
        assert f"## v{self.VERSION} —" in _read("docs/ui_journal.md")
        # механизм пачки остался в репозитории
        assert (ROOT / "app/pool/batch.py").is_file()

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.39': [", 1)[0]
        assert block.count("{ icon:") == 5
        for part in ("пачкой", "100", "дубликат", "оранжевый", "синий",
                     "красный", "анимация", "суточн"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_changelog_entry(self):
        assert f"## [{self.VERSION}]" in _read("CHANGELOG.md")

    def test_journal_entry(self):
        j = _read("docs/ui_journal.md")
        assert f"## v{self.VERSION} —" in j, "в журнале нет раздела выпуска"
        sec = j.split(f"## v{self.VERSION} —", 1)[1].split("## v1.70.39 —", 1)[0]
        for needle in ("FLOOD_PER_HOUR", "HONEYPOT_MS", "pushRecent",
                       "prefers-reduced-motion", "_authed_account"):
            assert needle in sec, f"в журнале нет «{needle}»"

    def test_previous_release_stays_in_history(self):
        """Прокат версии не стирает запись предыдущего выпуска."""
        assert "'1.70.39': [" in _read(APP_JS)
        assert "## [1.70.39]" in _read("CHANGELOG.md")
        assert "## v1.70.39 —" in _read("docs/ui_journal.md")
