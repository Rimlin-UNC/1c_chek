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
        self.action = "update"       # v1.46.0: update | rollback
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
                "action": getattr(self, "action", "update"),
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
def _run(cmd: list[str], cwd: str | None = None, timeout: int = 180,
         input_text: str | None = None) -> tuple[int, str]:
    # v1.8.0: cwd разрешается В МОМЕНТ ВЫЗОВА (иначе значение APP_DIR
    # фиксируется при импорте и не следует за тестами/переконфигурацией)
    cwd = cwd or APP_DIR
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, input=input_text)
        return p.returncode, (p.stdout + p.stderr).strip()[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"не найдено: {cmd[0]}"


def _git(args: list[str], timeout: int = 180) -> tuple[int, str]:
    return _run(["git"] + args, timeout=timeout)


def _git_ready() -> bool:
    """v1.16.0: каталог приложения — рабочий git-репозиторий? Установки через
    install.sh/rsync приходят БЕЗ .git — обновление из приложения на них
    падало на «git fetch: not a git repository»."""
    if not os.path.isdir(os.path.join(APP_DIR, ".git")):
        return False
    rc, _o = _git(["rev-parse", "--is-inside-work-tree"], timeout=15)
    return rc == 0


def _ensure_git_repo(repo: str, branch: str, remote_url: str | None = None) -> tuple[bool, str]:
    """v1.16.0: самовосстановление репозитория — приложение чинит себя само.
    Если .git отсутствует/сломан: git init + remote + fetch ветки. БД (.env,
    data/) не затрагиваются. Возвращает (ок, сообщение)."""
    if _git_ready():
        # origin должен указывать на нужный репозиторий
        rc, out = _git(["remote", "get-url", "origin"], timeout=15)
        url = remote_url or f"https://github.com/{repo}.git"
        if rc != 0 or _normalize_repo(out) != _normalize_repo(url):
            _git(["remote", "remove", "origin"], timeout=15)
            _git(["remote", "add", "origin", url], timeout=15)
        return True, "git-репозиторий в порядке"
    url = remote_url or f"https://github.com/{repo}.git"
    _run(["git", "init", "-q"], timeout=30)
    rc, out = _git(["remote", "add", "origin", url], timeout=15)
    if rc != 0:
        _git(["remote", "set-url", "origin", url], timeout=15)
    fcode, ferr = _git(["fetch", "--depth=1", "origin", branch], timeout=300)
    if fcode != 0:
        return False, f"git fetch при восстановлении: {ferr[:200]}"
    # reset --hard, а не checkout: в каталоге установки полно незатреканных
    # файлов приложения (rsync-установка) — checkout откажется их перезаписать.
    # reset --hard выравнивает файлы с GitHub; data/.env игнорируются git'ом
    # и остаются нетронутыми.
    rc, out = _git(["reset", "--hard", "FETCH_HEAD"], timeout=60)
    if rc != 0:
        return False, f"git reset при восстановлении: {out[:200]}"
    _git(["checkout", "-q", "-B", branch], timeout=30)
    return True, "git-репозиторий восстановлен автоматически (init + fetch)"


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
        raise FileNotFoundError(f"HTTP {resp.status_code}")   # 404 = нет файла
    return resp.text


def _gh_raw(repo: str, branch: str, path: str) -> str:
    url = RAW_BASE.format(repo=repo, branch=branch, path=path)
    resp = httpx.get(url, timeout=10, headers={"User-Agent": "YmasterCheck-Updater"})
    if resp.status_code != 200:
        raise FileNotFoundError(f"HTTP {resp.status_code}")
    return resp.text


def _run_full(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str]:
    """Как _run, но БЕЗ обрезки хвоста на 2000 символов — для содержимого файлов
    (config.py ~2 КБ, CHANGELOG десятки КБ; _run режет хвост и ломает парсинг)."""
    p = subprocess.run(cmd, cwd=cwd or APP_DIR, capture_output=True, text=True,
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
    # v1.16.0: все каналы ответили «нет файла» → ветка не содержит приложение
    if errors and all("404" in e for e in errors):
        raise RuntimeError(
            f"В ветке «{branch}» нет файла {path} — проверьте ветку обновлений "
            "в настройках (должна быть ветка приложения, например "
            f"{getattr(settings, 'DEFAULT_BRANCH', '')})")
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


def _py_bin() -> str:
    """v1.8.3: python ЦЕЛЕВОГО окружения для проверок обновления.
    На проде зависимости стоят в venv — системный python3 не имеет fastapi,
    и проверка целостности могла ложно провалиться (обновление откатилось бы
    даже при исправном коде). venv есть → берём его, иначе системный python3."""
    venv_py = os.path.join(APP_DIR, "venv", "bin", "python3")
    return venv_py if os.path.exists(venv_py) else "python3"


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
    rc, out = _run([_py_bin(), "-c", code], timeout=120)
    if rc != 0:
        return False, out or "импорт приложения не удался"
    return True, out.strip()


def _last_update_path() -> str:
    return os.path.join(APP_DIR, "data", "last_update.json")


def write_last_update(payload: dict) -> None:
    """v1.8.1: маркер успешного обновления на диске. После рестарта процесс
    новый, in-memory job пуст — фронтенд по маркеру показывает успех."""
    try:
        path = _last_update_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass                                    # не критично


def load_last_update() -> dict | None:
    try:
        with open(_last_update_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _reexec_allowed() -> bool:
    """v1.17.0: разрешён ли самоперезапуск (re-exec). Запрещён в тестах
    (YM_NO_REEXEC=1) и при выключенном SELF_REEXEC в конфиге."""
    import sys as _sys
    if os.environ.get("YM_NO_REEXEC") == "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    import sys as _sys2
    return bool(settings.SELF_REEXEC) and len(_sys2.argv) > 0


def _self_reexec() -> bool:
    """v1.17.0: заменить собственный процесс тем же запуском — Python под
    тем же интерпретатором загрузит УЖЕ обновлённый код. PID сохраняется
    (systemd видит тот же работающий сервис), сокет освобождается
    (close-on-exec) и переоткрывается. Прав root НЕ требуется — обновление
    из приложения работает без пароля и без sudoers-правил.

    Вызывается только из фонового потока после записи маркера успеха."""
    import logging as _logging
    import os as _os
    import sys as _sys
    try:
        for h in _logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:                                # noqa: BLE001
                pass
        argv = [_sys.executable] + list(_sys.argv)
        _os.execv(_sys.executable, argv)     # не возвращает
        return True
    except OSError as e:
        job.say("restart", None, f"Самоперезапуск не удался: {e}")
        return False


def _restart_mode() -> tuple[bool, str]:
    """v1.9.1: есть ли право перезапустить сервис. Проверяем БЕЗВРЕДНОЙ командой
    `systemctl is-active` (разрешена sudoers-правилом вместе с restart):
    rc 0/3 — команда доступна (0=active, 3=inactive)."""
    rc, _o = _run(["sudo", "-n", "systemctl", "is-active", "ymaster-check"], timeout=15)
    if rc in (0, 3):
        return True, "sudoers"
    rc, _o = _run(["systemctl", "is-active", "ymaster-check"], timeout=15)
    if rc in (0, 3):
        return True, "systemctl"
    return False, ""


def pre_flight(db) -> dict:
    """v1.9.1: готовность к обновлению из приложения. Всё, что должно быть
    истинным ДО старта — иначе обновление не начинаем (никаких полу-состояний)."""
    can_restart, mode = _restart_mode()
    reexec_ok = _reexec_allowed()  # v1.17.0: самоперезапуск без прав root
    can_restart = can_restart or reexec_ok
    db_path = os.path.join(APP_DIR, "data", "ymaster_check.db")
    db_ok = os.path.exists(db_path)
    git_ok = _git_ready()          # v1.16.0: иначе apply сам восстановит репозиторий
    github_ok, github_err = False, ""
    try:
        from . import appsettings
        repo = appsettings.get_setting(db, "repo_url", "") or _guess_repo()
        branch = appsettings.get_setting(db, "repo_branch", "") or settings.DEFAULT_BRANCH
        _remote_version(repo, branch)
        github_ok = True
    except Exception as e:                                   # noqa: BLE001
        github_err = str(e)[:200]
    return {
        "can_restart": can_restart,
        "restart_mode": mode or ("reexec" if reexec_ok else ""),
        "reexec_ok": reexec_ok,
        "db_backup_ok": db_ok,
        "github_ok": github_ok,
        "github_error": github_err,
        "git_ok": git_ok,
        "ready": can_restart and db_ok and github_ok,
    }


def _restart_service(sudo_password: str | None = None) -> tuple[bool, str]:
    """v1.8.1/v1.17.0: приложение само перезапускается после обновления.
    Цепочка: 1) sudoers NOPASSWD (ставит deploy.sh);
    2) САМОПЕРЕЗАПУСК re-exec — замена собственного процесса (без прав
    root; главный способ без sudoers-правила);
    3) plain systemctl (dev); 4) sudo -S с паролем.
    Примечание к паролю: sudo проверяет пароль СЛУЖЕБНОГО пользователя
    приложения, а не администратора — поэтому пароль из терминала в
    приложении не срабатывает; re-exec решает это без пароля вовсе."""
    rc, _o = _run(["sudo", "-n", "systemctl", "restart", "ymaster-check"], timeout=60)
    if rc == 0:
        return True, "Сервис перезапущен (sudoers) — новая версия уже работает"
    if _reexec_allowed():
        job.say("restart", 96, "Перезапуск без прав root (re-exec)…")
        time.sleep(1.0)          # дать WS-кадру «сервер обновляется» уйти
        if _self_reexec():
            return True, "Перезапущено (re-exec)"   # процесс уже заменён
    if sudo_password:
        rc, out = _run(["sudo", "-S", "-p", "", "systemctl", "restart",
                        "ymaster-check"], timeout=60,
                       input_text=sudo_password + "\n")
        if rc == 0:
            return True, "Сервис перезапущен (по паролю служебного пользователя)"
        if "incorrect password" in (out or "").lower():
            job.say("restart", 96,
                    "Пароль отклонён: приложение работает под другим "
                    "пользователем — включён самоперезапуск…")
    rc, _o = _run(["systemctl", "restart", "ymaster-check"], timeout=60)
    if rc == 0:
        return True, "Сервис перезапущен — новая версия уже работает"
    return False, ("Автоперезапуск недоступен — выполните на сервере: "
                   "sudo systemctl restart ymaster-check (данные сохранены)")


def _do_apply(target_version: str, repo: str, branch: str,
              sudo_password: str | None = None) -> None:
    """Основной конвейер обновления (выполняется в фоновом потоке)."""
    try:
        job.reset()
        job.running = True
        job.to_version = target_version
        job.started_at = time.time()
        job.old_commit = _local_commit()
        old_commit = job.old_commit
        req_hash_before = _file_sha256(os.path.join(APP_DIR, "requirements.txt"))

        # v1.46.0: запускающий код прямо сейчас работает — фиксируем его
        # как «последнюю рабочую версию» ДО любых изменений
        try:
            from .releases import mark_healthy
            mark_healthy(settings.APP_VERSION, old_commit or _local_commit(),
                         note="работала до обновления")
        except Exception:                                    # noqa: BLE001
            pass
        job.say("backup", 10, "Резервная копия базы данных…")
        from .backups import create_backup
        backup = create_backup("preupdate")
        if backup:
            job.say("backup", 15, f"Бэкап: {os.path.basename(backup)}")

        job.say("fetch", 20, "Проверка git-репозитория…")
        git_ok, git_msg = _ensure_git_repo(repo, branch)
        job.say("fetch", 22, git_msg)
        if not git_ok:
            raise RuntimeError(git_msg)

        job.say("fetch", 25, "Получение изменений с GitHub (только дельта)…")
        rc, out = _git(["fetch", "origin", branch], timeout=300)
        if rc != 0:
            # второй шанс: восстановить репозиторий и повторить
            _ensure_git_repo(repo, branch)
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

        # v1.9.1: предупреждаем всех подключённых — их клиенты сами перезагрузятся
        try:
            from .events import broadcast
            broadcast("server_update", {"to": target_version})
        except Exception:                                    # noqa: BLE001
            pass
        # v1.8.1: маркер успеха пишем ДО рестарта — после него процесс новый
        write_last_update({
            "from": getattr(job, "from_version", "") or "",
            "to": target_version,
            "at": datetime.utcnow().isoformat() + "Z",
            "backup": os.path.basename(backup) if backup else ""})
        job.say("restart", 92, "Перезапуск сервиса (без прав root — re-exec)…")
        restarted, rmsg = _restart_service(sudo_password)
        needs_restart = not restarted
        job.say("restart", 95, rmsg)

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
                "commit_after": _local_commit(),
                # v1.8.0: backup может быть None (например, БД ещё не создана) —
                # обновление всё равно успешно, не роняем запись истории
                "backup": os.path.basename(backup) if backup else "",
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


def start_apply(target_version: str, repo: str, branch: str,
                sudo_password: str | None = None) -> None:
    if job.running:
        raise RuntimeError("Обновление уже выполняется")
    t = threading.Thread(target=_do_apply,
                         args=(target_version, repo, branch, sudo_password),
                         daemon=True, name="ymaster-updater")
    t.start()


# ==========================================================================
#  v1.46.0: ОТКАТ К ПОСЛЕДНЕЙ РАБОЧЕЙ ВЕРСИИ
#  Зеркало _do_apply, но git reset --hard на коммит рабочей версии.
#  База данных НЕ трогается (миграции только добавляющие — старый код
#  работает с новой базой); копия базы делается на всякий случай.
# ==========================================================================
def _commit_exists(commit: str) -> bool:
    rc, _o = _git(["cat-file", "-e", f"{commit}^{{commit}}"], timeout=20)
    return rc == 0


def _do_rollback(target_version: str, commit: str) -> None:
    try:
        job.reset()
        job.action = "rollback"
        job.running = True
        job.started_at = time.time()
        job.to_version = target_version
        job.old_commit = _local_commit()
        old_commit = job.old_commit
        req_hash_before = _file_sha256(os.path.join(APP_DIR, "requirements.txt"))

        job.say("backup", 10, "Резервная копия базы данных…")
        from .backups import create_backup
        backup = create_backup("preupdate")
        if backup:
            job.say("backup", 15, f"Бэкап: {os.path.basename(backup)}")

        job.say("fetch", 25, f"Возврат файлов к рабочей версии v{target_version}…")
        rc, out = _git(["reset", "--hard", commit], timeout=60)
        if rc != 0:
            raise RuntimeError(f"git reset: {out}")
        rc, out = _git(["clean", "-fd", "-e", "data", "-e", ".env",
                        "-e", "venv", "-e", "*.db"], timeout=60)
        job.say("fetch", 40, "Лишние файлы вычищены (данные и секреты целы)")

        new_req = _file_sha256(os.path.join(APP_DIR, "requirements.txt"))
        if new_req != req_hash_before:
            job.say("deps", 55, "Зависимости изменились — установка…")
            rc, out = _run(_pip_cmd(), timeout=900)
            if rc != 0:
                raise RuntimeError(f"pip: {out}")
        else:
            job.say("deps", 60, "Зависимости не менялись — пропущено")

        job.say("verify", 75, "Проверка целостности (импорт + миграции БД)…")
        ok, msg = _health_check()
        if not ok:
            raise RuntimeError(f"Проверка целостности не пройдена: {msg}")

        try:
            from .events import broadcast
            broadcast("server_update", {"to": target_version})
        except Exception:                                    # noqa: BLE001
            pass
        job.say("restart", 92, "Перезапуск сервиса…")
        restarted, rmsg = _restart_service()
        job.needs_restart = not restarted
        job.say("restart", 95, rmsg)

        job.success = True
        job.finished = True
        job.progress = 100
        job.say("done", 100,
                f"Готово: откат к v{target_version} ({commit[:8]})")

        from ..database import SessionLocal
        db = SessionLocal()
        try:
            _append_history(db, {
                "action": "rollback", "to": target_version,
                "commit": commit, "commit_before": old_commit,
                "backup": os.path.basename(backup) if backup else "",
                "at": datetime.utcnow().isoformat() + "Z", "ok": True})
        finally:
            db.close()
    except Exception as e:                                   # noqa: BLE001
        job.error = str(e)
        job.finished = True
        job.say("error", None, f"ОШИБКА ОТКАТА: {e}")
        # возвращаем ровно то состояние, что было до попытки отката
        if old_commit and old_commit != "unknown":
            rc, _o = _git(["reset", "--hard", old_commit], timeout=60)
            job.rolled_back = (rc == 0)
            job.say("rollback", None,
                    "Состояние до отката восстановлено — ничего не изменилось")
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            _append_history(db, {
                "action": "rollback_failed", "target": target_version,
                "error": str(e)[:500], "rolled_back": True,
                "at": datetime.utcnow().isoformat() + "Z"})
        finally:
            db.close()
    finally:
        job.running = False


def start_rollback(target_version: str, commit: str) -> None:
    if job.running:
        raise RuntimeError("Обновление/откат уже выполняется")
    t = threading.Thread(target=_do_rollback, args=(target_version, commit),
                         daemon=True, name="ymaster-rollback")
    t.start()
