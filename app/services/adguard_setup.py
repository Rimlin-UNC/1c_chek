# ==============================================================================
#  Ямастер Чек — автоустановка AdGuard VPN из программы (v1.70.15)
#  Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
# ==============================================================================
#  Владелец нажимает «🛡 AdGuard VPN» в карточке Telegram — программа сама:
#    precheck   проверяет сервер (linux, systemd, curl, права root/sudo);
#    cli        ставит AdGuard VPN CLI официальным установщиком (пропускает,
#               если уже стоит; сама чинит ссылку /usr/local/bin);
#    login      запускает `adguardvpn-cli login` и отдаёт владельцу ссылку
#               подтверждения (OAuth device-code, живёт ~30 мин) — после
#               подтверждения в браузере шаг завершается сам;
#    socks      включает режим SOCKS5 (локальный прокси 127.0.0.1:1080,
#               маршруты и DNS системы не меняются);
#    tunnel     ставит юнит adguard-vpn-telegram (автоподключение туннеля);
#    autopilot  ставит автопилот tg-health (замер 15:00 + смена локации 15:10);
#    proxy      подставляет socks5://127.0.0.1:1080 в настройки программы;
#    verify     3 коротких запроса до Telegram через готовый путь (ping_proxy).
#  Механика повторяет механизм обновления: фазы по порядку, живой журнал,
#  состояние на диске (data/adguard_setup.json) — переживает рестарт.
#  Безопасность: команды фиксированные (нет пользовательского ввода в шелле);
#  всё админ-онли; ссылка входа видна только админу.
# ==============================================================================
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

from .updater import APP_DIR

INSTALL_CMD = ("curl -fsSL https://raw.githubusercontent.com/AdguardTeam/"
               "AdGuardVPNCLI/HEAD/scripts/release/install.sh | sh -s -- -v")
CLI_CANDIDATES = ("/usr/local/bin/adguardvpn-cli", "/opt/adguardvpn_cli/adguardvpn-cli")
LINK_CMD = ("ln -sf /opt/adguardvpn_cli/adguardvpn-cli "
            "/usr/local/bin/adguardvpn-cli")
DEPLOY_DIR = os.path.join(APP_DIR, "deploy", "adguard-vpn")
ETC_DIR = "/etc/systemd/system"          # юниты systemd
SBIN_DIR = "/usr/local/sbin"             # сценарий автопилота
UNIT_MAP = {  # комплект -> (каталог назначения, права)
    "adguard-vpn-telegram.service": ("etc", 0o644),
    "tg-health.service": ("etc", 0o644),
    "tg-health.timer": ("etc", 0o644),
    "tg-health.sh": ("sbin", 0o755),
}
DEST_DIRS = {"etc": ETC_DIR, "sbin": SBIN_DIR}
SETT_PROXY = "telegram_proxy"
PROXY_VALUE = "socks5://127.0.0.1:1080"
STEP_ORDER = ["precheck", "cli", "login", "socks", "tunnel",
              "autopilot", "proxy", "verify", "done"]
STEP_TITLE = {
    "precheck": "Проверка сервера",
    "cli": "Установка AdGuard VPN CLI",
    "login": "Вход в аккаунт AdGuard (ссылка-подтверждение)",
    "socks": "Режим SOCKS5 (локальный прокси)",
    "tunnel": "Туннель и автоподключение",
    "autopilot": "Автопилот: пинг 15:00, смена локации 15:10",
    "proxy": "Настройка программы (прокси в карточке)",
    "verify": "Проверка пути до Telegram",
    "done": "Готово",
}
CMD_TIMEOUT = 300.0        # обычная команда / установщик
LOGIN_TIMEOUT = 900.0      # ссылка входа живёт ~30 минут, ждём 15
LOGIN_URL_RE = re.compile(r"https?://[^\s\"']+")
STATE_FLUSH_S = 1.0


def _stamp() -> str:
    return datetime.utcnow().strftime("%H:%M:%S")


