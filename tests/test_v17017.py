# ==============================================================================
#  Ямастер Чек — тесты v1.70.17 «Обновление из приложения на другую ветку»
#  РЕГРЕССИЯ v1.70.16 и старше: deploy.sh клонирует `--depth 1 --branch <ветка>`,
#  такой клон отслеживает только свою ветку; `git fetch origin <другая ветка>`
#  возвращает rc 0, но обновляет один FETCH_HEAD — ref origin/<ветка> не
#  появляется, и `git reset --hard origin/<ветка>` падал:
#      fatal: ambiguous argument 'origin/<ветка>': unknown revision…
#  Обновление откатывалось, журнал показывал «Изменённых файлов: 0».
#  Здесь: живой git (локальный file://-remote, сеть не нужна) — клон ровно как
#  на сервере, затем fetch/refspec/reset новой механикой.
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings

import app.services.updater as up

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git недоступен")


def _read(path):
    return open(path, encoding="utf-8").read()


def _git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True)
    if check:
        assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r


# ==============================================================================
#  Версии и документы
# ==============================================================================
class TestVersions17017:
    def test_version(self):
        assert settings.APP_VERSION == "1.70.17"

    def test_changelog(self):
        ch = _read("CHANGELOG.md")
        assert ch.count("## [1.70.17]") == 1
        assert ch.index("## [1.70.17]") < ch.index("## [1.70.16]")
        block = ch[ch.index("## [1.70.17]"):ch.index("## [1.70.16]")]
        assert "ambiguous argument" in block
        assert "refs/remotes/origin" in block
        assert "deploy.sh" in block and "set-branches" in block

    def test_whatsnew(self):
        js = _read("app/static/js/app.js")
        assert "'1.70.17':" in js
        assert js.index("'1.70.17':") < js.index("'1.70.16':")

    def test_versions_everywhere(self):
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert settings.APP_VERSION in _read(f), f


# ==============================================================================
#  Механика: refspec, дописка ветки, разрешение цели
# ==============================================================================
class TestRefspecHelpers:
    def test_refspec_shape(self):
        assert up._branch_refspec("main") == \
            "+refs/heads/main:refs/remotes/origin/main"
        assert up._branch_refspec("arena/071f4163-1c-chek") == \
            ("+refs/heads/arena/071f4163-1c-chek:"
             "refs/remotes/origin/arena/071f4163-1c-chek")

    def test_refspec_is_forced(self):
        """«+» обязателен: без него fetch в shallow-клон мог бы отказаться
        обновить ref (non-fast-forward)."""
        assert up._branch_refspec("x").startswith("+")

    def test_fetch_cmd_uses_refspec(self):
        src = _read("app/services/updater.py")
        fn = src.split("def _git_fetch_progress")[1].split("\ndef ")[0]
        assert "_branch_refspec(branch)" in fn
        assert "_ensure_branch_tracking(branch)" in fn
        assert '"origin", branch]' not in fn

    def test_resolve_target_ref_prefers_origin(self, monkeypatch):
        monkeypatch.setattr(up, "_ref_exists",
                            lambda ref: ref == "origin/main")
        ref, msg = up._resolve_target_ref("main")
        assert ref == "origin/main" and "origin/main" in msg

    def test_resolve_target_ref_falls_back_to_fetch_head(self, monkeypatch):
        monkeypatch.setattr(up, "_ref_exists",
                            lambda ref: ref == "FETCH_HEAD")
        ref, msg = up._resolve_target_ref("main")
        assert ref == "FETCH_HEAD" and "запасной" in msg

    def test_resolve_target_ref_honest_error(self, monkeypatch):
        monkeypatch.setattr(up, "_ref_exists", lambda ref: False)
        ref, msg = up._resolve_target_ref("feature/x")
        assert ref == ""
        assert "feature/x" in msg and "Источник обновлений" in msg
        assert "ambiguous argument" not in msg      # человекочитаемо

    def test_changed_files_strict_raises(self, monkeypatch):
        monkeypatch.setattr(up, "_git",
                            lambda *a, **k: (128, "fatal: ambiguous argument"))
        with pytest.raises(RuntimeError) as ei:
            up._changed_files("abc123", "origin/nope", strict=True)
        assert "сравнить" in str(ei.value)

    def test_changed_files_lenient_by_default(self, monkeypatch):
        """Обратная совместимость: без strict — пустой список (test_v1530)."""
        monkeypatch.setattr(up, "_git", lambda *a, **k: (128, "fatal"))
        assert up._changed_files("abc123", "origin/nope") == []

    def test_do_apply_uses_resolved_ref(self):
        """_do_apply обязан брать цель из _resolve_target_ref и сравнивать
        строго — иначе снова получим «Изменённых файлов: 0» перед падением."""
        src = _read("app/services/updater.py")
        body = src.split("def _do_apply")[1].split("\ndef start_apply")[0]
        assert "_resolve_target_ref(branch)" in body
        assert "strict=True" in body
        assert 'reset", "--hard", target_ref' in body
        assert 'f"origin/{branch}"' not in body


