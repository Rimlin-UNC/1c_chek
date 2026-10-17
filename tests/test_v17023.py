# ==============================================================================
#  Ямастер Чек — тесты v1.70.23 «мастер AdGuard: ссылка /usr/local/bin
#  создаётся всегда, установщику передаётся правильный флаг»
#
#  Дефекты найдены сверкой с ИСХОДНИКОМ официального установщика
#  AdguardTeam/AdGuardVPNCLI scripts/release/install.sh и подтверждены живым
#  прогоном его логики (см. TestInstallerSemantics):
#    • B9  INSTALL_CMD передавал `-v` в расчёте на «без вопросов». По исходнику
#      (getopts 'vho:uV:p:a:') `-v` — это verbose, и он УЖЕ включён по умолчанию
#      (verbose='1'). Автоответ даёт только `-a y|n`. Без него ask_user() делает
#      `read -r response < /dev/tty`; у службы tty нет → ответ пустой →
#      установщик создаёт ${output_dir}/.nosymlink и ссылку НЕ делает —
#      причём .nosymlink отключает вопрос и при всех последующих запусках;
#    • B10 create_symlink() сразу делает return 0, если /usr/local/bin не входит
#      в PATH. У юнита программы PATH=/opt/ymaster-check/venv/bin:/usr/bin:/bin
#      — его там нет, поэтому в режиме root ссылка не появлялась вовсе.
#      А страховка в _step_cli срабатывала только при rc != 0 от --version:
#      _find_cli() находит и /opt/adguardvpn_cli/adguardvpn-cli, проверка
#      проходила — и ссылка не создавалась, хотя на неё жёстко прописаны
#      ExecStart= юнита туннеля и CLI= в tg-health.sh. Установка ломалась на
#      следующем шаге («Служба туннеля не запустилась»).
#
#  Сети нет: логика установщика воспроизводится локальной копией ask_user() и
#  create_symlink() (строки 187-196 и 330-365 исходника) — это делает тест
#  независимым от доступности GitHub.
#  ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ==============================================================================
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
README = "deploy/adguard-vpn/README.md"
TUNNEL_UNIT = "deploy/adguard-vpn/adguard-vpn-telegram.service"
TG_HEALTH = "deploy/adguard-vpn/tg-health.sh"
APP_JS = "app/static/js/app.js"
VER = "1.70.23"

SERVICE_PATH = "/opt/ymaster-check/venv/bin:/usr/bin:/bin"   # из юнита
ROOTISH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Копия логики официального установщика: ask_user() (187-196) +
# create_symlink() (330-365). Поведение сверено с исходником и доказано
# прогоном: без -a y и без tty создаётся .nosymlink и ссылки нет.
INSTALLER_STUB = r"""
#!/bin/sh
auto_answer=''
verbose='1'
while getopts 'va:' opt; do
  case "$opt" in
    v) verbose='1' ;;
    a) case "$OPTARG" in
         [Yy]*|[Nn]*) auto_answer="$OPTARG" ;;
         *) echo "Invalid value for -a."; exit 1 ;;
       esac ;;
    *) exit 1 ;;
  esac
done
out="$OUT_DIR"; exe='adguardvpn-cli'
case "$PATH" in
  */usr/local/bin*) ;;
  *) echo "GUARD"; exit 0 ;;
esac
if [ -f "$out/.nosymlink" ]; then echo "SKIP"; exit 0; fi
printf 'create a link in /usr/local/bin? [y/N] '
if [ -n "$auto_answer" ]; then
  response="$auto_answer"
else
  read -r response < /dev/tty
fi
case "$response" in
  [Yy]*) ln -sf "$out/$exe" "$LINK_DIR/$exe"; echo "LINK" ;;
  *) touch "$out/.nosymlink"; echo "NOSYMLINK" ;;
esac
"""


