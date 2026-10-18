# ==============================================================================
#  Ямастер Чек — автоустановка AdGuard VPN из программы (v1.70.15, v1.70.16)
#  Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
# ==============================================================================
#  v1.70.16 — мастер БЕЗ пароля: пароль сервера (sudo) из браузера больше не
#  спрашивается вовсе. Привилегированные шаги делает один помощник
#  /usr/local/sbin/ymaster-adguard-setup (deploy/adguard-vpn), разрешённый
#  узким sudoers-правилом /etc/sudoers.d/ymaster-adguard на фиксированные фазы.
#  Правило ставит deploy.sh при установке/обновлении; на сервере, где его ещё
#  нет, панель мастера показывает ОДНУ готовую команду для терминала (копируется
#  кнопкой) — после неё мастер работает сам и паролей не просит.
#  Владелец нажимает «🛡 AdGuard VPN» в карточке Telegram — программа сама:
#    precheck   проверяет сервер (linux, systemd, curl) и права: root,
#               sudo без пароля либо помощник (sudoers-правило) — пароль
#               сервера НЕ спрашивается никогда;
#    cli        ставит AdGuard VPN CLI официальным установщиком (пропускает,
#               если уже стоит; сама чинит ссылку /usr/local/bin);
#    login      запускает `adguardvpn-cli login` и отдаёт владельцу ссылку
#               подтверждения (OAuth device-code, живёт ~30 мин) — после
#               подтверждения в браузере шаг завершается сам;
#    tun        включает режим TUN + selective-исключения: через VPN идёт
#               ТОЛЬКО Telegram (api.telegram.org и его поддомены), весь
#               прочий трафик сервера остаётся прямым. DNS тоже уходит в
#               туннель, поэтому отравление DNS больше не ломает бота;
#    tunnel     ставит юнит adguard-vpn-telegram (автоподключение туннеля);
#    autopilot  ставит автопилот tg-health (замер 15:00 + смена локации 15:10);
#    proxy      УБИРАЕТ прокси из настроек программы: в TUN-режиме локального
#               SOCKS-слушателя нет, трафик идёт прозрачно;
#    verify     3 коротких запроса до Telegram через готовый путь (ping_proxy).
#  Механика повторяет механизм обновления: фазы по порядку, живой журнал,
#  состояние на диске (data/adguard_setup.json) — переживает рестарт.
#  Безопасность: команды фиксированные (нет пользовательского ввода в шелле);
#  привилегированные шаги — только через помощника с явными фазами (sudo -n);
#  всё админ-онли; ссылка входа видна только админу; пароли не принимаются.
# ==============================================================================
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime

from .updater import APP_DIR

INSTALL_URL = ("https://raw.githubusercontent.com/AdguardTeam/"
               "AdGuardVPNCLI/HEAD/scripts/release/install.sh")
INSTALL_TMP = "/tmp/ymaster-adguard-install.sh"
# v1.70.22: ДВУМЯ шагами (скачать, затем выполнить), а не `curl … | sh -s --`.
# В пайплайне код возврата принадлежит ПОСЛЕДНЕЙ команде (sh), поэтому отказ
# curl (нет сети, DNS, rc 6/35) выглядел как успешная установка, а владелец
# получал «CLI установлен, но не запускается: » с пустой причиной.
# `set -o pipefail` здесь не помощник: /bin/sh в Ubuntu/Debian — dash, а в нём
# это «Illegal option -o pipefail» (проверено).
#
# v1.70.23: `-a y` — автоответ «да» на вопрос установщика «create a link in
# /usr/local/bin? [y/N]». Раньше стоял `-v`, но по исходнику установщика
# (getopts 'vho:uV:p:a:') `-v` — это verbose, и он УЖЕ включён по умолчанию
# (verbose='1'), на вопросы он не отвечает. Без автоответа ask_user() читает
# `read -r response < /dev/tty`; у службы tty нет → ответ пустой → установщик
# создаёт файл .nosymlink и ссылка НЕ появляется. Кроме того, create_symlink()
# сразу делает return 0, если /usr/local/bin не входит в PATH (у юнита
# программы PATH=/opt/ymaster-check/venv/bin:/usr/bin:/bin — его там нет).
INSTALL_CMD = (f"curl -fsSL {INSTALL_URL} -o {INSTALL_TMP} "
               f"&& sh {INSTALL_TMP} -a y")
CLI_LINK = "/usr/local/bin/adguardvpn-cli"
CLI_REAL = "/opt/adguardvpn_cli/adguardvpn-cli"
CLI_CANDIDATES = (CLI_LINK, CLI_REAL)
LINK_CMD = f"ln -sf {CLI_REAL} {CLI_LINK}"
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
# v1.70.16: привилегированный помощник — единственная команда, которую
# разрешает sudoers-правило; фазы фиксированные, аргументов из приложения нет.
HELPER_NAME = "ymaster-adguard-setup"
HELPER_PATH = "/usr/local/sbin/" + HELPER_NAME
HELPER_SRC = "ymaster-adguard-setup.sh"          # в deploy/adguard-vpn
HELPER_RULE = "/etc/sudoers.d/ymaster-adguard"   # правило NOPASSWD
HELPER_PHASES = ("preflight", "rights", "install", "login", "tun", "off",
                 "units", "autopilot", "version", "status")
RIGHTS_MODES = ("root", "nopass", "helper")      # «pass» в v1.70.16 удалён
SETT_PROXY = "telegram_proxy"
# v1.70.25: имена юнитов — одни на сервис и помощник (раньше в сервисе были
# рассыпаны литералы, и переименование юнита разошлось бы молча).
TUNNEL_UNIT = "adguard-vpn-telegram"
TIMER_UNIT = "tg-health.timer"
# v1.70.24: в режиме TUN трафик до Telegram уходит в туннель ПРОЗРАЧНО
# (selective-исключение на api.telegram.org), а локального SOCKS-слушателя
# 127.0.0.1:1080 больше нет. Оставь мы там прежний socks5://… — бот ходил бы
# в несуществующий прокси и молчал. Поэтому мастер прокси НЕ подставляет,
# а очищает поле: пустое значение = прямое соединение = и есть путь через
# туннель. Замер пинга (ping_proxy) при пустом прокси измеряет ровно тот
# путь, которым реально пользуется бот.
PROXY_VALUE = ""
STEP_ORDER = ["precheck", "cli", "login", "tun", "tunnel",
              "autopilot", "verify", "done"]
