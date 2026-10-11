# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.3: оранжевые кнопки во всей программе,
# московское время на сервере (deploy.sh), закрепление переключателя
# вида в UI-харнессе.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import os


def worker_bundle_path():
    """Уникальный путь бандла на воркер pytest-xdist (параллельный прогон)."""
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"/tmp/ymaster_ui_bundle_{w}.js"


from tests.test_v1121 import worker_bundle_path



class TestVersion1583:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 58, 3)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.58.2" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.58.3':") < js.index("'1.58.2':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.58.3]") == 1
        assert ch.index("## [1.58.3]") < ch.index("## [1.58.2]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Адаптивный интерфейс"].split(".")) >= (1, 58, 3)
        assert reg["Проверка чеков (ФНС и источники)"] == "1.58.0"  # не задет
        assert reg["Чек-Пул"] == "1.56.1"


class TestCorporateOrange:
    """Все кнопки — в фирменном оранжевом; семантика сохранена."""

    def test_vars(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "--accent-soft: #fff1e3;" in css
        assert "--accent-text: #9a4a00;" in css

    def test_default_btn_orange(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "border: 1px solid rgba(255, 122, 0, 0.45);" in css
        btn_block = css.split(".btn {")[1].split("}")[0]
        assert "background: var(--accent-soft); color: var(--accent-text);" in btn_block
        assert "var(--panel-strong)" not in btn_block   # нейтральная тема убрана
        assert "border-color: var(--accent); background: #ffe4c9;" in css

    def test_semantics_kept(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".btn-ok { border-color: rgba(52,211,153,.4); color: var(--ok); background: #eaf7f1; }" in css
        assert ".btn-bad { border-color: rgba(248,113,113,.4); color: var(--bad); background: #fdeeec; }" in css
        assert ".btn-primary {" in css                       # градиент на месте

    def test_btn_icon_orange_hover(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".btn-icon:hover { background: var(--accent-soft); color: var(--accent); }" in css


class TestServerMsk:
    """deploy.sh переводит системные часы сервера в МСК."""

    def test_deploy_sets_timezone(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert "timedatectl set-timezone Europe/Moscow" in d
        assert "часовой пояс сервера: Europe/Moscow (МСК)" in d

    def test_app_side_untouched(self):
        """Приложение по-прежнему форсирует TZ Europe/Moscow и +0300 в журнале."""
        src = open("app/main.py", encoding="utf-8").read()
        assert 'os.environ["TZ"] = "Europe/Moscow"' in src
        assert "+0300" in src


class TestToggleLockedByHarness:
    """Переключатель «Список / Карточки» — живые клики в UI-харнессе."""

    def test_redraw_fix_present(self):
        """ФИКС v1.58.3: guard «данные не менялись» сброшен при смене вида."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "const switchView = (v) => {" in js
        assert "viewReceipts._sig = undefined;   // v1.58.3: guard «данные не менялись»" in js
        assert "segL.onclick = () => switchView('list');" in js
        assert "segC.onclick = () => switchView('cards');" in js

    def test_harness_has_toggle_checks(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "переключатель вида на странице и привязан" in h
        assert "чеки: режим «Карточки» — действия внизу карточки" in h
        assert "чеки: возврат в режим «Список»" in h
        assert "_docQ['#rc-view-cards']" in h

    def test_harness_runs_green(self):
        """Харнесс (v1.13/v1.14 + новые проверки) проходит целиком."""
        import shutil
        import subprocess
        import sys
        from pathlib import Path
        if shutil.which("node") is None:
            self.skipTest("node недоступен")
        # бандл строится из ТЕКУЩЕГО app.js (как в test_v1130)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tests.test_v1121 import _build_bundle
        open(worker_bundle_path(), "w", encoding="utf-8").write(
            _build_bundle())
        r = subprocess.run(
            ["node", "tests/ui_harness.js", worker_bundle_path()],
            capture_output=True, text=True, timeout=120,
            cwd=str(Path(__file__).resolve().parent.parent))
        assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]
        assert "HARNESS_OK" in r.stdout
        assert "режим «Карточки»" in r.stdout
