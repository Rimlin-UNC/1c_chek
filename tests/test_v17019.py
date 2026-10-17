# ==============================================================================
#  Ямастер Чек — тесты v1.70.19 «deploy.sh: --branch= больше не игнорируется»
#  До фикса флаг --branch= писал значение прямо в BRANCH, а ниже по скрипту
#  `source /etc/ymaster-check/deploy.conf` перезаписывал BRANCH запомненной
#  веткой; страховка ${OPT_BRANCH:-$BRANCH} не работала, потому что OPT_BRANCH
#  никто не выставлял. На любом сервере, где деплой уже запускался, явная ветка
#  молча подменялась старой. Проверяем:
#    • живой прогон настоящего блока разрешений: --branch= > BRANCH= (окружение)
#      > deploy.conf > main;
#    • подмена видна в логе («Ветка задана явно»), а при conf-only — нет;
#    • статика: OPT_BRANCH объявлен до использования (set -u), ENV_BRANCH
#      снимается ДО чтения конфига, deploy.conf перезаписывается разрешённым
#      значением, --help печатает шапку целиком;
#    • версии/журнал/What’s New/харнесс синхронны.
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings

OLD_BRANCH = "arena/01a0caaa-1c-chek"
CONF_BRANCH = "arena/conf-remembered"
FLAG_BRANCH = "arena/from-flag"
ENV_BRANCH_ = "arena/from-env"

RESOLUTION = 'BRANCH="${OPT_BRANCH:-${ENV_BRANCH:-$BRANCH}}"'


def _read(path):
    return open(path, encoding="utf-8").read()


def _probe_script():
    """Настоящий блок deploy.sh «значения по умолчанию + разбор аргументов +
    разрешение ветки». Единственная подмена — каталог конфига в песочницу
    (вместо /etc), чтобы прогонять сценарии с существующим deploy.conf."""
    sh = _read("deploy.sh")
    start = sh.index('REPO_URL="${REPO_URL:-')
    region = sh[start:sh.index(RESOLUTION) + len(RESOLUTION)]
    assert 'CONF_DIR="/etc/ymaster-check"' in region
    region = region.replace('CONF_DIR="/etc/ymaster-check"',
                            'CONF_DIR="${PROBE_DIR:?}"')
    # захватываем и строку «Ветка задана явно» — она сразу за разрешением
    tail = sh[sh.index(RESOLUTION) + len(RESOLUTION):]
    tail = tail[:tail.index("\nfi\n") + len("\nfi\n")]
    return ("#!/usr/bin/env bash\nset -uo pipefail\n" + region + tail
            + '\necho "RESOLVED_BRANCH=$BRANCH"\n')


def _run(tmp_path, args=(), env_extra=None, conf=None):
    """Прогон пробы; возвращает (stdout, rc)."""
    probe = tmp_path / "probe.sh"
    probe.write_text(_probe_script(), encoding="utf-8")
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir(exist_ok=True)
    if conf is not None:
        (conf_dir / "deploy.conf").write_text(
            f'DOMAIN="chek.ymaster.ru"\nSSL_DOMAIN="chek.ymaster.ru"\n'
            f'BRANCH="{conf}"\n', encoding="utf-8")
    env = dict(os.environ, PROBE_DIR=str(conf_dir))
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["bash", str(probe), *args], capture_output=True,
                       text=True, timeout=60, env=env)
    return r.stdout, r.returncode


def _resolved(out):
    m = re.search(r"^RESOLVED_BRANCH=(.+)$", out, re.M)
    assert m, f"нет RESOLVED_BRANCH в выводе: {out[-400:]}"
    return m.group(1)


HAS_CONF = pytest.mark.parametrize("conf", [CONF_BRANCH])


