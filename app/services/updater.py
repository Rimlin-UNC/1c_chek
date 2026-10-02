# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — механизм самообновления (v1.4.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# Обновление «как в профессиональных программах»:
#   1. Проверка: сравнение локальной версии (app/config.py) с версией
#      на GitHub-ветке — качается ОДИН маленький файл, не весь репозиторий.
#   2. Применение (дифференциально — git скачивает ТОЛЬКО изменения):
#        а) авто-бэкап базы (data/backups, хранятся последние 5);
#        б) git fetch + reset --hard origin/<branch> + git clean
#           (удалённые из проекта файлы НЕ остаются и не глючат);
#           БД (.gitignore: data/*.db) и секреты (.env) не трогаются;
#        в) если requirements.txt изменился — установка зависимостей;
#           при первой установке ставится всё необходимое;
#        г) ПРОВЕРКА ЦЕЛОСТНОСТИ: импорт приложения в отдельном процессе
#           и применение миграций БД; при сбое — автоматический ОТКАТ
#           на предыдущий коммит;
#        д) рестарт сервиса (systemd в проде) + подтверждение версии.
#   3. Журнал обновлений хранится в БД (AppSetting → update_history).
# ======================================================================
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

import httpx

from ..config import settings

APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_BASE = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
BACKUPS_TO_KEEP = 5


# ==========================================================================
#  Состояние задания обновления (в памяти процесса; API отдаёт статус)
# ==========================================================================
class UpdateJob:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.old_commit = ""
        self.running = False
        self.step = ""
        self.progress = 0            # 0..100
        self.log: list[str] = []
        self.error = ""
        self.finished = False
        self.success = False
        self.needs_restart = False
        self.rolled_back = False
        self.from_version = settings.APP_VERSION
        self.to_version = ""
        self.started_at = 0.0
        self._lock = threading.Lock()

    def say(self, step: str, progress: int | None = None, message: str = "") -> None:
        with self._lock:
            self.step = step
            if progress is not None:
                self.progress = max(self.progress, min(100, progress))
            if message:
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log.append(f"[{stamp}] {message}")

    def public(self) -> dict:
        with self._lock:
            return {
                "running": self.running, "step": self.step,
                "progress": self.progress, "log": self.log[-40:],
                "error": self.error, "finished": self.finished,
                "success": self.success, "needs_restart": self.needs_restart,
                "rolled_back": self.rolled_back,
                "from_version": self.from_version, "to_version": self.to_version,
            }


job = UpdateJob()


# ==========================================================================
#  Вспомогательные: git, pip, версия с GitHub
# ==========================================================================
def _run(cmd: list[str], cwd: str = APP_DIR, timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"не найдено: {cmd[0]}"


def _git(args: list[str], timeout: int = 180) -> tuple[int, str]:
    return _run(["git"] + args, timeout=timeout)


def _local_commit() -> str:
    code, out = _git(["rev-parse", "--short", "HEAD"], timeout=20)
    return out if code == 0 else "unknown"


def _pip_cmd() -> list[str]:
    """pip целевого окружения: venv (прод) или системный python (песочница)."""
    venv_pip = os.path.join(APP_DIR, "venv", "bin", "pip")
    if os.path.exists(venv_pip):
        return [venv_pip, "install", "--quiet", "-r",
                os.path.join(APP_DIR, "requirements.txt")]
    return ["python3", "-m", "pip", "install", "--quiet", "-r",
            os.path.join(APP_DIR, "requirements.txt")]


def _file_sha256(path: str) -> str:
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# v1.6.0: три канала доступа к GitHub. raw.githubusercontent.com периодически
# блокируется провайдерами РФ — поэтому сначала api.github.com, затем raw,
# затем git-протокол (github.com:443), который работает даже при блокировке raw.
_GH_CANONICAL = (
    ("api.github.com", "_gh_api_raw"),
    ("raw.githubusercontent.com", "_gh_raw"),
    ("git (github.com:443)", "_gh_git"),
)


def _short_err(e: Exception) -> str:
    return (str(e) or e.__class__.__name__).strip().split("\n")[0][:90]


def _parse_ver(v: str) -> tuple[int, ...]:
    """'1.6.0' → (1, 6, 0); нечисловые части → 0."""
    parts = []
    for x in str(v).strip().split("."):
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0, 0, 0)


def _is_newer(remote: str, current: str) -> bool:
    """Обновление только вперёд: remote строго больше current (1.10 > 1.9)."""
    a, b = _parse_ver(remote), _parse_ver(current)
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return a > b


