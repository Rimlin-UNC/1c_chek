# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Конфигурация приложения (переменные окружения / файл .env).
"""
from __future__ import annotations

import os
from pathlib import Path

# Базовая директория проекта
BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    """Чтение переменной из окружения с приоритетом файла .env."""
    val = os.environ.get(key)
    if val is not None:
        return val
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return default


class Settings:
    """Глобальные настройки системы «Ямастер Чек»."""

    APP_NAME: str = "Ямастер Чек"
    APP_VERSION: str = "1.0.0"
    VENDOR: str = "ООО «Ямастер»"
    VENDOR_SITE: str = "https://ymaster.ru"
    VENDOR_EMAIL: str = "info@ymaster.ru"

    # --- Безопасность -------------------------------------------------
    SECRET_KEY: str = _env("SECRET_KEY", "ymaster-dev-secret-change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(_env("ACCESS_TOKEN_EXPIRE_MINUTES", "720"))  # 12 часов

    # --- База данных ----------------------------------------------------
    # SQLite по умолчанию (работает из коробки), PostgreSQL для production:
    #   DATABASE_URL=postgresql+psycopg2://check:pass@localhost:5432/ymaster_check
    DATABASE_URL: str = _env(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'data' / 'ymaster_check.db'}",
    )

    # --- Интеграция с ФНС ------------------------------------------------
    # Провайдер проверки чеков: "mock" (демо) | "fns" (реальное API ФНС)
    FNS_PROVIDER: str = _env("FNS_PROVIDER", "mock")
    FNS_API_BASE: str = _env("FNS_API_BASE", "https://openapi.nalog.ru:8090")
    FNS_MASTER_TOKEN: str = _env("FNS_MASTER_TOKEN", "")
    FNS_CLIENT_APP_ID: str = _env("FNS_CLIENT_APP_ID", "YMASTER-CHECK")
    FNS_CACHE_TTL_DAYS: int = int(_env("FNS_CACHE_TTL_DAYS", "30"))
    FNS_TIMEOUT_SECONDS: int = int(_env("FNS_TIMEOUT_SECONDS", "15"))

    # --- Интеграция с 1С ---------------------------------------------------
    # Токен, по которому 1С обращается к сервису выгрузки (endpoint /onec/v1/*)
    ONEC_API_TOKEN: str = _env("ONEC_API_TOKEN", "ymaster-onec-token-change-me")
    ONEC_BATCH_SIZE: int = int(_env("ONEC_BATCH_SIZE", "100"))

    # --- Общие -----------------------------------------------------------
    CORS_ORIGINS: list[str] = _env("CORS_ORIGINS", "*").split(",")
    MAX_UPLOAD_MB: int = int(_env("MAX_UPLOAD_MB", "12"))
    FIRST_RUN_SEED: bool = _env("FIRST_RUN_SEED", "1") == "1"


settings = Settings()

# Директория хранения данных
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
