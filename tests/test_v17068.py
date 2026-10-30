"""v1.70.68 — отказоустойчивый доступ к Telegram API через Cloudflare Worker.

Задача владельца (дословно, существенная часть): «возвращаемся к блоку и
разработке телеграмма уведомлений и регистрация как у макса» плюс готовое
ТЗ «Внедрение отказоустойчивого доступа к Telegram API в проект chek
ymaster.ru»: «категорически запрещено использовать второй зарубежный
сервер единственный допустимый внешний посредник cloudflare workers
бесплатный тариф». Объём выпуска владелец подтвердил: только ТЗ по
прокси; Worker владелец разворачивает сам по подготовленной инструкции.

Ревизия: прямые запросы к api.telegram.org с московского сервера
блокируются — отправка уведомлений, long-poll и диагностика отказывают.
Поле прокси в программе есть с v1.29.0 (httpx передаёт его во все
вызовы), но готовой схемы для РФ не было: tg-bridge (v1.70.12) требует
вторую машину (теперь запрещена), AdGuard VPN (v1.70.24) тянет в туннель
весь трафик сервера. Готовые реверс-прокси воркеры без белого списка —
открытый прокси (бан Cloudflare) и токен бота виден посреднику.

Решение:
 1. Аудит шести репозиториев (четыре обязательных по ТЗ + два найдены
    поиском), выбор — winchanii/TgWsApiProxy: белый список ALLOWED_HOSTS
    в воркере, сквозной TLS (посредник не видит токен), нулевые
    зависимости, слушатель только 127.0.0.1. Отчёт:
    docs/telegram_proxy_audit_v17068.md.
 2. Комплект deploy/tg-cf-worker: вендоренный worker.js (MIT, без
    изменений кода), юнит systemd (Restart=always, усиление), install.sh
    (Ubuntu 24.04), check_proxy.sh (мониторинг цепочки), README.md
    (деплой, настройка, проверка трафика, обновление воркера).
 3. Интеграция без изменения клиентского кода: настройка прокси
    http://127.0.0.1:8080; подсказка настроек Telegram ведёт к комплекту.

Что здесь проверяется:
 1. Замки комплекта: белый список и диагностика воркера, юнит, установщик,
    скрипт проверки, инструкция, отсутствие постороннего письма.
 2. Отчёт аудита: все кандидаты, ключевые слова, выбор, стек, ТЗ §6.
 3. Интеграция: подсказка, TELEGRAM_PROXY/TELEGRAM_API_BASE, get_proxy,
    передача прокси в httpx.
 4. Ритуал выпуска 1.70.68 и историзация TestRelease17067.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
DEPLOY = ROOT / "deploy" / "tg-cf-worker"
WORKER_JS = DEPLOY / "worker.js"
UNIT = DEPLOY / "tgwsapiproxy.service"
INSTALL = DEPLOY / "install.sh"
CHECK = DEPLOY / "check_proxy.sh"
KIT_README = DEPLOY / "README.md"
REPORT = ROOT / "docs" / "telegram_proxy_audit_v17068.md"
SETTINGS_ROUTES = ROOT / "app" / "routers" / "settings_routes.py"
TG_BOT = ROOT / "app" / "services" / "telegram_bot.py"
CONFIG = APP / "config.py"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_67_TESTS = ROOT / "tests" / "test_v17067.py"

OWNER_QUOTE_A = ("возвращаемся к блоку и разработке телеграмма "
                 "уведомлений и регистрация как у макса")
OWNER_QUOTE_B = ("категорически запрещено использовать второй зарубежный "
                 "сервер единственный допустимый внешний посредник "
                 "cloudflare workers бесплатный тариф")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestCfWorkerKit:
    """Комплект deploy/tg-cf-worker — конфиски ТЗ §7.2."""

    def test_worker_vendored_with_whitelist(self):
        js = _read(WORKER_JS)
        # шапка: источник, лицензия и предупреждение не убирать список
        assert "github.com/winchanii/TgWsApiProxy" in js
        assert "MIT" in js
        assert "НЕ УБИРАТЬ" in js
        # белый список апстрима не вырезан (ТЗ §6)
        assert "const ALLOWED_HOSTS" in js
        assert '"api.telegram.org"' in js
        assert '"149.154.166.110"' in js
        assert "const ALLOWED_PORTS = new Set([443]);" in js
        assert 'import { connect } from "cloudflare:sockets";' in js
        # эндпоинты ретранслятора и диагностики
        for ep in ("/apiws", "/diag", "/healthz"):
            assert ep in js

    def test_unit_restarts_and_listens_locally(self):
        unit = _read(UNIT)
        assert "Restart=always" in unit
        assert "RestartSec=3" in unit
        assert "--listen-host 127.0.0.1" in unit
        assert "--listen-port 1080" in unit
        assert "--http-port 8080" in unit
        assert "__WORKER_HOST__" in unit
        assert "User=tgproxy" in unit
        for hardening in ("NoNewPrivileges=yes", "ProtectSystem=strict",
                          "ProtectHome=yes", "PrivateTmp=yes"):
            assert hardening in unit
        assert "WantedBy=multi-user.target" in unit

    def test_install_script(self):
        sh = _read(INSTALL)
        assert sh.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in sh
        assert "python3 -m venv" in sh
        assert "git+https://github.com/winchanii/TgWsApiProxy" in sh
        assert "useradd --system" in sh
        assert "s/__WORKER_HOST__/" in sh
        assert "systemctl daemon-reload" in sh
        assert "enable --now tgwsapiproxy" in sh
        assert "http://127.0.0.1:8080" in sh

    def test_check_script(self):
        sh = _read(CHECK)
        assert sh.startswith("#!/usr/bin/env bash")
        assert "systemctl is-active" in sh
        assert "/healthz" in sh
        assert '"$PROXY"' in sh
        assert "(1080|8080)" in sh
        assert 'exit "$fail"' in sh

    def test_kit_readme_is_full_instruction(self):
        md = _read(KIT_README)
        assert "Workers & Pages" in md
        assert "wrangler deploy" in md
        assert "sudo ./install.sh" in md
        assert "http://127.0.0.1:8080" in md
        assert "TELEGRAM_PROXY" in md
        assert "ALLOWED_HOSTS" in md
        assert "## Как проверить, что трафик идёт через прокси" in md
        assert "## Как обновлять Worker" in md

    def test_scripts_are_executable(self):
        import os
        for path in (INSTALL, CHECK):
            assert os.access(path, os.X_OK), f"{path.name}: нет +x"

    def test_kit_has_no_foreign_scripts(self):
        # никакого CJK/хангыла/каны в комплекте — только кириллица,
        # латиница и типографика
        for path in sorted(DEPLOY.iterdir()):
            text = _read(path)
            for ch in text:
                o = ord(ch)
                assert not (0x3000 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF
                            or 0xF900 <= o <= 0xFAFF), \
                    f"{path.name}: постороннее письмо {ch!r}"


class TestAuditReport:
    """Отчёт-сравнение — результат задания № 1 ТЗ."""

    def test_report_names_all_candidates(self):
        md = _read(REPORT)
        for repo in ("winchanii/TgWsApiProxy", "4n0nymou3/Telegram-API-Proxy",
                     "kushagra-goyal-14/telegram-bot-cloudflare-proxy",
                     "tuanpb99/cf-worker-telegram", "hermes-telegram-proxy",
                     "buithucdong/cloudflare-worker-telegram"):
            assert repo in md, f"в отчёте нет {repo}"

    def test_report_records_search_and_choice(self):
        md = _read(REPORT)
        assert "tgwsapiproxy" in md
        assert "dpi bypass russia" in md
        assert "Выбран" in md
        assert "белого списка" in md or "белым списком" in md

    def test_report_records_stack_audit(self):
        md = _read(REPORT)
        assert "FastAPI" in md
        assert "httpx" in md
        assert "aiogram" in md          # упомянут как НЕ используемый
        assert "get_proxy" in md
        assert "telegram_bot.py" in md

    def test_report_maps_tz_security_requirements(self):
        md = _read(REPORT)
        for needle in ("ALLOWED_HOSTS", "Restart=always", "check_proxy.sh",
                       "journalctl"):
            assert needle in md

    def test_report_states_the_constraint(self):
        md = _read(REPORT)
        assert "второй зарубежный сервер" in md
        assert "запрещ" in md


class TestAppIntegration:
    """Интеграция — задание №3 ТЗ: настройка, а не переписывание клиента."""

    def test_settings_hint_points_to_kit(self):
        src = _read(SETTINGS_ROUTES)
        assert "deploy/tg-cf-worker" in src
        assert "http://127.0.0.1:8080" in src
        assert "docs/telegram_proxy_audit_v17068.md" in src

    def test_proxy_config_intact(self):
        src = _read(CONFIG)
        assert 'TELEGRAM_PROXY: str = _env("TELEGRAM_PROXY", "")' in src
        assert "TELEGRAM_API_BASE" in src
        assert "https://api.telegram.org" in src

    def test_bot_passes_proxy_to_every_call(self):
        src = _read(TG_BOT)
        assert "def get_proxy(db" in src
        assert "settings.TELEGRAM_PROXY" in src
        assert "proxy=proxy or None" in src
        assert "_proxy = get_proxy(db)" in src


class TestRelease17068:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.67) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.68"
    PREVIOUS = "1.70.67"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(CONFIG)
        assert f"ymaster-check-v{self.VERSION}" in _read(SW)
        assert f'"version": "{self.VERSION}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        sw = _read(SW)
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_changed_assets_are_served_with_the_release_version(self):
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index

    def test_no_previous_version_left_in_sync_points(self):
        for path in (CONFIG, INDEX, SW, MANIFEST, MAX_HTML, CABINET):
            text = _read(path)
            for form in (f"v={self.PREVIOUS}", f'"{self.PREVIOUS}"',
                         f"check-v{self.PREVIOUS}"):
                assert form not in text, f"{path.name}: {form}"

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self.VERSION, \
            "запись выпуска не первая"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("уведомл", "прокси", "worker", "список"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"MAX", "Telegram", "service", "worker"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        flat = section.lower()
        for needle in ("cloudflare", "tgwsapiproxy", "прокси",
                       "белый список", "кэш-бастер", "отклонено"):
            assert needle in flat, f"в CHANGELOG нет «{needle}»"
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section, f"в CHANGELOG нет «{head}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "прокси", "worker",
                       "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
                assert needle in flat, f"{path.name}: нет цитаты владельца"

    def test_previous_release_class_is_history_only(self):
        src = _read(RELEASE_67_TESTS)
        block = src.split("class TestRelease17067:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_67_TESTS)
        block = src.split("class TestRelease17067:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
