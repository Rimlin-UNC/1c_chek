# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.52.1: исправление «зависания» отслеживания
# обновления (стриминг прогресса git fetch, итог до перезапуска,
# восстановление окна хода).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import subprocess


class TestVersion1521:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.52.1"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.52.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.52.1':" in js
        assert js.index("'1.52.1':") < js.index("'1.52.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.52.1]") == 1
        assert ch.index("## [1.52.1]") < ch.index("## [1.52.0]")


class TestFetchStreamsProgress:
    def test_fetch_progress_live(self, tmp_path, monkeypatch):
        """git fetch отдаёт прогресс «кареткой» (\r): чтение должно быть
        потоковым — фаза download появляется в статусе во время загрузки,
        а не после её завершения."""
        from app.services import updater
        origin = tmp_path / "origin"
        work = tmp_path / "work"
        def git(*args, **kw):
            subprocess.run(["git", *args], check=True,
                           capture_output=True, **kw)
        git("init", "-q", "-b", "main", str(origin))
        git("-C", str(origin), "config", "user.name", "t")
        git("-C", str(origin), "config", "user.email", "t@t")
        (origin / "f.txt").write_text("x" * 65536)
        git("-C", str(origin), "add", ".")
        git("-C", str(origin), "commit", "-qm", "c1")
        # file:// — принудительно пак-протокол с прогрессом Receiving objects
        git("clone", "-q", f"file://{origin}", str(work))
        (origin / "f.txt").write_text("y" * 131072)
        git("-C", str(origin), "add", ".")
        git("-C", str(origin), "commit", "-qm", "c2")

        monkeypatch.setattr(updater, "APP_DIR", str(work))
        updater.job.reset()
        try:
            rc, out, nbytes = updater._git_fetch_progress("main")
        finally:
            updater.job.reset()
        assert rc == 0, out

    def test_fetch_handles_empty_and_broken(self, tmp_path, monkeypatch):
        from app.services import updater
        origin = tmp_path / "origin"
        subprocess.run(["git", "init", "-q", "-b", "main", str(origin)],
                       check=True, capture_output=True)
        monkeypatch.setattr(updater, "APP_DIR", str(origin))
        updater.job.reset()
        try:
            # неизвестная ветка — git вернёт ошибку, функция не должна упасть
            rc, out, nbytes = updater._git_fetch_progress("no-such-branch")
            assert rc != 0
            assert nbytes == 0
        finally:
            updater.job.reset()

    def test_progress_parser_unchanged(self):
        from app.services.updater import _parse_fetch_progress
        pct, nbytes = _parse_fetch_progress(
            "Receiving objects:  45% (123/273), 55.00 KiB | 210.00 KiB/s")
        assert pct == 45 and nbytes == 55 * 1024


class TestTerminalStateBeforeRestart:
    def test_success_written_before_restart(self):
        """Итог (статус на диске + история) пишется ДО _restart_service:
        самоперезапуск (execv) не возвращает управление — после рестарта
        писать уже некому."""
        src = open("app/services/updater.py", encoding="utf-8").read()
        body = src.split("def _do_apply")[1].split("def start_apply")[0]
        assert body.index("job.success = True") \
            < body.index("_restart_service(sudo_password)")
        assert body.index('"action": "apply"') \
            < body.index("_restart_service(sudo_password)")
        # терминальная запись состояния тоже до рестарта
        assert body.index("job.elapsed_s = max(1, int(job.finished_at - job.started_at))") \
            < body.index("_restart_service(sudo_password)")
        # при неудаче рестарта — needs_restart и повторная запись
        assert "if not restarted:" in body
        assert body.index("if not restarted:") > body.index("_restart_service(sudo_password)")

    def test_error_path_persists_state(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        body = src.split("def _do_apply")[1].split("def start_apply")[0]
        err = body.split("except Exception as e:")[1]
        head = err[:600]
        assert "job.note_failure()" in head
        assert "_state_write()" in head

    def test_streaming_implementation(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        fn = src.split("def _git_fetch_progress")[1].split("def ")[0]
        # потоковое чтение по байтам + разбиение по \r и \n
        assert "select" in fn and "os.read(" in fn
        assert 're.split(rb"[' in fn
        # троттлинг сообщений — не спамим статус
        assert "last_say" in fn and "0.7" in fn
        # прежний блокирующий построчный паттерн исчез
        assert "for line in p.stderr" not in fn


class TestTrackingRestoreUI:
    def test_auto_reopen_and_dismiss(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # окно хода возвращается, если обновление ещё идёт
        assert "updAutoDismissed" in js
        assert "state.updAutoDismissed !== st.started_at" in js
        # ключ запуска запоминается при опросе и при закрытии окна
        assert "autoKey = st.started_at || autoKey;" in js
        assert "state.updAutoDismissed = autoKey;" in js
        # активные фазы после успеха отображаются завершёнными
        assert 'if (st.success) return `<div style="color:var(--ok)">✓ ${title}${dtxt}</div>`;' in js
        # журнал прокручивается
        assert 'id="upd-log" style="max-height:180px;overflow:auto' in js


class TestBlocksRegistry:
    def test_updates_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Обновления"] == "1.52.1"
        assert reg["Сканирование чеков"] == "1.52.0"   # не задет
