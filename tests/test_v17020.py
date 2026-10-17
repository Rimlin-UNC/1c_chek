# ==============================================================================
#  Ямастер Чек — тесты v1.70.20 «install-rule падал на unbound variable»
#  Найдено на боевом сервере: помощник AdGuard не устанавливался вовсе.
#  Проверяем:
#    • живой прогон НАСТОЯЩЕЙ фазы install-rule: комплект скопирован, помощник
#      0755, sudoers-правило содержит ровно разрешённые фазы (без install-rule/
#      remove-rule), а при непрохождении visudo -c правило удаляется;
#    • статический сканер ВСЕХ *.sh на класс дефекта «в одном local вторая
#      переменная ссылается на первую» (bash раскрывает слова до присваиваний);
#    • deploy.sh больше не глушит вывод помощника — причина отказа печатается;
#    • метка локальной ветки выравнивается по каналу (deploy.sh, оба пути, и
#      _do_apply апдейтера) + сквозная проверка на живом git: после смены канала
#      `rev-parse --abbrev-ref HEAD` = новая ветка, а не прежняя;
#    • версии/журнал/What’s New/харнесс синхронны.
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

HELPER = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
RESOLUTION = 'BRANCH="${OPT_BRANCH:-${ENV_BRANCH:-$BRANCH}}"'
ALLOWED = ("preflight", "rights", "install", "login", "tun", "off",
           "units", "autopilot", "version", "status")
FORBIDDEN_IN_RULE = ("install-rule", "remove-rule")


def _read(path):
    return open(path, encoding="utf-8").read()


def _sh(args, cwd=None, check=True, env=None):
    r = subprocess.run(args, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=300, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args)} → {r.returncode}: "
                             f"{r.stdout}{r.stderr}")
    return r


def _version_markers(ver: str) -> tuple:
    """Строки, которыми версия «прошита» в статику, — именно их наличие или
    отсутствие проверяют тесты версий."""
    return (f"?v={ver}", f"ymaster-check-v{ver}", f'"version": "{ver}"', ver)


def _pins_that_are_substrings_of_current_version() -> list:
    """«not in»-литералы с версией в тестах, которые являются ПОДСТРОКОЙ текущих
    маркеров версии. Только такие дают ложные падения при бампе: например
    «?v=1.70.2» ⊂ «?v=1.70.20». Исторические пины (?v=1.44.0 и т.п.) сюда не
    попадают — версии растут монотонно, подстрокой будущих они стать не могут."""
    markers = _version_markers(settings.APP_VERSION)
    offenders = []
    for p in sorted(pathlib.Path("tests").glob("test_v*.py")):
        if p.name == "test_v17020.py":
            continue
        for i, line in enumerate(
                p.read_text(encoding="utf-8").split("\n"), 1):
            if line.lstrip().startswith("#") or " not in " not in line:
                continue
            for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', line):
                value = a or b
                if not value or not re.search(r"\d+\.\d+\.\d+", value):
                    continue
                for mk in markers:
                    if value in mk:
                        offenders.append(
                            f"{p}:{i}: литерал «{value}» ⊂ «{mk}» — "
                            f"ложно падает при бампе версии (заякорите его "
                            f"символом внутри: '{value}\"')")
                        break
    return offenders


