#!/usr/bin/env python3
"""«Бригада тестировщиков»: проверки выпуска одной командой.

Репетиция обновления и этап Б (настоящее обновление из программы с GitHub) — от
всех опор подряд; ожидание CI ветки и итог ворот из аннотаций. Опоры — версии, с
которых проверяется обновление на текущую: строки реестра выпусков
docs/kb/09_releases.csv с «Опора репетиции» = «да».

    .venv/bin/python -m qa.release_gate refs
    .venv/bin/python -m qa.release_gate rehearsal [--only <коммит> ...] [--log-dir <каталог>]
    .venv/bin/python -m qa.release_gate stage-b [--only <коммит> ...]
    .venv/bin/python -m qa.release_gate ci [--sha <коммит>] [--timeout <секунды>]

Прогоны идут строго последовательно: параллельные репетиции не укладываются в
потолок времени проверок. Этап Б — только после push выпуска: обновление идёт
вперёд по номеру версии. Обе проверки живут во временных каталогах и работают на
синтетических данных (ADR-14). Код выхода: 0 — всё прошло, 1 — есть ошибки.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "kb" / "09_releases.csv"
PY = ROOT / ".venv" / "bin" / "python"
SLUG = "Rimlin-UNC/1c_chek"
SUITES = {"rehearsal": "qa.upgrade_rehearsal", "stage-b": "qa.live_update"}
DEFAULT_LOGS = Path("/tmp/kbwork")  # nosec B108 — логи прогонов, как в RUNBOOK


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def current_version() -> str:
    """Версия рабочего дерева — из app/config.py (без импорта приложения)."""
    text = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION: str = "([\d.]+)"', text)
    if not match:
        raise RuntimeError("в app/config.py не найден APP_VERSION")
    return match.group(1)


def load_refs(ledger: Path = LEDGER, below: str | None = None) -> list[tuple[str, str]]:
    """Опоры репетиции [(версия, коммит)] по возрастанию версии.

    ``below`` — только версии младше этой: обновление идёт вперёд по номеру, и
    репетиция с текущей версии на неё же ничего не проверяет.
    """
    rows = csv.DictReader(ledger.read_text(encoding="utf-8").splitlines())
    refs = [(r["Версия"], r["Коммит"]) for r in rows if r["Опора репетиции"].strip() == "да"]
    if below:
        refs = [ref for ref in refs if version_key(ref[0]) < version_key(below)]
    return sorted(refs, key=lambda ref: version_key(ref[0]))


def summarize(report: dict) -> tuple[int, int, list[str]]:
    """(прошло, всего, упавшие) по JSON-отчёту репетиции или этапа Б."""
    checks = report.get("checks") or []
    passed = sum(1 for c in checks if c.get("ok"))
    failed = [str(c.get("check", ""))[:160] for c in checks if not c.get("ok")]
    return passed, len(checks), failed


def have_commit(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT,
                          capture_output=True, check=False).returncode == 0


def ensure_commit(sha: str) -> bool:
    """Коммит опоры есть локально; нет — догрузить только его (удалённый не меняется)."""
    if not have_commit(sha):
        subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", sha], cwd=ROOT,
                       capture_output=True, check=False, timeout=300)
    return have_commit(sha)


def select(refs: list[tuple[str, str]], only: list[str] | None) -> list[tuple[str, str]]:
    if not only:
        return refs
    return [(v, sha) for v, sha in refs if any(sha.startswith(o) or v == o for o in only)]


def run_suite(name: str, refs: list[tuple[str, str]], log_dir: Path) -> int:
    module = SUITES[name]
    log_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for version, sha in refs:
        if not ensure_commit(sha):
            print(f"❌ {version} ({sha[:7]}): коммита нет локально, догрузить не удалось", flush=True)
            failures += 1
            continue
        started = time.monotonic()
        proc = subprocess.run([str(PY), "-m", module, "--from-ref", sha], cwd=ROOT,
                              capture_output=True, text=True, check=False, timeout=3600)
        base = log_dir / f"{name}_{sha[:7]}"
        base.with_suffix(".out").write_text(proc.stdout, encoding="utf-8")
        base.with_suffix(".err").write_text(proc.stderr, encoding="utf-8")
        try:
            passed, total, failed = summarize(json.loads(proc.stdout))
        except json.JSONDecodeError:
            passed, total, failed = 0, 0, [f"отчёт не в JSON (код {proc.returncode}), см. {base}.err"]
        good = proc.returncode == 0 and total > 0 and passed == total
        failures += 0 if good else 1
        tail = "" if good else " — " + "; ".join(failed[:3])
        print(f"{'✅' if good else '❌'} {version} ({sha[:7]}): {passed} из {total} "
              f"за {time.monotonic() - started:.0f} с{tail}", flush=True)
    print(f"Итог «{name}»: опор {len(refs)}, с ошибками {failures}; отчёты — {log_dir}", flush=True)
    return 1 if failures else 0


def gh_json(*args: str) -> object:
    proc = subprocess.run(["gh", "api", *args], cwd=ROOT, capture_output=True, text=True,
                          check=False, timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip()[:300] or f"gh api {' '.join(args)}: код {proc.returncode}")
    return json.loads(proc.stdout or "null")


def wait_ci(sha: str, timeout: int, poll: int = 30) -> int:
    """Дождаться прогонов CI на коммите и напечатать итог ворот (аннотации notice)."""
    deadline = time.monotonic() + timeout
    while True:
        runs = gh_json(f"repos/{SLUG}/actions/runs?head_sha={sha}&event=push&per_page=20")["workflow_runs"]
        pending = [r for r in runs if r["status"] != "completed"]
        if runs and not pending:
            break
        if time.monotonic() > deadline:
            state = f"идут: {len(pending)}" if runs else "прогонов на коммите нет — проверьте, что он отправлен"
            print(f"❌ CI на {sha[:7]}: за {timeout} с не завершился ({state})", flush=True)
            return 1
        time.sleep(poll)
    for r in runs:
        print(f"  прогон {r['id']} «{r['name']}»: {r['conclusion']} — {r['html_url']}", flush=True)
    checks = gh_json(f"repos/{SLUG}/commits/{sha}/check-runs?per_page=50")["check_runs"]
    notices = set()
    for check in checks:
        if not str(check.get("name", "")).startswith("tests ("):
            continue
        for note in gh_json(f"repos/{SLUG}/check-runs/{check['id']}/annotations"):
            if note.get("annotation_level") == "notice":
                notices.add(note["message"].splitlines()[0])
    for note in sorted(notices):
        print(f"  ворота: {note}", flush=True)
    good = any(r["conclusion"] == "success" for r in runs) and all(
        r["conclusion"] in ("success", "skipped") for r in runs)
    print(f"{'✅' if good else '❌'} CI на {sha[:7]}", flush=True)
    return 0 if good else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Проверки выпуска: репетиция, этап Б, CI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refs", help="опоры репетиции из реестра выпусков")
    for name in SUITES:
        p = sub.add_parser(name, help=f"{SUITES[name]} от всех опор подряд")
        p.add_argument("--only", nargs="+", help="только эти опоры (версия или начало коммита)")
        p.add_argument("--log-dir", type=Path, default=DEFAULT_LOGS)
    p = sub.add_parser("ci", help="дождаться CI коммита и напечатать итог ворот")
    p.add_argument("--sha", help="коммит (по умолчанию HEAD)")
    p.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args(argv)

    current = current_version()
    if args.cmd == "refs":
        print(f"Текущая версия {current}; опоры — младше неё:")
        for version, sha in load_refs(below=current):
            print(f"{version}  {sha[:7]}  {'есть локально' if have_commit(sha) else 'нет локально'}")
        return 0
    if args.cmd == "ci":
        # API GitHub ищет прогоны только по полному хешу — короткий разворачиваем
        sha = subprocess.run(["git", "rev-parse", args.sha or "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        return wait_ci(sha, args.timeout)
    refs = select(load_refs(below=current), args.only)
    if not refs:
        print("❌ опор не выбрано: проверьте --only и docs/kb/09_releases.csv", flush=True)
        return 1
    return run_suite(args.cmd, refs, args.log_dir)


if __name__ == "__main__":
    sys.exit(main())