class SetupJob:
    """Прогресс автоустановки — по образцу задания обновления (updater)."""

    def reset(self) -> None:
        self.running = False
        self.finished = False
        self.success = False
        self.error = ""
        self.step = ""
        self.progress = 0
        self.log: list[str] = []
        self.steps = {s: {"status": "pending", "message": ""}
                      for s in STEP_ORDER}
        self.login_url = ""
        self.cli_version = ""
        self.ping_ms = None
        self.sudo_used = False
        self.started_at = 0.0
        self.finished_at = 0.0
        self.elapsed_s = 0
        self.cancel_requested = False
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def __init__(self) -> None:
        self.reset()

    # --- журнал/фазы (упрощённый say() из updater) -------------------------
    def say(self, step: str, message: str = "",
            status: str | None = None, progress: int | None = None) -> None:
        flush = False
        with self._lock:
            prev = self.steps.get(self.step)
            if (self.step and self.step != step
                    and prev and prev["status"] == "active"):
                prev["status"] = "done"                    # шаг завершён сам
            if step in self.steps:
                self.step = step
                if status:
                    self.steps[step]["status"] = status
            if message:
                self.steps[step]["message"] = message
                self.log.append(f"[{_stamp()}] {message}")
            if progress is not None:
                self.progress = max(self.progress, min(100, progress))
            now = time.time()
            if now - self._last_flush >= STATE_FLUSH_S:
                self._last_flush = now
                flush = True
        if flush:
            _state_write()

    def finish(self, success: bool, error: str = "") -> None:
        with self._lock:
            self.running = False
            self.finished = True
            self.success = success
            self.error = error
            self.finished_at = time.time()
            self.elapsed_s = int(self.finished_at - self.started_at)
            self.progress = 100 if success else self.progress
            cur = self.steps.get(self.step)
            if cur and cur["status"] == "active":
                cur["status"] = "done" if success else "failed"
        _state_write()

    def public(self) -> dict:
        with self._lock:
            return {
                "running": self.running, "finished": self.finished,
                "success": self.success, "error": self.error,
                "step": self.step, "progress": self.progress,
                "steps": [{"id": s, "title": STEP_TITLE[s], **self.steps[s]}
                          for s in STEP_ORDER],
                "log": self.log[-80:],
                "login_url": self.login_url,
                "cli_version": self.cli_version,
                "ping_ms": self.ping_ms,
                "sudo_used": self.sudo_used,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_s": self.elapsed_s,
            }


job = SetupJob()


# ==========================================================================
#  Состояние на диске — переживает рестарт (как data/update_state.json)
# ==========================================================================
def _state_path() -> str:
    return os.path.join(APP_DIR, "data", "adguard_setup.json")


def _state_write() -> None:
    try:
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        data = job.public()
        data["saved_at"] = datetime.utcnow().isoformat() + "Z"
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            import json
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except OSError:
        pass                                    # журнал живёт в памяти


def _state_load() -> dict | None:
    try:
        import json
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def setup_status() -> dict:
    """Живое задание → из памяти; после рестарта — последнее с диска."""
    if job.running or job.finished:
        return job.public()
    st = _state_load() or {}
    return st if st.get("finished") or st.get("running") else job.public()


# ==========================================================================
#  Запуск/отмена
# ==========================================================================
def start_setup(db) -> None:
    """Запустить установку (409-контроль на маршруте; здесь — страховка)."""
    if job.running:
        raise RuntimeError("Установка уже выполняется")
    job.reset()
    job.running = True
    job.started_at = time.time()
    _state_write()
    t = threading.Thread(target=_worker, args=(db,), daemon=True,
                         name="adguard-setup")
    t.start()


def cancel_setup() -> bool:
    """Отмена: мягкая — между шагами, жёсткая — прерывание ожидания входа."""
    if not job.running:
        return False
    job.cancel_requested = True
    job.say(job.step or "precheck", "Остановка по запросу владельца…")
    return True