# ==============================================================================
#  Версии и документы
# ==============================================================================
class TestVersions17020:
    def test_version(self):
        # v1.70.21: пин ослаблен (точную версию проверяет свой тест версии)
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) \
            >= (1, 70, 20)

    def test_changelog(self):
        ch = _read("CHANGELOG.md")
        assert ch.count("## [1.70.20]") == 1
        assert ch.index("## [1.70.20]") < ch.index("## [1.70.19]")
        block = ch[ch.index("## [1.70.20]"):ch.index("## [1.70.19]")]
        for token in ("unbound variable", "install-rule", "local", "причина",
                      "checkout -q -B"):
            assert token in block, token

    def test_whatsnew(self):
        js = _read("app/static/js/app.js")
        assert "'1.70.20':" in js
        assert js.index("'1.70.20':") < js.index("'1.70.19':")
        entry = js[js.index("'1.70.20':"):js.index("'1.70.19':")]
        assert "unbound variable" in entry and "install-rule" in entry

    def test_versions_everywhere(self):
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert settings.APP_VERSION in _read(f), f

    def test_no_stale_cache_busting_versions(self):
        """Структурный замок: ВСЕ ?v= в index.html/sw.js — текущая версия.
        Закрывает класс мин «"?v=1.70.2" not in idx»: такой литерал является
        префиксом "?v=1.70.20" и давал ложное падение на бампе версии."""
        ver = settings.APP_VERSION
        idx = _read("app/static/index.html")
        assert set(re.findall(r"\?v=([0-9.]+)", idx)) == {ver}, \
            f"index.html: {sorted(set(re.findall(r'?v=([0-9.]+)', idx)))} != {ver}"
        sw = _read("app/static/sw.js")
        assert set(re.findall(r"\?v=([0-9.]+)", sw)) <= {ver}, sw
        assert f"ymaster-check-v{ver}" in sw
        mf = _read("app/static/manifest.webmanifest")
        assert f'"version": "{ver}"' in mf

    def test_no_test_pin_is_substring_of_current_version(self):
        """Регресс-гард на конкретный сбой v1.70.20.

        Старый тест проверял «"?v=1.70.2" not in idx», а текущая версия 1.70.20
        СОДЕРЖИТ этот литерал как префикс → ложное падение на ровном месте.
        Проверяем точное свойство: ни один «not in»-литерал с версией в тестах
        не является подстрокой текущих маркеров версии. Исторические пины
        (?v=1.44.0 и т.п.) не задеваем — версии растут монотонно, подстрокой
        будущих маркеров они стать не могут.
        """
        offenders = _pins_that_are_substrings_of_current_version()
        assert not offenders, "\n".join(offenders)

    def test_guard_catches_the_original_bug(self):
        """Гард не vacuous: исходную строку дефекта он бы поймал."""
        ver = settings.APP_VERSION
        markers = _version_markers(ver)
        # «?v=1.70.2» ⊂ «?v=1.70.20» — ровно то, что сломало прогон
        assert "1.70.2" in ver and any(
            "?v=1.70.2" in m for m in markers), markers
        # безопасные (заякоренные) формы подстрокой не являются
        for safe in ('?v=1.70.2"', "-v1.70.2'", '?v=1.70.1"'):
            assert not any(safe in m for m in markers), safe


# ==============================================================================
#  Сканер класса дефекта по всем *.sh репозитория
# ==============================================================================
def _local_cross_references(line: str):
    """Возвращает пары (имя, предшественник) для ОДНОГО local/declare, где
    правая часть ссылается на переменную, объявленную в том же операторе."""
    m = re.match(r"^\s*(?:local|declare)\s+(.*)$", line)
    if not m:
        return []
    parts = re.findall(r'(\w+)=("[^"]*"|\'[^\']*\'|\S+)', m.group(1))
    bad, seen = [], []
    for name, rhs in parts:
        for prev in seen:
            if re.search(r"\$\{?%s\b" % re.escape(prev), rhs):
                bad.append((name, prev))
        seen.append(name)
    return bad


class TestNoLocalCrossReference:
    def test_repo_wide_scan(self):
        """Регресс-гард: такой класс дефекта больше не пройдёт незамеченным."""
        offenders = []
        for p in sorted(pathlib.Path(".").rglob("*.sh")):
            if ".git" in p.parts or ".venv" in p.parts:
                continue
            for i, line in enumerate(
                    p.read_text(encoding="utf-8").split("\n"), 1):
                for name, prev in _local_cross_references(line):
                    offenders.append(f"{p}:{i} «{name}=» ← «{prev}»: {line.strip()}")
        assert not offenders, "\n".join(offenders)

    def test_scanner_catches_known_bad_form(self):
        """Сканер не vacuous: исходную строку дефекта он находит."""
        bad = 'local app_dir="${1:-$DEFAULT_APP_DIR}" src="$app_dir/deploy"'
        assert _local_cross_references(bad) == [("src", "app_dir")]

    def test_scanner_ignores_safe_forms(self):
        assert _local_cross_references('local app_dir="${1:-D}" u=""') == []
        assert _local_cross_references("local f") == []
        assert _local_cross_references('local cli; cli=$(cli_path)') == []
        assert _local_cross_references('x=1 y="$x"') == []   # не local/declare

    def test_helper_install_rule_split_in_two_lines(self):
        src = _read(HELPER)
        phase = src[src.index("phase_install_rule() {"):]
        phase = phase[:phase.index("\n}\n")]
        assert re.search(r'^\s*local app_dir="\$\{1:-\$DEFAULT_APP_DIR\}"\s*$',
                         phase, re.M), "app_dir обязан объявляться отдельной строкой"
        assert re.search(r'^\s*local src="\$app_dir/deploy/adguard-vpn"\s*$',
                         phase, re.M), "src — отдельной строкой, ПОСЛЕ app_dir"
        assert phase.index("local app_dir=") < phase.index("local src=")