def _read(path):
    return open(path, encoding="utf-8").read()


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Изолированный файл состояния (приём v1.70.20) + чистое задание."""
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    return tmp_path


# ==============================================================================
#  B9: установщику передаётся -a y, а не -v
# ==============================================================================
class TestAutoAnswerFlag:
    def test_service_passes_auto_answer(self):
        assert " -a y" in ag.INSTALL_CMD, \
            "без автоответа установщик ждёт /dev/tty и создаёт .nosymlink"

    def test_verbose_flag_is_not_the_answer(self):
        """`-v` — verbose (по умолчанию и так включён), вопросы он не снимает."""
        tail = ag.INSTALL_CMD.split("&&", 1)[1]
        assert "-v" not in tail, \
            "`-v` не отвечает на вопрос установщика — это verbose"

    def test_command_still_two_step(self):
        """Фикс v1.70.22 (два шага вместо пайплайна) не потерян."""
        assert "|" not in ag.INSTALL_CMD
        assert "&&" in ag.INSTALL_CMD
        assert ag.INSTALL_TMP in ag.INSTALL_CMD
        assert ag.INSTALL_CMD.index("curl") < \
            ag.INSTALL_CMD.index(f"sh {ag.INSTALL_TMP}")

    def test_helper_passes_auto_answer(self):
        src = _read(HELPER_SH)
        assert 'INSTALL_CMD="curl -fsSL $INSTALL_URL -o $INSTALL_TMP ' \
               '&& sh $INSTALL_TMP -a y"' in src

    def test_helper_no_longer_claims_v_silences_questions(self):
        """Комментарий «-v: без вопросов» был фактически неверным."""
        src = _read(HELPER_SH)
        assert "-v: без вопросов" not in src

    def test_helper_syntax_ok(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr


# ==============================================================================
#  Живое доказательство семантики установщика (копия его логики)
# ==============================================================================
class TestInstallerSemantics:
    """Прогон копии ask_user()/create_symlink() официального установщика.

    Запускаем в новой сессии (start_new_session) и с stdin=/dev/null — ровно
    те условия, в которых установщик работает из-под службы: терминала нет.
    """

    @pytest.fixture()
    def env(self, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text(INSTALLER_STUB, encoding="utf-8")
        opt = tmp_path / "opt"
        link_dir = tmp_path / "bin"
        opt.mkdir()
        link_dir.mkdir()
        (opt / "adguardvpn-cli").write_text("#!/bin/sh\necho 1.7.12\n",
                                            encoding="utf-8")
        (opt / "adguardvpn-cli").chmod(0o755)
        return {"script": str(script), "opt": str(opt),
                "link_dir": str(link_dir),
                "link": str(link_dir / "adguardvpn-cli")}

    def _run(self, env, args, path):
        e = dict(os.environ)
        e.update(PATH=path, OUT_DIR=env["opt"], LINK_DIR=env["link_dir"])
        r = subprocess.run(["sh", env["script"], *args], env=e,
                           capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        out = r.stdout + r.stderr
        return r, out, os.path.islink(env["link"]), \
            os.path.exists(os.path.join(env["opt"], ".nosymlink"))

    def test_without_flag_no_tty_leaves_no_link(self, env):
        """Дефект: `-v` не спасает — без tty ответ пустой, ссылки нет."""
        _, out, linked, nosym = self._run(env, ["-v"], ROOTISH_PATH)
        assert linked is False, "дефект не воспроизвёлся: ссылка появилась"
        assert nosym is True, ".nosymlink должен закреплять отказ"
        assert "NOSYMLINK" in out

    def test_auto_answer_creates_link(self, env):
        """Фикс: `-a y` отвечает за владельца и ссылка создаётся."""
        _, out, linked, nosym = self._run(env, ["-a", "y"], ROOTISH_PATH)
        assert linked is True, "фикс не работает: ссылки нет"
        assert nosym is False
        assert "LINK" in out
        assert os.readlink(env["link"]) == os.path.join(env["opt"],
                                                       "adguardvpn-cli")

    def test_path_guard_skips_question_entirely(self, env):
        """Второй путь к той же поломке: PATH юнита программы."""
        _, out, linked, _ = self._run(env, ["-a", "y"], SERVICE_PATH)
        assert "GUARD" in out
        assert linked is False, \
            "при PATH без /usr/local/bin установщик ссылку не делает вовсе"

    def test_nosymlink_blocks_all_next_runs(self, env):
        """`.nosymlink` отключает вопрос навсегда — переустановка не лечит."""
        self._run(env, ["-v"], ROOTISH_PATH)
        _, out, linked, _ = self._run(env, ["-a", "y"], ROOTISH_PATH)
        assert linked is False
        assert "SKIP" in out, "установщик обязан пропустить вопрос"

    def test_invalid_auto_answer_rejected(self, env):
        r, out, _, _ = self._run(env, ["-a", "maybe"], ROOTISH_PATH)
        assert r.returncode == 1
        assert "Invalid value for -a." in out


# ==============================================================================
#  B10: ссылка создаётся безусловно, а не только при сбое --version
# ==============================================================================
class TestLinkIsAlwaysEnsured:
    def _prepare(self, monkeypatch, link_exists, real_exists,
                 version=(0, "adguardvpn-cli 1.7.12"), install=(0, "OK")):
        """Возвращает список вызовов run_cmd."""
        calls = []
        real_exists_fn = os.path.exists

        def fake_exists(p):
            if p == ag.CLI_LINK:
                return link_exists
            if p == ag.CLI_REAL:
                return real_exists
            return real_exists_fn(p)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            text = cmd if kw.get("shell") else " ".join(cmd)
            if kw.get("shell") and "curl" in text:
                return install
            if kw.get("shell") and text.strip() == ag.LINK_CMD:
                return (0, "")
            if "--version" in text:
                return version
            return (0, "")

        monkeypatch.setattr(ag, "_find_cli",
                            lambda: ag.CLI_REAL if real_exists else "")
        monkeypatch.setattr(ag, "run_cmd", fake_run)
        monkeypatch.setattr(ag.os.path, "exists", fake_exists)
        ag.job.sudo_mode = "root"
        return calls

    def test_link_created_even_when_version_ok(self, monkeypatch, state_dir):
        """Главный регресс: /opt-бинарник работает, ссылки нет — её надо создать.

        До v1.70.23 создание ссылки было обёрнуто в `if rc != 0`, а проверка
        версии проходила успешно → ссылка не появлялась → юнит туннеля с
        ExecStart=/usr/local/bin/adguardvpn-cli не запускался.
        """
        calls = self._prepare(monkeypatch, link_exists=False, real_exists=True,
                              version=(0, "adguardvpn-cli 1.7.12"))
        ag._step_cli()
        assert any(c == ag.LINK_CMD for c in calls), \
            "ссылка не создана, хотя её нет, а бинарник в /opt есть"
        assert ag.job.cli_version.startswith("adguardvpn-cli")

    def test_link_not_recreated_when_present(self, monkeypatch, state_dir):
        calls = self._prepare(monkeypatch, link_exists=True, real_exists=True)
        ag._step_cli()
        assert not any(c == ag.LINK_CMD for c in calls), \
            "ln -sf не должен дёргаться, когда ссылка уже есть"

    def test_no_link_attempt_without_real_binary(self, monkeypatch, state_dir):
        """Нет бинарника в /opt — ссылку создавать не на что."""
        calls = self._prepare(monkeypatch, link_exists=False, real_exists=False,
                              install=(0, "OK"))
        with pytest.raises(RuntimeError) as e:
            ag._step_cli()
        assert not any(c == ag.LINK_CMD for c in calls)
        assert "не появился после установки" in str(e.value)

    def test_link_failure_reports_reason(self, monkeypatch, state_dir):
        """Отказ ln больше не проходит молча (класс дефектов v1.70.20/21)."""
        calls = self._prepare(monkeypatch, link_exists=False, real_exists=True)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            text = cmd if kw.get("shell") else " ".join(cmd)
            if kw.get("shell") and "curl" in text:
                return (0, "OK")
            if kw.get("shell") and text.strip() == ag.LINK_CMD:
                return (1, "ln: failed to create symbolic link: "
                           "Permission denied")
            return (0, "adguardvpn-cli 1.7.12")

        monkeypatch.setattr(ag, "run_cmd", fake_run)
        with pytest.raises(RuntimeError) as e:
            ag._step_cli()
        msg = str(e.value)
        assert ag.CLI_LINK in msg
        assert "Permission denied" in msg

    def test_message_explains_what_happened(self, monkeypatch, state_dir):
        self._prepare(monkeypatch, link_exists=False, real_exists=True)
        ag._step_cli()
        texts = " ".join(s.get("message", "") for s in ag.job.steps.values())
        log = " ".join(ag.job.log)
        assert "Создаю ссылку" in texts + log


# ==============================================================================
#  Почему ссылка обязательна: на неё жёстко прописаны юнит и автопилот
# ==============================================================================
class TestLinkIsMandatory:
    def test_constants_match_hardcoded_paths(self):
        assert ag.CLI_LINK == "/usr/local/bin/adguardvpn-cli"
        assert ag.CLI_REAL == "/opt/adguardvpn_cli/adguardvpn-cli"
        assert ag.LINK_CMD == f"ln -sf {ag.CLI_REAL} {ag.CLI_LINK}"
        assert list(ag.CLI_CANDIDATES) == [ag.CLI_LINK, ag.CLI_REAL]

    def test_tunnel_unit_uses_link_path(self):
        unit = _read(TUNNEL_UNIT)
        assert f"ExecStart={ag.CLI_LINK} connect --no-fork" in unit

    def test_autopilot_uses_link_path(self):
        assert ag.CLI_LINK in _read(TG_HEALTH)

    def test_helper_creates_link_unconditionally(self):
        """Помощник обеспечивает ссылку функцией ensure_link, а не по rc."""
        src = _read(HELPER_SH)
        assert re.search(r"^ensure_link\(\) \{$", src, re.M), \
            "в помощнике нет функции ensure_link"
        body = src.split("ensure_link() {", 1)[1].split("\n}", 1)[0]
        assert 'ln -sf "$CLI_REAL" "$CLI_LINK"' in body
        assert "die" in body, "отказ ln не должен проходить молча"
        # вызывается во всех точках, где нужен CLI_LINK
        for phase in ("phase_install", "phase_units"):
            chunk = src.split(f"{phase}() {{", 1)[1].split("\nphase_", 1)[0]
            assert "ensure_link" in chunk, \
                f"{phase} не обеспечивает ссылку"

    def test_helper_ensures_link_on_already_installed_path(self):
        """Ранний выход «CLI уже установлен» больше не минует ссылку."""
        src = _read(HELPER_SH)
        chunk = src.split("phase_install() {", 1)[1].split("\nphase_", 1)[0]
        # якорь — именно строка say, а не комментарий с той же фразой
        early = chunk.split('say "CLI уже установлен', 1)[0]
        assert "ensure_link" in early, \
            "на пути «CLI уже установлен» ссылка не обеспечивается"
        assert "return 0" in chunk.split('say "CLI уже установлен', 1)[1][:80]


# ==============================================================================
#  Инструкция ручной установки больше не ведёт к туннелю без лицензии
# ==============================================================================
class TestManualInstructions:
    def test_readme_uses_auto_answer(self):
        txt = _read(README)
        assert "-a y" in txt
        assert "| sh -s -- -v" not in txt, \
            "пайплайн прячет отказ curl (фикс v1.70.22) и лишает автоответа"

    def test_readme_explains_flag(self):
        txt = _read(README).lower()
        assert ".nosymlink" in txt
        assert "verbose" in txt

    def test_readme_requires_sudo_for_login(self):
        """Юнит запускает CLI от root (HOME=/root) — вход должен быть root'овым."""
        txt = _read(README)
        assert "sudo adguardvpn-cli login" in txt
        assert "sudo adguardvpn-cli config set-mode SOCKS" in txt

    def test_unit_really_runs_as_root(self):
        unit = _read(TUNNEL_UNIT)
        assert "User=root" in unit
        assert "Environment=HOME=/root" in unit

    def test_readme_mentions_disconnect_before_service(self):
        assert "sudo adguardvpn-cli disconnect" in _read(README)

    def test_readme_has_manual_link_fallback(self):
        assert f"ln -sf {ag.CLI_REAL} {ag.CLI_LINK}" in _read(README)


# ==============================================================================
#  Синхронность версии v1.70.23
# ==============================================================================
class TestVersions17023:
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
            assert "1.70.22" not in _read(f), f

    def test_changelog_and_whatsnew(self):
        assert _read("CHANGELOG.md").startswith(f"## [{VER}]")
        assert f"'{VER}':" in _read(APP_JS)
        assert f"'{VER}':" in _read("tests/ui_harness.js")

    def test_changelog_mentions_every_fix(self):
        top = _read("CHANGELOG.md").split("## [1.70.22]")[0]
        for needle in ("-a y", "verbose", ".nosymlink", "PATH", "безусловно",
                       "getopts", "ExecStart", "tg-health.sh", "sudo"):
            assert needle.lower() in top.lower(), needle

    def test_whatsnew_is_not_a_placeholder(self):
        js = _read(APP_JS)
        block = js.split(f"'{VER}':", 1)[1].split("'1.70.22':", 1)[0]
        assert len(block) > 400
        for needle in ("/usr/local/bin", "-a y", "PATH"):
            assert needle in block
