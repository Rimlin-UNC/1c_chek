#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI-ворота по отчётам JUnit XML: все тесты выполнены, ни одного пропуска.

Использование:
    python qa/junit_gate.py [--title TEXT] report.xml [report.xml ...]

Печатает итог и каждый пропущенный/упавший тест с причиной. Если задан
``$GITHUB_STEP_SUMMARY``, дописывает туда таблицу итогов. Код выхода 1, если
отчётов нет, тестов ноль, есть упавшие/ошибочные или пропущенные тесты.
В GitHub Actions (``GITHUB_ACTIONS=true``) каждый упавший/пропущенный тест
выводится аннотацией (``::error``/``::warning``) с причиной и хвостом трассировки:
её видно в сводке прогона и в PR, не открывая сырой лог. Итог прогона —
аннотация ``::notice`` (числа доступны и через API аннотаций). В итог входит
отпечаток набора тестов — sha256 отсортированного списка идентификаторов: если
он совпадает с отпечатком локального прогона того же коммита, наборы тестов
идентичны, и при 0 упавших и 0 пропущенных прошёл каждый тест списка
(так матрица тестирования подтверждает статус в CI без доступа к логам).

Почему пропуск — ошибка: тесты цепочки (PostgreSQL, ESLint, Vitest,
Playwright, Newman, stylelint, html-validate) пропускаются, когда инструмент не
установлен. В CI всё ставится заранее, поэтому пропуск означает, что проверка
фактически не выполнялась, — и прогон не должен выглядеть зелёным.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree


TAIL_LINES = 25          # строк трассировки в аннотации
FINGERPRINT_LEN = 16     # знаков отпечатка в итоге (64 бита — коллизии исключены)
MAX_EACH = 9             # GitHub показывает ~10 аннотаций-ошибок на шаг


@dataclass
class Report:
    name: str
    tests: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # (уровень, тест, причина, хвост трассировки) — для аннотаций GitHub
    details: list[tuple[str, str, str, str]] = field(default_factory=list)
    # идентификаторы всех тестов отчёта — для отпечатка набора
    ids: list[str] = field(default_factory=list)

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
        report.ids.append(cid)
        for tag, bucket in (("failure", report.failed), ("error", report.errors),
                            ("skipped", report.skipped)):
            node = case.find(tag)
            if node is not None:
                reason = _reason(node)
                bucket.append(f"{cid} — {reason}")
                tail = "\n".join((node.text or "").strip().splitlines()[-TAIL_LINES:])
                report.details.append((tag, cid, reason, tail))
                break
    return report


def _cmd_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _cmd_prop(text: str) -> str:
    return _cmd_data(text).replace(":", "%3A").replace(",", "%2C")


def github_annotations(reports: list[Report]) -> list[str]:
    """Команды workflow для аннотаций: по одной на первые MAX_EACH упавших
    (вместе с ошибками) и пропущенных, остальные — одной сводной."""
    lines = []
    for kinds, level, label in ((("failure", "error"), "error", "FAILED"),
                                (("skipped",), "warning", "SKIPPED")):
        items = [d for r in reports for d in r.details if d[0] in kinds]
        for _tag, cid, reason, tail in items[:MAX_EACH]:
            body = reason if not tail or tail == reason else f"{reason}\n\n{tail}"
            lines.append(f"::{level} title={_cmd_prop(f'{label} {cid}')}::{_cmd_data(body[:4000])}")
        rest = items[MAX_EACH:]
        if rest:
            listing = "\n".join(cid for _t, cid, _r, _tl in rest[:200])
            lines.append(f"::{level} title={_cmd_prop(f'{label}: ещё {len(rest)}')}::"
                         f"{_cmd_data(listing)}")
    return lines


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


def fingerprint(reports: list[Report]) -> str:
    """Отпечаток набора тестов: sha256 отсортированных идентификаторов из всех
    отчётов. Не зависит ни от порядка тестов, ни от разбиения на отчёты."""
    ids = sorted(cid for r in reports for cid in r.ids)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]


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
    totals = (f"отчётов {len(reports)}, тестов {total}: "
              f"прошло {total - failed - errors - skipped}, упало {failed}, "
              f"ошибок {errors}, пропущено {skipped}; отпечаток набора {fingerprint(reports)}")
    print(f"junit-gate: {totals}")
    for r in reports:
        for kind, items in (("FAILED", r.failed), ("ERROR", r.errors), ("SKIPPED", r.skipped)):
            for item in items:
                print(f"  {kind} [{r.name}] {item}")

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title={_cmd_prop(args.title)}::{_cmd_data(totals)}")
        for line in github_annotations(reports):
            print(line)

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