# ==============================================================================
#  Живой прогон настоящей фазы install-rule
# ==============================================================================
def _helper_driver(tmp_path, app_dir, visudo_rc=0, stub_visudo=True):
    """Драйвер: НАСТОЯЩИЙ текст помощника (без вызова main) + заглушки для
    root-операций и подменные пути. Логика фазы не пересказывается.

    stub_visudo=False — проверка правила выполняется НАСТОЯЩИМ visudo (как на
    сервере): иначе синтаксически неверное sudoers-правило прошло бы тесты, но
    на боевой машине помощник удалил бы его и отказал.
    """
    src = _read(HELPER)
    body = src[:src.rindex('main "$@"')]          # без автозапуска
    driver = tmp_path / "driver.sh"
    visudo_stub = f'visudo() {{ return {visudo_rc}; }}\n' if stub_visudo else ""
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"                                  # как в помощнике
        f"source {tmp_path / 'helper_body.sh'}\n"
        # пути боевой системы → песочница (после source, чтобы перебить константы)
        f'HELPER="{tmp_path / "sbin" / "ymaster-adguard-setup"}"\n'
        f'KIT_DIR="{tmp_path / "kit"}"\n'
        f'SUDOERS_FILE="{tmp_path / "sudoers.d" / "ymaster-adguard"}"\n'
        'need_root() { :; }\n'                      # uid != 0 в песочнице
        'chown() { :; }\n'
        + visudo_stub +
        # GNU install с -o root недоступен без root — эмулируем cp+chmod
        'install() {\n'
        '  local mode="" src="" dst=""\n'
        '  while [ $# -gt 0 ]; do\n'
        '    case "$1" in\n'
        '      -m) mode="$2"; shift 2 ;;\n'
        '      -o|-g) shift 2 ;;\n'
        '      *) if [ -z "$src" ]; then src="$1"; else dst="$1"; fi; shift ;;\n'
        '    esac\n'
        '  done\n'
        '  command mkdir -p "$(dirname "$dst")"\n'
        '  command cp "$src" "$dst" || return 1\n'
        '  [ -n "$mode" ] && command chmod "$mode" "$dst"\n'
        '  return 0\n'
        '}\n'
        'mkdir -p "$(dirname "$SUDOERS_FILE")"\n'
        f'phase_install_rule "{app_dir}"\n',
        encoding="utf-8")
    (tmp_path / "helper_body.sh").write_text(body, encoding="utf-8")
    return driver


@pytest.fixture()
def app_dir_like_server(tmp_path):
    """Каталог программы как на сервере: нужен только deploy/adguard-vpn
    с комплектом — берём настоящий из репозитория."""
    d = tmp_path / "ymaster-check"
    (d / "deploy").mkdir(parents=True)
    _sh(["cp", "-r", "deploy/adguard-vpn", str(d / "deploy" / "adguard-vpn")])
    return d


