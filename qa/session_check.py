#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка на старте сессии: git, окружение, GitHub и CI — одной командой.

Исполняемая версия шага 0 из AGENTS.md. Только чтение: ничего не ставит, не
сбрасывает и не пушит. Если коммита ветки нет локально, делает git fetch этой
ветки (добавляет объекты в локальный репозиторий, удалённый не меняет).

    python3 qa/session_check.py

Код выхода: 0 — можно работать; 1 — есть ошибки (команды исправления напечатаны).
ВНИМАНИЕ не мешает работе: например, браузеры Playwright в песочнице не скачать,
этот тест выполняется в CI.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "bin" / "python"
NODE_TOOLS = ("eslint", "vitest", "playwright", "newman")
# Валидаторы ставятся в /tmp так же, как в CI, и там же их ищут тесты; здесь —
# только проверка наличия, временные файлы не создаются (B108 — ложная тревога).
VALIDATORS = {"stylelint": Path("/tmp/csslint/node_modules/.bin/stylelint"),  # nosec B108
              "html-validate": Path("/tmp/htmlval/node_modules/.bin/html-validate")}  # nosec B108

results: list[tuple[str, str, str]] = []      # (уровень, что, подробности)


def report(level: str, what: str, details: str = "") -> None:
    results.append((level, what, details))
    print(f"[{level:8}] {what}" + (f" — {details}" if details else ""), flush=True)


def run(*cmd: str, env: dict | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=timeout, env=env, check=False)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ---------------------------------------------------------------- git
def check_git() -> str | None:
    code, branch = run("git", "branch", "--show-current")
    if code or not branch:
        report("ОШИБКА", "git: не удалось определить ветку", branch)
        return None
    _, head = run("git", "rev-parse", "HEAD")
    _, dirty = run("git", "status", "--short")
    changed = len([ln for ln in dirty.splitlines() if ln.strip()])
    code, remote = run("git", "ls-remote", "origin", f"refs/heads/{branch}", timeout=60)
    remote_sha = remote.split()[0] if code == 0 and remote else ""
    if not remote_sha:
        report("ВНИМАНИЕ", f"git: ветки {branch} нет на GitHub или нет доступа", remote[:120])
        return branch
    if head == remote_sha:
        level = "OK" if changed == 0 else "ВНИМАНИЕ"
        report(level, f"git: {branch} = GitHub ({head[:7]})",
               "изменённых файлов нет" if changed == 0 else f"незакоммиченных изменений: {changed}")
        return branch
    # Указатель расходится с GitHub — сверить дерево файлов через временный индекс
    if run("git", "cat-file", "-e", f"{remote_sha}^{{commit}}")[0] != 0:
        run("git", "fetch", "-q", "origin", branch, timeout=120)
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "check.idx"))
        run("git", "read-tree", remote_sha, env=env)
        run("git", "add", "-A", env=env)
        _, diff = run("git", "diff", "--cached", "--stat", remote_sha, env=env)
    if not diff:
        report("ОШИБКА", f"git: указатель {head[:7]} отстал от GitHub {remote_sha[:7]}, файлы совпадают",
               f"безопасно выполнить: git reset --mixed {remote_sha[:7]}")
    else:
        report("ОШИБКА", f"git: HEAD {head[:7]} ≠ GitHub {remote_sha[:7]} и файлы различаются",
               "не коммитить: разобраться (git diff) и сообщить владельцу")
    return branch


