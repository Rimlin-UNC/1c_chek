# ==============================================================================
#  Ямастер Чек — тесты v1.70.22 «мастер AdGuard: панель не застревает,
#  причина отказа не теряется»
#  Оба дефекта найдены ЖИВЫМ прогоном приложения (не только pytest):
#    • B7 сохранённое `running: true` после перезапуска процесса отдавалось как
#      живое задание: панель вечно показывала «установка выполняется», опрос не
#      останавливался, а «Остановить» отвечал 409 (в памяти задания нет).
#      Сценарий боевой — deploy.sh --update перезапускает службу;
#    • B8 установка CLI шла пайплайном `curl … | sh -s -- -v`: код возврата
#      принадлежит ПОСЛЕДНЕЙ команде, поэтому отказ curl (нет сети/DNS) выглядел
#      как успех, а `_step_cli` печатал «CLI установлен, но не запускается: »
#      с ПУСТЫМ хвостом (вывод установщика затирался результатом --version).
#      `set -o pipefail` не решение: /bin/sh в Ubuntu/Debian — dash.
#  Сети нет: недоступный источник — это 127.0.0.1:9 (мгновенный отказ).
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import json
import os
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
APP_JS = "app/static/js/app.js"
VER = "1.70.22"
DEAD_URL = "https://127.0.0.1:9/nope.sh"     # отказ мгновенный и оффлайн


