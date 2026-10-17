# ==============================================================================
#  Ямастер Чек — тесты v1.70.21 «мастер AdGuard: устойчивость»
#  Проверка на стрессоустойчивость нашла четыре дефекта, ВСЕ доказаны
#  экспериментом до правки и закрыты после:
#    • B1 гонка start_setup: два одновременных запроса поднимали двух воркеров
#      (7 двойных стартов из 60 раундов при 16 потоках) → атомарный try_start;
#    • B2 reset() подменял объект замка → поток отменённой установки терял
#      взаимное исключение;
#    • B3 отмена входа убивала только sudo, привилегированный CLI оставался
#      сиротой от root → _kill_proc() убивает всю группу процессов;
#    • B4 subprocess.TimeoutExpired вылетал наружу → владелец видел
#      «Непредвиденная ошибка: » без причины → код 124/127 и понятный текст.
#  Попутно: _copy_kit() больше не игнорирует коды возврата, помощник не глушит
#  вывод установщика AdGuard, маршрут отдаёт 409 вместо 500 при гонке.
#  Сети нет: subprocess и воркер подменяются, живые процессы — только локальные.
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import inspect
import os
import re
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.getcwd())

from fastapi import status

from app.config import settings
from app.services import adguard_setup as ag
from tests.test_v1121 import login

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
APP_JS = "app/static/js/app.js"
SERVICE = "app/services/adguard_setup.py"
ROUTES = "app/routers/settings_routes.py"
VER = "1.70.21"


def _read(path):
    return open(path, encoding="utf-8").read()


def _func_src(obj) -> str:
    return inspect.getsource(obj)


@pytest.fixture()
def fresh_job(tmp_path, monkeypatch):
    """Чистое задание И изолированный файл состояния (приём из v1.70.20)."""
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield ag.job
    ag.job.reset()


# ==============================================================================
#  B1 — гонка двойного запуска: атомарное занятие задания
# ==============================================================================
class TestStartRace:
    def test_try_start_is_atomic(self, fresh_job):
        """try_start() занят одним потоком → второй получает False."""
        assert fresh_job.try_start() is True
        assert fresh_job.running is True
        assert fresh_job.try_start() is False
        assert ag.job.try_start() is False

    def test_start_setup_raises_when_busy(self, fresh_job, monkeypatch):
        monkeypatch.setattr(ag, "_worker", lambda db=None: None)
        assert fresh_job.try_start() is True
        with pytest.raises(RuntimeError):
            ag.start_setup(None)

    def test_concurrent_starts_run_exactly_one_worker(self, monkeypatch):
        """Стресс: много раундов × потоков, воркер обязан быть ровно один.

        До правки (неатомарная проверка) этот же сценарий давал 7 двойных
        стартов из 60 раундов. Вытеснение потоков форсируется switchinterval —
        никакой инструментации самого кода.
        """
        started = []
        orig_worker = ag._worker
        monkeypatch.setattr(ag, "_worker",
                            lambda db=None: (started.append(1),
                                             time.sleep(0.01))[0])
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-9)
        rounds, n, doubles = 40, 16, 0
        try:
            for _ in range(rounds):
                del started[:]
                ag.job.reset()
                barrier = threading.Barrier(n)

                def racer():
                    barrier.wait()
                    try:
                        ag.start_setup(None)
                    except RuntimeError:
                        pass

                ts = [threading.Thread(target=racer) for _ in range(n)]
                [t.start() for t in ts]
                [t.join() for t in ts]
                time.sleep(0.03)
                if len(started) != 1:
                    doubles += 1
        finally:
            sys.setswitchinterval(old)
            ag._worker = orig_worker
            ag.job.reset()
        assert doubles == 0, f"двойных/нулевых стартов: {doubles} из {rounds}"

    def test_start_setup_uses_try_start(self):
        """Регресс-гард: атомарный вход нельзя молча вернуть к проверке."""
        src = _func_src(ag.start_setup)
        assert "job.try_start()" in src
        assert "job.running = True" not in src, \
            "занятие задания должно быть внутри try_start(), под замком"

    def test_try_start_holds_lock(self):
        src = _func_src(ag.SetupJob.try_start)
        assert "with self._lock:" in src
        assert src.index("with self._lock:") < src.index("if self.running")

    def test_route_returns_409_not_500_on_race(self, client, fresh_job):
        """Если гонка всё же случилась, владелец видит 409, а не 500."""
        adm = login(client, "admin", "admin123")
        fresh_job.running = True          # задание занято «другим» запросом
        r = client.post("/api/v1/settings/telegram/adguard/setup",
                        json={"action": "start"}, headers=adm)
        assert r.status_code == status.HTTP_409_CONFLICT, r.text

    def test_route_wraps_start_setup(self):
        block = _read(ROUTES)
        i = block.index("def adguard_setup_action(")
        body = block[i:i + 2600]
        assert "try:\n        ag.start_setup(db)" in body
        assert "except RuntimeError" in body
        assert "HTTP_409_CONFLICT" in body.split("except RuntimeError")[1][:400]