# ==============================================================================
#  Версии и документы
# ==============================================================================
class TestVersions17019:
    def test_version(self):
        assert settings.APP_VERSION == "1.70.19"

    def test_changelog(self):
        ch = _read("CHANGELOG.md")
        assert ch.count("## [1.70.19]") == 1
        assert ch.index("## [1.70.19]") < ch.index("## [1.70.18]")
        block = ch[ch.index("## [1.70.19]"):ch.index("## [1.70.18]")]
        for token in ("--branch=", "OPT_BRANCH", "deploy.conf", "main",
                      "ENV_BRANCH", "молча"):
            assert token in block, token

    def test_whatsnew(self):
        js = _read("app/static/js/app.js")
        assert "'1.70.19':" in js
        assert js.index("'1.70.19':") < js.index("'1.70.18':")
        entry = js[js.index("'1.70.19':"):js.index("'1.70.18':")]
        assert "OPT_BRANCH" in entry and "deploy.conf" in entry

    def test_versions_everywhere(self):
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert settings.APP_VERSION in _read(f), f


# ==============================================================================
#  Живой прогон: приоритет ветки
# ==============================================================================
class TestBranchPrecedenceLive:
    def test_flag_wins_over_conf(self, tmp_path):
        """Главная регрессия v1.70.19: --branch= важнее запомненной ветки."""
        out, rc = _run(tmp_path, ["--update", f"--branch={FLAG_BRANCH}"],
                       conf=CONF_BRANCH)
        assert rc == 0, out[-400:]
        assert _resolved(out) == FLAG_BRANCH

    def test_env_wins_over_conf(self, tmp_path):
        out, rc = _run(tmp_path, ["--update"],
                       env_extra={"BRANCH": ENV_BRANCH_}, conf=CONF_BRANCH)
        assert rc == 0, out[-400:]
        assert _resolved(out) == ENV_BRANCH_

    def test_flag_wins_over_env(self, tmp_path):
        out, _rc = _run(tmp_path, ["--update", f"--branch={FLAG_BRANCH}"],
                        env_extra={"BRANCH": ENV_BRANCH_}, conf=CONF_BRANCH)
        assert _resolved(out) == FLAG_BRANCH

    def test_conf_used_when_nothing_explicit(self, tmp_path):
        """Без явного выбора запомненная ветка по-прежнему применяется."""
        out, _rc = _run(tmp_path, ["--update"], conf=CONF_BRANCH)
        assert _resolved(out) == CONF_BRANCH

    def test_default_main_without_conf(self, tmp_path):
        out, _rc = _run(tmp_path, ["--update"], conf=None)
        assert _resolved(out) == "main"

    def test_flag_without_conf(self, tmp_path):
        out, _rc = _run(tmp_path, [f"--branch={FLAG_BRANCH}"], conf=None)
        assert _resolved(out) == FLAG_BRANCH

    def test_env_without_conf(self, tmp_path):
        out, _rc = _run(tmp_path, [], env_extra={"BRANCH": ENV_BRANCH_},
                        conf=None)
        assert _resolved(out) == ENV_BRANCH_

    def test_repo_flag_still_parses(self, tmp_path):
        """--repo= не должен ломаться о новый порядок разрешения."""
        out, rc = _run(tmp_path, ["--update", "--repo=o/r",
                                  f"--branch={FLAG_BRANCH}"], conf=CONF_BRANCH)
        assert rc == 0, out[-400:]
        assert _resolved(out) == FLAG_BRANCH

    def test_unknown_arg_still_rejected(self, tmp_path):
        out, rc = _run(tmp_path, ["--wat"], conf=None)
        assert rc == 1 and "Неизвестный аргумент" in out


