"""Этап Б выпуска: настоящее обновление из программы с GitHub.

Повторяет действия владельца на рабочем сервере, а не моделирует их:
  1. неглубокий клон ветки на коммите установленной версии — как deploy.sh
     (`git clone --depth 1`); окружение лежит в каталоге программы (venv/);
  2. база наполняется СТАРЫМ кодом — фаза SEED из qa/upgrade_rehearsal.py;
  3. сервер запускается командой из systemd-юнита
     (`venv/bin/uvicorn app.main:app --workers 1`), самоперезапуск разрешён;
  4. через API администратора: «Источник обновлений» → «Проверить» →
     предпроверка → «Обновить» → самоперезапуск → проверки;
  5. «Последние рабочие версии» → откат → проверки → повторное обновление.

Проверки после каждого шага: версия, тот же процесс (PID не меняется —
это самоперезапуск, а не новый сервер), старые таблицы не изменились,
итоги по ролям те же. После обновления дополнительно: шаг pip пропущен,
резервная копия создана, файлы — ровно коммит ветки на GitHub, новые
экран и API отвечают.

Запуск:  .venv/bin/python -m qa.live_update --from-ref b3db4fe [--keep]
Нужен доступ к github.com. Сервер слушает только 127.0.0.1.
Обновляется ветка, опубликованная на GitHub, — запускать ПОСЛЕ push.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from qa.upgrade_rehearsal import SEED, check, run_phase

ROOT = Path(__file__).resolve().parents[1]
USERS = ("admin", "acc_a", "acc_b", "user_a", "acc_g")
# те же таблицы и столбцы, что в отпечатке репетиции (COMMON.fingerprint)
OLD_TABLES = (
    ("companies", "SELECT id, name FROM companies ORDER BY id"),
    ("users", "SELECT id, username, role, company_id FROM users ORDER BY id"),
    ("receipts", "SELECT id, fn, fd, fp, total_sum, company_id, created_by FROM receipts ORDER BY id"),
    ("receipt_items", "SELECT receipt_id, name, quantity, total FROM receipt_items "
                      "ORDER BY receipt_id, position, id"),
    ("pool_receipts", "SELECT id, fn, fd, fp, total_sum, assigned_company_id, status, points_awarded "
                      "FROM pool_receipts ORDER BY id"),
    ("pool_items", "SELECT receipt_id, name, total FROM pool_items ORDER BY receipt_id, id"),
    ("pool_users", "SELECT id, points FROM pool_users ORDER BY id"),
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, timeout=180).stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fingerprint(db: Path) -> dict:
    """Отпечаток старых таблиц напрямую из файла БД (только чтение)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {}
        for name, sql in OLD_TABLES:
            rows = [[str(v) for v in r] for r in con.execute(sql)]
            out[name] = {"count": len(rows),
                         "sha": hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]}
        return out
    finally:
        con.close()


