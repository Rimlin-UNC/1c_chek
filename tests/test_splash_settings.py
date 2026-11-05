"""Длительность заставки загрузки (v1.70.100): 0–60 секунд, по умолчанию 5, меняет только администратор.

Значение отдаёт публичный /api/v1/about (заставка идёт до входа); журнал действий фиксирует изменение.
"""
from __future__ import annotations

from app.database import SessionLocal
from app.services import appsettings, audit_human, splash
from tests.conftest import login
from tests.test_v1140 import _mk_user


def test_admin_sets_duration_public_about_shows_it(client):
    admin = login(client, "admin", "admin123")
    try:
        got = client.get("/api/v1/settings/splash", headers=admin).json()
        assert {k: got[k] for k in ("default", "min", "max")} == {"default": 5, "min": 0, "max": 60}
        saved = client.put("/api/v1/settings/splash", headers=admin, json={"seconds": 12})
        assert saved.status_code == 200 and saved.json() == {
            "seconds": 12, "mode": "длинная — этапы, прогресс и подсказки о возможностях программы"}
        assert client.get("/api/v1/about").json()["splash_seconds"] == 12          # без входа
        assert client.put("/api/v1/settings/splash", headers=admin, json={"seconds": 0}).json()["mode"] == "без заставки"
        assert client.get("/api/v1/about").json()["splash_seconds"] == 0
        for wrong in (61, -1, "пять"):
            assert client.put("/api/v1/settings/splash", headers=admin, json={"seconds": wrong}).status_code == 422
    finally:
        client.put("/api/v1/settings/splash", headers=admin, json={"seconds": 5})
    assert client.get("/api/v1/about").json()["splash_seconds"] == 5


def test_only_admin_changes_duration(client):
    admin = login(client, "admin", "admin123")
    comp = client.post("/api/v1/companies", headers=admin, json={"name": "ООО Заставка 1"}).json()
    acc = _mk_user(client, admin, comp["id"], "splash_acc_1", role="accountant")
    assert client.get("/api/v1/settings/splash", headers=acc).status_code == 403
    assert client.put("/api/v1/settings/splash", headers=acc, json={"seconds": 9}).status_code == 403
    client.cookies.clear()                                  # без входа (cookie сессии после регистрации — долой)
    assert client.get("/api/v1/settings/splash").status_code == 401


def test_service_bounds_modes_and_broken_value():
    assert [splash.mode(v) for v in (0, 2, 5, 8)] == [
        "без заставки", "короткая — прогресс без списка этапов", "обычная — этапы загрузки и прогресс",
        "длинная — этапы, прогресс и подсказки о возможностях программы"]
    with SessionLocal() as db:
        before = appsettings.get_setting(db, splash.KEY, "")
        try:
            appsettings.set_setting(db, splash.KEY, "не число")
            assert splash.seconds(db) == 5
            appsettings.set_setting(db, splash.KEY, "600")
            assert splash.seconds(db) == 60                                         # не больше минуты
        finally:
            appsettings.set_setting(db, splash.KEY, before or "5")


def test_audit_phrase_splash():
    assert audit_human.humanize_audit("splash_settings_updated", {"seconds": 12, "before": 5}) == (
        "изменена длительность заставки загрузки", "было: 5 с, стало: 12 с")
    assert audit_human.humanize_audit("splash_settings_updated", {"seconds": 0, "before": 5})[1] == \
        "было: 5 с, стало: без заставки"
