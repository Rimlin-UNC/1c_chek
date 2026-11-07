# -*- coding: utf-8 -*-
"""v1.70.29 — «вход уже выполнен» не роняет мастер; ловушки инструкции закрыты.

Выпуск появился из живого прогона на сервере владельца. Картина была такая:

    adguardvpn-cli login
    You are already logged in, if you want to use another account, please
    log out first
     Current user is 0zona@bk.ru

То есть аккаунт уже привязан и ссылка не нужна в принципе. Но мастер при
ненулевом коде возврата CLI падал с «Вход не завершён» — ровно там, где всё
уже готово и можно сразу поднимать туннель.

Второе: владелец повторил документированную последовательность руками и упёрся
в две ловушки самой инструкции —
  * `site-exclusions add <домены Telegram>` — заполнитель, а не команда
    (bash: syntax error near unexpected token `newline');
  * `systemctl enable --now adguard-vpn-telegram` — «Unit file … does not
    exist», потому что юнит кладёт мастер (шаг `units`) из комплекта,
    разложенного `install-rule`.

Что здесь доказано:
  1) собственный текст CLI «вход уже выполнен» важнее его кода возврата —
     мастер не падает и продолжает (при ненулевом и при нулевом коде);
  2) порядок ветвей: «уже выполнен» проверяется РАНЬШЕ отказа, а «Failed to
     get auth link» по-прежнему фатален, когда входа действительно нет;
  3) текст используется только чтобы НЕ падать — успех по-прежнему доказывается
     ссылкой или документированным признаком, а не молчанием CLI;
  4) в инструкции нет заполнителя `<домены Telegram>`, обе ловушки описаны,
     настоящий список доменов и кавычки для wildcards на месте;
  5) версия, CHANGELOG, «Что нового» и синхронизация версий.

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings
from app.services import adguard_setup as ag

APP_JS = "app/static/js/app.js"
CHANGELOG = "CHANGELOG.md"
README = "deploy/adguard-vpn/README.md"
HELPER_SH = "deploy/adguard-vpn/ymaster-adguard-setup.sh"
VER = "1.70.29"
PREV = "1.70.28"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Дословный ответ CLI, снятый с живого сервера владельца.
REAL_ALREADY = ("You are already logged in, if you want to use another "
                "account, please log out first\n Current user is "
                "0zona@bk.ru")


def _read(path):
    return open(path, encoding="utf-8").read()


@pytest.fixture
def fresh_job(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "_state_path",
                        lambda: str(tmp_path / "adguard_setup.json"))
    ag.job.reset()
    yield ag.job
    ag.job.reset()


def _stub_login(monkeypatch, rc, out, url=""):
    monkeypatch.setattr(ag, "_find_cli", lambda: "/usr/local/bin/ag")
    monkeypatch.setattr(ag, "_login_worker", lambda cli: (rc, out, url))


# ==============================================================================
#  1) Уже выполненный вход — не ошибка
# ==============================================================================


class TestAlreadyLoggedInIsNotAFailure:
    def test_nonzero_rc_with_already_logged_in_does_not_raise(self, fresh_job,
                                                             monkeypatch):
        """Главный тест выпуска: именно этот случай заблокировал сервер.

        CLI отвечает «You are already logged in» и может вернуть ненулевой код.
        Аккаунт привязан — падать не из-за чего, нужно идти дальше к туннелю.
        """
        _stub_login(monkeypatch, 1, REAL_ALREADY)
        ag._step_login()                       # не raise
        text = "\n".join(fresh_job.log)
        assert "Вход уже выполнен" in text
        assert "ссылка не нужна" in text
        assert "Вход не завершён" not in text

    def test_zero_rc_with_already_logged_in_also_ok(self, fresh_job,
                                                    monkeypatch):
        _stub_login(monkeypatch, 0, REAL_ALREADY)
        ag._step_login()
        assert "Вход уже выполнен" in "\n".join(fresh_job.log)

    def test_marker_is_case_insensitive(self, fresh_job, monkeypatch):
        """CLI печатает строку с заглавной; сравниваем в нижнем регистре."""
        _stub_login(monkeypatch, 2, "YOU ARE ALREADY LOGGED IN")
        ag._step_login()
        assert "Вход уже выполнен" in "\n".join(fresh_job.log)

    def test_progress_advances_past_login(self, fresh_job, monkeypatch):
        """Шаг должен быть отмечен завершённым, иначе панель стоит на 30 %."""
        _stub_login(monkeypatch, 1, REAL_ALREADY)
        ag._step_login()
        st = fresh_job.steps.get("login")
        assert st is not None
        assert st["status"] in ("skip", "done")
        assert (fresh_job.progress or 0) >= 45

    def test_marker_list_is_grounded_in_cli_text(self):
        """Маркеры — это текст самого CLI, а не выдуманная отсебятина.

        Каждый маркер обязан встречаться в реальном ответе, снятом с живого
        сервера. Тест намеренно строгий: именно он не даст добавить сюда
        «правдоподобную» формулировку, которой CLI никогда не печатал, — с ней
        мастер начал бы считать вход выполненным на пустом месте.
        """
        assert ag.ALREADY_LOGGED_IN
        assert "already logged in" in ag.ALREADY_LOGGED_IN
        for m in ag.ALREADY_LOGGED_IN:
            assert m in REAL_ALREADY.lower(), \
                f"маркер {m!r} не подтверждён живым выводом CLI"
            assert m == m.lower(), "сравнение идёт в нижнем регистре"


# ==============================================================================
#  2) Порядок ветвей: защита не ослаблена
# ==============================================================================


class TestBranchOrderKeepsProtections:
    def test_auth_link_error_still_fatal_without_login(self, fresh_job,
                                                       monkeypatch):
        """Если входа нет, «Failed to get auth link» обязан остаться фатальным
        и по-прежнему давать команду лечения сертификатов."""
        _stub_login(monkeypatch, 1, "Failed to get auth link")
        with pytest.raises(RuntimeError) as ei:
            ag._step_login()
        assert "ca-certificates" in str(ei.value)
        assert "#142" in str(ei.value) and "#169" in str(ei.value)

    def test_already_logged_in_wins_over_auth_link_error(self, fresh_job,
                                                         monkeypatch):
        """«Уже выполнен» проверяется раньше отказа: если аккаунт привязан,
        жалоба CLI на ссылку не имеет смысла — вход-то есть."""
        _stub_login(monkeypatch, 1,
                    "Failed to get auth link\nYou are already logged in")
        ag._step_login()
        assert "Вход уже выполнен" in "\n".join(fresh_job.log)

    def test_plain_failure_still_raises(self, fresh_job, monkeypatch):
        _stub_login(monkeypatch, 1, "connection refused")
        with pytest.raises(RuntimeError) as ei:
            ag._step_login()
        assert "Вход не завершён" in str(ei.value)

    def test_cancel_still_wins_over_everything(self, fresh_job, monkeypatch):
        """Отмена владельцем проверяется до любых трактовок вывода."""
        _stub_login(monkeypatch, 1, REAL_ALREADY)
        fresh_job.cancel_requested = True
        with pytest.raises(RuntimeError) as ei:
            ag._step_login()
        assert "Отменено владельцем" in str(ei.value)

    def test_confirmed_link_still_reports_done(self, fresh_job, monkeypatch):
        _stub_login(monkeypatch, 0, "ok",
                    "https://auth.adguard.io/device_code?user_code=X")
        ag._step_login()
        assert "Вход подтверждён" in "\n".join(fresh_job.log)

    def test_silent_no_url_is_not_claimed_as_success(self, fresh_job,
                                                     monkeypatch):
        """v1.70.28 сохранён: без ссылки и без признаков входа мастер честен."""
        _stub_login(monkeypatch, 0, "Choose an option:")
        ag._step_login()
        text = "\n".join(fresh_job.log)
        assert "Вход уже выполнен" not in text
        assert ag.LOGIN_ANSWER_HINT in text


# ==============================================================================
#  3) Инструкция: ловушки закрыты
# ==============================================================================


class TestReadmeTrapsClosed:
    def test_no_placeholder_domain_command(self):
        """Заполнитель `<домены Telegram>` стоил владельцу ошибки bash:
        `site-exclusions add` принимает конкретные домены."""
        rd = _read(README)
        assert "<домены Telegram>" not in rd
        assert "site-exclusions add api.telegram.org" in rd

    def test_both_traps_are_documented(self):
        rd = _read(README)
        assert "Две ловушки ручного повтора" in rd
        assert "does not exist" in rd, "ловушка с юнитом обязана быть описана"
        assert "/usr/local/share/ymaster-adguard" in rd

    def test_empty_selective_list_warning(self):
        """Пустой список в selective = через VPN не идёт ничего, включая
        Telegram. Это надо сказать прямо."""
        rd = _read(README)
        assert "через VPN не идёт" in rd

    def test_wildcard_quoting_documented(self):
        rd = _read(README)
        assert '"*.telegram.org"' in rd
        assert "#148" in rd

    def test_full_domain_list_matches_helper(self):
        """Список в инструкции обязан совпадать с TG_DOMAINS помощника —
        иначе ручной повтор даст другой результат, чем мастер."""
        import re
        sh = _read(HELPER_SH)
        m = re.search(r"TG_DOMAINS=\(([^)]*)\)", sh, re.S)
        assert m
        domains = re.findall(r'"([^"]+)"', m.group(1))
        assert "api.telegram.org" in domains
        assert "*.telegram.org" in domains
        rd = _read(README)
        for d in ("api.telegram.org", "core.telegram.org", "telegram.me",
                  "telegram.dog", "t.me"):
            assert d in rd, d

    def test_switch_is_offered_as_the_way_out(self):
        rd = _read(README)
        assert "AdGuard VPN на сервере" in rd


# ==============================================================================
#  4) Версия, CHANGELOG, «Что нового»
# ==============================================================================


class TestVersion17029:
    def test_app_version(self):
        # Без жёсткого пина: каждый следующий выпуск закономерно поднимает
        # версию, и проверка «ровно VER» ломалась бы на каждом bump.
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= \
            (1, 70, 29)

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_matches_current_version(self, path):
        assert settings.APP_VERSION in _read(path), path

    def test_changelog_entry_once_and_first(self):
        md = _read(CHANGELOG)
        assert md.count(f"## [{VER}]") == 1
        assert md.index(f"## [{VER}]") < md.index(f"## [{PREV}]")
        entry = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        for needle in ("already logged in", "заполнитель", "does not exist",
                       "selective"):
            assert needle in entry, needle

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        assert f"'{VER}':" in js
        entry = js[js.index(f"'{VER}':"):js.index(f"'{PREV}':")]
        assert "уже выполнен" in entry
        assert "ловуш" in entry

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr

    def test_helper_syntax(self):
        r = subprocess.run(["bash", "-n", HELPER_SH], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr
