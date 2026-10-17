# ==============================================================================
#  Ямастер Чек — тесты v1.70.18 «Канал обновлений — main»
#  Веткой обновлений по умолчанию стала main вместо временной рабочей ветки
#  арены: DEFAULT_BRANCH (app/config.py), BRANCH (deploy.sh) и все ссылки
#  комплекта. Проверяем:
#    • версии/журнал/What’s New синхронны;
#    • значение по умолчанию — main (в настройках и в deploy.sh);
#    • явный источник (repo_branch / --branch= / BRANCH=) важнее значения
#      по умолчанию;
#    • в поставляемых файлах не осталось ссылок на старую ветку;
#    • обновление только вперёд: при более старой версии в источнике
#      update_available=False (переход на main не откатит программу назад).
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import appsettings
import app.services.updater as up

OLD_BRANCH = "arena/01a0caaa-1c-chek"
NEW_BRANCH = "main"

# Файлы, которые видит владелец/сервер: старая ветка там недопустима.
# CHANGELOG — история (упоминания остаются), tests — свои сценарии.
SHIPPED = ("deploy.sh", "install.sh", "README.md", "app/config.py",
           "app/static/js/app.js", "docs/DEPLOYMENT.md",
           "docs/HOSTING_COMPARISON.md", "docs/update_playbook.md",
           "deploy/adguard-vpn/README.md")


def _read(path):
    return open(path, encoding="utf-8").read()


# ==============================================================================
#  Версии и документы
# ==============================================================================
class TestVersions17018:
    def test_version(self):
        # v1.70.19: пин ослаблен (точную версию проверяет свой тест)
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) \
            >= (1, 70, 18)

    def test_changelog(self):
        ch = _read("CHANGELOG.md")
        assert ch.count("## [1.70.18]") == 1
        assert ch.index("## [1.70.18]") < ch.index("## [1.70.17]")
        block = ch[ch.index("## [1.70.18]"):ch.index("## [1.70.17]")]
        assert "main" in block and OLD_BRANCH in block
        assert "DEFAULT_BRANCH" in block and "deploy.sh" in block

    def test_whatsnew(self):
        js = _read("app/static/js/app.js")
        assert "'1.70.18':" in js
        assert js.index("'1.70.18':") < js.index("'1.70.17':")

    def test_versions_everywhere(self):
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert settings.APP_VERSION in _read(f), f


# ==============================================================================
#  Значение по умолчанию — main
# ==============================================================================
class TestDefaultBranchIsMain:
    def test_settings(self):
        assert settings.DEFAULT_BRANCH == NEW_BRANCH

    def test_config_source(self):
        src = _read("app/config.py")
        m = re.search(r'DEFAULT_BRANCH: str = "([^"]+)"', src)
        assert m and m.group(1) == NEW_BRANCH

    def test_deploy_sh_default(self):
        sh = _read("deploy.sh")
        # v1.70.19: значение по умолчанию берётся через ENV_BRANCH, чтобы
        # BRANCH= из окружения не затирался source deploy.conf
        m = re.search(r'BRANCH="\$\{(?:BRANCH|ENV_BRANCH):-([^}]+)\}"', sh)
        assert m, "не найден BRANCH по умолчанию в deploy.sh"
        assert m.group(1) == NEW_BRANCH

    def test_deploy_sh_quick_start_url(self):
        sh = _read("deploy.sh")
        assert f"1c_chek/{NEW_BRANCH}/deploy.sh" in sh
        assert OLD_BRANCH not in sh

    def test_explicit_branch_still_wins_in_deploy(self):
        """--branch= и BRANCH= важнее значения по умолчанию."""
        sh = _read("deploy.sh")
        assert re.search(r"--branch=\*\)\s+OPT_BRANCH=", sh), \
            "v1.70.19: --branch= должен писать в OPT_BRANCH"
        assert re.search(r'BRANCH="\$\{OPT_BRANCH:-', sh)

    def test_ui_placeholder(self):
        js = _read("app/static/js/app.js")
        assert 'id="upd-branch-input" placeholder="main"' in js
        assert f'placeholder="{OLD_BRANCH}"' not in js