# ==============================================================================
#  Подмену видно в логе
# ==============================================================================
class TestExplicitBranchIsLogged:
    def test_logged_for_flag(self, tmp_path):
        out, _rc = _run(tmp_path, [f"--branch={FLAG_BRANCH}"],
                        conf=CONF_BRANCH)
        assert "Ветка задана явно" in out
        assert FLAG_BRANCH in out

    def test_logged_for_env(self, tmp_path):
        out, _rc = _run(tmp_path, [], env_extra={"BRANCH": ENV_BRANCH_},
                        conf=CONF_BRANCH)
        assert "Ветка задана явно" in out

    def test_not_logged_when_conf_only(self, tmp_path):
        """Без явного выбора лишней строки нет — лог не шумит."""
        out, _rc = _run(tmp_path, ["--update"], conf=CONF_BRANCH)
        assert "Ветка задана явно" not in out
        assert "Параметры прошлого запуска" in out


# ==============================================================================
#  Статика deploy.sh
# ==============================================================================
class TestDeployShStatic:
    def test_flag_writes_opt_branch(self):
        sh = _read("deploy.sh")
        assert re.search(r"--branch=\*\)\s+OPT_BRANCH=", sh)
        assert '--branch=*) BRANCH="${arg#*=}"' not in sh, \
            "старая запись прямо в BRANCH возвращает регрессию"

    def test_opt_branch_declared_before_use(self):
        """set -u: OPT_BRANCH обязан иметь значение до первого обращения."""
        sh = _read("deploy.sh")
        assert re.search(r'^OPT_BRANCH=""', sh, re.M)
        assert sh.index('OPT_BRANCH=""') < sh.index(RESOLUTION)

    def test_env_branch_captured_before_conf_source(self):
        sh = _read("deploy.sh")
        assert 'ENV_BRANCH="${BRANCH:-}"' in sh
        assert 'BRANCH="${ENV_BRANCH:-main}"' in sh
        assert sh.index('ENV_BRANCH="${BRANCH:-}"') \
            < sh.index('source "$CONF_FILE"') < sh.index(RESOLUTION)

    def test_resolution_after_conf_source(self):
        sh = _read("deploy.sh")
        assert sh.index('source "$CONF_FILE"') < sh.index(RESOLUTION)
        # рядом с разрешением — пояснение приоритета (иначе правку «откатят»)
        window = sh[sh.index(RESOLUTION) - 500:sh.index(RESOLUTION)]
        assert "--branch=" in window and "deploy.conf" in window

    def test_conf_writeback_stores_resolved_branch(self):
        """deploy.conf перезаписывается УЖЕ разрешённой веткой."""
        sh = _read("deploy.sh")
        assert sh.index(RESOLUTION) < sh.index('cat > "$CONF_FILE"')
        heredoc = sh[sh.index('cat > "$CONF_FILE"'):]
        # первый CFGEOF — в «<<CFGEOF», нам нужен закрывающий
        first = heredoc.index("CFGEOF")
        heredoc = heredoc[:heredoc.index("CFGEOF", first + 1)]
        assert 'BRANCH="$BRANCH"' in heredoc

    def test_default_branch_still_main(self):
        sh = _read("deploy.sh")
        assert 'BRANCH="${ENV_BRANCH:-main}"' in sh
        assert OLD_BRANCH not in sh

    def test_help_prints_whole_header(self):
        """--help печатал шапку до 25-й строки и обрывал список «Что делает»."""
        sh = _read("deploy.sh")
        m = re.search(r"sed -n '2,(\d+)p' \"\$0\"", sh)
        assert m, "не найден диапазон --help"
        last_item_line = sh[:sh.index("9) опционально")].count("\n") + 1
        assert int(m.group(1)) >= last_item_line, \
            f"--help обрывает шапку: {m.group(1)} < {last_item_line}"

    def test_bash_syntax(self):
        if not os.path.exists("/bin/bash"):
            pytest.skip("нет bash")
        r = subprocess.run(["bash", "-n", "deploy.sh"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  Приложение: канал по умолчанию не поехал
# ==============================================================================
class TestAppDefaultUntouched:
    def test_default_branch_main(self):
        assert settings.DEFAULT_BRANCH == "main"

    def test_repo_branch_setting_still_wins(self, client):
        from app.database import SessionLocal
        from app.routers.admin import _repo_branch
        from app.services import appsettings
        db = SessionLocal()
        try:
            before = appsettings.get_setting(db, "repo_branch", "")
            appsettings.set_setting(db, "repo_branch", "feature/custom")
            assert _repo_branch(db)[1] == "feature/custom"
            appsettings.set_setting(db, "repo_branch", "")
            assert _repo_branch(db)[1] == "main"
            appsettings.set_setting(db, "repo_branch", before)
        finally:
            db.close()


# ==============================================================================
#  UI-харнесс
# ==============================================================================
class TestHarness17019:
    def test_harness_check_added(self):
        h = _read("tests/ui_harness.js")
        assert "1.70.19" in h and "OPT_BRANCH" in h \
            and "Ветка задана явно" in h


# ==============================================================================
#  Сквозной живой прогон: конфиг → --branch= → fetch → reset (как на сервере)
# ==============================================================================
needs_git = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="нет git")


def _git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=300)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} → {r.returncode}: "
                             f"{r.stdout}{r.stderr}")
    return r