STEP_TITLE = {
    "precheck": "Проверка сервера",
    "cli": "Установка AdGuard VPN CLI",
    "login": "Вход в аккаунт AdGuard (ссылка-подтверждение)",
    "tun": "Режим TUN: через VPN только Telegram",
    "tunnel": "Туннель и автоподключение",
    "autopilot": "Автопилот: пинг 15:00, смена локации 15:10",
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
        self.sudo_mode = "none"      # none|root|nopass|helper (после precheck)
        self.one_time_cmd = ""       # v1.70.16: команда для терминала (1 раз)
        self.started_at = 0.0
        self.finished_at = 0.0
        self.elapsed_s = 0
        self.cancel_requested = False
        self._last_flush = 0.0
        # v1.70.21: замок создаётся ОДИН раз. Подмена объекта замка в reset()
        # лишала взаимного исключения поток, который ещё держал старый замок
        # (воркер отменённой установки продолжал писать в чужое задание).
        self._lock = getattr(self, "_lock", None) or threading.Lock()

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
                "sudo_mode": self.sudo_mode,
                "one_time_cmd": self.one_time_cmd,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_s": self.elapsed_s,
            }


    def try_start(self) -> bool:
        """v1.70.21: атомарно занять задание под замком.

        Раньше `if job.running` и `job.running = True` разделяло окно, поэтому
        два одновременных запроса успевали пройти проверку оба и поднимали двух
        воркеров: параллельно ставили CLI, перезапускали службы и писали в одно
        задание. Доказано экспериментом: 7 двойных стартов из 60 раундов при 16
        одновременных вызовах (sys.setswitchinterval(1e-9)).
        """
        with self._lock:
            if self.running:
                return False
            self.reset()
            self.running = True
            self.started_at = time.time()
            return True


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
    """Живое задание → из памяти; после рестарта — последнее с диска.

    v1.70.22: сохранённое `running: true` после перезапуска процесса заведомо
    устарело — поток установки умер вместе с процессом. Раньше панель вечно
    показывала «установка выполняется», опрос не останавливался, а «Остановить»
    отвечал 409 (в памяти задания нет): владелец застревал в фантомном
    состоянии. Сценарий вполне боевой — `deploy.sh --update` перезапускает
    службу после каждого обновления. Такое состояние помечаем прерванным,
    журнал сохраняем.
    """
    if job.running or job.finished:
        return job.public()
    st = _state_load() or {}
    if not (st.get("finished") or st.get("running")):
        return job.public()
    if st.get("running"):
        st = dict(st)
        st["running"] = False
        st["finished"] = True
        st["success"] = False
        st["interrupted"] = True
        st["error"] = st.get("error") or (
            "Установка была прервана перезапуском программы — журнал сохранён; "
            "запустите мастер ещё раз.")
        steps = []
        for s in st.get("steps") or []:
            s = dict(s)
            if s.get("status") == "active":
                s["status"] = "failed"          # спиннер не должен крутиться
            steps.append(s)
        if steps:
            st["steps"] = steps
        st["log"] = list(st.get("log") or []) + \
            [f"[{_stamp()}] {st['error']}"]
    return st


# ==========================================================================
#  Запуск/отмена
# ==========================================================================
def start_setup(db) -> None:
    """Запустить установку (409-контроль на маршруте; здесь — страховка).

    v1.70.16: паролей мастер не принимает вовсе — привилегированные шаги
    идут через помощника (sudoers NOPASSWD) либо от root/sudo без пароля.
    Если прав нет, precheck честно останавливается и показывает ОДНУ
    команду для терминала (job.one_time_cmd).

    v1.70.21: занятие задания атомарно (job.try_start()) — проверка
    «не выполняется ли уже» и установка running=True больше не разделены
    окном, поэтому одновременные запросы не поднимают двух воркеров."""
    if not job.try_start():
        raise RuntimeError("Установка уже выполняется")
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
def _cmd_name(cmd) -> str:
    """Человекочитаемое имя команды для сообщений (список или строка shell)."""
    if isinstance(cmd, str):
        return cmd.strip().split(" ")[0][:60] or cmd[:60]
    return str(cmd[0]) if cmd else ""


def _timeout_text(timeout: float, name: str) -> str:
    """v1.70.21: таймаут — понятный исход (код 124, как в updater._run),
    а не «Непредвиденная ошибка: » в панели владельца."""
    return (f"timeout ({timeout:g} с): {name} — команда не завершилась "
            f"вовремя и была прервана")


def _reason(text: str, limit: int = 300) -> str:
    """Хвост вывода с гарантией НЕпустоты (v1.70.22).

    Пустая «причина» после двоеточия — ровно та диагностика, которой не хватило
    на боевом сервере: сообщение есть, а зацепки нет. Если вывод пуст, говорим
    наиболее вероятное (нет сети до источника) вместо пустой строки.
    """
    tail = (text or "").strip()[-limit:]
    return tail or ("вывод пуст — вероятнее всего, нет доступа к источнику "
                    "(raw.githubusercontent.com: DNS/файрвол/прокси)")


