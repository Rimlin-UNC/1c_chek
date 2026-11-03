"""Среда агента: реестр выпусков, «бригада тестировщиков», проверка старта, задание и чек-листы.

Задание владельца 03.11.2026: быстрая и предсказуемая работа в Arena — история,
которая позволяет вспомнить и откатиться (docs/kb/09_releases.csv), проверки выпуска
одной командой (qa/release_gate.py), старт сессии с точной подсказкой при расхождении
с GitHub (qa/session_check.py), задание агента, чек-лист задачи и политика
коннекторов в AGENTS.md, исследования и нововведения с источниками
(docs/kb/10_research.md).
"""
from __future__ import annotations

import csv
import re
import subprocess
from pathlib import Path

import pytest

from qa import release_gate, session_check

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "docs" / "kb"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def releases() -> list[dict]:
    return list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))


class TestReleaseLedger:
    def test_columns_and_line_endings(self):
        raw = (KB / "09_releases.csv").read_bytes()
        assert b"\r\n" not in raw                      # базы docs/kb — с LF
        assert raw.decode("utf-8").splitlines()[0] == (
            "Версия,Дата,Коммит,Суть,Тестов в CI,Прогон CI,Репетиция,Этап Б,Опора репетиции,"
            "Откат на неё,Источник")

    def test_versions_ascend_and_end_with_current(self):
        from app.config import settings
        versions = [release_gate.version_key(r["Версия"]) for r in releases()]
        assert versions == sorted(versions) and len(set(versions)) == len(versions)
        assert releases()[-1]["Версия"] == settings.APP_VERSION

    def test_every_row_has_source_and_known_commit(self):
        for row in releases():
            assert row["Источник"].startswith("https://github.com/Rimlin-UNC/1c_chek/"), row["Версия"]
            commit = row["Коммит"]
            assert re.fullmatch(r"[0-9a-f]{40}", commit) or commit.startswith("требует уточнения"), commit
            if row["Опора репетиции"] == "да":
                assert re.fullmatch(r"[0-9a-f]{40}", commit), row["Версия"]
            assert row["Опора репетиции"] in ("да", "нет")

    def test_release_commits_carry_their_version(self):
        """Коммит опоры — коммит выпуска: его сообщение начинается с «vX.Y.Z:» (если он есть локально)."""
        for row in releases():
            if row["Опора репетиции"] != "да" or not release_gate.have_commit(row["Коммит"]):
                continue
            subject = subprocess.run(["git", "log", "-1", "--format=%s", row["Коммит"]], cwd=ROOT,
                                     capture_output=True, text=True, check=True).stdout
            assert subject.startswith(f"v{row['Версия']}:"), (row["Версия"], subject[:60])


