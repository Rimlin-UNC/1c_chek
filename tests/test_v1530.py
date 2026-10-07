# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.53.0: живой ход обновления (стрим pip, таймеры
# этапов, пульс) + автоматическое решение о перезагрузке (код — сервис и
# страница перезапускаются сами; только документация — работа продолжается).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import subprocess
import sys


class TestVersion1530:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.53.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.52.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.53.0':" in js
        assert js.index("'1.53.0':") < js.index("'1.52.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.53.0]") == 1
        assert ch.index("## [1.53.0]") < ch.index("## [1.52.1]")


class TestRestartDecision:
    def test_needs_restart_rules(self):
        from app.services.updater import _needs_restart
        assert _needs_restart([]) is True                       # неизвестно — да
        assert _needs_restart(["docs/plan.md", "README.md"]) is False
        assert _needs_restart(["CHANGELOG.md"]) is False
        assert _needs_restart([".github/workflows/ci.yml"]) is False
        assert _needs_restart(["app/services/updater.py"]) is True
        assert _needs_restart(["requirements.txt"]) is True
        assert _needs_restart(["CHANGELOG.md", "app/main.py"]) is True

    def test_changed_files_real_git(self, tmp_path, monkeypatch):
        from app.services import updater
        origin, work = tmp_path / "origin", tmp_path / "work"

        def git(*args):
            subprocess.run(["git", *args], check=True, capture_output=True)

        git("init", "-q", "-b", "main", str(origin))
        git("-C", str(origin), "config", "user.name", "t")
        git("-C", str(origin), "config", "user.email", "t@t")
        (origin / "README.md").write_text("v1")
        git("-C", str(origin), "add", ".")
        git("-C", str(origin), "commit", "-qm", "c1")
        git("clone", "-q", f"file://{origin}", str(work))
        c1 = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
        # коммит-документация
        (origin / "README.md").write_text("v2")
        git("-C", str(origin), "add", ".")
        git("-C", str(origin), "commit", "-qm", "c2-docs")
        # коммит с кодом
        (origin / "app").mkdir()
        (origin / "app" / "mod.py").write_text("x = 1")
        git("-C", str(origin), "add", ".")
        git("-C", str(origin), "commit", "-qm", "c3-code")

        monkeypatch.setattr(updater, "APP_DIR", str(work))
        subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"],
                       check=True, capture_output=True)
        files = updater._changed_files(c1, "origin/main")
        assert "README.md" in files and "app/mod.py" in files
        assert updater._needs_restart(files) is True
        docs_only = updater._changed_files(c1, "origin/main~1")
        assert docs_only == ["README.md"]
        assert updater._needs_restart(docs_only) is False


class TestJobAndStream:
    def test_public_fields(self, tmp_path, monkeypatch):
        from app.services import updater
        monkeypatch.setattr(updater, "APP_DIR", str(tmp_path))
        updater.job.reset()
        pub = updater.job.public()
        assert pub["restart_required"] is True
        assert pub["changed_count"] == 0
        assert pub["last_activity"] == 0.0
        updater.job.say("backup", 10, "копия…")
        pub2 = updater.job.public()
        assert pub2["last_activity"] > 0
        updater.job.reset()

    def test_run_stream_success_and_fail(self, tmp_path, monkeypatch):
        from app.services import updater
        monkeypatch.setattr(updater, "APP_DIR", str(tmp_path))
        updater.job.reset()
        rc, out = updater._run_stream(
            [sys.executable, "-c", "print('alpha'); print('beta')"],
            "deps", 58, 14)
        assert rc == 0 and "beta" in out
        rc2, out2 = updater._run_stream(
            [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
            "deps", 58, 14)
        assert rc2 != 0 and "boom" in out2
        updater.job.reset()

    def test_pip_uses_stream_and_decision(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        assert "_run_stream(pip_cmd," in src
        assert '--progress-bar", "off"' in src
        assert '_needs_restart(changed)' in src
        assert '"restart_planned"' in src
        # прежний тихий запуск pip заменён
        assert "_run(_pip_cmd(), timeout=900)" not in src

    def test_restart_only_when_required(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        body = src.split("def _do_apply")[1].split("def start_apply")[0]
        # перезапуск — внутри условия; при docs-only — продолжение работы
        assert "if job.restart_required:" in body
        assert "Перезапуск не потребовался" in body
        # итог по-прежнему фиксируется до вызова рестарта (регресс 1.52.1)
        assert body.index("job.success = True") \
            < body.index("_restart_service(sudo_password)")


class TestUIReloadDecision:
    def test_ui_markers(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # версия интерфейса этой страницы
        assert "bootVersion" in js and 'script[src*="app.js?v="]' in js
        # решение: перезагружать, только если версия интерфейса сменилась
        assert ("!state.bootVersion || st.current_version !== state.bootVersion" in js)
        # программа перезагружается сама, без кнопки
        assert "setTimeout(() => location.reload(), 1800)" in js
        assert 'if (st.success && !st.needs_restart) location.reload();' not in js
        # путь без перезагрузки: данные обновляются на месте
        assert "перезагрузка не потребовалась" in js
        assert "try { refreshBadges(); } catch (e) {}" in js
        assert "try { route(true); } catch (e) {}" in js
        # ожидание рестарта — только если он был запланирован
        assert "st.restart_required !== false" in js
        # живой ход: таймер этапа, пульс, признак жизни процесса
        assert "upd-active" in js and "upd-pulse" in js
        assert "p.started_at * 1000" in js
        assert "этап идёт дольше обычного" in js
        # в отчёте: файлы и перезагрузка
        assert "Изменено файлов" in js and "Перезагрузка</dt>" in js

    def test_whatsnew_mentions_both_paths(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        chunk = js.split("'1.53.0':")[1].split("'1.52.1':")[0]
        assert "перезагружаются автоматически" in chunk
        assert "без перезагрузки" in chunk


class TestBlocksRegistry:
    def test_updates_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Обновления"] == "1.53.0"
        assert reg["Сканирование чеков"] == "1.52.0"   # не задет
