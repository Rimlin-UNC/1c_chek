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
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

init_db()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_security_state():
    """Между тестами сбрасываем rate-limit и блокировки входа."""
    from app.security import limiter, login_guard
    limiter._hits.clear()
    login_guard._fails.clear()
    login_guard._blocked_until.clear()
    yield
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


def login(client, username, password):
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
