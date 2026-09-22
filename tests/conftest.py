# -*- coding: utf-8 -*-
"""
Ямастер Чек — фикстуры тестов. ООО «Ямастер» | ymaster.ru | info@ymaster.ru

Каждый тестовый прогон использует отдельную временную базу (изолированно от data/).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ymaster-test-")
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


def login(client, username, password):
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
