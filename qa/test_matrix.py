#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Матрица тестирования миграции: CSV для QA-панели в Google Sheets.

GitHub — единственный источник истины: матрица целиком выводится из
репозитория (отчёты JUnit прогона, данные аудита пропусков, правила ниже) и
загружается в Google Sheets только как представление.

Использование:
    YM_JUNIT_DIR=/tmp/junit ./test.sh full -j2
    python qa/test_matrix.py --junit /tmp/junit/*.xml --commit <sha> \\
        --ci-run <id> --ci-fingerprint <отпечаток из итога CI> --out /tmp/test_matrix.csv

Статус в CI. Логи и артефакты CI недоступны вне GitHub, поэтому статус
выводится строго: ворота CI печатают отпечаток набора тестов (qa/junit_gate.py);
если он совпадает с отпечатком локальных отчётов того же коммита, наборы
тестов идентичны, а зелёный прогон ворот означает 0 упавших и 0 пропущенных —
значит, прошёл каждый тест списка. При несовпадении статус «нет данных».
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qa import junit_gate  # noqa: E402  — после настройки sys.path

REPO = "https://github.com/Rimlin-UNC/1c_chek"
SKIPS_CSV = ROOT / "qa" / "reports" / "skips_ffe77e3.csv"

# Области и критичность для миграции — явные списки, без эвристик по тексту.
AREA_POSTGRES = "PostgreSQL / Supabase"
AREA_UNIFIED = "Единый реестр / перенос данных"
AREA_UI = "Интерфейс и JS-цепочка"
AREA_APP = "Функционал приложения"
FILES_BY_AREA = {
    AREA_POSTGRES: {"tests/test_postgres_identity_optin.py", "tests/test_unified_postgres.py",
                    "tests/test_supabase_migrations.py", "tests/test_migration_compat_postgres.py"},
    AREA_UNIFIED: {"tests/test_unified_receipts.py", "tests/test_v17082.py",
                   "tests/test_receipt_copy_migration.py", "tests/test_pool_bridge.py",
                   "tests/test_unified_review.py", "tests/test_migration_e2e.py",
                   "tests/test_unified_phase0.py", "tests/test_unified_claims.py",
                   # v1.70.88: фаза 3 (число организаций), сравнение позиций копий и
                   # фаза 4а (сверка копий) — тоже единый реестр
                   "tests/test_unified_phase3.py", "tests/test_unified_item_comparison.py",
                   "tests/test_unified_copies.py",
                   # v1.70.89: фаза 4б — правило слияния и страж единой точки записи
                   "tests/test_unified_merge.py", "tests/test_unified_write_point.py",
                   # v1.70.91: шаг 4в — данные единой записи в карточке
                   "tests/test_unified_read.py"},
    AREA_UI: {"tests/test_v17033.py", "tests/test_v17035.py", "tests/test_v17059.py",
              "tests/test_v17060.py"},
}
CRITICAL_AREAS = {AREA_POSTGRES, AREA_UNIFIED}
# Задачи GitHub: пропуски — разбор #19; критичные для миграции — сквозной тест
# #20; остальные охраняет общий контур CI — #12.
ISSUE_SKIPS, ISSUE_MIGRATION, ISSUE_CI = "#19", "#20", "#12"

COLUMNS = ["Тест", "Файл", "Область", "Критичен для миграции", "Статус локально",
           "Статус в CI", "Пропуск на ffe77e3 (без npm и PostgreSQL)", "Причина пропуска",
           "Issue", "Ссылка на файл"]


def nodeid(classname: str, name: str) -> str:
    """tests.test_x.TestY + test_z → tests/test_x.py::TestY::test_z."""
    parts = classname.split(".")
    path = "/".join(parts[:2]) + ".py"
    klass = "::".join(parts[2:])
    return f"{path}::{klass}::{name}" if klass else f"{path}::{name}"


def local_status(case) -> str:
    for tag, label in (("failure", "упал"), ("error", "ошибка"), ("skipped", "пропущен")):
        node = case.find(tag)
        if node is not None:
            reason = " ".join((node.get("message") or "").split())[:200]
            return f"{label}: {reason}" if reason else label
    return "прошёл"


def area_of(path: str) -> str:
    for area, files in FILES_BY_AREA.items():
        if path in files:
            return area
    return AREA_APP


def load_skips(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {row["Тест"]: row["Причина пропуска"] for row in csv.DictReader(fh)}


def build_rows(junit_paths: list[Path], commit: str, ci_run: str | None,
               ci_fingerprint: str | None, skips: dict[str, str]) -> tuple[list[dict], dict]:
    from defusedxml import ElementTree

    reports = [junit_gate.parse(p) for p in junit_paths]
    local_fp = junit_gate.fingerprint(reports)
    ci_confirmed = bool(ci_fingerprint) and ci_fingerprint == local_fp
    if ci_confirmed:
        ci_text = f"прошёл (run {ci_run})" if ci_run else "прошёл"
    else:
        ci_text = "нет данных: набор тестов не совпал с прогоном CI" if ci_fingerprint else "нет данных"

    rows = []
    for path in junit_paths:
        for case in ElementTree.parse(path).getroot().iter("testcase"):
            tid = nodeid(case.get("classname") or "", case.get("name") or "?")
            file = tid.split("::")[0]
            area = area_of(file)
            critical = area in CRITICAL_AREAS
            skipped_then = tid in skips
            rows.append({
                "Тест": tid,
                "Файл": file,
                "Область": area,
                "Критичен для миграции": "да" if critical else "нет",
                "Статус локально": local_status(case),
                "Статус в CI": ci_text,
                "Пропуск на ffe77e3 (без npm и PostgreSQL)": "да" if skipped_then else "нет",
                "Причина пропуска": skips.get(tid, ""),
                "Issue": ISSUE_SKIPS if skipped_then else ISSUE_MIGRATION if critical else ISSUE_CI,
                "Ссылка на файл": f"{REPO}/blob/{commit}/{file}",
            })
    rows.sort(key=lambda r: (r["Критичен для миграции"] != "да", r["Файл"], r["Тест"]))
    meta = {"local_fingerprint": local_fp, "ci_confirmed": ci_confirmed,
            "skips_found": sum(r["Пропуск на ffe77e3 (без npm и PostgreSQL)"] == "да" for r in rows)}
    return rows, meta


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--junit", nargs="+", type=Path, required=True)
    ap.add_argument("--commit", required=True, help="коммит, на котором получены отчёты")
    ap.add_argument("--ci-run", help="номер прогона CI на том же коммите")
    ap.add_argument("--ci-fingerprint", help="отпечаток набора из итога ворот CI")
    ap.add_argument("--skips", type=Path, default=SKIPS_CSV)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    paths = [p for p in args.junit if p.is_file()]
    if not paths:
        print("test-matrix: нет отчётов JUnit", file=sys.stderr)
        return 1
    skips = load_skips(args.skips)
    rows, meta = build_rows(paths, args.commit, args.ci_run, args.ci_fingerprint, skips)
    args.out.write_text(to_csv(rows), encoding="utf-8")

    by_area = Counter(r["Область"] for r in rows)
    local = Counter(r["Статус локально"].split(":")[0] for r in rows)
    print(f"test-matrix: тестов {len(rows)}; по областям: "
          + ", ".join(f"{k} — {v}" for k, v in sorted(by_area.items())))
    print("test-matrix: локально: " + ", ".join(f"{k} — {v}" for k, v in sorted(local.items())))
    print(f"test-matrix: отпечаток локальных отчётов {meta['local_fingerprint']}; CI "
          + ("подтверждён" if meta["ci_confirmed"] else "НЕ подтверждён"))
    if meta["skips_found"] != len(skips):
        print(f"test-matrix: из {len(skips)} тестов аудита пропусков найдено "
              f"{meta['skips_found']} — тесты переименованы?", file=sys.stderr)
        return 1
    if args.ci_fingerprint and not meta["ci_confirmed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
