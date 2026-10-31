# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.50: клиенты приложения и передача данных.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерий приёмки (указание владельца): «У нас есть в приложении блок
# сервер и команды добавить сюда все клиенты с описаниями. И информацию
# по серверу если есть информация по передаче данных и прочего тоже
# добавить.»
#
# Решения владельца на уточняющие вопросы:
#   clients_meaning = both    — и справочник клиентских поверхностей, и
#                               живой список подключений;
#   data_transfer   = both    — и куда уходят данные, и сетевые сведения
#                               о сервере;
#   placement       = cards   — две новые отдельные карточки рядом
#                               в разделе «Данные», а не внутри прежней;
#   facts_source    = measure — живой сбор по таблицам, которые программа
#                               уже ведёт.
#
# Что закреплено:
#  1. справочник не содержит выдумки: каждый источник называет
#     существующий файл, каждая таблица-свидетель есть в моделях;
#  2. новый маршрут отдаёт справочник целиком и живые счётчики;
#  3. в ответе нет секретов — восемь запрещённых имён по нулям;
#  4. поток без журнала-свидетеля счётчиков не получает: карточка пишет,
#     что журнал не ведётся, вместо выдуманного числа;
#  5. доступ только администратору: не-админ и аноним отсечены;
#  6. прежний маршрут системной сводки не изменён — карточка «Сервер и
#     команды» продолжает работать как работала;
#  7. две карточки стоят в разделе «Данные» рядом с прежней, порядок 14
#     и 15, и грузятся одним запросом;
#  8. значения из ответа экранируются: текст справочника не стал
#     способом вставить разметку в страницу администратора;
#  9. ритуал выпуска: точки синхронизации, кэш-бастеры, «Что нового»,
#     CHANGELOG и журнал вёрстки.
# ======================================================================
from __future__ import annotations

import importlib
import pathlib
import re

from tests.conftest import login

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
DOCS = ROOT / "docs"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"

INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
APP_JS = STATIC / "js" / "app.js"

ADMIN_ROUTER = APP / "routers" / "admin.py"
CATALOG = APP / "services" / "server_catalog.py"

ROUTE = "/api/v1/admin/clients"
SYSTEM_ROUTE = "/api/v1/admin/system"

# Имена, которых в ответе быть не должно: секреты и содержимое журналов.
FORBIDDEN_KEYS = ("token_hash", "key_hash", "public_key", "request_data",
                  "response_data", "payload", "ua_hash", "password")

# Цитата требования владельца — дословно, как она прозвучала.
OWNER_QUOTE_HEAD = "блок сервер и команды добавить сюда все клиенты"
OWNER_QUOTE_TAIL = "информацию по серверу если есть информация по передаче данных"


def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _catalog():
    from app.services.server_catalog import (
        CLIENT_SURFACES,
        DATA_FLOWS,
        FLOW_LOG_TABLES,
        NETWORK_FACTS,
    )
    return CLIENT_SURFACES, DATA_FLOWS, NETWORK_FACTS, FLOW_LOG_TABLES


def _mk_user(client, role, username):
    """Регистрация пользователя по приглашению админа → заголовки."""
    hdr = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites", json={"role": role},
                      headers=hdr).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# ======================================================================