def _read(path):
    return open(path, encoding="utf-8").read()


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Изолированный файл состояния (приём v1.70.20) + чистое задание."""
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    return tmp_path


def _write_stale(tmp_path, **over):
    st = {
        "running": True, "finished": False, "success": False, "error": "",
        "step": "login", "progress": 30,
        "steps": [{"id": "precheck", "status": "done", "message": "готов"},
                  {"id": "login", "status": "active", "message": "ожидание"}],
        "log": ["[12:00:01] Проверяем сервер…"],
        "login_url": "https://auth.adguard.io/device_code?user_code=XXXX",
        "cli_version": "1.2.3", "ping_ms": None, "sudo_mode": "nopass",
        "one_time_cmd": "", "started_at": time.time() - 600,
        "finished_at": 0.0, "elapsed_s": 0,
    }
    st.update(over)
    (tmp_path / "adguard_setup.json").write_text(
        json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


# ==============================================================================
#  B7 — протухшее «running» после перезапуска
# ==============================================================================
class TestStaleRunningState:
    def test_stale_running_reported_as_interrupted(self, state_dir):
        _write_stale(state_dir)
        st = ag.setup_status()
        assert st["running"] is False, "фантомное «выполняется» после рестарта"
        assert st["finished"] is True
        assert st["success"] is False
        assert st.get("interrupted") is True
        assert "прервана перезапуском" in st["error"]

    def test_journal_and_login_url_preserved(self, state_dir):
        _write_stale(state_dir)
        st = ag.setup_status()
        assert st["login_url"].startswith("https://auth.adguard.io/")
        assert any("Проверяем сервер" in l for l in st["log"])
        assert "прервана перезапуском" in st["log"][-1]

    def test_active_step_becomes_failed(self, state_dir):
        """Спиннер не должен крутиться: активный шаг → failed."""
        _write_stale(state_dir)
        st = ag.setup_status()
        assert [s["id"] for s in st["steps"] if s["status"] == "active"] == []
        assert "login" in [s["id"] for s in st["steps"]
                           if s["status"] == "failed"]

    def test_state_file_not_rewritten(self, state_dir):
        """Чтение не обязано затирать историю на диске."""
        _write_stale(state_dir)
        before = (state_dir / "adguard_setup.json").read_text(encoding="utf-8")
        ag.setup_status()
        after = (state_dir / "adguard_setup.json").read_text(encoding="utf-8")
        assert json.loads(before)["running"] is True
        assert json.loads(after)["running"] is True   # на диске всё как было

    def test_live_job_wins_over_disk(self, state_dir):
        """Живое задание из памяти важнее любого файла."""
        _write_stale(state_dir)
        ag.job.running = True
        ag.job.step = "socks"
        st = ag.setup_status()
        assert st["running"] is True
        assert st.get("interrupted") is None
        assert st["step"] == "socks"

    def test_finished_state_passed_through(self, state_dir):
        """Завершённое задание не помечается прерванным."""
        _write_stale(state_dir, running=False, finished=True, success=True,
                     error="")
        st = ag.setup_status()
        assert st["success"] is True
        assert "interrupted" not in st
        assert st["error"] == ""

    def test_no_state_returns_memory_job(self, state_dir):
        st = ag.setup_status()
        assert st["running"] is False and st["finished"] is False
        assert "interrupted" not in st

    def test_corrupt_state_does_not_crash(self, state_dir):
        (state_dir / "adguard_setup.json").write_text("{не json", encoding="utf-8")
        st = ag.setup_status()
        assert st["running"] is False

    def test_existing_error_is_kept(self, state_dir):
        """Своя причина важнее общей фразы про перезапуск."""
        _write_stale(state_dir, error="Вход не завершён (код 124)")
        st = ag.setup_status()
        assert st["error"] == "Вход не завершён (код 124)"
        assert st.get("interrupted") is True

    def test_source_marks_interrupted(self):
        import inspect
        src = inspect.getsource(ag.setup_status)
        assert '"interrupted"' in src or "'interrupted'" in src
        assert 'st["running"] = False' in src


# ==============================================================================
#  B8 — отказ установщика больше не выглядит успехом
# ==============================================================================
class TestInstallCommandPropagatesFailure:
    def test_install_cmd_is_not_a_pipeline(self):
        assert "|" not in ag.INSTALL_CMD, \
            "пайплайн прячет код возврата curl за кодом sh"
        assert "&&" in ag.INSTALL_CMD
        assert "-o " in ag.INSTALL_CMD

    def test_install_cmd_downloads_then_runs(self):
        assert ag.INSTALL_TMP in ag.INSTALL_CMD
        assert ag.INSTALL_URL in ag.INSTALL_CMD
        i_curl = ag.INSTALL_CMD.index("curl")
        i_run = ag.INSTALL_CMD.index(f"sh {ag.INSTALL_TMP}")
        assert i_curl < i_run, "сначала скачиваем, потом выполняем"

    def test_pipeline_hides_failure_but_two_step_does_not(self):
        """Живое доказательство дефекта и фикса на недоступном источнике."""
        old = subprocess.run(
            ["sh", "-c", f"curl -fsSL {DEAD_URL} | sh -s -- -v"],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        new = subprocess.run(
            ["sh", "-c", f"curl -fsSL {DEAD_URL} -o /tmp/ym-test-inst.sh "
                         f"&& sh /tmp/ym-test-inst.sh -v"],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        assert old.returncode == 0, "дефект не воспроизвёлся: пайплайн упал"
        assert new.returncode != 0, "фикс не работает: отказ curl не виден"
        assert "curl" in (new.stdout + new.stderr).lower()

    def test_dash_has_no_pipefail(self):
        """Обоснование, почему выбраны два шага, а не pipefail."""
        r = subprocess.run(["sh", "-c", "set -o pipefail"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode != 0
        assert "pipefail" in (r.stdout + r.stderr)

    def test_helper_uses_same_two_step_command(self):
        src = _read(HELPER_SH)
        assert "INSTALL_URL=" in src and "INSTALL_TMP=" in src
        assert 'INSTALL_CMD="curl -fsSL $INSTALL_URL -o $INSTALL_TMP ' \
               '&& sh $INSTALL_TMP -v"' in src
        install_line = [l for l in src.split("\n")
                        if "$INSTALL_CMD" in l and "sh -c" in l]
        assert install_line, "строка запуска установщика не найдена"
        for l in install_line:
            assert "| sh -s --" not in l

    def test_helper_syntax_ok(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  Причина отказа никогда не пустая
# ==============================================================================
class TestReasonNeverEmpty:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t ", None])
    def test_empty_output_gives_hint(self, text):
        out = ag._reason(text)
        assert out.strip()
        assert "вывод пуст" in out

    def test_real_output_is_kept(self):
        assert "curl: (35)" in ag._reason("curl: (35) SSL_connect failed")

    def test_tail_is_limited(self):
        assert len(ag._reason("x" * 5000)) <= 300
        assert ag._reason("y" * 5000, limit=50) == "y" * 50


class TestStepCliMessages:
    def _run_cli_step(self, monkeypatch, state_dir, install=(0, ""),
                      cli_found="", version=(1, "")):
        monkeypatch.setattr(ag, "_find_cli", lambda: cli_found)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if kw.get("shell") and "curl" in str(cmd):
                return install
            return version

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        monkeypatch.setattr(ag.os.path, "exists", lambda p: False)
        ag.job.sudo_mode = "nopass"
        return calls

    def test_installer_failure_shows_curl_reason(self, monkeypatch, state_dir):
        self._run_cli_step(monkeypatch, state_dir,
                           install=(35, "curl: (35) OpenSSL SSL_connect: "
                                        "SSL_ERROR_SYSCALL"))
        with pytest.raises(RuntimeError) as e:
            ag._step_cli()
        assert "Установщик AdGuard завершился с ошибкой" in str(e.value)
        assert "curl: (35)" in str(e.value)

    def test_missing_cli_says_so_and_keeps_installer_output(self, monkeypatch,
                                                            state_dir):
        """Ровно тот случай, что давал пустой хвост на бою."""
        self._run_cli_step(monkeypatch, state_dir, install=(0, ""),
                           cli_found="", version=(1, ""))
        with pytest.raises(RuntimeError) as e:
            ag._step_cli()
        msg = str(e.value)
        assert "не появился после установки" in msg
        assert not msg.endswith(": "), f"пустая причина: {msg!r}"
        assert msg.split(": ", 1)[1].strip()

    def test_broken_cli_includes_path(self, monkeypatch, state_dir):
        self._run_cli_step(monkeypatch, state_dir, install=(0, ""),
                           cli_found="/usr/local/bin/adguardvpn-cli",
                           version=(127, "bash: not found"))
        with pytest.raises(RuntimeError) as e:
            ag._step_cli()
        msg = str(e.value)
        assert "не запускается" in msg
        assert "/usr/local/bin/adguardvpn-cli" in msg
        assert "bash: not found" in msg

    def test_silent_installer_still_explains(self, monkeypatch, state_dir):
        """Установщик «успешен», но CLI нет и вывод пуст — подсказка про сеть."""
        self._run_cli_step(monkeypatch, state_dir, install=(0, ""),
                           cli_found="")
        with pytest.raises(RuntimeError) as e:
            ag._step_cli()
        assert "raw.githubusercontent.com" in str(e.value)


# ==============================================================================
#  Синхронность версии v1.70.22
# ==============================================================================
class TestVersions17022:
    def test_config_version(self):
        assert settings.APP_VERSION == VER

    def test_static_pins(self):
        idx = _read("app/static/index.html")
        assert len(re.findall(r"\?v=" + re.escape(VER), idx)) == 2
        sw = _read("app/static/sw.js")
        assert f"ymaster-check-v{VER}" in sw
        assert len(re.findall(r"\?v=" + re.escape(VER), sw)) == 2
        assert f'"version": "{VER}"' in _read("app/static/manifest.webmanifest")
        assert f"?v={VER}" in _read("app/static/maxapp/index.html")

    def test_no_previous_version_left(self):
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert "1.70.21" not in _read(f), f

    def test_changelog_and_whatsnew(self):
        assert _read("CHANGELOG.md").startswith(f"## [{VER}]")
        assert f"'{VER}':" in _read(APP_JS)
        assert f"'{VER}':" in _read("tests/ui_harness.js")

    def test_changelog_mentions_both_fixes(self):
        top = _read("CHANGELOG.md").split("## [1.70.21]")[0]
        for needle in ("перезапуска", "пайплайн", "pipefail", "dash",
                       "пустым хвостом"):
            assert needle.lower() in top.lower(), needle