def run_cmd(cmd: list[str] | str, timeout: float = CMD_TIMEOUT,
            shell: bool = False, input_text: str = "") -> tuple[int, str]:
    """Выполнить команду, вернуть (код, вывод).

    Права — по режиму из precheck: root — как есть; nopass — `sudo -n`;
    helper — произвольные команды от root НЕ выполняются вовсе, работает
    только run_helper() с фиксированной фазой (v1.70.16).
    Режим `pass` (пароль служебного пользователя через stdin) в v1.70.16
    удалён: мастер пароли не принимает."""
    if job.sudo_mode == "helper":
        raise RuntimeError("В режиме помощника произвольные команды от root "
                           "не выполняются — только run_helper(фаза).")
    if job.sudo_mode == "nopass":
        inner = ["sh", "-c", cmd] if shell else list(cmd)
        full = ["sudo", "-n"] + inner
        shell = False
    else:
        full = cmd
    try:
        p = subprocess.run(full, shell=shell, capture_output=True,
                           text=True, timeout=timeout,
                           input=input_text or None)
    except subprocess.TimeoutExpired:
        # v1.70.21: как в updater._run — таймаут это понятный исход (код 124),
        # а не «Непредвиденная ошибка» в панели.
        return 124, _timeout_text(timeout, _cmd_name(full))
    except FileNotFoundError:
        return 127, f"Команда не найдена: {_cmd_name(full)}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run_helper(phase: str, timeout: float = CMD_TIMEOUT,
               stream: bool = False, mode: str | None = None) -> tuple[int, str]:
    """v1.70.16: привилегированный шаг через помощника — `sudo -n <helper> <фаза>`.

    Фаза — только из HELPER_PHASES (фиксированный набор, аргументов нет):
    ровно эти команды разрешает sudoers-правило /etc/sudoers.d/ymaster-adguard.
    В режиме root помощник не нужен — выполняем фазу тем же сценарием напрямую
    (он лежит в комплекте), а в режиме nopass — через `sudo -n`.

    v1.70.25: `mode` можно передать явно — выключение VPN (turn_off) работает
    вне задания установки, где job.sudo_mode ещё не определён precheck'ом."""
    if phase not in HELPER_PHASES:
        raise RuntimeError(f"Неизвестная фаза помощника: {phase}")
    argv = helper_argv(phase, mode)
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # v1.70.21: фаза помощника могла зависнуть (установщик, systemctl) —
        # возвращаем понятный код и текст, шаг честно падает с причиной.
        return 124, _timeout_text(timeout, f"помощник {phase}")
    except FileNotFoundError:
        return 127, (f"Помощник не найден: {argv[-2] if len(argv) > 1 else ''}"
                     " — выполните одноразовую команду установки правила")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def helper_script() -> str:
    """Путь к сценарию помощника: установленный (root) либо из комплекта."""
    if os.path.exists(HELPER_PATH):
        return HELPER_PATH
    src = os.path.join(DEPLOY_DIR, HELPER_SRC)
    return src if os.path.exists(src) else ""


def helper_argv(phase: str, mode: str | None = None) -> list[str]:
    """Аргументы вызова помощника по режиму прав (см. run_helper).

    v1.70.25: `mode` можно передать явно. Это нужно выключению VPN
    (turn_off) — оно выполняется вне задания установки, и ради него
    подменять job.sudo_mode было бы грязно: на это состояние смотрит
    живой журнал установки.
    """
    script = HELPER_PATH if os.path.exists(HELPER_PATH) \
        else os.path.join(DEPLOY_DIR, HELPER_SRC)
    if (mode or job.sudo_mode) == "root":
        return ["bash", script, phase]
    return ["sudo", "-n", script, phase]


def helper_allowed() -> bool:
    """Разрешён ли помощник через sudoers (безвредная проверка: фаза `rights`).

    Проверяем именно установленный помощник /usr/local/sbin — правило
    перечисляет этот путь, сценарий из каталога программы sudo не пустит."""
    if os.geteuid() == 0 or not os.path.exists(HELPER_PATH):
        return False
    try:
        return subprocess.run(["sudo", "-n", HELPER_PATH, "rights"],
                              capture_output=True, timeout=20,
                              stdin=subprocess.DEVNULL).returncode == 0
    except Exception:                                    # noqa: BLE001
        return False


def one_time_cmd() -> str:
    """Одна команда для терминала: ставит помощник и sudoers-правило.

    Показываем в панели мастера (кнопка «Скопировать») и в тексте ошибки —
    после неё пароль не нужен ни разу."""
    return (f"sudo bash {os.path.join(DEPLOY_DIR, HELPER_SRC)} install-rule "
            f"{APP_DIR}")


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

# v1.70.30: AdGuard НАМЕРЕННО использует сменяемые домены авторизации в
# странах, где VPN блокируют. Дословный ответ разработчика AdGuard (sfionov) в
# AdguardTeam/AdGuardVPNCLI#188:
#
#   «AdGuard VPN can not use main domains in countries which may block VPNs
#    (they are either blocked, or will be blocked if used by AG VPN), so some
#    rotating ones are used. If you are in one of such countries, this is
#    expected behaviour.»
#
# На живом сервере владельца ссылка пришла с guidedeficit.blog, в самом #188 —
# с auctionassociation.space. Прежний белый список отбрасывал обе, то есть
# ломал ровно тех, кому VPN нужнее всего: панель не показывала ссылку вовсе, а
# мастер ждал подтверждения до таймаута. Подмена домена, от которой этот список
# должен был защищать, оказалась штатным механизмом AdGuard.
#
# Поэтому признаком ссылки служит её документированная ФОРМА (OAuth
# device-code), одинаковая у всех трёх независимых источников:
#   https://auth.adguard.io/device_code?user_code=ABCD-1234    (официальный вид)
#   https://auctionassociation.space/device_code?user_code=X   (#188)
#   https://guidedeficit.blog/device_code?user_code=NKHD-WXSR  (живой сервер)
DEVICE_CODE_MARKERS = ("device_code", "user_code")

ROTATING_DOMAIN_NOTE = (
    "Домен ссылки не входит в известный список AdGuard — это ШТАТНОЕ "
    "поведение: AdGuard намеренно использует сменяемые домены авторизации в "
    "странах, где VPN блокируют (ответ разработчика AdGuard в обращении "
    "AdguardTeam/AdGuardVPNCLI#188). Сверьте код в ссылке с кодом в строке "
    "выше и открывайте её.")


def is_adguard_domain(host: str) -> bool:
    """Известный домен AdGuard. v1.70.30: это СПРАВКА, а не фильтр —
    сменяемые домены авторизации в него не попадают и попадать не должны."""
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in ADGUARD_DOMAINS)