# 1. Справочник: только подтверждённые кодом факты
# ======================================================================
class TestCatalogIsGrounded:
    """Справочник живёт отдельно от маршрута и не имеет права содержать
    выдумку: у каждого факта назван модуль-источник, и он существует."""

    SURFACE_FIELDS = {"id", "icon", "name", "address", "audience",
                      "transport", "source", "text"}
    FLOW_FIELDS = {"id", "direction", "icon", "service", "hosts", "sends",
                   "protection", "log", "source"}
    FACT_FIELDS = {"id", "label", "value", "source"}

    def test_module_exists_and_is_importable(self):
        assert CATALOG.exists(), "модуль справочника не создан"
        mod = importlib.import_module("app.services.server_catalog")
        for name in ("CLIENT_SURFACES", "DATA_FLOWS", "NETWORK_FACTS",
                     "SURFACE_IDS", "FLOW_IDS", "FLOW_LOG_TABLES"):
            assert hasattr(mod, name), name

    def test_surfaces_are_complete(self):
        surfaces, _flows, _facts, _tables = _catalog()
        assert len(surfaces) == 8, "поверхностей должно быть восемь"
        for s in surfaces:
            assert self.SURFACE_FIELDS <= set(s), f"{s.get('id')}: неполный состав"
            for k in self.SURFACE_FIELDS - {"id"}:
                assert str(s[k]).strip(), f"{s['id']}: пусто поле {k}"

    def test_flows_are_complete(self):
        _surfaces, flows, _facts, _tables = _catalog()
        assert len(flows) == 11, "потоков должно быть одиннадцать"
        for f in flows:
            assert self.FLOW_FIELDS <= set(f), f"{f.get('id')}: неполный состав"
            for k in self.FLOW_FIELDS - {"id", "hosts", "log"}:
                assert str(f[k]).strip(), f"{f['id']}: пусто поле {k}"
            # «browser» — не направление потока сервера: в этих обращениях
            # сервер не участвует, и называть их «наружу» было бы неправдой
            assert f["direction"] in ("in", "out", "both", "browser"), f["id"]

    def test_network_facts_are_complete(self):
        _surfaces, _flows, facts, _tables = _catalog()
        assert len(facts) == 8, "сетевых фактов должно быть восемь"
        for f in facts:
            assert self.FACT_FIELDS <= set(f), f"{f.get('id')}: неполный состав"
            for k in self.FACT_FIELDS - {"id"}:
                assert str(f[k]).strip(), f"{f['id']}: пусто поле {k}"

    def test_ids_are_unique_and_match_the_tuples(self):
        surfaces, flows, facts, _tables = _catalog()
        from app.services.server_catalog import FLOW_IDS, SURFACE_IDS
        for items, ids in ((surfaces, SURFACE_IDS), (flows, FLOW_IDS)):
            got = [i["id"] for i in items]
            assert len(set(got)) == len(got), "повторяющийся идентификатор"
            assert tuple(got) == tuple(ids), "кортеж идентификаторов разошёлся"
        assert len({f["id"] for f in facts}) == len(facts)

    def test_every_source_names_a_real_file(self):
        """Маска или несуществующий путь означали бы выдуманный источник."""
        surfaces, flows, facts, _tables = _catalog()
        checked = 0
        for item in surfaces + flows + facts:
            for part in re.split(r"[,;]", item["source"]):
                part = re.sub(r"\([^)]*\)", "", part).strip()
                for tok in part.split():
                    if re.search(r"\.(py|js|html|css|sh|md|json|webmanifest)$",
                                 tok):
                        assert "*" not in tok and "?" not in tok, \
                            f"{item['id']}: маска вместо пути — {tok}"
                        assert (ROOT / tok).exists(), \
                            f"{item['id']}: нет файла {tok}"
                        checked += 1
        assert checked >= 40, f"слишком мало путей проверено: {checked}"

    def test_witness_tables_exist_in_models(self):
        """Поток получает счётчики только по таблице, которая существует."""
        _surfaces, _flows, _facts, tables = _catalog()
        real = set()
        for name in ("app.models", "app.pool.models", "app.b2b.models"):
            mod = importlib.import_module(name)
            for attr in dir(mod):
                t = getattr(getattr(mod, attr), "__tablename__", None)
                if t:
                    real.add(t)
        assert tables, "реестр таблиц-свидетелей пуст"
        for t in tables:
            assert t in real, f"таблицы {t} нет в моделях"

    def test_flow_log_is_a_known_witness(self):
        _surfaces, flows, _facts, tables = _catalog()
        for f in flows:
            assert f["log"] in ("", *tables), f"{f['id']}: чужой журнал {f['log']}"
        # хотя бы часть потоков измерима, иначе карточка была бы пустой
        assert sum(1 for f in flows if f["log"]) >= 4

    def test_hosts_are_bare_hostnames(self):
        """Адрес потока — это имя хоста: схема и путь жили бы в другом поле."""
        _surfaces, flows, _facts, _tables = _catalog()
        for f in flows:
            for h in f["hosts"]:
                assert "://" not in h, f"{f['id']}: схема в адресе — {h}"
                assert "/" not in h, f"{f['id']}: путь в адресе — {h}"
                assert " " not in h, f"{f['id']}: пробел в адресе — {h}"

    def test_catalog_prose_has_no_latin_1c(self):
        """Проект пишет «1С» кириллицей: латинская запись в прозе запрещена
        реестром отклонённых находок языкового аудита."""
        text = _read(CATALOG)
        assert "1C" not in text, "латинское 1C в справочнике"
        assert "1С" in text, "кириллическое 1С пропало из справочника"


