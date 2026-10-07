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

import hashlib
import json
import os
import shutil
from datetime import datetime

from ..config import settings

RETENTION = {"daily": 7, "weekly": 2, "preupdate": 5, "manual": 10,
             "archive": 12, "imported": 10}   # v1.49.0: + загруженные из файла


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


def _verify_copy(path: str) -> bool:
    """v1.47.0: каждая копия проверяется (PRAGMA quick_check) — на диск
    и в список попадают только целые, разворачиваемые снимки."""
    import sqlite3
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and row[0] == "ok"


def _snapshot_rows(path: str) -> dict:
    """v1.47.0: состав снимка — количества строк, прочитанные ИЗ САМОЙ
    КОПИИ (не из живой базы): манифест не может разойтись с данными."""
    import sqlite3
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        rows = {}
        for t, label in (("receipts", "receipts"), ("users", "users"),
                         ("companies", "companies")):
            if t in tables:
                rows[label] = con.execute(
                    f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        return rows
    finally:
        con.close()


def _manifest_write(name: str, entry: dict) -> None:
    p = os.path.join(backups_dir(), "manifest.json")
    try:
        data = json.loads(open(p, encoding="utf-8").read()) \
            if os.path.exists(p) else {}
    except (OSError, ValueError):
        data = {}
    data[name] = entry
    alive = {f for f in os.listdir(backups_dir())
             if f.startswith("db-") and f.endswith(".db")}
    data = {k: v for k, v in data.items() if k in alive}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def create_backup(kind: str = "manual") -> str | None:
    """Создать копию БД. kind: manual | daily | weekly | preupdate | archive.
    v1.46.0: база работает в WAL-режиме — простое копирование файла могло
    давать копию БЕЗ последних данных (они жили в db-wal). Теперь снимок
    делается через SQLite Backup API — консистентный при работающем приложении;
    при недоступности API — прежнее копирование файла (лучше, чем ничего).
    v1.47.0: копия проверяется (quick_check) и получает запись в манифесте
    с составом (чеки/пользователи/компании — из самого снимка); сбойная
    копия на диск не остаётся. Недельные и месячные копии обновления не
    трогают: чистка выполняется строго по своему типу."""
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
    if not _verify_copy(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _manifest_write(os.path.basename(dest), {
        "version": settings.APP_VERSION,
        "created": datetime.utcnow().isoformat() + "Z",
        "rows": _snapshot_rows(dest),
        "sha256": h.hexdigest(),
        "verified": True,
    })
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


def _manifest_read() -> dict:
    p = os.path.join(backups_dir(), "manifest.json")
    try:
        data = json.loads(open(p, encoding="utf-8").read())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_backups() -> list[dict]:
    d = backups_dir()
    manifest = _manifest_read()
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
        entry = manifest.get(f) or {}
        items.append({"name": f, "kind": kind,
                      "size_kb": round(os.path.getsize(p) / 1024, 1),
                      "created_at": mtime.isoformat() + "Z",
                      "rows": entry.get("rows"),
                      "copy_version": entry.get("version"),
                      "verified": bool(entry.get("verified")),
                      "source": entry.get("source")})
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def verify_backup(name: str) -> tuple[bool, str]:
    """v1.48.0: проверка копии перед восстановлением — целостность базы
    (PRAGMA quick_check) и совпадение SHA-256 с манифестом (файл не был
    изменён/повреждён после создания)."""
    p = backup_path(name)
    if p is None:
        return False, "Копия не найдена"
    entry = _manifest_read().get(name) or {}
    if not _verify_copy(p):
        return False, "База в копии повреждена (quick_check не пройден)"
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if entry.get("sha256") and entry["sha256"] != digest:
        return False, "Файл копии изменился после создания (хеш не совпал)"
    rows = _snapshot_rows(p)
    return True, ("Копия целая: база прошла проверку, хеш совпадает. "
                  + "В копии: " + ", ".join(
                      f"{k}: {v}" for k, v in rows.items() if v is not None))


def import_backup(tmp_path: str, source_name: str) -> tuple[bool, str, str | None]:
    """v1.49.0: загрузка копии из внешнего источника (файл, скачанный
    раньше кнопкой «⬇»). Файл проверяется как база Ямастер Чек
    (PRAGMA quick_check + наличие основных таблиц), получает имя
    db-imported-*.db, запись в манифесте (sha256, состав, источник) и
    хранится как отдельный тип «загруженная» (10) — архивная история
    (дневные, недельные, месячные) при этом не трогается.
    Возвращает (ok, сообщение, путь файла или None). После успеха
    временный файл перемещается на постоянное место."""
    import sqlite3
    if not (source_name or "").lower().endswith((".db", ".sqlite", ".sqlite3")):
        return False, "Файл должен быть копией базы: .db, .sqlite или .sqlite3", None
    if not os.path.exists(tmp_path):
        return False, "Файл не получен", None
    try:
        con = sqlite3.connect(tmp_path)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return False, f"Файл не читается как база SQLite: {e.__class__.__name__}", None
    if not row or row[0] != "ok":
        return False, (f"База в файле повреждена "
                       f"(quick_check: {row[0] if row else 'нет ответа'})"), None
    if not {"users", "receipts"} <= tables:
        return False, ("В файле нет таблиц Ямастер Чек (users, receipts) — "
                       "похоже, это база другой программы"), None
    d = backups_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(d, f"db-imported-{stamp}.db")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(d, f"db-imported-{stamp}-{n}.db")
    os.replace(tmp_path, dest)
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    rows = _snapshot_rows(dest)
    _manifest_write(os.path.basename(dest), {
        "version": None,
        "created": datetime.utcnow().isoformat() + "Z",
        "rows": rows,
        "sha256": h.hexdigest(),
        "verified": True,
        "method": "import",
        "source": source_name,
    })
    _cleanup("imported")
    msg = ("Копия загружена и проверена: " + os.path.basename(dest)
           + ". В копии: " + ", ".join(f"{k}: {v}" for k, v in rows.items()))
    return True, msg, dest


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
    """Расписание копий (v1.47.0, схема «дед-отец-сын»):
    • каждый день — дневная копия (хранятся 7);
    • каждый понедельник — недельная (хранятся 2);
    • 1-го числа — месячный архив (хранятся 12).
    Архивная история НЕ переписывается обновлениями: копия перед
    обновлением создаётся отдельным типом (preupdate), чистка выполняется
    только среди копий своего типа. Фоновый поток проверяет раз в час."""

    def __init__(self) -> None:
        self._last_date = ""
        self._running = False

    @staticmethod
    def pick_kinds(d: datetime) -> list:
        kinds = ["daily"]
        if d.weekday() == 0:                  # понедельник
            kinds.append("weekly")
        if d.day == 1:                        # первый день месяца
            kinds.append("archive")
        return kinds

    def tick(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today != self._last_date:
            for k in self.pick_kinds(now):
                create_backup(k)
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