# ==============================================================================
#  Живой git: клон как на сервере (--depth 1 --branch) и чужая ветка
# ==============================================================================
@pytest.fixture()
def server_like_clone(tmp_path):
    """origin с двумя ветками + клон ТОЛЬКО ветки main (как ставит deploy.sh)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main", str(origin)], origin.parent)
    _git(["config", "user.name", "t"], origin)
    _git(["config", "user.email", "t@t"], origin)
    (origin / "app").mkdir()
    (origin / "app" / "config.py").write_text(
        'APP_VERSION: str = "1.70.15"\n', encoding="utf-8")
    (origin / "README.md").write_text("main\n", encoding="utf-8")
    _git(["add", "."], origin)
    _git(["commit", "-qm", "v1.70.15"], origin)

    # вторая ветка — «новая версия»
    _git(["checkout", "-q", "-b", "arena/new-branch"], origin)
    (origin / "app" / "config.py").write_text(
        'APP_VERSION: str = "1.70.17"\n', encoding="utf-8")
    (origin / "NEW_FILE.txt").write_text("new\n", encoding="utf-8")
    _git(["add", "."], origin)
    _git(["commit", "-qm", "v1.70.17"], origin)
    _git(["checkout", "-q", "main"], origin)

    clone = tmp_path / "appdir"
    # file:// — чтобы --depth реально применился (для локального пути git
    # глубину игнорирует), и clone --branch = single-branch refspec
    _git(["clone", "-q", "--depth", "1", "--branch", "main",
          f"file://{origin}", str(clone)], origin.parent)
    cfg = (clone / ".git" / "config").read_text(encoding="utf-8")
    assert cfg.count("fetch = ") == 1, "клон обязан отслеживать одну ветку"
    assert "refs/heads/main" in cfg and "arena/new-branch" not in cfg
    return origin, clone


@needs_git
class TestRealGitRegression:
    def test_root_cause_reproduced_without_refspec(self, server_like_clone,
                                                    monkeypatch):
        """Фиксируем причину: обычный fetch чужой ветки rc 0, но ref не создан."""
        _origin, clone = server_like_clone
        r = _git(["fetch", "-q", "origin", "arena/new-branch"], clone)
        assert r.returncode == 0                    # как на сервере: «успех»
        bad = _git(["rev-parse", "--verify", "--quiet",
                    "origin/arena/new-branch"], clone, check=False)
        assert bad.returncode != 0                  # ref НЕ появился
        rst = _git(["reset", "--hard", "origin/arena/new-branch"], clone,
                   check=False)
        assert rst.returncode != 0
        assert "ambiguous argument" in (rst.stdout + rst.stderr)

    def test_fetch_with_refspec_creates_ref(self, server_like_clone,
                                            monkeypatch):
        _origin, clone = server_like_clone
        monkeypatch.setattr(up, "APP_DIR", str(clone))
        up.job.reset()
        try:
            rc, out, _n = up._git_fetch_progress("arena/new-branch")
        finally:
            up.job.reset()
        assert rc == 0, out
        # ref создан — ровно то, чего не хватало на сервере
        ok = _git(["rev-parse", "--verify", "--quiet",
                   "origin/arena/new-branch"], clone, check=False)
        assert ok.returncode == 0, "origin/<ветка> не создан"

    def test_tracking_added_to_config(self, server_like_clone, monkeypatch):
        """Ветка дописана в remote.origin.fetch — следующий fetch обновит ref
        уже без явного refspec (и `git fetch` без аргументов тоже)."""
        _origin, clone = server_like_clone
        monkeypatch.setattr(up, "APP_DIR", str(clone))
        up.job.reset()
        try:
            up._git_fetch_progress("arena/new-branch")
        finally:
            up.job.reset()
        cfg = (clone / ".git" / "config").read_text(encoding="utf-8")
        assert "refs/heads/arena/new-branch:refs/remotes/origin/" \
               "arena/new-branch" in cfg

    def test_full_update_sequence(self, server_like_clone, monkeypatch):
        """Полная последовательность апдейтера: fetch → сравнение → reset."""
        origin, clone = server_like_clone
        monkeypatch.setattr(up, "APP_DIR", str(clone))
        old_commit = up._local_commit()
        assert old_commit, "не удалось прочитать текущий коммит клона"

        up.job.reset()
        try:
            rc, out, _n = up._git_fetch_progress("arena/new-branch")
        finally:
            up.job.reset()
        assert rc == 0, out

        target_ref, msg = up._resolve_target_ref("arena/new-branch")
        assert target_ref == "origin/arena/new-branch", msg

        changed = up._changed_files(old_commit, target_ref, strict=True)
        assert "app/config.py" in changed and "NEW_FILE.txt" in changed
        assert up._needs_restart(changed) is True

        rc, out = up._git(["reset", "--hard", target_ref], timeout=60)
        assert rc == 0, out                         # здесь и падало на сервере
        rc, out = up._git(["clean", "-fd", "-e", "data", "-e", ".env",
                           "-e", "venv", "-e", "*.db"], timeout=60)
        assert rc == 0, out
        cfg = (clone / "app" / "config.py").read_text(encoding="utf-8")
        assert 'APP_VERSION: str = "1.70.17"' in cfg
        assert (clone / "NEW_FILE.txt").exists()

    def test_unknown_branch_fails_clearly(self, server_like_clone,
                                          monkeypatch):
        _origin, clone = server_like_clone
        monkeypatch.setattr(up, "APP_DIR", str(clone))
        up.job.reset()
        try:
            rc, out, nbytes = up._git_fetch_progress("no-such-branch")
        finally:
            up.job.reset()
        assert rc != 0 and nbytes == 0
        ref, msg = up._resolve_target_ref("no-such-branch")
        assert ref == "" and "no-such-branch" in msg

    def test_ensure_git_repo_tracks_branch(self, server_like_clone,
                                           monkeypatch):
        """Самовосстановление репозитория тоже дописывает ветку в origin."""
        origin, clone = server_like_clone
        shutil.rmtree(clone / ".git")               # «сломанный» репозиторий
        monkeypatch.setattr(up, "APP_DIR", str(clone))
        ok, msg = up._ensure_git_repo("x/y", "arena/new-branch",
                                      remote_url=f"file://{origin}")
        assert ok, msg
        cfg = (clone / ".git" / "config").read_text(encoding="utf-8")
        assert "arena/new-branch" in cfg
        assert 'APP_VERSION: str = "1.70.17"' in \
            (clone / "app" / "config.py").read_text(encoding="utf-8")

    def test_same_branch_update_still_works(self, server_like_clone,
                                            monkeypatch):
        """Своя ветка (обычный сценарий) не сломана новым refspec'ом."""
        origin, clone = server_like_clone
        _git(["checkout", "-q", "main"], origin)
        (origin / "app" / "config.py").write_text(
            'APP_VERSION: str = "1.70.16"\n', encoding="utf-8")
        _git(["commit", "-qam", "v1.70.16"], origin)
        monkeypatch.setattr(up, "APP_DIR", str(clone))
        up.job.reset()
        try:
            rc, out, _n = up._git_fetch_progress("main")
        finally:
            up.job.reset()
        assert rc == 0, out
        ref, msg = up._resolve_target_ref("main")
        assert ref == "origin/main", msg
        rc, out = up._git(["reset", "--hard", ref], timeout=60)
        assert rc == 0, out
        assert 'APP_VERSION: str = "1.70.16"' in \
            (clone / "app" / "config.py").read_text(encoding="utf-8")


# ==============================================================================
#  deploy.sh: свежие установки отслеживают все ветки
# ==============================================================================
class TestDeployTracksAllBranches:
    def test_set_branches_wildcard(self):
        sh = _read("deploy.sh")
        assert "remote set-branches origin '*'" in sh

    def test_block_is_best_effort(self):
        sh = _read("deploy.sh")
        block = sh[sh.index("remote set-branches origin '*'"):]
        block = block[:block.index("bold \"4/9")]
        assert "ok " in block and "warn" in block

    def test_comment_explains_reason(self):
        sh = _read("deploy.sh")
        block = sh[sh.index("remote set-branches origin '*'") - 500:]
        assert "ambiguous argument" in block
        assert "v1.70.17" in block


# ==============================================================================
#  UI-харнесс
# ==============================================================================
class TestHarness17017:
    def test_harness_check_added(self):
        h = _read("tests/ui_harness.js")
        assert "1.70.17" in h and "refspec" in h
