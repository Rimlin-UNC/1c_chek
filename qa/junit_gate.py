#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI-ворота по отчётам JUnit XML: все тесты выполнены, ни одного пропуска.

Использование:
    python qa/junit_gate.py [--title TEXT] report.xml [report.xml ...]

Печатает итог и каждый пропущенный/упавший тест с причиной. Если задан
``$GITHUB_STEP_SUMMARY``, дописывает туда таблицу итогов. Код выхода 1, если
отчётов нет, тестов ноль, есть упавшие/ошибочные или пропущенные тесты.

Почему пропуск — ошибка: тесты цепочки (PostgreSQL, ESLint, Vitest,
Playwright, Newman, stylelint, html-validate) пропускаются, когда инструмент не
установлен. В CI всё ставится заранее, поэтому пропуск означает, что проверка
фактически не выполнялась, — и прогон не должен выглядеть зелёным.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree


@dataclass
class Report:
    name: str
    tests: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return self.tests - len(self.failed) - len(self.errors) - len(self.skipped)


def _case_id(case) -> str:
    cls = case.get("classname") or ""
    name = case.get("name") or "?"
    return f"{cls}::{name}" if cls else name


def _reason(node) -> str:
    text = (node.get("message") or node.text or "").strip()
    return " ".join(text.split())[:300]


def parse(path: Path) -> Report:
    report = Report(name=path.name)
    root = ElementTree.parse(path).getroot()
    for case in root.iter("testcase"):
        report.tests += 1
        cid = _case_id(case)
        for tag, bucket in (("failure", report.failed), ("error", report.errors),
                            ("skipped", report.skipped)):
            node = case.find(tag)
            if node is not None:
                bucket.append(f"{cid} — {_reason(node)}")
                break
    return report


def _summary_markdown(title: str, reports: list[Report]) -> str:
    lines = [f"### {title}", "",
             "| Отчёт | Тестов | Прошло | Упало | Ошибки | Пропущено |",
             "|---|---:|---:|---:|---:|---:|"]
    for r in reports:
        lines.append(f"| `{r.name}` | {r.tests} | {r.passed} | {len(r.failed)} | "
                     f"{len(r.errors)} | {len(r.skipped)} |")
    problems = [(kind, item) for r in reports
                for kind, items in (("Упал", r.failed), ("Ошибка", r.errors),
                                    ("Пропущен", r.skipped))
                for item in items]
    if problems:
        lines += ["", "<details><summary>Не прошли или не выполнялись</summary>", ""]
        lines += [f"- **{kind}:** `{item}`" for kind, item in problems[:200]]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--title", default="Тесты")
    ap.add_argument("reports", nargs="*", type=Path)
    args = ap.parse_args(argv)

    paths = [p for p in args.reports if p.is_file()]
    if not paths:
        print("junit-gate: нет отчётов JUnit XML — тесты не запускались", file=sys.stderr)
        return 1
    reports = [parse(p) for p in paths]

    total = sum(r.tests for r in reports)
    failed = sum(len(r.failed) for r in reports)
    errors = sum(len(r.errors) for r in reports)
    skipped = sum(len(r.skipped) for r in reports)
    print(f"junit-gate: отчётов {len(reports)}, тестов {total}: "
          f"прошло {total - failed - errors - skipped}, упало {failed}, "
          f"ошибок {errors}, пропущено {skipped}")
    for r in reports:
        for kind, items in (("FAILED", r.failed), ("ERROR", r.errors), ("SKIPPED", r.skipped)):
            for item in items:
                print(f"  {kind} [{r.name}] {item}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(_summary_markdown(args.title, reports))

    if total == 0:
        print("junit-gate: в отчётах ноль тестов", file=sys.stderr)
        return 1
    if failed or errors:
        print("junit-gate: есть упавшие тесты", file=sys.stderr)
        return 1
    if skipped:
        print("junit-gate: есть пропуски — инструмент цепочки не установлен, "
              "проверка не выполнялась", file=sys.stderr)
        return 1
    print("junit-gate: OK — выполнены все тесты, пропусков нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
