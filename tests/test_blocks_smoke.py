# -*- coding: utf-8 -*-
# Смоук-пробег по всем блокам (v1.46.1) — ООО «Ямастер» | ymaster.ru
import pytest
from tests.conftest import login

ADMIN_GETS = [
    ("/api/v1/auth/me", "вход"),
    ("/api/v1/dashboard/stats", "Дашборд: статистика"),
    ("/api/v1/dashboard/recent", "Дашборд: последние чеки"),
    ("/api/v1/receipts", "База чеков: список"),
    ("/api/v1/receipts/creators", "База чеков: кассиры"),
    ("/api/v1/receipts/categories", "База чеков: категории"),
    ("/api/v1/settings/mapping", "Маппинг реквизитов"),
    ("/api/v1/settings/mapping/catalog", "Маппинг: каталог"),
    ("/api/v1/users", "Пользователи"),
    ("/api/v1/invites", "Приглашения"),
    ("/api/v1/admin/system", "Система: статус"),
    ("/api/v1/settings/app", "Настройки приложения"),
    ("/api/v1/settings/fns", "Настройки ФНС"),
    ("/api/v1/settings/onec", "Настройки 1С"),
    ("/api/v1/settings/external", "Внешние проверки"),
    ("/api/v1/settings/checko", "Checko"),
    ("/api/v1/settings/telegram", "Telegram-бот"),
    ("/api/v1/companies", "Компании"),
    ("/api/v1/admin/update/check", "Обновления: проверка"),
    ("/api/v1/admin/update/preflight", "Обновления: готовность"),
    ("/api/v1/admin/update/status", "Обновления: статус"),
    ("/api/v1/admin/update/changelog", "Обновления: история версий"),
    ("/api/v1/admin/update/releases", "Последние рабочие версии"),
    ("/api/v1/admin/backups", "Резервные копии"),
    ("/api/v1/pool-admin/overview", "Чек-Пул: сводка"),
    ("/api/v1/pool-admin/receipts", "Чек-Пул: чеки"),
    ("/api/v1/pool-admin/dicts", "Чек-Пул: справочники"),
    ("/api/v1/pool-admin/participants", "Чек-Пул: участники"),
    ("/api/v1/pool-admin/smtp", "Чек-Пул: SMTP"),
    ("/api/v1/pool-admin/api-keys", "Чек-Пул: ключи API"),
    ("/api/v1/mail-admin/config", "Почта: конфигурация"),
    ("/api/v1/mail-admin/rules", "Почта: правила"),
    ("/api/v1/mail-admin/log", "Почта: журнал"),
    ("/api/v1/mail-admin/suppressed", "Почта: отписки"),
    ("/api/v1/public/pool/info", "Гостям: статус пула"),
    ("/api/v1/public/pool/leaderboard", "Лидерборд"),
    ("/api/v1/public/pool/partners", "Партнёры и кэшбэк"),
    ("/api/v1/manual", "Инструкция"),
]


@pytest.mark.parametrize("path,block", ADMIN_GETS)
def test_block_responds(client, path, block):
    adm = login(client, "admin", "admin123")
    r = client.get(path, headers=adm)
    assert r.status_code == 200, f"{block} ({path}): HTTP {r.status_code} {r.text[:200]}"


def test_role_gates(client):
    """Бухгалтеру админ-блоки и опасные мутации закрыты; гость — ничего."""
    adm = login(client, "admin", "admin123")
    r = client.post("/api/v1/companies", headers=adm, json={
        "name": "Смоук ООО", "inn": "7801234564"})
    assert r.status_code in (200, 201), r.text
    cid = r.json().get("id") or r.json().get("company", {}).get("id")
    inv = client.post("/api/v1/invites", headers=adm,
                      json={"role": "accountant", "company_id": cid}).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": "smoke_acc",
        "password": "parol123", "full_name": "Смоук Бухгалтер"})
    assert r.status_code in (200, 201), r.text
    acc = {"Authorization": "Bearer " + r.json()["access_token"]}
    for path in ("/api/v1/admin/backups", "/api/v1/admin/update/releases",
                 "/api/v1/users", "/api/v1/pool-admin/overview",
                 "/api/v1/mail-admin/config"):
        resp = client.get(path, headers=acc)
        assert resp.status_code in (401, 403), \
            f"{path}: бухгалтер получил {resp.status_code}"
    for path, body in (("/api/v1/admin/backups/restore", {"name": "x.db"}),
                       ("/api/v1/admin/update/rollback",
                        {"version": "1.0.0", "commit": "a" * 40}),
                       ("/api/v1/admin/backups", None)):
        resp = (client.post(path, headers=acc, json=body) if body is not None
                else client.post(path, headers=acc))
        assert resp.status_code in (401, 403), \
            f"POST {path}: бухгалтер получил {resp.status_code}"
    assert client.get("/api/v1/admin/backups").status_code in (401, 403)
