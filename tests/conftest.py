# -*- coding: utf-8 -*-
"""
Ямастер Чек — фикстуры тестов. ООО «Ямастер» | ymaster.ru | chek@ymaster.ru

Каждый тестовый прогон использует отдельную временную базу (изолированно от data/).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ymaster-test-")
os.environ["YM_NO_REEXEC"] = "1"   # v1.17.0: тесты не должны ре-исполнять процесс
os.environ["YM_FETCHQ_OFF"] = "1"  # v1.68.0: очередь загрузки off — детерминизм
os.environ["YM_DOCREQ_OFF"] = "1"  # v1.70.97: тик напоминаний клиентам — тесты вызывают его сами
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


class Store:
    """База для прогона на двух СУБД (v1.70.113 — общая для согласований и сверки выписки): name и фабрика сессий."""

    def __init__(self, name, session):
        self.name, self.session = name, session


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request):
    """SQLite тестов или одноразовая схема PostgreSQL (get_db подменён на её сессии). PostgreSQL-вариант — в CI и
    qa/pg_local.py (UNIFIED_POSTGRES_TEST_URL); там ворота требуют ноль пропусков."""
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base, SessionLocal, get_db
    if request.param == "sqlite":
        yield Store("SQLite", SessionLocal)
        return
    url = os.getenv("UNIFIED_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("PostgreSQL-вариант: нужен UNIFIED_POSTGRES_TEST_URL (CI, qa/pg_local.py)")
    schema = "qa_store_" + uuid4().hex
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен вести в PostgreSQL")
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    pg_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def pg_db():
        db = pg_session()
        try:
            yield db
        finally:
            db.close()

    try:
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = pg_db
        yield Store("PostgreSQL", pg_session)
    finally:
        app.dependency_overrides.pop(get_db, None)
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_security_state():
    """Между тестами сбрасываем rate-limit и блокировки входа."""
    from app.security import limiter, login_guard
    from app.services import max_login
    max_login._ip_times.clear()
    limiter._hits.clear()
    login_guard._fails.clear()
    login_guard._blocked_until.clear()
    yield
    max_login._ip_times.clear()
    limiter._hits.clear()
    login_guard._fails.clear()
    login_guard._blocked_until.clear()


@pytest.fixture(autouse=True)
def _isolate_app_settings():
    """v1.70.39: настройки приложения не должны протекать между тестами.

    Таблица app_settings общая на весь сеанс прогона, а тесты настраивают в ней
    SMTP, токен бота MAX, Чек-Пул, ключи Checko и вебхуки. Раньше результат
    проверки зависел от того, что оставил сосед: так появлялись плавающие
    падения — test_magic_requires_smtp получал 200 вместо 400, когда SMTP
    настроил чужой тест, test_should_run_requires_enabled_and_token и
    test_start_link_public_nick спотыкались о чужой max_bot_token,
    test_card_stats_and_settings — о чужие настройки карты.

    Снимок делается до теста и восстанавливается после: значения сохраняются
    как есть, в том числе зашифрованные «enc:v1:…» из SECRET_KEYS, поэтому
    шифрование секретов не затрагивается. Кэш имени бота в памяти процесса
    сбрасывается — иначе восстановление настройки было бы не видно.
    """
    from app.database import SessionLocal
    from app.models import AppSetting

    def _snapshot():
        sess = SessionLocal()
        try:
            return {r.key: r.value for r in sess.query(AppSetting).all()}
        finally:
            sess.rollback(); sess.close()

    before = _snapshot()
    yield
    after = _snapshot()
    if after != before:
        sess = SessionLocal()
        try:
            for row in sess.query(AppSetting).all():
                if row.key not in before:
                    sess.delete(row)
                elif row.value != before[row.key]:
                    row.value = before[row.key]
            for key, value in before.items():
                if sess.get(AppSetting, key) is None:
                    sess.add(AppSetting(key=key, value=value))
            sess.commit()
        finally:
            sess.rollback(); sess.close()
    # кэш имени бота MAX живёт в памяти процесса: если его не сбросить,
    # восстановленная настройка останется невидимой для следующих тестов
    from app.services import max_bot
    max_bot._username_cache = ""


def drop_jar_cookie(client, name: str) -> None:
    """Убрать из банка сессионного клиента все cookie с этим именем.

    Явную cookie запроса (`cookies=`) httpx склеивает с банком клиента. Если
    там уже лежит одноимённая cookie другого домена (её выдал сервер в
    соседнем тесте), в заголовок уходят обе, а сервер берёт последнюю —
    порядок зависит от того, какие тесты шли раньше в этом процессе.
    Перед запросом с явной cookie зовите этот помощник.
    """
    for cookie in list(client.cookies.jar):
        if cookie.name == name:
            client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)


def login(client, username, password):
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
