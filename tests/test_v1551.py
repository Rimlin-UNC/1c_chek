# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.55.1 (ООО «Ямастер»)
# 1) Адаптив главной: страница прокручивается целиком, форма входа не
#    обрезается (телефон и браузер).
# 2) Сохранность данных: страховые копии базы ВНЕ каталога приложения,
#    повторный deploy = обновление на месте, автовосстановление, зеркало.
# ======================================================================
import os
import re
import subprocess


class TestVersion1551:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.55.1 перенесён в tests/test_v1560.py (версия ушла вперёд)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 55, 1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.55.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.55.1':") < js.index("'1.55.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.55.1]") == 1
        assert ch.index("## [1.55.1]") < ch.index("## [1.55.0]")


class TestLandingAdaptive:
    def test_login_screen_grows_and_scrolls(self):
        """Высота по контенту: верх страницы достижим, обрезки нет."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        blk = css.split(".login-screen {")[1][:400]
        assert "min-height: 100vh" in blk and "min-height: 100dvh" in blk
        assert not re.search(r"(?<!min-)height: 100", blk)  # фикс. высоты нет
        assert "align-items: flex-start" in blk      # верх не «съедается» центром
        assert "overflow-y: auto" in blk             # страховка прокрутки
        assert ".login-screen > * { margin: auto; }" in css

    def test_no_viewport_overflow(self):
        """Ширины 96vw/94vw были шире области с паддингом — форма уезжала."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "width: min(420px, 100%)" in css      # карточка входа
        assert "width: min(420px, 94vw)" not in css
        assert "width: min(1120px, 100%)" in css     # лендинг
        assert "width: min(1120px, 96vw)" not in css
        assert "minmax(min(300px, 100%), 420px)" in css   # колонка не шире экрана

    def test_phone_layout(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@media (max-width: 520px)" in css
        m = css.split("@media (max-width: 520px)")[1][:900]
        assert "font-size: 16px" in m                # без авто-зума iOS
        assert "flex: 1 1 100%" in m                 # кнопки во всю ширину
        # карточка входа по центру мобильной колонки
        assert "justify-self: center" in css
        assert "max-width: 440px" in css
        # альбомная ориентация — компактнее
        assert "@media (max-height: 560px)" in css

    def test_landing_classes_intact(self):
        """v1.54.0: классы лендинга на месте (не ломаем старые пины)."""
        idx = open("app/static/index.html", encoding="utf-8").read()
        for cls in ("land-wrap", "land-left", "land-right", "land-h1",
                    "land-stats", "land-steps", "land-faq", "login-card"):
            assert cls in idx, cls


class TestDeployDataSafety:
    def test_offapp_backup_before_anything(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert 'BACKUP_ROOT="/var/backups/ymaster-check"' in d
        assert 'DB_FILE="$APP_DIR/data/ymaster_check.db"' in d
        assert 'cp -a "$DB_FILE" "$BACKUP_ROOT/ymaster_check-$TS.db"' in d
        assert 'cp -a "$APP_DIR/.env" "$BACKUP_ROOT/env-$TS.bak"' in d
        assert "tail -n +31" in d                    # хранить 30 копий

    def test_rerun_is_inplace_update(self):
        d = open("deploy.sh", encoding="utf-8").read()
        # старое опасное поведение удалено
        assert 'rm -rf "$APP_DIR/app"' not in d
        # любая установка с .git обновляется на месте
        assert 'if [[ -d "$APP_DIR/.git" ]]; then' in d
        # каталог без git init-ится на месте, данные вне чистки
        assert "clean -fd" in d and "-e data -e .env -e venv" in d

    def test_restore_from_backup_on_missing_db(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert 'if [[ ! -s "$DB_FILE" ]]' in d
        assert "файл базы отсутствовал — восстановлен из копии" in d

    def test_diagnose_shows_db(self):
        d = open("deploy.sh", encoding="utf-8").read()
        assert 'echo "-- база данных:"' in d
        assert 'sqlite3 "$DB_FILE"' in d

    def test_deploy_sh_syntax(self):
        r = subprocess.run(["bash", "-n", "deploy.sh"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_rollback_and_updater_exclusions_intact(self):
        rb = open("rollback.sh", encoding="utf-8").read()
        assert "git clean -fd -e data" in rb
        up = open("app/services/updater.py", encoding="utf-8").read()
        assert '"clean", "-fd", "-e", "data"' in up


class TestBackupMirror:
    def test_mirror_called_and_defined(self):
        src = open("app/services/backups.py", encoding="utf-8").read()
        assert "def _mirror_offapp" in src
        assert src.index("_mirror_offapp(dest)") < src.index("_cleanup(kind)")

    def test_mirror_survives_dir_reinstall(self, tmp_path, monkeypatch):
        """Зеркало пишется в отдельный каталог и переживает «переустановку»."""
        import sqlite3
        from app.services import backups
        extra = tmp_path / "offapp"
        extra.mkdir()
        monkeypatch.setenv("YMASTER_BACKUP_EXTRA_DIR", str(extra))
        src = backups._db_path()
        if not os.path.exists(src):
            con = sqlite3.connect(src)
            con.execute("CREATE TABLE IF NOT EXISTS t(x INTEGER)")
            con.commit()
            con.close()
        dest = backups.create_backup("manual")
        assert dest, "копия БД не создалась"
        assert os.path.exists(os.path.join(str(extra), os.path.basename(dest)))
        # «переустановка каталога приложения»: основное хранилище стёрто —
        # копия во внешнем каталоге осталась
        backups_dir = os.path.dirname(dest)
        for f in os.listdir(backups_dir):
            os.unlink(os.path.join(backups_dir, f))
        assert os.path.exists(os.path.join(str(extra), os.path.basename(dest)))


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        vt = lambda v: tuple(int(x) for x in v.split("."))
        # точные пины 1.55.1 перенесены в tests/test_v1560.py
        assert vt(reg["Чек-Пул"]) >= (1, 55, 1)      # лендинг/адаптив
        assert vt(reg["Обновления"]) >= (1, 55, 1)   # deploy.sh/бэкапы
        assert vt(reg["Проверка чеков (ФНС и источники)"]) >= (1, 55, 0)
