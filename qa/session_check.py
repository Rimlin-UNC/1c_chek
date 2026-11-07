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

import csv
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

# Локальный Chromium из qa/local_chromium.sh (ставит qa/setup_dev.sh --full) — только проверка наличия.
LOCAL_CHROMIUM = Path("/tmp/ym-chromium/chrome.sh")  # nosec B108
KB = ROOT / "docs" / "kb"

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
    # Указатель расходится с GitHub: восстановленная песочница (указатель на старом
    # коммите) или параллельная сессия, отправившая коммиты в ту же ветку. Сверяем
    # дерево рабочих файлов с историей ветки на GitHub — так видно, есть ли
    # незакоммиченная работа и какой шаг безопасен.
    if run("git", "cat-file", "-e", f"{remote_sha}^{{commit}}")[0] != 0:
        run("git", "fetch", "-q", "origin", branch, timeout=120)
    level, what, fix = diagnose_divergence(head, remote_sha)
    report(level, what, fix)
    return branch


def worktree_tree(base: str) -> str:
    """Хеш дерева рабочих файлов (с неотслеживаемыми, без игнорируемых) через временный индекс."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "check.idx"))
        run("git", "read-tree", base, env=env)
        run("git", "add", "-A", env=env)
        _, tree = run("git", "write-tree", env=env)
    return tree.strip()


def find_tree_in_history(tree: str, tip: str, depth: int = 300) -> tuple[str, int] | None:
    """Коммит из истории ``tip`` с тем же деревом: (коммит, на сколько коммитов позади)."""
    _, log = run("git", "log", "--format=%H %T", f"-n{depth}", tip)
    for behind, line in enumerate(log.splitlines()):
        sha, _, commit_tree = line.partition(" ")
        if commit_tree == tree:
            return sha, behind
    return None


def nearest_commit(tree: str, candidates: list[str]) -> tuple[str, int]:
    """Коммит-кандидат с наименьшим числом отличающихся от дерева файлов."""
    best = (candidates[0], 10**9)
    for sha in candidates:
        _, names = run("git", "diff-tree", "-r", "--name-only", sha, tree)
        count = len([n for n in names.splitlines() if n.strip()])
        if count < best[1]:
            best = (sha, count)
    return best


def diagnose_divergence(head: str, remote_sha: str) -> tuple[str, str, str]:
    """(уровень, что, как исправить), когда HEAD не совпадает с веткой на GitHub."""
    tree = worktree_tree(head)
    found = find_tree_in_history(tree, remote_sha)
    if found and found[1] == 0:
        return ("ОШИБКА", f"git: указатель {head[:7]} отстал от GitHub {remote_sha[:7]}, файлы совпадают",
                f"безопасно выполнить: git reset --mixed {remote_sha[:7]}")
    if found:
        sha, behind = found
        return ("ОШИБКА", f"git: файлы = коммиту {sha[:7]} из истории GitHub ({behind} коммит(ов) позади "
                          f"{remote_sha[:7]}); незакоммиченной работы нет",
                f"безопасно выполнить: git reset --mixed {sha[:7]} && git merge --ff-only {remote_sha[:7]}")
    _, recent = run("git", "log", "--format=%H", "-n30", remote_sha)
    base, files = nearest_commit(tree, [head, *recent.split()])
    return ("ОШИБКА", f"git: файлы не совпадают ни с одним коммитом GitHub — незакоммиченная работа "
                      f"поверх {base[:7]} ({files} файл(ов))",
            f"не терять: git reset --mixed {base[:7]}, проверить git status и git diff, закоммитить, "
            f"затем git rebase {remote_sha[:7]}; без reset --hard и push --force")


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
    chromium = Path(os.environ.get("YM_PLAYWRIGHT_CHROMIUM") or LOCAL_CHROMIUM)
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    if chromium.exists():
        report("OK", "Chromium для Playwright", f"YM_PLAYWRIGHT_CHROMIUM={chromium} — ворота запускать отдельно")
    elif browsers.is_dir() and any(browsers.iterdir()):
        report("OK", "браузеры Playwright")
    else:
        report("ВНИМАНИЕ", "Chromium для Playwright нет",
               "qa/setup_dev.sh --full ставит его (qa/local_chromium.sh); без него локально 1 пропуск")


# ---------------------------------------------------------------- опоры и база знаний
def check_release_refs() -> None:
    """Коммиты опор репетиции (docs/kb/09_releases.csv) есть локально."""
    sys.path.insert(0, str(ROOT))
    try:
        from qa.release_gate import current_version, have_commit, load_refs
        refs = load_refs(below=current_version())
    except (OSError, KeyError, RuntimeError, ValueError) as exc:
        report("ВНИМАНИЕ", "опоры репетиции: реестр выпусков не прочитан", str(exc)[:120])
        return
    missing = [version for version, sha in refs if not have_commit(sha)]
    report("OK" if not missing else "ВНИМАНИЕ", f"опоры репетиции: {len(refs)}",
           ("нет локально: " + ", ".join(missing) + " — qa.release_gate догрузит их сам") if missing
           else ", ".join(version for version, _ in refs))


def print_kb_summary() -> None:
    """Что дальше по базе знаний: задачи в работе, ждущие владельца вопросы, последний выпуск."""
    try:
        tasks = list(csv.DictReader((KB / "02_roadmap.csv").read_text(encoding="utf-8").splitlines()))
        questions = list(csv.DictReader((KB / "06_open_questions.csv").read_text(encoding="utf-8").splitlines()))
        releases = list(csv.DictReader((KB / "09_releases.csv").read_text(encoding="utf-8").splitlines()))
    except (OSError, csv.Error) as exc:
        print(f"\nБаза знаний не прочитана: {exc}", flush=True)
        return
    def ids(rows: list[dict], limit: int = 8) -> str:
        names = [r["ID"] for r in rows]
        return ", ".join(names[:limit]) + (f" и ещё {len(names) - limit}" if len(names) > limit else "") or "—"
    in_work = [t for t in tasks if t["Статус"] == "in-progress"]
    ready = [t for t in tasks if t["Статус"] == "todo" and t["Приоритет"] in ("P0", "P1")]
    owner = [q for q in questions if q["Статус"] == "open" and q["Кто решает"] == "владелец"]
    last = releases[-1] if releases else {}
    print("\nДальше по базе знаний (docs/kb):", flush=True)
    print(f"  последний выпуск: {last.get('Версия', '—')} ({str(last.get('Коммит', ''))[:7]}) — "
          f"{str(last.get('Суть', ''))[:90]}", flush=True)
    print(f"  в работе: {ids(in_work)}; todo P0–P1: {ids(ready)}; "
          f"заблокировано: {sum(t['Статус'] == 'blocked' for t in tasks)}", flush=True)
    print(f"  ждут владельца: {ids(owner)}", flush=True)


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
    check_release_refs()
    check_github(branch)
    print_kb_summary()
    errors = [r for r in results if r[0] == "ОШИБКА"]
    warnings = [r for r in results if r[0] == "ВНИМАНИЕ"]
    print(f"\nИтог: проверок {len(results)}, ошибок {len(errors)}, предупреждений {len(warnings)} — "
          + ("можно работать" if not errors else "сначала исправить ошибки выше"), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