# ==============================================================================
#  Приоритет явно заданного источника (repo_branch)
# ==============================================================================
class TestExplicitSourcePriority:
    def test_repo_branch_setting_wins(self, client):
        from app.database import SessionLocal
        from app.routers.admin import _repo_branch
        db = SessionLocal()
        try:
            before = appsettings.get_setting(db, "repo_branch", "")
            appsettings.set_setting(db, "repo_branch", "feature/custom")
            _repo, branch = _repo_branch(db)
            assert branch == "feature/custom"
            appsettings.set_setting(db, "repo_branch", "")
            _repo, branch = _repo_branch(db)
            assert branch == settings.DEFAULT_BRANCH == NEW_BRANCH
            appsettings.set_setting(db, "repo_branch", before)
        finally:
            db.close()

    def test_check_update_uses_default_branch(self, client, monkeypatch):
        """check_update берёт ветку из настроек, иначе DEFAULT_BRANCH (= main)."""
        from app.database import SessionLocal
        seen = {}

        def fake_remote(repo, branch):
            seen["repo"], seen["branch"] = repo, branch
            return {"version": settings.APP_VERSION, "source": "test",
                    "checked_at": "2026-10-17T00:00:00Z"}

        monkeypatch.setattr(up, "_remote_version", fake_remote)
        db = SessionLocal()
        try:
            before = appsettings.get_setting(db, "repo_branch", "")
            appsettings.set_setting(db, "repo_branch", "")
            up.check_update(db, force=True)
            assert seen["branch"] == NEW_BRANCH, seen
            appsettings.set_setting(db, "repo_branch", before)
        finally:
            db.close()


# ==============================================================================
#  Обновление только вперёд: переход на main не откатит программу
# ==============================================================================
class TestNoDowngradeOnChannelSwitch:
    def test_is_newer_semantics(self):
        assert up._is_newer("1.70.19", "1.70.18") is True
        assert up._is_newer("1.70.18", "1.70.18") is False
        assert up._is_newer("1.70.15", "1.70.18") is False
        assert up._is_newer("1.71.0", "1.70.99") is True

    def test_older_source_is_not_available(self, client, monkeypatch):
        """В источнике версия старее установленной → update_available False,
        то есть «обновление» на main до слияния ничего не откатит."""
        from app.database import SessionLocal
        monkeypatch.setattr(up, "_remote_version",
                            lambda repo, branch: {"version": "1.70.15",
                                                  "source": "test",
                                                  "checked_at": "2026-10-17T00:00:00Z"})
        db = SessionLocal()
        try:
            r = up.check_update(db, force=True)
            assert r["remote_version"] == "1.70.15"
            assert r["update_available"] is False
            assert r["current_version"] == settings.APP_VERSION
        finally:
            db.close()

    def test_newer_source_is_available(self, client, monkeypatch):
        from app.database import SessionLocal
        monkeypatch.setattr(up, "_remote_version",
                            lambda repo, branch: {"version": "1.70.99",
                                                  "source": "test",
                                                  "checked_at": "2026-10-17T00:00:00Z"})
        db = SessionLocal()
        try:
            r = up.check_update(db, force=True)
            assert r["update_available"] is True
        finally:
            db.close()

    def test_apply_route_refuses_when_not_available(self):
        """Маршрут применения не запускает start_apply без update_available."""
        src = _read("app/routers/admin.py")
        body = src[src.index("def update_apply"):]
        body = body[:body.index("\n@router")] if "\n@router" in body else body
        assert 'if not info["update_available"]:' in body
        assert body.index('if not info["update_available"]:') \
            < body.index("start_apply(")