class TestInstallRuleLive:
    def test_phase_succeeds_and_installs_everything(self, tmp_path,
                                                    app_dir_like_server):
        driver = _helper_driver(tmp_path, app_dir_like_server)
        r = _sh(["bash", str(driver)], check=False)
        assert r.returncode == 0, f"install-rule снова падает:\n{r.stdout}\n{r.stderr}"
        assert "unbound variable" not in (r.stdout + r.stderr)
        assert "Мастер AdGuard работает без пароля" in r.stdout

        # комплект скопирован целиком (root-каталог)
        kit = tmp_path / "kit"
        for f in ("adguard-vpn-telegram.service", "tg-health.service",
                  "tg-health.timer", "tg-health.sh",
                  "ymaster-adguard-setup.sh"):
            assert (kit / f).is_file(), f"в комплекте нет {f}"

        # помощник установлен и исполняем (0755)
        helper = tmp_path / "sbin" / "ymaster-adguard-setup"
        assert helper.is_file(), "помощник не установлен"
        assert helper.stat().st_mode & 0o777 == 0o755, \
            oct(helper.stat().st_mode & 0o777)

        # sudoers-правило: ровно разрешённые фазы, install-rule/remove-rule НЕТ
        rule = (tmp_path / "sudoers.d" / "ymaster-adguard").read_text(
            encoding="utf-8")
        assert "NOPASSWD:" in rule
        for ph in ALLOWED:
            assert f"ymaster-adguard-setup {ph}" in rule, ph
        for ph in FORBIDDEN_IN_RULE:
            assert f"ymaster-adguard-setup {ph}" not in rule, \
                f"{ph} не должен быть в sudoers-правиле"
        assert (tmp_path / "sudoers.d" / "ymaster-adguard").stat().st_mode \
            & 0o777 == 0o440

    def test_rule_user_is_service_user(self, tmp_path, app_dir_like_server):
        """Правило пишется на служебного пользователя (владелец каталога),
        а не на root — иначе приложение не сможет звать помощника."""
        driver = _helper_driver(tmp_path, app_dir_like_server)
        r = _sh(["bash", str(driver)])
        rule = (tmp_path / "sudoers.d" / "ymaster-adguard").read_text(
            encoding="utf-8")
        owner = app_dir_like_server.stat().st_uid
        import pwd
        assert rule.startswith(f"{pwd.getpwuid(owner).pw_name} ALL=(root)"), rule

    def test_rule_passes_real_visudo(self, tmp_path, app_dir_like_server):
        """Проверка НАСТОЯЩИМ visudo -c — как на сервере.

        В остальных тестах visudo заглушён, поэтому синтаксически неверное
        sudoers-правило прошло бы прогон, а на боевой машине помощник удалил бы
        его и отказал («правило не прошло visudo -c»). Здесь заглушки нет.
        """
        visudo = shutil.which("visudo")
        env = dict(os.environ, PATH=os.environ["PATH"] + ":/usr/sbin")
        if not visudo:
            visudo = "/usr/sbin/visudo" if os.path.exists(
                "/usr/sbin/visudo") else None
        if not visudo:
            pytest.skip("visudo недоступен в этой среде")
        driver = _helper_driver(tmp_path, app_dir_like_server,
                                stub_visudo=False)
        r = subprocess.run(["bash", str(driver)], capture_output=True,
                           text=True, timeout=120, env=env)
        assert r.returncode == 0, \
            f"настоящий visudo отверг правило:\n{r.stdout}\n{r.stderr}"
        assert "Мастер AdGuard работает без пароля" in r.stdout
        rule = tmp_path / "sudoers.d" / "ymaster-adguard"
        assert rule.exists(), "при успешной проверке правило не удаляется"
        # независимая перепроверка тем же visudo, вне логики помощника
        chk = subprocess.run([visudo, "-cf", str(rule)], capture_output=True,
                             text=True, timeout=60, env=env)
        assert chk.returncode == 0, chk.stdout + chk.stderr
        assert "parsed OK" in chk.stdout

    def test_visudo_failure_removes_rule(self, tmp_path, app_dir_like_server):
        """Невалидное правило не должно оставаться в /etc/sudoers.d."""
        driver = _helper_driver(tmp_path, app_dir_like_server, visudo_rc=1)
        r = _sh(["bash", str(driver)], check=False)
        assert r.returncode != 0
        assert "visudo" in (r.stdout + r.stderr)
        assert not (tmp_path / "sudoers.d" / "ymaster-adguard").exists(), \
            "правило обязано быть удалено после провала visudo -c"

    def test_missing_kit_dir_is_reported(self, tmp_path):
        """Без каталога комплекта — понятный отказ, а не unbound variable."""
        driver = _helper_driver(tmp_path, tmp_path / "no-such-app-dir")
        r = _sh(["bash", str(driver)], check=False)
        assert r.returncode != 0
        assert "каталог комплекта не найден" in (r.stdout + r.stderr)
        assert "unbound variable" not in (r.stdout + r.stderr)

    def test_helper_syntax(self):
        r = _sh(["bash", "-n", HELPER], check=False)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  deploy.sh: причина отказа помощника больше не глушится
