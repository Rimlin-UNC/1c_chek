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
    """Создать копию БД. kind: manual | daily | preupdate | archive.
    v1.46.0: база работает в WAL-режиме — простое копирование файла могло
    давать копию БЕЗ последних данных (они жили в db-wal). Теперь снимок
    делается через SQLite Backup API — консистентный при работающем приложении;
    при недоступности API — прежнее копирование файла (лучше, чем ничего)."""
    import sqlite3
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
    try:
        scon = sqlite3.connect(src)
        try:
            dcon = sqlite3.connect(dest)
            try:
                scon.backup(dcon)
            finally:
                dcon.close()
        finally:
            scon.close()
    except sqlite3.Error:
        shutil.copy2(src, dest)          # запасной путь — как в v1.5.0
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


def restore_backup(name: str) -> tuple[bool, str]:
    """v1.46.0: восстановить базу из копии — прямо из приложения.
    Страховка: свежая копия текущей базы (preupdate), проверка целостности
    копии (PRAGMA quick_check) на временном файле, атомарная замена.
    Возвращает (ok, сообщение)."""
    import sqlite3
    src = backup_path(name)
    if src is None:
        return False, "Копия не найдена"
    dst = _db_path()
    if not os.path.exists(dst):
        return False, "Файл рабочей базы не найден"
    safety = create_backup("preupdate")     # страховая копия «как есть сейчас»
    tmp = os.path.join(backups_dir(), f"db-restore-tmp-{os.getpid()}.db")
    try:
        shutil.copy2(src, tmp)
        try:
            con = sqlite3.connect(tmp)
            try:
                row = con.execute("PRAGMA quick_check").fetchone()
            finally:
                con.close()
        except sqlite3.DatabaseError as e:
            return False, f"Копия повреждена (не читается как база: {e})"
        if not row or row[0] != "ok":
            return False, f"Копия повреждена (quick_check: {row[0] if row else 'нет ответа'})"
        # устаревшие журналы WAL/SHM не должны пережить замену базы
        for tail in ("-wal", "-shm"):
            try:
                os.remove(dst + tail)
            except OSError:
                pass
        os.replace(tmp, dst)                # атомарно (data/ — тот же диск)
        # сбрасываем пул соединений: старые держат заменённый файл
        try:
            from ..database import engine
            engine.dispose()
        except Exception:                   # noqa: BLE001
            pass
    except Exception as e:                  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"{e.__class__.__name__}: {e}"
    who = os.path.basename(safety) if safety else "не создана"
    return True, ("База восстановлена из " + name +
                  f". Страховая копия прежней базы: {who}. "
                  "Перезапустите приложение, чтобы все данные подхватились: "
                  "sudo systemctl restart ymaster-check")


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
