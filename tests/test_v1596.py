# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.6: время — строго Московское.
# 1) Журнал хода обновления (метки [ЧЧ:ММ:СС]) ставился наивным
#    datetime.now() — на сервере с UTC отставал на 3 часа (карточки
#    при этом верные: там UTC+«Z», браузер сам переводит в МСК).
#    Теперь метки — по Московскому времени всегда.
# 2) deploy.sh: timedatectl молча не сработал на проде — часовой пояс
#    теперь ставится и прямой записью /etc/localtime + /etc/timezone
#    (работает без systemd-timedated), journald перезапускается.
# 3) systemd-юнит: Environment=TZ=Europe/Moscow — время процесса
#    Московское, даже если системный пояс применить не удалось.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re
from datetime import datetime, timedelta, timezone


class TestVersion1596:
    """Строгий версионный замок релиза v1.59.6."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.60.0: структурная проверка — строгий замок версии в test_v1600
        assert tuple(int(x) for x in ver.split(".")) >= (1, 59, 6)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.5" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.6':") < js.index("'1.59.5':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.6]") == 1
        assert ch.index("## [1.59.6]") < ch.index("## [1.59.5]")

    def test_updates_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in reg["Обновления"].split(".")) >= (1, 59, 6)


class TestUpdateLogMoscow:
    """Метки журнала обновления — по Московскому времени всегда."""

    def test_log_stamp_is_utc_z(self):
        """v1.60.0: метка журнала — момент в UTC с «Z» (зону выбирает браузер)."""
        from app.services.updater import _utc_stamp
        s = _utc_stamp()
        assert s.endswith("Z") and "T" in s
        got = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert abs((got - datetime.now(timezone.utc)).total_seconds()) <= 2

    def test_say_log_line_carries_z(self):
        from app.services.updater import UpdateJob
        job = UpdateJob()
        job.say("check", message="проверка времени")
        assert job.log and job.log[0].startswith("[2")
        assert job.log[0].split("]")[0].endswith("Z")

    def test_naive_now_removed_from_say(self):
        src = open("app/services/updater.py", encoding="utf-8").read()
        assert 'stamp = datetime.now().strftime("%H:%M:%S")' not in src
        assert "stamp = _utc_stamp()" in src
        assert "_msk_stamp" not in src

    def test_deploy_scripts_syntax(self):
        import subprocess
        for f in ("deploy.sh", "rollback.sh"):
            r = subprocess.run(["bash", "-n", f], capture_output=True, text=True)
            assert r.returncode == 0, (f, r.stderr[:300])


class TestServerTimezone:
    """Сервер и сервис — в Московском времени (deploy.sh + юнит)."""

    def test_deploy_sets_tz_without_timedated(self):
        sh = open("deploy.sh", encoding="utf-8").read()
        # прямая запись — работает даже когда timedatectl недоступен
        assert "ln -snf /usr/share/zoneinfo/Europe/Moscow /etc/localtime" in sh
        assert 'echo "Europe/Moscow" > /etc/timezone' in sh
        # journald перечитывает зону — префикс журнала становится МСК
        assert "systemctl restart systemd-journald" in sh

    def test_unit_runs_in_moscow_tz(self):
        unit = open("deploy/systemd/ymaster-check.service",
                    encoding="utf-8").read()
        assert "Environment=TZ=Europe/Moscow" in unit