# ==============================================================================
class TestDeployShowsHelperFailureReason:
    def test_output_is_captured_not_discarded(self):
        sh = _read("deploy.sh")
        block = sh[sh.index("AG_HELPER_SRC="):]
        block = block[:block.index("8/9 Nginx")]
        assert 'install-rule "$APP_DIR" >/dev/null 2>&1' not in block, \
            "вывод помощника снова глушится — причина отказа не видна"
        assert "AG_HELPER_OUT=$(bash" in block and "2>&1" in block

    def test_reason_is_printed_on_failure(self):
        sh = _read("deploy.sh")
        block = sh[sh.index("AG_HELPER_SRC="):]
        block = block[:block.index("8/9 Nginx")]
        assert "причина:" in block
        assert "tail -n 5" in block
        # успех по-прежнему отмечается одной строкой ok
        assert 'ok "sudoers: мастер AdGuard VPN работает без пароля' in block

    def test_failure_does_not_break_deploy(self):
        """Отказ помощника — предупреждение, не падение деплоя (set -e)."""
        sh = _read("deploy.sh")
        block = sh[sh.index("AG_HELPER_SRC="):]
        block = block[:block.index("8/9 Nginx")]
        assert block.lstrip().startswith("AG_HELPER_SRC=")
        assert "if [[ -f" in block and "else" in block and "\nfi" in block


# ==============================================================================
#  Метка локальной ветки выравнивается по каналу
# ==============================================================================
class TestBranchLabelAligned:
    def _update_block(self):
        sh = _read("deploy.sh")
        start = sh.index('if [[ -d "$APP_DIR/.git" ]]; then')
        return sh[start:sh.index('ok "обновлено из GitHub', start)]

    def test_deploy_update_path_relabels_branch(self):
        block = self._update_block()
        assert 'checkout -q -B "$BRANCH"' in block
        assert block.index('reset --hard "origin/$BRANCH"') \
            < block.index('checkout -q -B "$BRANCH"')

    def test_deploy_update_path_sets_upstream(self):
        """Без upstream `git pull` на сервере отвечает «Already up to date»."""
        block = self._update_block()
        assert '--set-upstream-to="origin/$BRANCH"' in block
        assert block.index('checkout -q -B "$BRANCH"') \
            < block.index('--set-upstream-to="origin/$BRANCH"')

    def test_deploy_init_on_place_path_relabels_branch(self):
        sh = _read("deploy.sh")
        start = sh.index("каталог занят без .git")
        block = sh[start:sh.index('ok "код получен', start)]
        assert 'checkout -q -B "$BRANCH"' in block
        assert '--set-upstream-to="origin/$BRANCH"' in block
        assert block.index('reset --hard "origin/$BRANCH"') \
            < block.index('checkout -q -B "$BRANCH"')

    def test_relabel_is_best_effort(self):
        """Сбой выравнивания не должен ронять деплой (set -e)."""
        block = self._update_block()
        for token in ("checkout -q -B", "--set-upstream-to="):
            lines = [l for l in block.split("\n") if token in l]
            assert lines, token
            # `|| true` может стоять на последней строке продолжения команды
            joined = "\n".join(lines)
            tail = block[block.index(token):block.index(token) + 300]
            assert "|| true" in joined or "|| true" in tail.split("\n\n")[0], \
                f"нет «|| true» у {token}"

    def test_updater_do_apply_relabels_branch(self):
        src = _read("app/services/updater.py")
        body = src[src.index("def _do_apply("):]
        body = body[:body.index("\ndef ", 10)]
        assert '["checkout", "-q", "-B", branch]' in body
        assert body.index('["reset", "--hard", target_ref]') \
            < body.index('["checkout", "-q", "-B", branch]')

    def test_updater_do_apply_sets_upstream(self):
        src = _read("app/services/updater.py")
        body = src[src.index("def _do_apply("):]
        body = body[:body.index("\ndef ", 10)]
        assert '--set-upstream-to=origin/{branch}' in body
        assert body.index('["checkout", "-q", "-B", branch]') \
            < body.index("--set-upstream-to=origin/{branch}")

    def test_updater_relabel_not_fatal(self):
        """После checkout -B нет проверки rc с raise — шаг необязательный."""
        src = _read("app/services/updater.py")
        body = src[src.index("def _do_apply("):]
        i = body.index('["checkout", "-q", "-B", branch]')
        after = body[i:i + 220]
        assert "raise RuntimeError" not in after.split("\n\n")[0], after