class Server:
    """Сервер программы — та же команда, что в deploy/systemd/ymaster-check.service."""

    def __init__(self, app_dir: Path, env: dict, log_path: Path):
        self.port = free_port()
        cmd = [str(app_dir / "venv" / "bin" / "uvicorn"), "app.main:app",
               "--host", "127.0.0.1", "--port", str(self.port), "--workers", "1"]
        self.log = open(log_path, "ab")  # noqa: SIM115 — закрывается в stop()
        self.proc = subprocess.Popen(cmd, cwd=app_dir, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=60)

    def version(self) -> str | None:
        try:
            return self.http.get("/health").json().get("version")
        except (httpx.HTTPError, ValueError):
            return None

    def wait_up(self, timeout: float = 120) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"сервер завершился с кодом {self.proc.returncode}")
            v = self.version()
            if v:
                return v
            time.sleep(0.5)
        raise RuntimeError("сервер не ответил на /health")

    def wait_version(self, headers: dict, target: str, timeout: float = 300) -> tuple[dict, float]:
        """Ждём, пока /health покажет целевую версию; ошибка задания — сразу выход."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"сервер завершился с кодом {self.proc.returncode}")
            if self.version() == target:
                break
            try:
                job = self.http.get("/api/v1/admin/update/status", headers=headers).json().get("job") or {}
                if job.get("finished") and job.get("error"):
                    break
            except (httpx.HTTPError, ValueError):
                pass  # идёт самоперезапуск — сокет на мгновение закрыт
            time.sleep(1)
        elapsed = time.monotonic() - start
        status = self.http.get("/api/v1/admin/update/status", headers=headers).json()
        return status, elapsed

    def stop(self) -> None:
        self.http.close()
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.log.close()


def login(http: httpx.Client, username: str, password: str) -> dict:
    r = http.post("/api/v1/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def totals(http: httpx.Client, headers: dict) -> dict:
    out = {}
    for user, h in headers.items():
        r = http.get("/api/v1/receipts", headers=h, params={"per_page": 1})
        out[user] = r.json().get("total") if r.status_code == 200 else r.status_code
    return out


def job_log(status: dict) -> str:
    return json.dumps((status.get("job") or {}).get("log") or [], ensure_ascii=False)


def run(args: argparse.Namespace, work: Path, checks: list) -> dict:
    target = re.search(r'APP_VERSION: str = "([^"]+)"', (ROOT / "app" / "config.py").read_text("utf-8"))[1]
    remote_head = git(ROOT, "ls-remote", f"https://github.com/{args.repo}.git",
                      f"refs/heads/{args.branch}").split()[0]
    # 1. неглубокий клон на старом коммите — как deploy.sh
    app_dir = work / "app"
    app_dir.mkdir()
    old_commit = git(ROOT, "rev-parse", args.from_ref)
    git(app_dir, "init", "-q")
    git(app_dir, "remote", "add", "origin", f"https://github.com/{args.repo}.git")
    git(app_dir, "fetch", "-q", "--depth", "1", "origin", old_commit)
    git(app_dir, "checkout", "-q", "-B", args.branch, "FETCH_HEAD")
    (app_dir / "venv").symlink_to(ROOT / ".venv")
    (app_dir / "data").mkdir()
    db = app_dir / "data" / "ymaster_check.db"

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SUPABASE_", "DATABASE_URL", "PYTEST_", "YM_NO_REEXEC", "PYTHONPATH"))}
    env.update({"DATABASE_URL": f"sqlite:///{db}", "SECRET_KEY": secrets.token_hex(32),
                "YM_FETCHQ_OFF": "1", "YM_OSMON_OFF": "1", "TZ": "Europe/Moscow",
                "PATH": f"{app_dir / 'venv' / 'bin'}:/usr/bin:/bin"})
    password = "Live-" + secrets.token_hex(6)
    # 2. база наполняется старым кодом
    seed = run_phase("старая версия: база", SEED, app_dir,
                     {**env, "YM_NO_REEXEC": "1", "REH_PASSWORD": password})
    old_version = seed["version"]

    srv = Server(app_dir, env, work / "server.log")
    try:
        started = srv.wait_up()
        pid = srv.proc.pid
        heads = {u: login(srv.http, u, password) for u in USERS}
        adm = heads["admin"]
        base_totals, base_fp = totals(srv.http, heads), fingerprint(db)
        check(checks, started == old_version,
              f"сервер запущен на v{started} (коммит {old_commit[:7]}, неглубокий клон)")

        # 3. «Источник обновлений» → «Проверить» → предпроверка → «Обновить»
        repo = srv.http.put("/api/v1/admin/update/repo", headers=adm,
                            json={"repo_url": args.repo, "repo_branch": args.branch}).json()
        found = srv.http.get("/api/v1/admin/update/check", headers=adm, params={"force": 1}).json()
        check(checks, repo.get("repo_branch") == args.branch and found.get("update_available") is True
              and found.get("remote_version") == target,
              f"«Проверить»: на GitHub v{found.get('remote_version')}, обновление предложено")
        pre = srv.http.get("/api/v1/admin/update/preflight", headers=adm).json()
        check(checks, pre.get("ready") is True,
              "предпроверка: право на перезапуск (самоперезапуск), GitHub и копия БД — готово")
        applied = srv.http.post("/api/v1/admin/update/apply", headers=adm, json={}).json()
        status, spent = srv.wait_version(adm, target)
        job = status.get("job") or {}
        check(checks, applied.get("updated") is True and srv.version() == target
              and srv.proc.pid == pid and srv.proc.poll() is None,
              f"«Обновить»: v{old_version} → v{srv.version()} за {spent:.0f} с, тот же процесс (самоперезапуск)")
        check(checks, fingerprint(db) == base_fp, "старые таблицы после обновления не изменились")
        check(checks, totals(srv.http, heads) == base_totals,
              f"итоги по ролям после обновления те же: {base_totals}")
        check(checks, "Зависимости не менялись" in job_log(status) and bool(job.get("backup_file")),
              f"шаг pip пропущен; резервная копия перед обновлением: {job.get('backup_file')}")
        check(checks, git(app_dir, "rev-parse", "HEAD") == remote_head
              and git(app_dir, "status", "--porcelain", "--untracked-files=no") == "",
              f"файлы программы — ровно коммит ветки на GitHub ({remote_head[:7]})")
        screen = srv.http.get("/js/unified_review.js").status_code
        api = srv.http.get("/api/v1/unified/documents", headers=adm,
                           params={"conflicts": "true", "flag": 8}).status_code
        check(checks, screen == 200 and api == 200, "новый экран и API «Разбор конфликтов» отвечают")

        # 4. откат из «Последних рабочих версий»
        releases = srv.http.get("/api/v1/admin/update/releases", headers=adm).json().get("items", [])
        check(checks, any(x.get("version") == old_version for x in releases),
              f"в «Последних рабочих версиях» есть v{old_version}")
        srv.http.post("/api/v1/admin/update/rollback", headers=adm, json={"version": old_version})
        status, spent = srv.wait_version(adm, old_version)
        check(checks, srv.version() == old_version and srv.proc.pid == pid,
              f"откат из программы на v{srv.version()} за {spent:.0f} с, тот же процесс")
        check(checks, fingerprint(db) == base_fp and totals(srv.http, heads) == base_totals,
              "после отката старые таблицы и итоги по ролям те же")

        # 5. повторное обновление — как если бы владелец вернулся на новую версию
        found = srv.http.get("/api/v1/admin/update/check", headers=adm, params={"force": 1}).json()
        applied = srv.http.post("/api/v1/admin/update/apply", headers=adm, json={}).json()
        status, spent = srv.wait_version(adm, target)
        check(checks, found.get("update_available") is True and srv.version() == target
              and srv.proc.pid == pid and fingerprint(db) == base_fp
              and totals(srv.http, heads) == base_totals,
              f"повторное обновление до v{srv.version()} за {spent:.0f} с: данные и итоги те же")
        return {"from": old_version, "to": target, "remote_head": remote_head,
                "totals": base_totals, "last_job": status.get("job")}
    finally:
        srv.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Этап Б: обновление из программы с GitHub, откат и повтор")
    ap.add_argument("--from-ref", default="b3db4fe", help="коммит установленной версии")
    ap.add_argument("--repo", default="Rimlin-UNC/1c_chek")
    ap.add_argument("--branch", default=git(ROOT, "rev-parse", "--abbrev-ref", "HEAD"))
    ap.add_argument("--keep", action="store_true", help="не удалять временный каталог")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="ym-live-update-"))
    checks: list = []
    try:
        result = run(args, work, checks)
    except Exception as e:  # noqa: BLE001 — отчёт о сбое вместо трассировки
        result = {"error": str(e)[:2000]}
        check(checks, False, f"сбой прогона: {str(e)[:300]}")
    finally:
        if args.keep:
            print(f"каталог прогона сохранён: {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)
    for c in checks:
        print(("✅ " if c["ok"] else "❌ ") + c["check"], file=sys.stderr)
    passed = sum(c["ok"] for c in checks)
    print(f"Итого: {passed} из {len(checks)} проверок пройдено", file=sys.stderr)
    print(json.dumps({"checks": checks, **result}, ensure_ascii=False, indent=1, default=str))
    return 0 if checks and passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