# ==============================================================================
#  Ссылки комплекта переведены на main
# ==============================================================================
class TestShippedFilesSwitched:
    @pytest.mark.parametrize("path", SHIPPED)
    def test_no_old_branch_in_shipped_files(self, path):
        if not os.path.exists(path):
            pytest.skip(f"{path} отсутствует")
        assert OLD_BRANCH not in _read(path), path

    def test_readme_curl_main(self):
        rd = _read("README.md")
        assert "1c_chek/main/deploy.sh" in rd

    def test_deployment_doc(self):
        d = _read("docs/DEPLOYMENT.md")
        assert "1c_chek/main/deploy.sh" in d
        assert "git clone --branch main" in d
        assert "Клонирует ветку `main`" in d

    def test_hosting_comparison_doc(self):
        d = _read("docs/HOSTING_COMPARISON.md")
        assert "1c_chek/main/deploy.sh" in d

    def test_update_playbook(self):
        d = _read("docs/update_playbook.md")
        assert "-b main https://github.com/Rimlin-UNC/1c_chek.git" in d
        assert "git push origin main" in d

    def test_adguard_readme_base(self):
        d = _read("deploy/adguard-vpn/README.md")
        assert d.count("B=https://raw.githubusercontent.com/Rimlin-UNC/"
                       "1c_chek/main") == 2
        # бессмысленная после замены фраза убрана
        assert "замените" not in d or "`main` на `main`" not in d

    def test_install_sh_has_no_old_branch(self):
        if not os.path.exists("install.sh"):
            pytest.skip("install.sh отсутствует")
        assert OLD_BRANCH not in _read("install.sh")

    def test_changelog_keeps_history(self):
        """История в CHANGELOG остаётся — переписывать прошлое не нужно."""
        assert OLD_BRANCH in _read("CHANGELOG.md")


# ==============================================================================
#  deploy.sh живьём: ветка по умолчанию = main
# ==============================================================================
class TestDeployShResolvesMain:
    @pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="нет bash")
    def test_branch_resolution(self):
        """Гоняем настоящий блок deploy.sh «значения по умолчанию + разбор
        аргументов» и смотрим, какая ветка выйдет без аргументов и с
        --branch= (без root-проверки и без самого деплоя)."""
        sh = _read("deploy.sh")
        start = sh.index('REPO_URL="${REPO_URL:-')
        end_marker = 'BRANCH="${OPT_BRANCH:-${ENV_BRANCH:-$BRANCH}}"'
        region = sh[start:sh.index(end_marker) + len(end_marker)]
        probe = ("#!/usr/bin/env bash\nset -uo pipefail\n"
                 + region + '\necho "BRANCH=$BRANCH"\n')
        r = subprocess.run(["bash", "-c", probe, "deploy.sh"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (r.stdout[-400:], r.stderr[-400:])
        assert "BRANCH=main" in r.stdout, (r.stdout[-400:], r.stderr[-400:])
        r2 = subprocess.run(["bash", "-c", probe, "deploy.sh",
                             "--branch=feature/x"],
                            capture_output=True, text=True, timeout=60)
        assert "BRANCH=feature/x" in r2.stdout, r2.stdout[-400:]

    @pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="нет bash")
    def test_env_branch_wins_too(self):
        """BRANCH из окружения важнее значения по умолчанию (deploy.sh)."""
        sh = _read("deploy.sh")
        start = sh.index('REPO_URL="${REPO_URL:-')
        end_marker = 'BRANCH="${OPT_BRANCH:-${ENV_BRANCH:-$BRANCH}}"'
        region = sh[start:sh.index(end_marker) + len(end_marker)]
        probe = ("#!/usr/bin/env bash\nset -uo pipefail\n"
                 + region + '\necho "BRANCH=$BRANCH"\n')
        env = dict(os.environ, BRANCH="hotfix/urgent")
        r = subprocess.run(["bash", "-c", probe, "deploy.sh"],
                           capture_output=True, text=True, timeout=60, env=env)
        assert "BRANCH=hotfix/urgent" in r.stdout, r.stdout[-400:]


# ==============================================================================
#  UI-харнесс
# ==============================================================================
class TestHarness17018:
    def test_harness_check_added(self):
        h = _read("tests/ui_harness.js")
        assert "1.70.18" in h and 'placeholder="main"' in h