# ==========================================================================
#  Исполнение команд (фиксированные строки, без пользовательского ввода)
# ==========================================================================
def run_cmd(cmd: list[str] | str, timeout: float = CMD_TIMEOUT,
            shell: bool = False) -> tuple[int, str]:
    """Выполнить команду, вернуть (код, вывод). Префикс sudo — по правам."""
    if job.sudo_used:
        if shell:
            full: list[str] | str = ["sudo", "-n", "sh", "-c", cmd]
            shell = False
        else:
            full = ["sudo", "-n"] + list(cmd)
    else:
        full = cmd
    p = subprocess.run(full, shell=shell, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _find_cli() -> str:
    w = shutil.which("adguardvpn-cli")
    if w:
        return w
    for c in CLI_CANDIDATES:
        if os.path.exists(c):
            return c
    return ""


ADGUARD_DOMAINS = ("adguard.io", "adguard.com", "adguard-vpn.com",
                   "adguardaccount.com")


def extract_login_url(line: str) -> str:
    """Ссылка подтверждения из строки stdout (OAuth device-code AdGuard).

    Официальный вид: https://auth.adguard.io/device_code?user_code=XXXX.
    Принимаем ТОЛЬКО домены AdGuard: CLI на скомпрометированной сети мог
    получить подменённый домен (issue AdguardTeam/AdGuardVPNCLI #188) —
    такую ссылку владельцу не показываем, шаг честно упадёт по таймауту.
    """
    from urllib.parse import urlsplit
    m = LOGIN_URL_RE.search(line or "")
    if not m:
        return ""
    u = m.group(0)
    host = (urlsplit(u).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in ADGUARD_DOMAINS):
        return u
    return ""


def _login_worker(cli: str) -> tuple[int, str, str]:
    """`login` с живым журналом: строки пишем в job, ссылку — в job.login_url.

    Возвращает (код, вывод, ссылка). Ссылку берём из stdout (OAuth
    device-code: «You need to authorize … https://auth.adguard.io/…»).
    Читает отдельный поток — цикл отмены не блокируется молчаливым stdout.
    """
    out: list[str] = []
    url = ""
    proc = subprocess.Popen([cli, "login"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True)

    def _pump() -> None:
        nonlocal url
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            out.append(line)
            job.say("login", line)
            if not url:
                url = extract_login_url(line)
                if url:
                    job.login_url = url
                    job.say("login",
                            "Ссылка для подтверждения входа: " + url +
                            " — откройте её и войдите в аккаунт AdGuard; "
                            "установка продолжится сама.")

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    deadline = time.time() + LOGIN_TIMEOUT
    try:
        while proc.poll() is None:
            if job.cancel_requested:
                proc.kill()
                return 130, "\n".join(out), url
            if time.time() > deadline:
                proc.kill()
                return 124, "\n".join(out), url
            time.sleep(0.5)
        pump.join(timeout=5)
    finally:
        if proc.poll() is None:                 # отмена/зависание
            try:
                proc.kill()
            except OSError:
                pass
    return proc.returncode or 0, "\n".join(out), url


# ==========================================================================
#  Шаги установки
# ==========================================================================
def _step_precheck() -> None:
    job.say("precheck", "Проверяем сервер…", "active", 3)
    if not sys_platform_linux():
        raise RuntimeError("Автоустановка работает на Linux (сервер Ubuntu).")
    rc, out = run_cmd(["systemctl", "--version"], timeout=15)
    if rc != 0:
        raise RuntimeError("systemd не найден — автоустановка требует "
                           "systemd (Ubuntu/Debian).")
    if not shutil.which("curl"):
        raise RuntimeError("curl не найден: sudo apt install -y curl")
    if os.geteuid() == 0:
        job.sudo_used = False
        job.say("precheck", "Права: root — хорошо.")
    else:
        rc, _ = run_cmd(["sudo", "-n", "true"], timeout=15)
        if rc != 0:
            raise RuntimeError("Нужны права root: запустите программу от "
                               "root (как на проде) или дайте sudo без "
                               "пароля пользователю программы.")
        job.sudo_used = True
        job.say("precheck", "Права: через sudo (без пароля) — хорошо.")
    job.say("precheck", "Сервер готов к установке.", "done", 8)


def sys_platform_linux() -> bool:
    return os.uname().sysname.lower().startswith("linux")


def _step_cli() -> None:
    job.say("cli", "Проверяем AdGuard VPN CLI…", "active", 12)
    cli = _find_cli()
    if cli:
        rc, out = run_cmd([cli, "--version"], timeout=30)
        if rc == 0:
            job.cli_version = (out or "").strip().splitlines()[0][:40] \
                if out.strip() else "установлен"
            job.say("cli", f"CLI уже установлен ({job.cli_version}) — "
                           "пропускаю установку.", "skip", 20)
            return
    job.say("cli", "Ставим официальным установщиком (1–2 минуты)…")
    rc, out = run_cmd(INSTALL_CMD, shell=True)
    if rc != 0:
        raise RuntimeError("Установщик AdGuard завершился с ошибкой: "
                           + (out or "").strip()[-300:])
    cli = _find_cli()
    rc, out = run_cmd([cli, "--version"], timeout=30) if cli else (1, "")
    if rc != 0:
        # известный случай: вопрос установщика про ссылку остался без ответа
        if os.path.exists("/opt/adguardvpn_cli/adguardvpn-cli"):
            job.say("cli", "Создаю ссылку /usr/local/bin (установщик не успел).")
            run_cmd(LINK_CMD, shell=True)
            rc, out = run_cmd(["/usr/local/bin/adguardvpn-cli", "--version"],
                              timeout=30) if _find_cli() else (1, "")
    if rc != 0:
        raise RuntimeError("CLI установлен, но не запускается: "
                           + (out or "").strip()[-200:])
    job.cli_version = (out or "").strip().splitlines()[0][:40] \
        if out.strip() else "установлен"
    job.say("cli", f"CLI готов ({job.cli_version}).", "done", 25)


def _step_login() -> None:
    job.say("login", "Запрашиваем ссылку входа AdGuard…", "active", 30)
    cli = _find_cli()
    if not cli:
        raise RuntimeError("CLI не найден — это внутренняя ошибка, "
                           "сообщите разработчику.")
    rc, out, url = _login_worker(cli)
    if job.cancel_requested:
        raise RuntimeError("Отменено владельцем.")
    if rc != 0:
        tail = (out or "").strip()[-300:]
        raise RuntimeError("Вход не завершён (код " + str(rc) + "): "
                           + (tail or "ссылка не подтверждена или истекла — "
                                      "запустите установку ещё раз."))
    if not url:
        job.say("login", "Вход уже выполнен — подтверждение не нужно.", "skip")
    else:
        job.say("login", "Вход подтверждён ✔", "done", 45)


def _step_socks() -> None:
    job.say("socks", "Включаем режим SOCKS5 (только локальный прокси)…",
            "active", 50)
    cli = _find_cli()
    rc, out = run_cmd([cli, "config", "set-mode", "SOCKS"], timeout=60)
    if rc != 0:
        raise RuntimeError("set-mode SOCKS не удался: "
                           + (out or "").strip()[-200:])
    job.say("socks", "SOCKS5 включён: socks5://127.0.0.1:1080 "
                     "(остальные службы сервера не затронуты).", "done", 55)


def _copy_kit() -> None:
    """Разложить комплект юнитов из deploy/adguard-vpn по системе."""
    missing = [n for n in UNIT_MAP
               if not os.path.exists(os.path.join(DEPLOY_DIR, n))]
    if missing:
        raise RuntimeError("Файлы комплекта не найдены в "
                           "deploy/adguard-vpn (" + ", ".join(missing) + ") — "
                           "обновите программу и повторите.")
    for name, (dst_key, mode) in UNIT_MAP.items():
        src = os.path.join(DEPLOY_DIR, name)
        dst = os.path.join(DEST_DIRS[dst_key], name)
        if job.sudo_used:
            run_cmd(["sudo", "-n", "cp", src, dst], timeout=30)
            run_cmd(["sudo", "-n", "chmod", f"{mode:o}", dst], timeout=15)
        else:
            shutil.copyfile(src, dst)
            os.chmod(dst, mode)


def _step_tunnel() -> None:
    job.say("tunnel", "Ставим службу туннеля (автоподключение после "
                      "перезагрузки)…", "active", 60)
    _copy_kit()
    rc, out = run_cmd(["systemctl", "daemon-reload"], timeout=30)
    if rc != 0:
        raise RuntimeError("daemon-reload не удался: " + (out or "")[-200:])
    run_cmd(["systemctl", "enable", "adguard-vpn-telegram"], timeout=30)
    rc, out = run_cmd(["systemctl", "restart", "adguard-vpn-telegram"],
                      timeout=60)
    if rc != 0:
        raise RuntimeError("Служба туннеля не запустилась: "
                           + (out or "").strip()[-300:])
    for _ in range(10):                        # до 20 с на подключение
        rc, _ = run_cmd(["systemctl", "is-active", "adguard-vpn-telegram"],
                        timeout=15)
        if rc == 0:
            break
        time.sleep(2)
    if rc != 0:
        raise RuntimeError("Туннель не поднялся за 20 с — смотрите "
                           "journalctl -u adguard-vpn-telegram.")
    job.say("tunnel", "Туннель подключён, служба активна "
                      "(переживает перезагрузку).", "done", 68)


def _step_autopilot() -> None:
    job.say("autopilot", "Ставим автопилот (замер 15:00, смена локации "
                         "15:10)…", "active", 74)
    rc, out = run_cmd(["systemctl", "daemon-reload"], timeout=30)
    if rc != 0:
        raise RuntimeError("daemon-reload не удался: " + (out or "")[-200:])
    run_cmd(["systemctl", "enable", "tg-health.timer"], timeout=30)
    rc, out = run_cmd(["systemctl", "enable", "--now", "tg-health.timer"],
                      timeout=30)
    if rc != 0:
        raise RuntimeError("Таймер автопилота не включился: "
                           + (out or "").strip()[-200:])
    job.say("autopilot", "Автопилот включён: каждый день 15:10 по серверному "
                         "времени (= МСК).", "done", 80)


def _step_proxy(db) -> None:
    job.say("proxy", "Подставляем прокси в настройки программы…", "active", 85)
    from . import appsettings
    appsettings.set_setting(db, SETT_PROXY, PROXY_VALUE)
    job.say("proxy", f"Прокси {PROXY_VALUE} записан в карточку Telegram.",
            "done", 90)


def _step_verify(db) -> None:
    job.say("verify", "Проверяем путь: 3 коротких запроса до Telegram…",
            "active", 93)
    from . import telegram_bot as tg
    r = tg.ping_proxy(db, samples=3, scheduled=False)
    if not r.get("ok"):
        raise RuntimeError("Путь до Telegram не отвечает через прокси — "
                           "проверьте вход/лицензию AdGuard; локацию подберёт "
                           "автопилот в 15:10 или кнопка «📏 Пинг».")
    job.ping_ms = r.get("ms")
    job.say("verify", f"Путь жив: средний пинг {r.get('ms')} мс.", "done", 97)


def _worker(db=None) -> None:
    """Фон-поток установки. Сессия запроса могла уже закрыться — своя."""
    try:
        from ..database import SessionLocal
        own = SessionLocal()
        try:
            _run_steps(own)
        finally:
            own.close()
    except RuntimeError as e:
        job.say(job.step or "precheck", f"Ошибка: {e}", "failed")
        job.finish(success=False, error=str(e))
    except Exception as e:                                  # noqa: BLE001
        job.finish(success=False, error=f"Непредвиденная ошибка: {e}")
    finally:
        _state_write()


def _run_steps(db) -> None:
    _step_precheck()
    if job.cancel_requested:
        raise RuntimeError("Отменено владельцем.")
    _step_cli()
    if job.cancel_requested:
        raise RuntimeError("Отменено владельцем.")
    _step_login()
    if job.cancel_requested:
        raise RuntimeError("Отменено владельцем.")
    _step_socks()
    _step_tunnel()
    _step_autopilot()
    _step_proxy(db)
    _step_verify(db)
    job.say("done", "Готово: путь до Telegram работает, пинг "
                    f"{job.ping_ms} мс. Осталось включить бота — "
                    "«💾 Сохранить и запустить» в карточке.", "done", 100)
    job.finish(success=True)