# ==============================================================================
#  B2 — объект замка переживает reset()
# ==============================================================================
class TestLockIdentity:
    def test_reset_keeps_same_lock(self, fresh_job):
        lock = fresh_job._lock
        for _ in range(3):
            fresh_job.reset()
            assert fresh_job._lock is lock, "reset() не должен подменять замок"

    def test_new_job_has_lock(self):
        j = ag.SetupJob()
        assert isinstance(j._lock, type(threading.Lock()))
        j.reset()
        assert j._lock is not None

    def test_source_does_not_recreate_lock(self):
        src = _func_src(ag.SetupJob.reset)
        assert "threading.Lock()" in src           # создаётся только если нет
        assert "getattr(self, \"_lock\", None) or threading.Lock()" in src


# ==============================================================================
#  B3 — отмена убивает всю группу процессов (сирот от root не остаётся)
# ==============================================================================
class TestProcessGroupKill:
    def test_kill_proc_kills_grandchild(self):
        """Живая проверка: внук умирает вместе с родителем."""
        p = subprocess.Popen(
            ["bash", "-c", "sleep 30 & echo child=$!; wait"],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            child = int(p.stdout.readline().strip().split("=")[1])
            ag._kill_proc(p)
            p.wait(timeout=10)
            time.sleep(0.3)
            alive = subprocess.run(["ps", "-p", str(child), "-o", "pid="],
                                   capture_output=True, text=True).stdout.strip()
            assert not alive, f"сирота выжил: pid {child}"
        finally:
            if p.poll() is None:
                p.kill()

    def test_kill_proc_survives_dead_process(self):
        p = subprocess.Popen(["true"], start_new_session=True)
        p.wait(timeout=10)
        ag._kill_proc(p)                  # не должно бросать исключение

    def test_login_worker_cancel_kills_group(self, fresh_job, tmp_path,
                                             monkeypatch):
        """Отмена во время входа: привилегированный потомок не остаётся."""
        pidfile = tmp_path / "child.pid"
        monkeypatch.setattr(ag, "_login_argv", lambda cli: [
            "bash", "-c",
            "sleep 30 & echo $! > " + str(pidfile) +
            "; echo 'authorize https://auth.adguard.io/device_code?user_code=ABCD'"
            "; wait"])
        monkeypatch.setattr(ag, "LOGIN_TIMEOUT", 60.0)
        box = {}

        def run():
            box["res"] = ag._login_worker("/bin/true")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 20
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "потомок не успел стартовать"
        child = int(pidfile.read_text().strip())
        fresh_job.cancel_requested = True
        t.join(timeout=25)
        assert not t.is_alive(), "_login_worker не завершился после отмены"
        rc = box["res"][0]
        assert rc == 130, box["res"][:2]
        time.sleep(0.3)
        alive = subprocess.run(["ps", "-p", str(child), "-o", "pid="],
                               capture_output=True, text=True).stdout.strip()
        assert not alive, f"привилегированный сирота выжил: pid {child}"

    def test_no_bare_kill_in_login_worker(self):
        """Регресс-гард: голого proc.kill() в _login_worker быть не должно."""
        src = _func_src(ag._login_worker)
        assert "proc.kill()" not in src
        assert src.count("_kill_proc(proc)") >= 3   # отмена, таймаут, finally

    def test_kill_proc_uses_killpg(self):
        src = _func_src(ag._kill_proc)
        assert "os.killpg(" in src and "signal.SIGKILL" in src
        assert "proc.kill()" in src                 # фолбэк сохранён


# ==============================================================================
#  B4 — таймаут и отсутствующая команда: понятный исход вместо «Непредвиденной»
# ==============================================================================
class TestTimeoutReporting:
    def test_run_cmd_timeout_returns_124(self, fresh_job):
        fresh_job.sudo_mode = "root"
        rc, out = ag.run_cmd(["sleep", "5"], timeout=0.2)
        assert rc == 124
        assert "timeout" in out and "sleep" in out
        assert "Непредвиденная" not in out

    def test_run_cmd_missing_command_returns_127(self, fresh_job):
        fresh_job.sudo_mode = "root"
        rc, out = ag.run_cmd(["/нет/такой/команды"], timeout=5)
        assert rc == 127
        assert "не найдена" in out.lower()

    def test_run_cmd_timeout_text_keeps_fraction(self, fresh_job):
        """«0 с» вместо «0.2 с» — тоже баг сообщения."""
        fresh_job.sudo_mode = "root"
        _rc, out = ag.run_cmd(["sleep", "5"], timeout=0.25)
        assert "0.25 с" in out, out

    def test_run_helper_timeout_returns_124(self, fresh_job, monkeypatch):
        fresh_job.sudo_mode = "root"
        monkeypatch.setattr(ag, "helper_argv",
                            lambda phase, mode=None: ["sleep", "5"])
        rc, out = ag.run_helper("tun", timeout=0.2)
        assert rc == 124
        assert "помощник tun" in out, out

    def test_run_helper_missing_helper_returns_127(self, fresh_job,
                                                   monkeypatch):
        fresh_job.sudo_mode = "root"
        monkeypatch.setattr(ag, "helper_argv",
                            lambda phase, mode=None:
                            ["/нет/такой/comанды", "x", phase])
        rc, out = ag.run_helper("tun", timeout=5)
        assert rc == 127
        assert "Помощник не найден" in out

    def test_worker_reports_timeout_readably(self, fresh_job, monkeypatch):
        """Сквозное: владелец видит причину таймаута, а не «Непредвиденная»."""
        def steps(db):
            rc, out = ag.run_cmd(["sleep", "5"], timeout=0.2)
            assert rc == 124
            raise RuntimeError(out)

        monkeypatch.setattr(ag, "_run_steps", steps)
        ag._worker(None)
        assert fresh_job.finished and not fresh_job.success
        assert "timeout" in fresh_job.error
        assert "Непредвиденная" not in fresh_job.error

    def test_updater_pattern_is_shared(self):
        """Образец — updater._run: те же коды 124/127."""
        from app.services import updater as up
        src = _func_src(up._run)
        assert "return 124" in src and "return 127" in src
        assert "return 124" in _func_src(ag.run_cmd)
        assert "return 127" in _func_src(ag.run_cmd)


# ==============================================================================
#  B6 — копирование комплекта не игнорирует коды возврата
# ==============================================================================
class TestCopyKitReportsFailure:
    def test_cp_failure_raises_with_reason(self, fresh_job, monkeypatch):
        fresh_job.sudo_mode = "nopass"
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            if cmd[0] == "cp":
                return 1, "cp: cannot create regular file: Permission denied"
            return 0, ""

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        with pytest.raises(RuntimeError) as e:
            ag._copy_kit()
        assert "Не скопировать" in str(e.value)
        assert "Permission denied" in str(e.value)

    def test_chmod_failure_raises_with_reason(self, fresh_job, monkeypatch):
        fresh_job.sudo_mode = "nopass"

        def fake_run(cmd, **kw):
            if cmd[0] == "chmod":
                return 1, "chmod: changing permissions: Operation not permitted"
            return 0, ""

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        with pytest.raises(RuntimeError) as e:
            ag._copy_kit()
        assert "Не выставить права" in str(e.value)

    def test_source_checks_rc(self):
        src = _func_src(ag._copy_kit)
        assert "rc != 0" in src
        assert src.count("rc, out = run_cmd(") >= 2


# ==============================================================================
#  B5 — помощник не глушит вывод установщика AdGuard
# ==============================================================================
class TestHelperInstallOutput:
    def _phase_install(self) -> str:
        src = _read(HELPER_SH)
        i = src.index("phase_install() {")
        return src[i:src.index("\n}\n", i)]

    def test_installer_output_not_suppressed(self):
        phase = self._phase_install()
        line = [l for l in phase.split("\n") if "$INSTALL_CMD" in l]
        assert line, "строка запуска установщика не найдена"
        for l in line:
            assert ">/dev/null" not in l, f"вывод установщика снова глушится: {l}"

    def test_failure_reason_is_printed(self):
        phase = self._phase_install()
        assert "install_out=$(sh -c \"$INSTALL_CMD\" </dev/null 2>&1)" in phase
        assert "install_rc" in phase
        assert "причина: " in phase
        assert "tail -n 5" in phase

    def test_no_local_cross_reference(self):
        """Приём v1.70.20: зависимые переменные — отдельными строками."""
        phase = self._phase_install()
        for l in phase.split("\n"):
            if l.strip().startswith("local "):
                assert l.strip() in ("local install_out", "local install_rc=0",
                                     "local cli"), l

    def test_helper_syntax_ok(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr

    def test_install_phase_runs_and_reports_reason(self, tmp_path):
        """Живой прогон: установщик «падает» → причина видна в выводе фазы."""
        src = _read(HELPER_SH)
        body = src[:src.rindex('main "$@"')]
        driver = tmp_path / "driver.sh"
        driver.write_text(body + """
# заглушки: root не нужен, установщик имитирует отказ сети
need_root() { :; }
cli_path() { return 1; }
INSTALL_CMD="echo 'curl: (6) Could not resolve host: raw.githubusercontent.com' >&2; exit 7"
phase_install
""", encoding="utf-8")
        r = subprocess.run(["bash", str(driver)], capture_output=True,
                           text=True, timeout=120)
        assert "Could not resolve host" in r.stdout + r.stderr, r.stdout
        assert "причина: " in r.stdout + r.stderr, r.stdout
        assert "код 7" in r.stdout + r.stderr, r.stdout


# ==============================================================================
#  Синхронность версии v1.70.21
# ==============================================================================
class TestVersions17021:
    """Пины версии устойчивы к бампу: точную версию проверяет тест СВОЕЙ версии,
    здесь — «не ниже 1.70.21» и синхронность статики с текущей версией."""

    def test_version_at_least(self):
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) \
            >= (1, 70, 21)

    def test_static_pins_follow_current_version(self):
        ver = settings.APP_VERSION
        idx = _read("app/static/index.html")
        assert len(re.findall(r"\?v=" + re.escape(ver), idx)) == 2
        sw = _read("app/static/sw.js")
        assert f"ymaster-check-v{ver}" in sw
        assert len(re.findall(r"\?v=" + re.escape(ver), sw)) == 2
        assert f'"version": "{ver}"' in _read("app/static/manifest.webmanifest")
        assert f"?v={ver}" in _read("app/static/maxapp/index.html")

    def test_no_stale_previous_version(self):
        for f in ("app/static/index.html", "app/static/sw.js",
                  "app/static/manifest.webmanifest",
                  "app/static/maxapp/index.html"):
            assert "1.70.20" not in _read(f), f

    def test_changelog_and_whatsnew(self):
        assert _read("CHANGELOG.md").count(f"## [{VER}]") == 1
        assert f"'{VER}':" in _read(APP_JS)
        assert f"'{VER}':" in _read("tests/ui_harness.js")

    def test_changelog_mentions_every_fix(self):
        ch = _read("CHANGELOG.md")
        i = ch.index(f"## [{VER}]")
        j = ch.index("## [1.70.20]", i)
        section = ch[i:j]
        for needle in ("гонка", "сирот", "124", "замка", "_copy_kit",
                       "установщика"):
            assert needle.lower() in section.lower(), needle