STALE_BRANCH = "arena/stale-channel"
TARGET_BRANCH = "arena/target-channel"


@pytest.fixture()
def server_like_clone(tmp_path):
    """origin с «устаревшим каналом» и «целевой» веткой + клон ТОЛЬКО main
    (--depth 1 --branch, как ставит deploy.sh) — то есть ровно состояние
    боевого сервера: один refspec в .git/config."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main", str(origin)], origin.parent)
    _git(["config", "user.name", "t"], origin)
    _git(["config", "user.email", "t@t"], origin)
    (origin / "app").mkdir()
    (origin / "app" / "config.py").write_text(
        'APP_VERSION: str = "1.70.15"\n', encoding="utf-8")
    (origin / "MARKER.txt").write_text("main\n", encoding="utf-8")
    _git(["add", "."], origin)
    _git(["commit", "-qm", "main"], origin)

    for branch, marker in ((STALE_BRANCH, "stale"), (TARGET_BRANCH, "target")):
        _git(["checkout", "-q", "-b", branch], origin)
        (origin / "MARKER.txt").write_text(marker + "\n", encoding="utf-8")
        _git(["add", "."], origin)
        _git(["commit", "-qm", branch], origin)
    _git(["checkout", "-q", "main"], origin)

    clone = tmp_path / "appdir"
    # file:// — иначе git игнорирует --depth для локального пути
    _git(["clone", "-q", "--depth", "1", "--branch", "main",
          f"file://{origin}", str(clone)], origin.parent)
    cfg = (clone / ".git" / "config").read_text(encoding="utf-8")
    assert cfg.count("fetch = ") == 1, "клон обязан отслеживать одну ветку"
    return origin, clone


def _e2e_script(clone, conf_dir):
    """Настоящий блок разрешений + настоящие команды обновления из deploy.sh.
    Подменены только пути (APP_DIR/APP_USER/CONF_DIR) и заглушены ok/warn/sudo —
    текст самого скрипта не пересказывается."""
    sh = _read("deploy.sh")
    start = sh.index('REPO_URL="${REPO_URL:-')
    region = sh[start:sh.index(RESOLUTION) + len(RESOLUTION)]
    region = region.replace('CONF_DIR="/etc/ymaster-check"',
                            'CONF_DIR="${PROBE_DIR:?}"')
    tail = sh[sh.index(RESOLUTION) + len(RESOLUTION):]
    tail = tail[:tail.index("\nfi\n") + len("\nfi\n")]

    upd_start = sh.index('if [[ -d "$APP_DIR/.git" ]]; then')
    upd = sh[upd_start:sh.index('ok "обновлено из GitHub', upd_start)]
    upd = upd.split("\n", 1)[1]                      # без строки «if … ; then»

    return ("#!/usr/bin/env bash\n"
            "set -euo pipefail\n"                    # как в deploy.sh
            "ok() { :; }\nwarn() { :; }\nbold() { :; }\n"
            'sudo() { shift 2; "$@"; }\n'            # sudo -u <user> → команда
            + region + tail
            # пути боевого сервера подменяем ПОСЛЕ блока разрешений
            + f'\nAPP_DIR="{clone}"\nAPP_USER="$(id -un)"\n'
            + 'echo "RESOLVED_BRANCH=$BRANCH"\n'
            + upd + "\n")


def _run_e2e(tmp_path, clone, conf_branch, args=(), env_extra=None):
    conf_dir = tmp_path / "etc-ymaster-check"
    conf_dir.mkdir(exist_ok=True)
    if conf_branch is not None:
        (conf_dir / "deploy.conf").write_text(
            f'DOMAIN="chek.ymaster.ru"\nSSL_DOMAIN=""\n'
            f'BRANCH="{conf_branch}"\n', encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(_e2e_script(clone, conf_dir), encoding="utf-8")
    env = dict(os.environ, PROBE_DIR=str(conf_dir))
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["bash", str(driver), *args], capture_output=True,
                       text=True, timeout=300, env=env)
    return r, _resolved(r.stdout)


@needs_git
class TestEndToEndBranchSwitch:
    def test_flag_beats_conf_and_resets_to_target(self, server_like_clone,
                                                  tmp_path):
        """Главный сценарий боевого сервера: в deploy.conf запомнена устаревшая
        ветка, оператор явно просит другую — обновление обязано уйти на явную."""
        _origin, clone = server_like_clone
        assert (clone / "MARKER.txt").read_text(encoding="utf-8") == "main\n"
        r, resolved = _run_e2e(tmp_path, clone, STALE_BRANCH,
                               ["--update", f"--branch={TARGET_BRANCH}"])
        assert r.returncode == 0, (r.stdout[-600:], r.stderr[-600:])
        assert resolved == TARGET_BRANCH
        assert (clone / "MARKER.txt").read_text(encoding="utf-8") == "target\n"
        # ref целевой ветки создан — ровно то, чего не хватало в v1.70.17
        assert _git(["rev-parse", "--verify", "--quiet",
                     f"origin/{TARGET_BRANCH}"], clone,
                    check=False).returncode == 0

    def test_env_branch_beats_conf_too(self, server_like_clone, tmp_path):
        _origin, clone = server_like_clone
        r, resolved = _run_e2e(tmp_path, clone, STALE_BRANCH, ["--update"],
                               env_extra={"BRANCH": TARGET_BRANCH})
        assert r.returncode == 0, (r.stdout[-600:], r.stderr[-600:])
        assert resolved == TARGET_BRANCH
        assert (clone / "MARKER.txt").read_text(encoding="utf-8") == "target\n"

    def test_without_flag_conf_branch_is_used(self, server_like_clone,
                                              tmp_path):
        """Без явного выбора запомненная ветка применяется — поведение
        «параметры прошлого запуска» не сломано (и тест не vacuous)."""
        _origin, clone = server_like_clone
        r, resolved = _run_e2e(tmp_path, clone, STALE_BRANCH, ["--update"])
        assert r.returncode == 0, (r.stdout[-600:], r.stderr[-600:])
        assert resolved == STALE_BRANCH
        assert (clone / "MARKER.txt").read_text(encoding="utf-8") == "stale\n"

    def test_no_conf_defaults_to_main_and_succeeds(self, server_like_clone,
                                                   tmp_path):
        """Чистая машина без deploy.conf: ветка main, обновление проходит
        (single-branch клон больше не роняет деплой)."""
        _origin, clone = server_like_clone
        r, resolved = _run_e2e(tmp_path, clone, None, ["--update"])
        assert r.returncode == 0, (r.stdout[-600:], r.stderr[-600:])
        assert resolved == "main"
        assert "ambiguous argument" not in (r.stdout + r.stderr)