# ---------------------------------------------------------------- окружение
def check_environment() -> None:
    if not VENV_PY.exists():
        report("ОШИБКА", "окружение: нет .venv", "qa/setup_dev.sh --full")
        return
    _, version = run(str(VENV_PY), "--version")
    report("OK", f"окружение: {version} в .venv")
    probe = ("import importlib.util as u, json; print(json.dumps({m: bool(u.find_spec(m)) for m in "
             "('pytest', 'xdist', 'hypothesis', 'defusedxml', 'psycopg', 'pgserver')}))")
    code, out = run(str(VENV_PY), "-c", probe)
    found = json.loads(out.splitlines()[-1]) if code == 0 else {}
    required = [m for m in ("pytest", "xdist", "hypothesis", "defusedxml") if not found.get(m)]
    if required:
        report("ОШИБКА", "окружение: нет пакетов " + ", ".join(required), "qa/setup_dev.sh --full")
    else:
        report("OK", "окружение: pytest, xdist, hypothesis, defusedxml")
    for module, hint in (("psycopg", "драйвер PostgreSQL"), ("pgserver", "локальный PostgreSQL для qa/pg_local.py")):
        report("OK" if found.get(module) else "ОШИБКА", f"окружение: {module} ({hint})",
               "" if found.get(module) else "qa/setup_dev.sh --full")
    missing = [t for t in NODE_TOOLS if not (ROOT / "node_modules" / ".bin" / t).exists()]
    report("ОШИБКА" if missing else "OK", "Node-инструменты: " + ", ".join(NODE_TOOLS),
           ("нет: " + ", ".join(missing) + " — qa/setup_dev.sh --full") if missing else "")
    for name, path in VALIDATORS.items():
        report("OK" if path.exists() else "ОШИБКА", f"валидатор {name} (как в CI)",
               "" if path.exists() else "qa/setup_dev.sh --full")
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    if browsers.is_dir() and any(browsers.iterdir()):
        report("OK", "браузеры Playwright")
    else:
        report("ВНИМАНИЕ", "браузеров Playwright нет",
               "в песочнице CDN закрыт — браузерный тест выполняется в CI (локально 1 пропуск)")


# ---------------------------------------------------------------- GitHub и CI
def check_github(branch: str | None) -> None:
    if not shutil.which("gh"):
        report("ВНИМАНИЕ", "GitHub: нет gh — проверка CI пропущена")
        return
    code, login = run("gh", "api", "user", "--jq", ".login")
    if code:
        report("ОШИБКА", "GitHub: вход не выполнен", "подключение GitHub в Arena требует внимания")
        return
    _, headers = run("gh", "api", "-i", "user")
    expires = re.search(r"(?im)^github-authentication-token-expiration:\s*(.+)$", headers)
    report("OK", f"GitHub: вход как {login}",
           f"токен действует до {expires.group(1).strip()}" if expires else "")
    _, remote = run("git", "remote", "get-url", "origin")
    slug = re.sub(r"(\.git)?$", "", remote.split("github.com")[-1].lstrip(":/"))
    if not branch or not slug:
        return
    code, out = run("gh", "api", f"repos/{slug}/actions/runs?branch={branch}&event=push&per_page=1",
                    "--jq", ".workflow_runs[0] | [.id, .status, .conclusion, .head_sha] | @tsv")
    if code or not out:
        report("ВНИМАНИЕ", "CI: прогонов ветки не найдено", out[:120])
        return
    run_id, status, conclusion, sha = (out.split("\t") + ["", "", "", ""])[:4]
    _, head = run("git", "rev-parse", "HEAD")
    same = "на текущем HEAD" if sha == head else f"на {sha[:7]}, а HEAD {head[:7]}"
    if status != "completed":
        report("ВНИМАНИЕ", f"CI: прогон {run_id} ещё идёт ({same})")
        return
    _, notices = run("gh", "api", f"repos/{slug}/commits/{sha}/check-runs", "--jq",
                     '.check_runs[] | select(.name | startswith("tests (")) | .id')
    totals = []
    for check_id in notices.split():
        _, msg = run("gh", "api", f"repos/{slug}/check-runs/{check_id}/annotations", "--jq",
                     '.[] | select(.annotation_level == "notice") | .message')
        if msg:
            totals.append(msg.splitlines()[0])
    report("OK" if conclusion == "success" else "ОШИБКА", f"CI: прогон {run_id} — {conclusion} ({same})",
           "; ".join(sorted(set(totals))) or "итогов в аннотациях нет")


def main() -> int:
    print("Проверка на старте сессии — шаг 0 AGENTS.md (только чтение)\n", flush=True)
    branch = check_git()
    check_environment()
    check_github(branch)
    errors = [r for r in results if r[0] == "ОШИБКА"]
    warnings = [r for r in results if r[0] == "ВНИМАНИЕ"]
    print(f"\nИтог: проверок {len(results)}, ошибок {len(errors)}, предупреждений {len(warnings)} — "
          + ("можно работать" if not errors else "сначала исправить ошибки выше"), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
