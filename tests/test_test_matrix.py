"""qa/test_matrix.py — генератор матрицы тестирования миграции.

Матрица — представление данных репозитория в Google Sheets, поэтому сам
генератор проверяется: статус в CI ставится только при совпадении отпечатка
набора тестов, области и задачи берутся из явных правил, исчезновение тестов
из аудита пропусков роняет генерацию.
"""
from __future__ import annotations

import csv

from qa import junit_gate, test_matrix


def _junit(tmp_path, name, cases):
    path = tmp_path / name
    path.write_text('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
                    f"{cases}</testsuite></testsuites>", encoding="utf-8")
    return path


PG = ('<testcase classname="tests.test_postgres_identity_optin" '
      'name="test_postgres_identity_lock_serializes_transactions[commit]"/>')
UI_SKIP = ('<testcase classname="tests.test_v17060.TestPlaywrightGate" '
           'name="test_scenarios_pass_with_browsers"><skipped message="браузеры не загружены"/></testcase>')
APP_FAIL = ('<testcase classname="tests.test_roles.TestRoles" name="test_admin">'
            '<failure message="assert 1 == 2">trace</failure></testcase>')
UNIFIED = '<testcase classname="tests.test_v17082.TestRegistry" name="test_move"/>'


def _skips(tmp_path):
    path = tmp_path / "skips.csv"
    path.write_text("Тест,Причина пропуска\n"
                    "tests/test_postgres_identity_optin.py::test_postgres_identity_lock_serializes_transactions[commit],"
                    "dedicated PostgreSQL test URL not configured\n", encoding="utf-8")
    return path


def _rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["Тест"]: r for r in csv.DictReader(fh)}


def test_nodeid_keeps_class_and_module_level_functions():
    assert test_matrix.nodeid("tests.test_x.TestY", "test_z") == "tests/test_x.py::TestY::test_z"
    assert test_matrix.nodeid("tests.test_x", "test_z[a]") == "tests/test_x.py::test_z[a]"


def test_rows_carry_area_status_and_issue(tmp_path):
    report = _junit(tmp_path, "full.xml", PG + UI_SKIP + APP_FAIL + UNIFIED)
    out = tmp_path / "m.csv"
    assert test_matrix.main(["--junit", str(report), "--commit", "abc123",
                             "--skips", str(_skips(tmp_path)), "--out", str(out)]) == 0
    rows = _rows(out)
    pg = rows["tests/test_postgres_identity_optin.py::test_postgres_identity_lock_serializes_transactions[commit]"]
    assert (pg["Область"], pg["Критичен для миграции"], pg["Issue"]) == ("PostgreSQL / Supabase", "да", "#19")
    assert pg["Пропуск на ffe77e3 (без npm и PostgreSQL)"] == "да"
    assert pg["Причина пропуска"] == "dedicated PostgreSQL test URL not configured"
    assert pg["Ссылка на файл"].endswith("/blob/abc123/tests/test_postgres_identity_optin.py")
    unified = rows["tests/test_v17082.py::TestRegistry::test_move"]
    assert (unified["Область"], unified["Issue"], unified["Статус локально"]) == \
        ("Единый реестр / перенос данных", "#20", "прошёл")
    ui = rows["tests/test_v17060.py::TestPlaywrightGate::test_scenarios_pass_with_browsers"]
    assert (ui["Область"], ui["Критичен для миграции"]) == ("Интерфейс и JS-цепочка", "нет")
    assert ui["Статус локально"] == "пропущен: браузеры не загружены"
    app = rows["tests/test_roles.py::TestRoles::test_admin"]
    assert (app["Область"], app["Issue"], app["Статус локально"]) == \
        ("Функционал приложения", "#12", "упал: assert 1 == 2")
    assert all(r["Статус в CI"] == "нет данных" for r in rows.values())
    # критичные для миграции — в начале таблицы
    assert list(rows)[0].startswith(("tests/test_postgres", "tests/test_v17082"))


def test_ci_status_only_when_fingerprint_matches(tmp_path, capsys):
    report = _junit(tmp_path, "full.xml", PG + UNIFIED)
    fp = junit_gate.fingerprint([junit_gate.parse(report)])
    out = tmp_path / "ok.csv"
    assert test_matrix.main(["--junit", str(report), "--commit", "c", "--ci-run", "41", "--ci-fingerprint", fp,
                             "--skips", str(_skips(tmp_path)), "--out", str(out)]) == 0
    assert {r["Статус в CI"] for r in _rows(out).values()} == {"прошёл (run 41)"}
    assert "CI подтверждён" in capsys.readouterr().out
    bad = tmp_path / "bad.csv"
    assert test_matrix.main(["--junit", str(report), "--commit", "c", "--ci-run", "41",
                             "--ci-fingerprint", "0" * len(fp), "--skips", str(_skips(tmp_path)),
                             "--out", str(bad)]) == 1
    assert {r["Статус в CI"] for r in _rows(bad).values()} == \
        {"нет данных: набор тестов не совпал с прогоном CI"}


def test_missing_audited_test_fails_generation(tmp_path, capsys):
    report = _junit(tmp_path, "full.xml", UNIFIED)          # теста из аудита нет
    assert test_matrix.main(["--junit", str(report), "--commit", "c",
                             "--skips", str(_skips(tmp_path)), "--out", str(tmp_path / "m.csv")]) == 1
    assert "тесты переименованы?" in capsys.readouterr().err


def test_real_skip_audit_matches_current_tests():
    """Все 12 тестов из qa/reports/skips_ffe77e3.csv существуют в текущем наборе."""
    import subprocess
    import sys
    skips = test_matrix.load_skips(test_matrix.SKIPS_CSV)
    assert len(skips) == 12
    files = sorted({tid.split("::")[0] for tid in skips})
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           *files], cwd=test_matrix.ROOT, capture_output=True, text=True, timeout=300,
                          check=False)
    collected = set(proc.stdout.split())
    missing = [tid for tid in skips if tid not in collected]
    assert not missing, f"тесты из аудита пропусков не найдены: {missing}"


def test_every_unified_test_file_is_migration_critical():
    """v1.70.88: каждый tests/test_unified_*.py попадает в область единого реестра или PostgreSQL.

    Списки областей явные; без этой проверки новый файл единого реестра (так было
    с фазой 3 и сверкой копий) молча уходил в «Функционал приложения» и терял
    отметку «критичен для миграции».
    """
    critical = set().union(*(test_matrix.FILES_BY_AREA[a] for a in test_matrix.CRITICAL_AREAS))
    files = {f"tests/{p.name}" for p in (test_matrix.ROOT / "tests").glob("test_unified_*.py")}
    assert files and not files - critical, sorted(files - critical)
    for area_files in test_matrix.FILES_BY_AREA.values():
        assert all((test_matrix.ROOT / f).is_file() for f in area_files), area_files