needs_git = pytest.mark.skipif(
    _sh(["git", "--version"], check=False).returncode != 0, reason="нет git")

STALE = "arena/stale-channel"
TARGET = "arena/target-channel"


@pytest.fixture()
def server_like_clone(tmp_path):
    """Клон --depth 1 --branch main (как на сервере) + две ветки в origin."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _sh(["git", "init", "-q", "-b", "main", str(origin)], origin.parent)
    _sh(["git", "config", "user.name", "t"], origin)
    _sh(["git", "config", "user.email", "t@t"], origin)
    (origin / "app").mkdir()
    (origin / "app" / "config.py").write_text(
        'APP_VERSION: str = "1.70.15"\n', encoding="utf-8")
    (origin / "MARKER.txt").write_text("main\n", encoding="utf-8")
    _sh(["git", "add", "."], origin)
    _sh(["git", "commit", "-qm", "main"], origin)
    for branch, marker in ((STALE, "stale"), (TARGET, "target")):
        _sh(["git", "checkout", "-q", "-b", branch], origin)
        (origin / "MARKER.txt").write_text(marker + "\n", encoding="utf-8")
        _sh(["git", "add", "."], origin)
        _sh(["git", "commit", "-qm", branch], origin)
    _sh(["git", "checkout", "-q", "main"], origin)

    clone = tmp_path / "appdir"
    _sh(["git", "clone", "-q", "--depth", "1", "--branch", "main",
         f"file://{origin}", str(clone)], origin.parent)
    return origin, clone


@needs_git
class TestBranchLabelLive:
    """Сквозной прогон настоящего блока deploy.sh: смена канала обязана
    оставить сервер на НОВОЙ ветке и по содержимому, и по метке HEAD."""

    def _driver(self, tmp_path, clone, conf_branch):
        sh = _read("deploy.sh")
        start = sh.index('REPO_URL="${REPO_URL:-')
        region = sh[start:sh.index(RESOLUTION) + len(RESOLUTION)]
        region = region.replace('CONF_DIR="/etc/ymaster-check"',
                                'CONF_DIR="${PROBE_DIR:?}"')
        upd_start = sh.index('if [[ -d "$APP_DIR/.git" ]]; then')
        upd = sh[upd_start:sh.index('ok "обновлено из GitHub', upd_start)]
        upd = upd.split("\n", 1)[1]
        conf_dir = tmp_path / "etc-ymaster-check"
        conf_dir.mkdir(exist_ok=True)
        if conf_branch:
            (conf_dir / "deploy.conf").write_text(
                f'DOMAIN="d"\nSSL_DOMAIN=""\nBRANCH="{conf_branch}"\n',
                encoding="utf-8")
        driver = tmp_path / "driver.sh"
        driver.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "ok() { :; }\nwarn() { :; }\nbold() { :; }\n"
            'sudo() { shift 2; "$@"; }\n'
            + region
            + f'\nAPP_DIR="{clone}"\nAPP_USER="$(id -un)"\n'
            + upd
            + '\necho "HEAD_BRANCH=$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)"\n',
            encoding="utf-8")
        return driver, conf_dir

    def test_head_branch_follows_channel(self, tmp_path, server_like_clone):
        """Регрессия боевого сервера: код с новой ветки, а HEAD — на старой."""
        _origin, clone = server_like_clone
        driver, conf_dir = self._driver(tmp_path, clone, STALE)
        env = dict(os.environ, PROBE_DIR=str(conf_dir))
        r = subprocess.run(["bash", str(driver), "--update",
                            f"--branch={TARGET}"], capture_output=True,
                           text=True, timeout=300, env=env)
        assert r.returncode == 0, (r.stdout[-600:], r.stderr[-600:])
        assert (clone / "MARKER.txt").read_text(encoding="utf-8") == "target\n"
        m = re.search(r"^HEAD_BRANCH=(.+)$", r.stdout, re.M)
        assert m, r.stdout[-400:]
        assert m.group(1) == TARGET, \
            f"метка ветки осталась {m.group(1)} — git pull потянет старый канал"

    def test_pull_after_switch_stays_on_channel(self, tmp_path,
                                                server_like_clone):
        """После смены канала `git pull` не уезжает на прежнюю ветку."""
        origin, clone = server_like_clone
        driver, conf_dir = self._driver(tmp_path, clone, STALE)
        env = dict(os.environ, PROBE_DIR=str(conf_dir))
        r = subprocess.run(["bash", str(driver), "--update",
                            f"--branch={TARGET}"], capture_output=True,
                           text=True, timeout=300, env=env)
        assert r.returncode == 0, r.stderr[-400:]
        # в целевой ветке появляется новый коммит — pull обязан его принести
        _sh(["git", "checkout", "-q", TARGET], origin)
        (origin / "MARKER.txt").write_text("target-2\n", encoding="utf-8")
        _sh(["git", "commit", "-qam", "target-2"], origin)
        _sh(["git", "checkout", "-q", STALE], origin)
        pull = _sh(["git", "pull", "--ff-only"], clone, check=False)
        assert pull.returncode == 0, (pull.stdout, pull.stderr)
        assert (clone / "MARKER.txt").read_text(encoding="utf-8") == "target-2\n"


# ==============================================================================
#  Изоляция тестов мастера AdGuard (механизм «плавающего» падения)
# ==============================================================================
class TestAdGuardStateIsolation:
    """v1.70.20: setup_status() при не-живом задании читает состояние С ДИСКА,
    поэтому общий data/adguard_setup.json подменял результат чужого теста —
    падение test_status_endpoint_exposes_one_time_cmd «плавало» между воркерами
    xdist. Фикстура fresh_job изолирует _state_path в tmp_path."""

    def test_stale_state_file_overrides_job(self, tmp_path, monkeypatch):
        """Воспроизводим механизм: чужой «finished»-файл обнуляет one_time_cmd."""
        stale = tmp_path / "adguard_setup.json"
        stale.write_text(json.dumps({"finished": True, "running": False,
                                     "one_time_cmd": ""}), encoding="utf-8")
        monkeypatch.setattr(ag, "_state_path", lambda: str(stale))
        ag.job.reset()
        ag.job.one_time_cmd = ag.one_time_cmd()
        try:
            st = ag.setup_status()
            assert st["one_time_cmd"] == "", st      # ровно то, что видел тест
        finally:
            ag.job.reset()

    def test_isolated_state_lets_job_win(self, tmp_path, monkeypatch):
        """С изоляцией чужого файла нет — состояние задания не подменяется."""
        monkeypatch.setattr(ag, "_state_path",
                            lambda: str(tmp_path / "adguard_setup.json"))
        ag.job.reset()
        ag.job.one_time_cmd = ag.one_time_cmd()
        try:
            st = ag.setup_status()
            assert "install-rule" in st["one_time_cmd"], st
        finally:
            ag.job.reset()

    @pytest.mark.parametrize("path", ("tests/test_v17015.py",
                                      "tests/test_v17016.py"))
    def test_fresh_job_fixture_isolates_state(self, path):
        """Регресс-гард: изоляцию из фикстуры нельзя молча убрать."""
        src = _read(path)
        i = src.index("def fresh_job(")
        block = src[i:src.index("\n\n\n", i)]
        assert "_state_path" in block, f"{path}: fresh_job не изолирует состояние"
        assert "tmp_path" in block and "monkeypatch" in block, path


# ==============================================================================
#  UI-харнесс
# ==============================================================================
class TestHarness17020:
    def test_harness_check_added(self):
        h = _read("tests/ui_harness.js")
        assert "1.70.20" in h and "unbound variable" in h \
            and "install-rule" in h