class TestReleaseGate:
    def test_refs_are_older_than_current_and_sorted(self):
        current = release_gate.current_version()
        refs = release_gate.load_refs(below=current)
        assert refs and all(release_gate.version_key(v) < release_gate.version_key(current) for v, _ in refs)
        assert [v for v, _ in refs] == sorted((v for v, _ in refs), key=release_gate.version_key)
        assert "1.70.81" not in {v for v, _ in refs}   # промежуточная сборка — не опора
        assert refs[0][0] == "1.70.80"                # версия рабочего сервера по отчёту владельца

    def test_select_by_version_or_commit_prefix(self):
        refs = [("1.70.88", "faa6e75" + "0" * 33), ("1.70.89", "2976560" + "0" * 33)]
        assert release_gate.select(refs, None) == refs
        assert release_gate.select(refs, ["2976560"]) == refs[1:]
        assert release_gate.select(refs, ["1.70.88"]) == refs[:1]

    def test_summarize_counts_json_checks(self):
        report = {"checks": [{"check": "а", "ok": True}, {"check": "б", "ok": False}, {"check": "в", "ok": True}]}
        assert release_gate.summarize(report) == (2, 3, ["б"])
        assert release_gate.summarize({}) == (0, 0, [])

    def test_suites_run_sequentially_from_ledger(self):
        src = read("qa/release_gate.py")
        assert 'SUITES = {"rehearsal": "qa.upgrade_rehearsal", "stage-b": "qa.live_update"}' in src
        assert "09_releases.csv" in src and "Опора репетиции" in src
        assert "ThreadPool" not in src and "concurrent.futures" not in src   # строго по одному


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Временный репозиторий A → B → C; session_check работает в нём."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "agent@example.invalid")
    _git(path, "config", "user.name", "Агент")
    shas = []
    for step in ("A", "B", "C"):
        (path / "file.txt").write_text(f"шаг {step}\n", encoding="utf-8")
        (path / f"{step}.txt").write_text(step, encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", f"шаг {step}")
        shas.append(_git(path, "rev-parse", "HEAD"))
    monkeypatch.setattr(session_check, "ROOT", path)
    return path, shas


class TestSessionCheckDivergence:
    def test_pointer_behind_files_match_remote(self, repo):
        path, (a, _, c) = repo
        _git(path, "reset", "-q", "--mixed", a)              # указатель на A, файлы — C
        level, what, fix = session_check.diagnose_divergence(a, c)
        assert level == "ОШИБКА" and "файлы совпадают" in what
        assert fix == f"безопасно выполнить: git reset --mixed {c[:7]}"

    def test_files_equal_older_commit_of_remote(self, repo):
        path, (a, b, c) = repo
        _git(path, "checkout", "-q", b, "--", ".")
        (path / "C.txt").unlink()                             # файлы ровно как в B
        _git(path, "reset", "-q", "--mixed", a)
        level, what, fix = session_check.diagnose_divergence(a, c)
        assert f"файлы = коммиту {b[:7]}" in what and "незакоммиченной работы нет" in what
        assert fix == f"безопасно выполнить: git reset --mixed {b[:7]} && git merge --ff-only {c[:7]}"

    def test_uncommitted_work_is_never_reset_hard(self, repo):
        path, (a, b, c) = repo
        _git(path, "checkout", "-q", b, "--", ".")
        (path / "C.txt").unlink()
        (path / "wip.txt").write_text("незакоммиченная работа", encoding="utf-8")
        _git(path, "reset", "-q", "--mixed", a)
        level, what, fix = session_check.diagnose_divergence(a, c)
        assert "незакоммиченная работа поверх" in what and f"{b[:7]} (1 файл(ов))" in what
        assert "reset --hard" in fix and "без reset --hard и push --force" in fix
        assert fix.startswith(f"не терять: git reset --mixed {b[:7]}")


class TestAgentDocs:
    def test_mission_checklist_policy_history(self):
        agents = read("AGENTS.md")
        for head in ("## Задание агента (владелец, 03.11.2026)", "## Чек-лист задачи (от идеи до отчёта)",
                     "## История и откат", "### Политика: только нужные коннекторы"):
            assert head in agents, head
        mission = agents.split("## Задание агента (владелец, 03.11.2026)", 1)[1].split("\n## ", 1)[0]
        for needle in ("№ 1", "Только вперёд", "Хабр", "10_research.md", "реестр выпусков", "release_gate.py"):
            assert needle in mission, needle
        policy = agents.split("### Политика: только нужные коннекторы", 1)[1].split("\n### ", 1)[0]
        for name in ("GitHub", "Google Sheets, Docs, Slides", "Supabase", "Vercel", "Google Drive", "Notion"):
            assert f"| {name}" in policy, name

    def test_kb_index_links_new_sections(self):
        index = read("docs/kb/README.md")
        assert "(09_releases.md)" in index and "(09_releases.csv)" in index and "(10_research.md)" in index

    def test_research_cites_sources_and_feeds_roadmap(self):
        research = read("docs/kb/10_research.md")
        table = research.split("## Конкуренты и аналоги", 1)[1].split("\n## ", 1)[0]
        rows = [ln for ln in table.splitlines() if ln.startswith("| ") and not ln.startswith("| Продукт")]
        assert len(rows) >= 5 and all("](https://" in ln for ln in rows)
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        for n in range(41, 50):
            row = tasks[f"T-{n}"]
            assert row["Статус"] == "todo" and row["Источник"].endswith("docs/kb/10_research.md")
            assert f"| T-{n} |" in research
        questions = {r["ID"]: r for r in csv.DictReader(read("docs/kb/06_open_questions.csv").splitlines())}
        assert questions["Q-29"]["Статус"] == "open" and questions["Q-29"]["Кто решает"] == "владелец"

    def test_relative_links_resolve(self):
        """Относительные ссылки AGENTS.md и docs/kb ведут на существующие файлы."""
        link = re.compile(r"\]\(([^)\s]+)\)")
        broken = []
        for path in (ROOT / "AGENTS.md", *sorted(KB.glob("*.md"))):
            text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
            for target in link.findall(text):
                if re.match(r"^(https?:|mailto:|#)", target):
                    continue
                file_part = target.split("#", 1)[0]
                if file_part and not (path.parent / file_part).resolve().exists():
                    broken.append(f"{path.relative_to(ROOT)} → {target}")
        assert not broken, broken