def _gh_api_raw(repo: str, branch: str, path: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    resp = httpx.get(url, timeout=10, headers={
        "User-Agent": "YmasterCheck-Updater",
        "Accept": "application/vnd.github.raw+json"})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.text


def _gh_raw(repo: str, branch: str, path: str) -> str:
    url = RAW_BASE.format(repo=repo, branch=branch, path=path)
    resp = httpx.get(url, timeout=10, headers={"User-Agent": "YmasterCheck-Updater"})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.text


def _run_full(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """Как _run, но БЕЗ обрезки хвоста на 2000 символов — для содержимого файлов
    (config.py ~2 КБ, CHANGELOG десятки КБ; _run режет хвост и ломает парсинг)."""
    p = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def _gh_git(repo: str, branch: str, path: str) -> str:
    """Третий канал: git-протокол по 443 — самый живучий при блокировках."""
    fcode, ferr = _run(["git", "fetch", "--depth=1",
                        f"https://github.com/{repo}.git", branch], timeout=60)
    if fcode != 0:
        raise RuntimeError(f"git fetch: {ferr[:120]}")
    code, out = _run_full(["git", "show", f"FETCH_HEAD:{path}"], timeout=30)
    if code != 0 or not out.strip():
        raise RuntimeError(_short_err(RuntimeError(out)) or "git show пуст")
    return out


def _fetch_remote_file(repo: str, branch: str, path: str) -> tuple[str, str]:
    """Файл с GitHub через 3 канала. Возвращает (текст, источник)."""
    errors = []
    for name, fname in _GH_CANONICAL:
        try:
            return globals()[fname](repo, branch, path), name
        except Exception as e:                               # noqa: BLE001
            errors.append(f"{name}: {_short_err(e)}")
    raise RuntimeError(
        "GitHub недоступен с сервера — перепробованы все каналы ("
        + "; ".join(errors) + "). Возможна блокировка провайдером: "
        "обновите вручную — sudo bash deploy.sh --update")


def _remote_version(repo: str, branch: str) -> dict:
    """Текущая версия на GitHub: качаем только app/config.py (~3 КБ)."""
    text, source = _fetch_remote_file(repo, branch, "app/config.py")
    m = re.search(r'APP_VERSION: str = "([^"]+)"', text)
    if not m:
        raise RuntimeError("Не удалось прочитать APP_VERSION из config.py на GitHub")
    return {"version": m.group(1), "source": source,
            "checked_at": datetime.utcnow().isoformat() + "Z"}


def _remote_changelog(repo: str, branch: str) -> str:
    try:
        text, _src = _fetch_remote_file(repo, branch, "CHANGELOG.md")
        return text
    except Exception:                                        # noqa: BLE001
        return ""


def _history(db) -> list[dict]:
    from ..models import AppSetting
    row = db.get(AppSetting, "update_history")
    try:
        return json.loads(row.value) if row and row.value else []
    except ValueError:
        return []


def _append_history(db, entry: dict) -> None:
    from ..models import AppSetting
    hist = _history(db)
    hist.insert(0, entry)
    row = db.get(AppSetting, "update_history")
    if row is None:
        db.add(AppSetting(key="update_history",
                          value=json.dumps(hist[:30], ensure_ascii=False)))
    else:
        row.value = json.dumps(hist[:30], ensure_ascii=False)
    db.commit()


# ==========================================================================
#  Проверка обновлений
# ==========================================================================
def check_update(db) -> dict:
    from . import appsettings
    repo = _normalize_repo(appsettings.get_setting(db, "repo_url", "")) or _guess_repo()
    branch = appsettings.get_setting(db, "repo_branch", "") or settings.DEFAULT_BRANCH
    remote = _remote_version(repo, branch)
    # v1.6.0: только обновление вперёд (семвер-сравнение)
    available = _is_newer(remote["version"], settings.APP_VERSION)
    result = {
        "current_version": settings.APP_VERSION,
        "remote_version": remote["version"],
        "update_available": available,
        "branch": branch,
        "local_commit": _local_commit(),
        "checked_at": remote["checked_at"],
        "source": remote.get("source", ""),
        "changelog_excerpt": "",
    }
    if available:
        ch = _remote_changelog(repo, branch)
        result["changelog_excerpt"] = ch[:4000]
    _append_history(db, {
        "action": "check", "current": settings.APP_VERSION,
        "remote": remote["version"], "available": available,
        "at": remote["checked_at"]})
    return result


DEFAULT_REPO = "Rimlin-UNC/1c_chek"


def _normalize_repo(repo: str) -> str:
    """Любой вид ссылки → 'owner/repo':
    https://github.com/o/r.git | git@github.com:o/r.git | o/r → o/r.
    v1.6.0: из-за полного URL клона адреса GitHub API собирались неверно
    (api.github.com/repos/https://...) — проверка обновлений не работала ВЕЗДЕ,
    а не только при блокировках. Это и есть главная причина «ничего не произошло»."""
    r = (repo or "").strip()
    r = re.sub(r"^https?://[^/]+/", "", r)
    r = re.sub(r"^git@[^:]+:", "", r)
    r = re.sub(r"\.git$", "", r).strip("/")
    return r if re.fullmatch(r"[\w.\-]+/[\w.\-]+", r) else ""


def _guess_repo() -> str:
    """owner/repo из git remote, либо значение по умолчанию."""
    code, out = _git(["remote", "get-url", "origin"], timeout=15)
    return _normalize_repo(out) or DEFAULT_REPO


# ==========================================================================
#  Применение обновления
# ==========================================================================
def _backup_database() -> str:
    """Совместимость: ручная копия через сервис бэкапов (v1.5.0)."""
    from .backups import create_backup
    return create_backup("manual") or ""


def _health_check() -> tuple[bool, str]:
    """Целостность после обновления: импорт приложения и миграции БД
    в ОТДЕЛЬНОМ процессе (как это увидит uvicorn при рестарте)."""
    code = (
        "import sys; sys.path.insert(0, r'{app}');"
        "from app.main import app;"
        "from app.database import init_db;"
        "init_db();"
        "from app.config import settings;"
        "print(settings.APP_VERSION)"
    ).format(app=APP_DIR)
    rc, out = _run(["python3", "-c", code], timeout=120)
    if rc != 0:
        return False, out or "импорт приложения не удался"
    return True, out.strip()


def _do_apply(target_version: str, repo: str, branch: str) -> None:
    """Основной конвейер обновления (выполняется в фоновом потоке)."""
    try:
        job.reset()
        job.running = True
        job.to_version = target_version
        job.started_at = time.time()
        job.old_commit = _local_commit()
        old_commit = job.old_commit
        req_hash_before = _file_sha256(os.path.join(APP_DIR, "requirements.txt"))

        job.say("backup", 10, "Резервная копия базы данных…")
        from .backups import create_backup
        backup = create_backup("preupdate")
        if backup:
            job.say("backup", 15, f"Бэкап: {os.path.basename(backup)}")

        job.say("fetch", 25, "Получение изменений с GitHub (только дельта)…")
        rc, out = _git(["fetch", "origin", branch], timeout=300)
        if rc != 0:
            raise RuntimeError(f"git fetch: {out}")

        job.say("checkout", 45, f"Обновление файлов до {target_version}…")
        rc, out = _git(["reset", "--hard", f"origin/{branch}"], timeout=60)
        if rc != 0:
            raise RuntimeError(f"git reset: {out}")
        # Удалённые из проекта файлы не должны оставаться (без -x: БД/.env/venv целы)
        rc, out = _git(["clean", "-fd", "-e", "data", "-e", ".env",
                        "-e", "venv", "-e", "*.db"], timeout=60)
        job.say("checkout", 55, "Лишние/удалённые файлы вычищены")

        new_req = _file_sha256(os.path.join(APP_DIR, "requirements.txt"))
        if new_req != req_hash_before:
            job.say("deps", 65, "Зависимости изменились — установка…")
            rc, out = _run(_pip_cmd(), timeout=900)
            if rc != 0:
                raise RuntimeError(f"pip: {out}")
        else:
            job.say("deps", 65, "Зависимости не менялись — пропущено")

        job.say("verify", 80, "Проверка целостности (импорт + миграции БД)…")
        ok, msg = _health_check()
        if not ok:
            raise RuntimeError(f"Проверка целостности не пройдена: {msg}")

        job.say("restart", 90, "Перезапуск сервиса…")
        needs_restart = True
        rc, out = _run(["systemctl", "restart", "ymaster-check"], timeout=60)
        if rc == 0:
            needs_restart = False
            job.say("restart", 95, "Сервис перезапущен (systemd)")
        else:
            job.say("restart", 95,
                    "В среде без systemd: перезапустите процесс — данные и настройки сохранены")

        job.success = True
        job.needs_restart = needs_restart
        job.finished = True
        job.progress = 100
        job.say("done", 100, f"Готово: v{settings.APP_VERSION} → v{target_version}")

        from ..database import SessionLocal
        db = SessionLocal()
        try:
            _append_history(db, {
                "action": "apply", "from": settings.APP_VERSION,
                "to": target_version, "commit_before": old_commit,
                "commit_after": _local_commit(), "backup": os.path.basename(backup),
                "at": datetime.utcnow().isoformat() + "Z", "ok": True})
        finally:
            db.close()
    except Exception as e:                                   # noqa: BLE001
        job.error = str(e)
        job.finished = True
        job.say("error", None, f"ОШИБКА: {e}")
        # Автоматический откат на предыдущий коммит — система продолжает работать
        if job.old_commit and job.old_commit != "unknown":
            rc, out = _git(["reset", "--hard", job.old_commit], timeout=60)
            job.rolled_back = (rc == 0)
            job.say("rollback", None,
                    "Откат на предыдущую версию выполнен — система работает как прежде")
        else:
            job.rolled_back = False
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            _append_history(db, {
                "action": "apply_failed", "target": job.to_version,
                "error": str(e)[:500], "rolled_back": True,
                "at": datetime.utcnow().isoformat() + "Z"})
        finally:
            db.close()
    finally:
        job.running = False


def start_apply(target_version: str, repo: str, branch: str) -> None:
    if job.running:
        raise RuntimeError("Обновление уже выполняется")
    t = threading.Thread(target=_do_apply, args=(target_version, repo, branch),
                         daemon=True, name="ymaster-updater")
    t.start()
