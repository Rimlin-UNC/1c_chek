# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — резервное копирование (v1.5.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# Уровни копий (data/backups): daily (7) / preupdate (5) / manual (10) /
# archive-ГГГГММ — архив месяца (12). Копии вне git и вне git clean.
# ======================================================================
from __future__ import annotations

import os
import shutil
from datetime import datetime

from ..config import settings

RETENTION = {"daily": 7, "preupdate": 5, "manual": 10, "archive": 12}


def _app_dir() -> str:
    from .updater import APP_DIR
    return APP_DIR


def _db_path() -> str:
    url = settings.DATABASE_URL
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "")
    return os.path.join(_app_dir(), "data", "ymaster_check.db")


def backups_dir() -> str:
    d = os.path.join(_app_dir(), "data", "backups")
    os.makedirs(d, exist_ok=True)
    return d


def create_backup(kind: str = "manual") -> str | None:
    """Создать копию БД. kind: manual | daily | preupdate | archive."""
    src = _db_path()
    if not os.path.exists(src):
        return None
    d = backups_dir()
    if kind == "archive":
        dest = os.path.join(d, f"db-archive-{datetime.now().strftime('%Y%m')}.db")
        if os.path.exists(dest):
            return dest
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(d, f"db-{kind}-{stamp}.db")
    shutil.copy2(src, dest)
    _cleanup(kind)
    return dest


def _cleanup(kind: str) -> None:
    keep = RETENTION.get(kind, 5)
    prefix = f"db-{kind}-"
    files = sorted(f for f in os.listdir(backups_dir()) if f.startswith(prefix))
    for old in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(backups_dir(), old))
        except OSError:
            pass


def list_backups() -> list[dict]:
    d = backups_dir()
    items = []
    for f in os.listdir(d):
        if not (f.startswith("db-") and f.endswith(".db")):
            continue
        p = os.path.join(d, f)
        stem = f[3:-3]
        kind = stem.split("-", 1)[0] if stem else "manual"
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(p))
        except OSError:
            continue
        items.append({"name": f, "kind": kind,
                      "size_kb": round(os.path.getsize(p) / 1024, 1),
                      "created_at": mtime.isoformat() + "Z"})
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def backup_path(name: str) -> str | None:
    if "/" in name or "\\" in name or not name.startswith("db-") or not name.endswith(".db"):
        return None
    p = os.path.join(backups_dir(), name)
    return p if os.path.exists(p) else None


class DailyBackupGuard:
    """Ежедневная копия: фоновый поток раз в час проверяет «копия за сегодня?»."""

    def __init__(self) -> None:
        self._last_date = ""
        self._running = False

    def tick(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_date:
            create_backup("daily")
            create_backup("archive")
            self._last_date = today

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        def loop():
            import time
            time.sleep(20)
            while True:
                try:
                    self.tick()
                except Exception:
                    pass
                time.sleep(3600)

        import threading
        threading.Thread(target=loop, daemon=True, name="ymaster-daily-backup").start()


daily_backup = DailyBackupGuard()