def login_url_host(url: str) -> str:
    """Домен ссылки входа (для пояснения владельцу, что он открывает)."""
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def extract_login_url(line: str) -> str:
    """Ссылка подтверждения входа из строки вывода CLI (OAuth device-code).

    Официальный вид: `https://auth.adguard.io/device_code?user_code=XXXX`,
    но домен может быть любым сменяемым (см. DEVICE_CODE_MARKERS) — в
    блокирующих VPN странах AdGuard основные домены не использует.

    v1.70.30: домен больше НЕ фильтр. Фильтр — форма ссылки: схема http(s),
    наличие хоста и маркера device-code. Это отсекает случайные URL из
    журнала (например, ссылку на инструкцию в выводе установщика), но
    пропускает настоящую ссылку входа с любого домена AdGuard.
    """
    from urllib.parse import urlsplit
    m = LOGIN_URL_RE.search(line or "")
    if not m:
        return ""
    u = m.group(0)
    try:
        sp = urlsplit(u)
    except ValueError:
        return ""
    if sp.scheme not in ("http", "https") or not sp.hostname:
        return ""
    low = u.lower()
    if not any(k in low for k in DEVICE_CODE_MARKERS):
        return ""           # случайный URL в выводе — это не ссылка входа
    return u


def _login_argv(cli: str) -> list[str]:
    """Аргументы `login` с учётом режима прав (см. run_cmd/run_helper)."""
    if job.sudo_mode == "helper":
        return helper_argv("login")          # sudo -n <помощник> login
    if job.sudo_mode == "nopass":
        return ["sudo", "-n", cli, "login"]
    return [cli, "login"]