# ======================================================================
# 2. Маршрут: справочник плюс живой сбор
# ======================================================================
class TestClientsRoute:
    def test_admin_gets_all_four_blocks(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get(ROUTE, headers=hdr)
        assert r.status_code == 200, r.text
        d = r.json()
        assert set(d) == {"surfaces", "connections", "network",
                          "network_facts", "flows"}, "состав ответа изменился"
        surfaces, flows, facts, _tables = _catalog()
        assert [s["id"] for s in d["surfaces"]] == [s["id"] for s in surfaces]
        assert [f["id"] for f in d["flows"]] == [f["id"] for f in flows]
        assert len(d["network_facts"]) == len(facts)

    def test_live_numbers_are_attached_to_surfaces(self, client):
        """Живой сбор добавлен к справочнику, а не подменён им: поля
        справочника приходят в ответ дословно, а рядом встают счётчики."""
        hdr = login(client, "admin", "admin123")
        d = client.get(ROUTE, headers=hdr).json()
        surfaces, _flows, _facts, _tables = _catalog()
        catalog = {s["id"]: s for s in surfaces}
        with_live = 0
        for s in d["surfaces"]:
            ref = catalog[s["id"]]
            for k in ("name", "address", "audience", "transport", "source",
                      "text"):
                assert s[k] == ref[k], f"{s['id']}: поле {k} изменено"
            assert isinstance(s["live"], list), f"{s['id']}: нет счётчиков"
            with_live += bool(s["live"])
        assert with_live >= 5, f"живых счётчиков слишком мало: {with_live}"

    def test_measured_flag_follows_the_witness_table(self, client):
        hdr = login(client, "admin", "admin123")
        d = client.get(ROUTE, headers=hdr).json()
        _surfaces, flows, _facts, _tables = _catalog()
        catalog = {f["id"]: f for f in flows}
        for f in d["flows"]:
            if catalog[f["id"]]["log"]:
                assert f["measured"] is True, f"{f['id']}: журнал есть, чисел нет"
                assert f["stats"], f"{f['id']}: пустые счётчики"
            else:
                assert f["measured"] is False, f"{f['id']}: числа без журнала"
                assert f["stats"] == [], f"{f['id']}: выдуманные счётчики"

    def test_metrics_are_label_and_value_pairs(self, client):
        hdr = login(client, "admin", "admin123")
        d = client.get(ROUTE, headers=hdr).json()
        seen = 0
        for f in d["flows"]:
            for m in f["stats"]:
                assert set(m) == {"label", "value"}, m
                assert str(m["label"]).strip(), m
                seen += 1
        for s in d["surfaces"]:
            for m in s["live"]:
                assert set(m) == {"label", "value"}, m
                assert str(m["label"]).strip(), m
                seen += 1
        assert seen >= 15, f"слишком мало живых чисел: {seen}"

    def test_no_secrets_in_response(self, client):
        """Хэши токенов, открытые ключи и содержимое журналов наружу не
        отдаются: карточка показывает метку, время и лимиты."""
        hdr = login(client, "admin", "admin123")
        raw = client.get(ROUTE, headers=hdr).text
        for key in FORBIDDEN_KEYS:
            assert key not in raw, f"в ответе встретилось {key}"

    def test_connections_block_is_present(self, client):
        hdr = login(client, "admin", "admin123")
        c = client.get(ROUTE, headers=hdr).json()["connections"]
        for k in ("passkeys_total", "passkeys", "logins_24h",
                  "logins_failed_24h", "logins", "pool_users", "api_keys"):
            assert k in c, k
        assert isinstance(c["passkeys"], list)
        assert isinstance(c["logins"], list)
        assert isinstance(c["api_keys"], list)
        # списки ограничены: карточка не должна расти бесконечно
        assert len(c["passkeys"]) <= 8 and len(c["logins"]) <= 8
        assert len(c["api_keys"]) <= 8

    def test_login_rows_do_not_carry_secrets(self, client):
        hdr = login(client, "admin", "admin123")
        for row in client.get(ROUTE, headers=hdr).json()["connections"]["logins"]:
            assert set(row) == {"username", "action", "ip", "at"}, row

    def test_api_key_rows_show_prefix_only(self, client):
        hdr = login(client, "admin", "admin123")
        for row in client.get(ROUTE, headers=hdr).json()["connections"]["api_keys"]:
            assert set(row) == {"name", "prefix", "active", "rate_per_hour",
                                "monthly_quota", "last_used_at"}, row

    def test_network_block_reports_the_real_database(self, client):
        """Путь к базе выводится из настройки, а не захардкожен: иначе
        карточка называла бы чужой файл и показывала нулевой размер."""
        from app.services.backups import _db_path
        hdr = login(client, "admin", "admin123")
        n = client.get(ROUTE, headers=hdr).json()["network"]
        for k in ("site_url", "database_kind", "db_path", "db_size_mb",
                  "backups_total", "fetch_queue", "repo_url", "branch",
                  "commit"):
            assert k in n, k
        assert n["db_path"] == _db_path(), "путь к базе не из настройки"
        assert n["site_url"].startswith("http"), n["site_url"]
        assert "queued" in n["fetch_queue"]

    def test_route_is_registered_in_the_application(self, client):
        hdr = login(client, "admin", "admin123")
        # маршрут отвечает, а не падает в SPA-ловушку страницей входа
        r = client.get(ROUTE, headers=hdr)
        assert r.headers.get("content-type", "").startswith("application/json")

    def test_non_admin_is_refused(self, client):
        hdr = _mk_user(client, "user", "cl_usr50")
        assert client.get(ROUTE, headers=hdr).status_code == 403

    def test_accountant_is_refused(self, client):
        hdr = _mk_user(client, "accountant", "cl_acc50")
        assert client.get(ROUTE, headers=hdr).status_code == 403

    def test_anonymous_is_refused(self, client):
        # Фикстура client сессионная: предыдущий тест мог войти по cookie.
        client.cookies.clear()
        assert client.get(ROUTE).status_code in (401, 403)

    def test_system_summary_is_untouched(self, client):
        """Карточка «Сервер и команды» осталась прежней: новый маршрут
        добавлен рядом, а не вместо неё."""
        hdr = login(client, "admin", "admin123")
        r = client.get(SYSTEM_ROUTE, headers=hdr)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("version", "python", "uptime_s", "service_user", "unit",
                  "sudoers_cmd", "reexec", "counts", "fetch_queue",
                  "db_size_mb", "secret_key_ok"):
            assert k in d, k
        assert "sudo tee /etc/sudoers.d/" in d["sudoers_cmd"]
        assert "NOPASSWD" in d["sudoers_cmd"]

    def test_router_source_keeps_both_routes(self):
        src = _read(ADMIN_ROUTER)
        assert '@router.get("/clients"' in src, "маршрут не зарегистрирован"
        assert '@router.get("/system"' in src, "прежний маршрут пропал"
        assert "def clients_and_flows(" in src
        assert "def system_info(" in src


# ======================================================================
# 3. Карточки в разделе «Данные»
# ======================================================================
class TestCards:
    def test_two_cards_are_in_the_data_section(self):
        js = _read(APP_JS)
        assert 'id="st-clients" data-sec="data" style="order:14"' in js
        assert 'id="st-transfer" data-sec="data" style="order:15"' in js

    def test_card_titles(self):
        js = _read(APP_JS)
        assert "👥 Клиенты приложения" in js
        assert "🔀 Передача данных и сеть" in js
        assert "(v1.70.50)" in js

    def test_cards_stand_right_after_the_server_card(self):
        """Владелец просил добавить к блоку «Сервер и команды»: обе карточки
        идут сразу за ним и в том же разделе."""
        js = _read(APP_JS)
        i_server = js.index('id="st-server"')
        i_clients = js.index('id="st-clients"')
        i_transfer = js.index('id="st-transfer"')
        assert i_server < i_clients < i_transfer

    def test_cards_are_admin_only(self):
        js = _read(APP_JS)
        start = js.rindex("${isAdmin() ? `", 0, js.index('id="st-server"'))
        end = js.index('id="st-update"')
        block = js[start:end]
        assert 'id="st-clients"' in block, "карточка вне админского блока"
        assert 'id="st-transfer"' in block, "карточка вне админского блока"

    def test_single_fetch_feeds_both_cards(self):
        js = _read(APP_JS)
        assert js.count(f"api.get('{ROUTE}')") == 1, \
            "запрос к маршруту должен быть один на две карточки"
        assert "clientsInfo" in js

    def test_render_targets_exist_and_are_filled(self):
        js = _read(APP_JS)
        for cid in ("cl-surfaces", "cl-connections", "tr-network", "tr-flows"):
            assert f'id="{cid}"' in js, f"{cid}: нет контейнера в разметке"
            assert f"$('#{cid}')" in js, f"{cid}: контейнер не заполняется"

    def test_render_reuses_existing_styles(self):
        """Новых стилей выпуск не тащит: карточки собраны из прежних классов."""
        js = _read(APP_JS)
        start = js.index('id="st-clients"')
        end = js.index('id="st-update"')
        block = js[start:end]
        for cls in ("glass card", "card-title", "form-hint", "kv"):
            assert cls in block, f"класс {cls} не использован"

    def test_render_escapes_server_values(self):
        """Значения из ответа экранируются: текст справочника не должен
        стать способом вставить разметку в страницу администратора."""
        js = _read(APP_JS)
        start = js.index("// v1.70.50: «Клиенты приложения»")
        end = js.index("makeSettingsCollapsible(container)", start)
        block = js[start:end]
        for needle in ("esc(s.name)", "esc(s.text)", "esc(f.service)",
                       "esc(f.label)", "esc(f.value)", "esc(_v(n.site_url))"):
            assert needle in block, f"не экранируется {needle}"
        for raw in ("${s.text}", "${f.value}", "${f.sends}", "${n.db_path}"):
            assert raw not in block, f"сырая вставка {raw}"

    def test_flows_without_journal_say_so(self):
        """Карточка пишет, что журнала нет, вместо выдуманного числа."""
        js = _read(APP_JS)
        assert "Счётчиков нет: поток не ведёт журнал" in js

    def test_directions_are_translated(self):
        """Направление потока показывается по-русски: служебное слово из
        справочника не должно выходить в интерфейс администратора."""
        js = _read(APP_JS)
        for needle in ("out: 'наружу'", "in: 'внутрь'",
                       "both: 'в обе стороны'", "browser: 'браузер, не сервер'"):
            assert needle in js, f"нет перевода направления: {needle}"

    def test_connections_are_collapsed_by_default(self):
        """Раздел не перегружен: списки устройств и входов свёрнуты."""
        js = _read(APP_JS)
        start = js.index('id="st-clients"')
        end = js.index('id="st-transfer"')
        block = js[start:end]
        assert "<details" in block and "<summary>" in block
        assert "Подключённые устройства и последние входы" in block


# ======================================================================
# 4. Ритуал выпуска
# ======================================================================
class TestRelease17050:
    """v1.70.51: выпуск 1.70.50 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17051, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.49).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.
    """

    VERSION = "1.70.50"
    PREVIOUS = "1.70.49"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(APP / "config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 50)

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 5, "в записи выпуска не пять пунктов"
        for needle in ("клиент", "поток", "журнал", "секрет", "кэш-бастер"):
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
        for needle in ("клиент", "передач", "журнал", "секрет", "кэш-бастер",
                       "Отклонено", "справочник"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section, f"в CHANGELOG нет раздела «{head}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in (OWNER_QUOTE_HEAD, OWNER_QUOTE_TAIL):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "адрес", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        """Цитата требования владельца присутствует дословно во всех трёх
        местах. Внутри цитаты допустим перевод строки — на состав слов он не
        влияет, поэтому сравнение идёт по тексту с нормализованными
        пробелами, как в соседней проверке раздела CHANGELOG."""
        needle = re.sub(r"\s+", " ", OWNER_QUOTE_HEAD).lower()
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            assert needle in flat, f"{path.name}: нет цитаты владельца"
