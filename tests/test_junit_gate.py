# -*- coding: utf-8 -*-
"""qa/junit_gate.py — ворота CI по отчётам JUnit XML.

Ворота пропускают только полностью выполненный прогон: упавший, ошибочный
или пропущенный тест, пустой отчёт или отсутствие отчётов дают код 1. Отчёт —
в формате, который пишет ``pytest --junitxml``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from qa import junit_gate

ROOT = Path(__file__).resolve().parents[1]


def _xml(cases: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
            f'<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">{cases}'
            '</testsuite></testsuites>')


PASS = '<testcase classname="tests.test_a" name="test_ok" time="0.01"/>'
SKIP = ('<testcase classname="tests.test_b" name="test_pg" time="0">'
        '<skipped type="pytest.skip" message="dedicated PostgreSQL test URL not configured">'
        'tests/test_b.py:5: dedicated PostgreSQL test URL not configured</skipped></testcase>')
FAIL = ('<testcase classname="tests.test_c" name="test_bad" time="0.1">'
        '<failure message="assert 1 == 2">trace</failure></testcase>')
ERROR = ('<testcase classname="tests.test_d" name="test_fixture" time="0">'
         '<error message="failed on setup with &quot;boom&quot;">trace</error></testcase>')


def _write(tmp_path, name, cases):
    path = tmp_path / name
    path.write_text(_xml(cases), encoding="utf-8")
    return str(path)


def test_all_passed_is_ok(tmp_path, capsys):
    rc = junit_gate.main([_write(tmp_path, "full.xml", PASS * 3),
                          _write(tmp_path, "full-test_v1310.xml", PASS)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "тестов 4: прошло 4, упало 0, ошибок 0, пропущено 0" in out


def test_skip_fails_the_gate_and_names_the_reason(tmp_path, capsys):
    rc = junit_gate.main([_write(tmp_path, "full.xml", PASS + SKIP)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "SKIPPED [full.xml] tests.test_b::test_pg — dedicated PostgreSQL test URL not configured" \
        in captured.out
    assert "пропуски" in captured.err


def test_failure_and_error_fail_the_gate(tmp_path, capsys):
    assert junit_gate.main([_write(tmp_path, "f.xml", PASS + FAIL)]) == 1
    assert junit_gate.main([_write(tmp_path, "e.xml", PASS + ERROR)]) == 1
    out = capsys.readouterr().out
    assert "FAILED [f.xml] tests.test_c::test_bad — assert 1 == 2" in out
    assert 'ERROR [e.xml] tests.test_d::test_fixture — failed on setup with "boom"' in out


def test_no_reports_or_zero_tests_fail_the_gate(tmp_path):
    assert junit_gate.main([]) == 1
    assert junit_gate.main([str(tmp_path / "missing.xml")]) == 1
    assert junit_gate.main([_write(tmp_path, "empty.xml", "")]) == 1


def test_step_summary_table_is_written(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    junit_gate.main(["--title", "Тесты (Python 3.12)",
                     _write(tmp_path, "full.xml", PASS + SKIP)])
    text = summary.read_text(encoding="utf-8")
    assert "### Тесты (Python 3.12)" in text
    assert "| `full.xml` | 2 | 1 | 0 | 0 | 1 |" in text
    assert "**Пропущен:** `tests.test_b::test_pg" in text


def test_runs_as_a_script(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "qa" / "junit_gate.py"),
                           _write(tmp_path, "full.xml", PASS)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_github_annotations_only_inside_actions(tmp_path, capsys, monkeypatch):
    report = _write(tmp_path, "full.xml", PASS + FAIL + SKIP)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    junit_gate.main([report])
    assert "::error" not in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    junit_gate.main([report])
    out = capsys.readouterr().out
    assert "::notice title=Тесты::отчётов 1, тестов 3: прошло 1, упало 1, ошибок 0, пропущено 1" in out
    assert "::error title=FAILED tests.test_c%3A%3Atest_bad::assert 1 == 2%0A%0Atrace" in out
    assert "::warning title=SKIPPED tests.test_b%3A%3Atest_pg::dedicated PostgreSQL" in out


def test_annotations_escape_and_respect_the_per_step_limit(tmp_path):
    many = "".join(
        f'<testcase classname="tests.t" name="test_{i}" time="0">'
        f'<failure message="bad 100%, line {i}">a\nb</failure></testcase>' for i in range(12))
    path = tmp_path / "many.xml"
    path.write_text(_xml(many), encoding="utf-8")
    lines = junit_gate.github_annotations([junit_gate.parse(path)])
    errors = [ln for ln in lines if ln.startswith("::error")]
    assert len(errors) == junit_gate.MAX_EACH + 1          # 9 по одной + сводная
    assert "bad 100%25, line 0%0A%0Aa%0Ab" in errors[0]     # % и переводы строк экранированы
    assert errors[-1].startswith("::error title=FAILED%3A ещё 3::")
    assert "tests.t::test_11" in errors[-1]