def _kill_proc(proc) -> None:
    """v1.70.21: убивать ВСЮ группу процессов, а не одного прямого потомка.

    `login` идёт как `sudo -n <помощник> login`, а помощник делает
    `exec adguardvpn-cli login`; `proc.kill()` снимал только sudo, и
    привилегированный CLI оставался висеть сиротой, продолжая ждать
    подтверждения входа (проверено: внук жив после kill и мёртв после killpg).
    Процесс стартовал с start_new_session=True, поэтому группа — это он сам.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


# v1.70.28: `adguardvpn-cli login` — ИНТЕРАКТИВНАЯ команда. По документации
# AdGuardVPNCLI (раздел «Log in and log out») она показывает меню и ждёт выбора:
#
#     b - Open link in browser
#     s - Speed up check
#     x - Cancel
#
# Мастер запускал CLI с stdin=/dev/null — меню получало EOF, и ссылка
# подтверждения не появлялась вовсе. В SSH-терминале (как в v1.70.13) владелец
# нажимал `b` руками, поэтому там всё работало. Теперь команде даётся
# настоящий псевдотерминал и мастер отвечает сам.
# v1.70.30: порядок ответов исправлен по живому выводу CLI. Настоящий
# `adguardvpn-cli login` печатает ссылку ДО меню:
#
#   You need to authorize in your browser. The following link will be
#   available for 1799 seconds: https://…/device_code?user_code=…
#   b - Open link in browser
#   s - Speed up check
#   x - Cancel
#
# Поэтому отвечать надо независимо от того, увидели мы ссылку или нет, а «s»
# («speed up check») уместно повторять, пока владелец подтверждает вход в
# браузере — ровно для этого он и описан в документации.
LOGIN_ANSWERS = ((0.7, "b"), (8.0, "s"), (20.0, "s"), (45.0, "s"))
LOGIN_ANSWER_HINT = ("CLI завершился без ссылки входа. Если VPN не поднимется "
                     "на следующем шаге, выполните в SSH: sudo adguardvpn-cli "
                     "login и нажмите «b» — ссылка появится в терминале.")
AUTH_LINK_ERROR = "failed to get auth link"
# v1.70.29: CLI на уже выполненном входе отвечает своим текстом и может
# вернуть ненулевой код. Это НЕ ошибка: аккаунт привязан, ссылка не нужна.
# Падать здесь — значит заблокировать установку на сервере, где вход уже
# выполнен (ровно так и вышло у владельца: «You are already logged in …
# Current user is …», а мастер не дал бы продолжить).
#
# Маркер ровно один и он снят с живого вывода CLI, а не придуман: другие
# формулировки добавлять нельзя, пока они не подтверждены реальным ответом —
# иначе мастер начнёт принимать за «вход выполнен» то, чего CLI не говорил.
ALREADY_LOGGED_IN = ("already logged in",)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>]")


def _clean_tty(text: str) -> str:
    """Вывод псевдотерминала: снять ANSI-эскейпы и возвраты каретки."""
    return ANSI_RE.sub("", text or "").replace("\r", "")


def _login_worker(cli: str) -> tuple[int, str, str]:
    """`login` под псевдотерминалом: строки идут в живой журнал, ссылка — в
    `job.login_url`. Возвращает (код, вывод, ссылка).

    v1.70.28 — корень проблемы «ссылка не пришла»: команде нужен терминал и
    ответ на меню (см. LOGIN_ANSWERS). Даём pty с окном 80×24 и выключенным
    эхом ввода, отвечаем `b` (документированный выбор), затем `s`
    («speed up check») и повторно `b`, если ссылка ещё не пришла. Это в
    точности воспроизводит сеанс SSH, в котором вход работал.

    Если pty недоступен, остаётся запасной путь: канал, в который «b\n»
    записывается сразу (всё равно лучше, чем /dev/null).
    """
    out: list[str] = []
    url = ""
    lock = threading.Lock()

    def _feed(line: str) -> None:
        nonlocal url
        line = _clean_tty(line).strip()
        if not line:
            return
        with lock:
            out.append(line)
        job.say("login", line)
        if not url:
            u = extract_login_url(line)
            if u:
                url = u
                job.login_url = u
                job.say("login",
                        "Ссылка для регистрации сервера: " + u +
                        " — откройте её и войдите в аккаунт AdGuard; "
                        "установка продолжится сама.")
                # v1.70.30: сменяемый домен пугает владельца и выглядит как
                # подделка. Поясняем сразу, что это норма, и просим сверить
                # код — иначе ссылку не откроют, и вход сорвётся по таймауту.
                if not is_adguard_domain(login_url_host(u)):
                    job.say("login", ROTATING_DOMAIN_NOTE)

    argv = _login_argv(cli)
    stop = threading.Event()
    tail = [""]
    master = slave = -1
    try:
        import pty
        master, slave = pty.openpty()
    except Exception:                                        # noqa: BLE001
        master = slave = -1

    if master >= 0:
        try:
            import fcntl
            import struct
            import termios
            # размер окна обязателен: без него CLI может не отрисовать меню
            fcntl.ioctl(master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, 80, 0, 0))
            attrs = termios.tcgetattr(master)
            attrs[3] = attrs[3] & ~termios.ECHO   # наш ввод не попадает в журнал
            termios.tcsetattr(master, termios.TCSANOW, attrs)
        except Exception:                                    # noqa: BLE001
            pass
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, close_fds=True)
        os.close(slave)
        slave = -1

        def _reader() -> None:
            import select
            while not stop.is_set():
                try:
                    r, _, _ = select.select([master], [], [], 0.4)
                except (OSError, ValueError):
                    return
                if not r:
                    continue
                try:
                    data = os.read(master, 4096)
                except OSError:
                    return                 # EIO — потомок закрыл терминал
                if not data:
                    return
                tail[0] += _clean_tty(data.decode("utf-8", "replace"))
                while "\n" in tail[0]:
                    line, tail[0] = tail[0].split("\n", 1)
                    _feed(line)

        def _answer() -> None:
            # v1.70.30: выход по «url уже есть» был ошибкой. Ссылка появляется
            # РАНЬШЕ меню, поэтому такой выход оставлял меню без ответа: CLI
            # висел в ожидании выбора, а мастер ждал его до LOGIN_TIMEOUT
            # (15 минут) и сообщал об отказе входа на полностью рабочем
            # сервере. Прерываем ответы только по отмене или по завершению
            # процесса.
            for delay, key in LOGIN_ANSWERS:
                time.sleep(delay)
                if stop.is_set() or proc.poll() is not None:
                    return
                try:
                    os.write(master, (key + "\r").encode())
                except OSError:
                    return

        reader = threading.Thread(target=_reader, daemon=True)
        answer = threading.Thread(target=_answer, daemon=True)
        reader.start()
        answer.start()
    else:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", start_new_session=True)
        try:
            assert proc.stdin is not None
            proc.stdin.write("b\n")
            proc.stdin.flush()
        except (OSError, ValueError, AssertionError):
            pass

        def _pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                _feed(line)

        reader = threading.Thread(target=_pump, daemon=True)
        reader.start()

    deadline = time.time() + LOGIN_TIMEOUT
    try:
        while proc.poll() is None:
            if job.cancel_requested:
                stop.set()
                _kill_proc(proc)                # v1.70.21: вся группа
                return 130, "\n".join(out), url
            if time.time() > deadline:
                stop.set()
                _kill_proc(proc)
                return 124, "\n".join(out), url
            time.sleep(0.3)
    finally:
        stop.set()
        if proc.poll() is None:                 # отмена/зависание
            _kill_proc(proc)
        try:
            reader.join(timeout=3)
        except Exception:                                        # noqa: BLE001
            pass
        if master >= 0:
            if tail[0].strip():
                _feed(tail[0])
            try:
                os.close(master)
            except OSError:
                pass
    return proc.returncode or 0, "\n".join(out), url


# ==========================================================================
#  Шаги установки
# ==========================================================================
def _detect_rights() -> tuple[str, str]:
    """Режим прав и человекочитаемая подпись (v1.70.16 — без пароля вовсе).

    Порядок: root → sudo без пароля → помощник (sudoers-правило).
    Возвращает ("", "") если привилегий нет: мастер честно остановится
    и покажет одну команду для терминала."""
    if os.geteuid() == 0:
        return "root", "Права: root — хорошо."
    try:
        if subprocess.run(["sudo", "-n", "true"], capture_output=True,
                          timeout=15,
                          stdin=subprocess.DEVNULL).returncode == 0:
            return "nopass", "Права: sudo без пароля — хорошо."
    except Exception:                                        # noqa: BLE001
        pass
    if helper_allowed():
        return "helper", ("Права: помощник ymaster-adguard-setup "
                          "(sudoers-правило) — пароль не нужен.")
    return "", ""


NO_RIGHTS_ERROR = (
    "Программа работает без прав root, а правило для помощника не "
    "установлено — пароль мастер больше не спрашивает. Выполните на "
    "сервере ОДНУ команду (готовая — кнопкой «Скопировать» в панели, "
    "её же ставит deploy.sh при обновлении) и нажмите «Проверить снова»:")


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
    # v1.70.27: /dev/net/tun — ПРЕДУПРЕЖДЕНИЕ в журнале, а не отказ. Узел
    # устройства может появиться позже (модуль tun грузится по требованию), а
    # авторитетный вердикт выносит сам CLI: если TUN невозможен, `config
    # set-mode TUN` и `connect` упадут со своим сообщением. Блокировать
    # установку из-за собственной догадки — значит мешать запуску VPN.
    if not os.path.exists("/dev/net/tun"):
        job.say("precheck", "Внимание: /dev/net/tun не найден — если TUN-режим "
                            "не включится, причина в этом (модуль: modprobe "
                            "tun). Продолжаю: вердикт вынесет сам CLI.")
    mode, note = _detect_rights()
    if not mode:
        job.one_time_cmd = one_time_cmd()
        raise RuntimeError(NO_RIGHTS_ERROR + " " + job.one_time_cmd)
    job.sudo_mode = mode
    job.say("precheck", note)
    if mode == "helper":
        rc, out = run_helper("preflight", timeout=60)
        if rc != 0:
            raise RuntimeError("Помощник не готов к установке: "
                               + (out or "").strip()[-200:])
        job.say("precheck", "Помощник проверил сервер: "
                            + (out or "").strip().splitlines()[-1][:80])
    job.say("precheck", "Сервер готов к установке — пароль не потребуется.",
            "done", 8)


def preflight() -> dict:
    """Готовность прав ДО старта (v1.70.16): пароль не нужен никогда.

    UI решает: запускать мастер сразу либо показать одну команду для
    терминала (one_time_cmd) и кнопку «Проверить снова»."""
    root = os.geteuid() == 0
    nopass = False
    if not root:
        try:
            nopass = subprocess.run(["sudo", "-n", "true"],
                                    capture_output=True, timeout=15,
                                    stdin=subprocess.DEVNULL).returncode == 0
        except Exception:                                # noqa: BLE001
            nopass = False
    helper = (not root and not nopass) and helper_allowed()
    mode = "root" if root else ("nopass" if nopass
                                else ("helper" if helper else "none"))
    ready = mode in RIGHTS_MODES
    cmd = "" if ready else one_time_cmd()
    out = {"root": root, "sudo_nopass": nopass, "helper_ready": helper,
           "helper_path": HELPER_PATH, "sudoers_rule": HELPER_RULE,
           "helper_installed": os.path.exists(HELPER_PATH),
           "mode": mode, "ready": ready,
           "need_password": False,          # v1.70.16: пароля нет вовсе
           "one_time_cmd": cmd}
    if ready:
        out["hint"] = ("Мастер работает без пароля: привилегированные шаги "
                       "делает помощник ymaster-adguard-setup (режим «"
                       + mode + "»).")
    else:
        out["hint"] = (NO_RIGHTS_ERROR + " " + cmd)
    return out


def _systemctl_state(unit: str) -> str:
    """Состояние юнита БЕЗ root: active|inactive|failed|unknown.

    `systemctl is-active` доступен любому пользователю, поэтому помощник и
    sudo здесь не нужны — состояние кнопки-переключателя должно читаться
    всегда, в том числе когда правило помощника ещё не установлено.
    """
    try:
        p = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = (p.stdout or "").strip()
    # rc у is-active ненулевой для всего, кроме active — важен именно текст
    return out or "unknown"


def vpn_status() -> dict:
    """v1.70.25: включён ли VPN сейчас — для цвета кнопки-переключателя.

    `known=False` означает «systemd недоступен» (контейнер/песочница):
    кнопка в этом случае не утверждает, что VPN выключен, а честно говорит,
    что состояние неизвестно. Врать владельцу цветом нельзя.
    """
    st = _systemctl_state(TUNNEL_UNIT)
    return {"on": st == "active", "state": st, "unit": TUNNEL_UNIT,
            "known": st != "unknown"}


def turn_off() -> dict:
    """v1.70.25: выключить VPN одной кнопкой (фаза `off` помощника).

    CLI и вход в аккаунт НЕ удаляются — повторное включение идёт без ссылки
    подтверждения. Помощник гасит СНАЧАЛА автопилот и только потом туннель:
    иначе tg-health.sh в 15:10 увидел бы мёртвый путь и сам выполнил
    `connect -l`, то есть включил бы VPN обратно за спиной владельца.
    """
    if job.running:
        return {"ok": False,
                "error": "Сейчас идёт установка — сначала остановите её "
                         "(кнопка «Остановить» в панели)."}
    mode, _note = _detect_rights()
    if not mode:
        cmd = one_time_cmd()
        return {"ok": False, "need_rule": True, "one_time_cmd": cmd,
                "error": NO_RIGHTS_ERROR + " " + cmd}
    rc, out = run_helper("off", timeout=120, mode=mode)
    if rc != 0:
        return {"ok": False, "error": "Не выключить VPN: " + _reason(out)}
    return {"ok": True, "vpn": vpn_status(),
            "message": "VPN выключен: туннель остановлен, автопилот отключён. "
                       "Вход и лицензия сохранены — повторное включение "
                       "без ссылки."}


def sys_platform_linux() -> bool:
    return os.uname().sysname.lower().startswith("linux")


def _ensure_cli_link() -> None:
    """Ссылка /usr/local/bin/adguardvpn-cli обязана существовать (v1.70.23).

    На этот путь жёстко прописаны `ExecStart=` юнита adguard-vpn-telegram и
    `CLI=` в tg-health.sh, а официальный установщик ссылку не создаёт, если:
      • нет терминала для вопроса «create a link in /usr/local/bin? [y/N]»
        (у службы tty нет → ответ пустой → создаётся `.nosymlink`, который
        отключает вопрос и при всех последующих запусках);
      • /usr/local/bin не входит в PATH — тогда create_symlink() молча
        делает return 0 (у юнита программы PATH короткий).
    Поэтому обеспечиваем ссылку сами и ВСЕГДА, когда её нет, а бинарник в
    /opt есть — в том числе на пути «CLI уже установлен», где раньше происходил
    ранний выход и установка ломалась на следующем шаге.
    """
    if os.path.exists(CLI_LINK) or not os.path.exists(CLI_REAL):
        return
    job.say("cli", "Создаю ссылку /usr/local/bin/adguardvpn-cli "
                   "(её требуют служба туннеля и автопилот).")
    rc, out = run_cmd(LINK_CMD, shell=True)
    if rc != 0:
        raise RuntimeError("Не создать ссылку " + CLI_LINK + ": "
                           + _reason(out))


def _step_cli() -> None:
    job.say("cli", "Проверяем AdGuard VPN CLI…", "active", 12)
    if job.sudo_mode == "helper":
        # v1.70.16: всё привилегированное — через помощника (одна команда)
        rc, out = run_helper("version", timeout=30)
        if rc == 0 and (out or "").strip():
            job.cli_version = out.strip().splitlines()[0][:40]
            job.say("cli", f"CLI уже установлен ({job.cli_version}) — "
                           "пропускаю установку.", "skip", 20)
            return
        job.say("cli", "Ставим официальным установщиком (1–2 минуты)…")
        rc, out = run_helper("install", timeout=CMD_TIMEOUT)
        if rc != 0:
            raise RuntimeError("Установщик AdGuard завершился с ошибкой: "
                               + (out or "").strip()[-300:])
        rc, out = run_helper("version", timeout=30)
        if rc != 0:
            raise RuntimeError("CLI установлен, но не запускается: "
                               + (out or "").strip()[-200:])
        job.cli_version = out.strip().splitlines()[0][:40]
        job.say("cli", f"CLI готов ({job.cli_version}).", "done", 25)
        return
    cli = _find_cli()
    if cli:
        rc, out = run_cmd([cli, "--version"], timeout=30)
        if rc == 0:
            # v1.70.23: _find_cli() находит и /opt/adguardvpn_cli/adguardvpn-cli,
            # поэтому проверка версии проходила и шаг завершался РАНЬШЕ, чем
            # создавалась ссылка /usr/local/bin — а без неё следующий шаг
            # (юнит туннеля с ExecStart=/usr/local/bin/…) падал.
            _ensure_cli_link()
            job.cli_version = (out or "").strip().splitlines()[0][:40] \
                if out.strip() else "установлен"
            job.say("cli", f"CLI уже установлен ({job.cli_version}) — "
                           "пропускаю установку.", "skip", 20)
            return
    job.say("cli", "Ставим официальным установщиком (1–2 минуты)…")
    rc, install_out = run_cmd(INSTALL_CMD, shell=True)
    if rc != 0:
        raise RuntimeError("Установщик AdGuard завершился с ошибкой: "
                           + _reason(install_out))
    _ensure_cli_link()
    cli = _find_cli()
    rc, out = run_cmd([cli, "--version"], timeout=30) if cli else (1, "")
    if rc != 0:
        # известный случай: вопрос установщика про ссылку остался без ответа
        if os.path.exists(CLI_REAL):
            job.say("cli", "Создаю ссылку /usr/local/bin (установщик не успел).")
            run_cmd(LINK_CMD, shell=True)
            cli = _find_cli()
            rc, out = run_cmd([cli, "--version"], timeout=30) if cli else (1, "")
    if rc != 0:
        # v1.70.22: причина обязательна и точна. Раньше здесь печаталось
        # «CLI установлен, но не запускается: » с ПУСТЫМ хвостом: вывод
        # установщика затирался результатом --version, а самого CLI могло и не
        # быть. Владелец оставался без единой зацепки.
        if not cli:
            raise RuntimeError("CLI не появился после установки: "
                               + _reason(install_out))
        raise RuntimeError(f"CLI установлен, но не запускается ({cli}): "
                           + _reason(out or install_out))
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
    low = (out or "").lower()
    already = any(m in low for m in ALREADY_LOGGED_IN)

    # Порядок ветвей принципиален: сначала «вход уже выполнен» (собственный
    # текст CLI важнее его кода возврата), потом отказы, потом успех по ссылке.
    if already:
        # v1.70.29: на сервере с привязанным аккаунтом CLI отвечает «You are
        # already logged in … Current user is …» и может вернуть ненулевой код.
        # Это не ошибка: ссылка не нужна, можно сразу поднимать туннель. Падать
        # здесь — значит заблокировать установку ровно там, где всё уже готово
        # (так и вышло на живом сервере владельца). Направление безопасное:
        # текст используется, чтобы НЕ падать, а не чтобы объявить успех.
        job.say("login", "Вход уже выполнен — аккаунт AdGuard привязан, "
                         "ссылка не нужна. Продолжаю.", "skip", 45)
    elif rc != 0:
        tail = (out or "").strip()[-300:]
        # v1.70.28: «Failed to get auth link» — болезнь самого CLI, а не наша.
        # AdguardTeam/AdGuardVPNCLI#142 (исправлен в VPN Client v0.19.35) и
        # открытый #169 на Ubuntu 24.04 с CLI 1.7.12: причина в недоступных
        # корневых сертификатах, и лечится одной командой на сервере. Без этой
        # подсказки владелец видит «вход не завершён» и не понимает, что делать.
        if AUTH_LINK_ERROR in low:
            raise RuntimeError(
                "CLI не получил ссылку входа («Failed to get auth link»). Это "
                "дефект самого AdGuard VPN CLI: #142 (исправлен в VPN Client "
                "v0.19.35) и открытый #169 на Ubuntu 24.04 с CLI 1.7.12 — "
                "причина в недоступных корневых сертификатах. Выполните на "
                "сервере: sudo apt install -y ca-certificates && sudo "
                "update-ca-certificates, затем повторите включение. Вывод CLI: "
                + (tail or "пуст"))
        raise RuntimeError("Вход не завершён (код " + str(rc) + "): "
                           + (tail or "ссылка не подтверждена или истекла — "
                                      "запустите установку ещё раз."))
    elif url:
        job.say("login", "Вход подтверждён ✔", "done", 45)
    elif "logged in" in low or "authorized" in low:
        # v1.70.28: признак успеха берётся из документированного текста CLI
        # («Successfully logged in»), а не из факта «ссылки не было».
        job.say("login", "Вход уже выполнен — подтверждение не нужно.",
                "skip", 45)
    else:
        # v1.70.28: ссылки не было и признаков входа нет — честно говорим об
        # этом и даём запасной путь (ручной вход в SSH, как в v1.70.13).
        job.say("login", LOGIN_ANSWER_HINT, "skip")


def _step_tun(db) -> None:
    """v1.70.24: TUN + selective — через VPN идёт только Telegram.

    Шаг исторически назывался «socks» (режим SOCKS5, локальный прокси
    127.0.0.1:1080). От него отказались по двум причинам:

      • httpx/socksio не поддерживают socks5h — имя api.telegram.org
        резолвилось ЛОКАЛЬНЫМ DNS сервера. При отравлении DNS бот получал
        подменённый адрес и не подключался, хотя сам прокси был жив;
      • в TUN-режиме DNS уходит в туннель, а selective-исключения оставляют
        весь прочий трафик сервера прямым — то есть цель «VPN только для
        Telegram» достигается штатным механизмом AdGuard.

    Самодельная маршрутизация по CIDR (режим SCRIPT) не используется:
    AdguardTeam/AdGuardVPNCLI#69 и #190 — «Unknown error», не подключается.

    v1.70.25: шаг поглотил прежний отдельный шаг «proxy». В TUN-режиме прокси
    как понятие не применяется (трафик уходит в туннель прозрачно), поэтому
    отдельный шаг «Настройка программы (прокси не нужен — убираем» только
    путал владельца. Очистка поля осталась — она обязательна: оставленное
    socks5://127.0.0.1:1080 отправило бы бота в порт, который больше никто
    не слушает.
    """
    job.say("tun", "Включаем TUN и правило «через VPN только Telegram»…",
            "active", 50)
    # run_helper сам выбирает обёртку по режиму прав (root — bash, nopass и
    # helper — sudo -n), поэтому ветвление по sudo_mode здесь не нужно.
    rc, out = run_helper("tun", timeout=120)
    if rc != 0:
        raise RuntimeError("Не включить режим TUN с selective-исключениями: "
                           + _reason(out))
    tail = (out or "").strip().splitlines()
    job.say("tun", (tail[-1][:90] if tail else
                    "TUN + selective: через VPN идёт только Telegram."))

    # Прокси в TUN-режиме не нужен — поле обязано быть пустым.
    from . import appsettings
    before = appsettings.get_setting(db, SETT_PROXY, "")
    appsettings.set_setting(db, SETT_PROXY, PROXY_VALUE)
    if before:
        job.say("tun", f"Прежний прокси ({before}) убран из карточки: "
                       "в TUN трафик идёт прозрачно.")
    # env TELEGRAM_PROXY переопределяет пустую настройку (get_proxy) — если он
    # задан, честнее сказать об этом сразу, чем искать «почему бот молчит».
    from ..config import settings
    if settings.TELEGRAM_PROXY:
        job.say("tun", "Внимание: в окружении задан TELEGRAM_PROXY="
                       f"{settings.TELEGRAM_PROXY} — он переопределит пустое "
                       "поле; уберите его из .env для TUN-режима.")
    job.say("tun", "Режим TUN включён: через VPN идёт только Telegram.",
            "done", 58)


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
        if job.sudo_mode == "root":
            shutil.copyfile(src, dst)
            os.chmod(dst, mode)
        else:
            # v1.70.21: коды возврата больше не игнорируются. Раньше отказ cp
            # всплывал позже как «Служба туннеля не запустилась» — владелец
            # искал причину не там.
            rc, out = run_cmd(["cp", src, dst], timeout=30)
            if rc != 0:
                raise RuntimeError(f"Не скопировать {name} в {dst}: "
                                   + (out or "").strip()[-200:])
            rc, out = run_cmd(["chmod", f"{mode:o}", dst], timeout=15)
            if rc != 0:
                raise RuntimeError(f"Не выставить права {mode:o} на {dst}: "
                                   + (out or "").strip()[-200:])


def _step_tunnel() -> None:
    job.say("tunnel", "Ставим службу туннеля (автоподключение после "
                      "перезагрузки)…", "active", 60)
    if job.sudo_mode == "helper":
        # v1.70.16: помощник сам раскладывает комплект (он уже root-владелец
        # копии в /usr/local/share/ymaster-adguard), делает daemon-reload,
        # enable+restart и ждёт активного туннеля до 20 с.
        rc, out = run_helper("units", timeout=180)
        if rc != 0:
            raise RuntimeError("Служба туннеля не запустилась: "
                               + (out or "").strip()[-300:])
        job.say("tunnel", (out or "").strip().splitlines()[-1][:80]
                if out.strip() else "Туннель подключён.")
        job.say("tunnel", "Туннель подключён, служба активна "
                          "(переживает перезагрузку).", "done", 68)
        return
    _copy_kit()
    rc, out = run_cmd(["systemctl", "daemon-reload"], timeout=30)
    if rc != 0:
        raise RuntimeError("daemon-reload не удался: " + (out or "")[-200:])
    run_cmd(["systemctl", "enable", TUNNEL_UNIT], timeout=30)
    rc, out = run_cmd(["systemctl", "restart", TUNNEL_UNIT],
                      timeout=60)
    if rc != 0:
        raise RuntimeError("Служба туннеля не запустилась: "
                           + (out or "").strip()[-300:])
    # v1.70.27: до 60 с вместо 20 — первое подключение в TUN-режиме на
    # медленном канале в 20 с не укладывается (как и в помощнике).
    for _ in range(30):
        rc, _ = run_cmd(["systemctl", "is-active", TUNNEL_UNIT],
                        timeout=15)
        if rc == 0:
            break
        time.sleep(2)
    if rc != 0:
        raise RuntimeError("Туннель не поднялся за 60 с — смотрите "
                           "journalctl -u " + TUNNEL_UNIT + " (частая "
                           "причина: нет активной лицензии AdGuard).")
    job.say("tunnel", "Туннель подключён, служба активна "
                      "(переживает перезагрузку).", "done", 68)


def _step_autopilot() -> None:
    job.say("autopilot", "Ставим автопилот (замер 15:00, смена локации "
                         "15:10)…", "active", 74)
    if job.sudo_mode == "helper":
        rc, out = run_helper("autopilot", timeout=90)
        if rc != 0:
            raise RuntimeError("Таймер автопилота не включился: "
                               + (out or "").strip()[-200:])
        job.say("autopilot", "Автопилот включён: каждый день 15:10 по "
                             "серверному времени (= МСК).", "done", 80)
        return
    rc, out = run_cmd(["systemctl", "daemon-reload"], timeout=30)
    if rc != 0:
        raise RuntimeError("daemon-reload не удался: " + (out or "")[-200:])
    # v1.70.26: одним вызовом. `enable --now` и ставит автозапуск, и
    # запускает таймер, поэтому стоявший перед ним отдельный `enable` был
    # лишним повтором той же работы.
    rc, out = run_cmd(["systemctl", "enable", "--now", TIMER_UNIT],
                      timeout=30)
    if rc != 0:
        raise RuntimeError("Таймер автопилота не включился: "
                           + (out or "").strip()[-200:])
    job.say("autopilot", "Автопилот включён: каждый день 15:10 по серверному "
                         "времени (= МСК).", "done", 80)


def _step_verify(db) -> None:
    job.say("verify", "Проверяем путь: 3 коротких запроса до Telegram…",
            "active", 93)
    from . import telegram_bot as tg
    r = tg.ping_proxy(db, samples=3, scheduled=False)
    if not r.get("ok"):
        raise RuntimeError("Путь до Telegram не отвечает через туннель — "
                           "проверьте вход/лицензию AdGuard (`adguardvpn-cli "
                           "license`), что режим исключений selective "
                           "(`site-exclusions mode`), а api.telegram.org в "
                           "списке (`site-exclusions show`); локацию подберёт "
                           "автопилот в 15:10 или кнопка «📏 Пинг».")
    job.ping_ms = r.get("ms")
    job.say("verify", f"Путь жив: средний пинг {r.get('ms')} мс "
                      "(замерен напрямую — в TUN это и есть путь через "
                      "туннель).", "done", 97)


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
    _step_tun(db)
    _step_tunnel()
    _step_autopilot()
    _step_verify(db)
    job.say("done", "Готово: VPN включён, путь до Telegram работает, пинг "
                    f"{job.ping_ms} мс. Кнопка «🛡 AdGuard VPN» горит зелёным; "
                    "повторное нажатие выключит VPN.", "done", 100)
    job.finish(success=True)
