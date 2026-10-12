# Ямастер Чек — тесты v1.68.1: стресс-дожим (уведомления + пул).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
import re
import time


class TestVersion1681:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.68.1"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.68.1]") == 1
        assert ch.index("## [1.68.1]") < ch.index("## [1.68.0]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.68.1':") < js.index("'1.68.0':")

    def test_blocks(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Telegram-уведомления"] == "1.68.1"


class TestPoolCapacity:
    def test_overflow_headroom(self):
        from app.database import engine
        assert engine.pool.size() >= 20
        assert getattr(engine.pool, "_max_overflow", 0) >= 60

    def test_notify_bounded(self):
        src = open("app/services/telegram_bot.py", encoding="utf-8").read()
        assert "_NOTIFY_SEM = threading.Semaphore(8)" in src
        assert "_NOTIFY_SEM.acquire(blocking=False)" in src

    def test_notify_skips_when_busy(self, monkeypatch):
        from app.services import telegram_bot as tg

        spawned = []

        class _FakeT:
            def __init__(self, target=None, daemon=None, name=None):
                pass
            def start(self):
                spawned.append(1)

        class _BusySem:
            def acquire(self, blocking=False):
                return False
            def release(self):
                pass

        monkeypatch.setattr(tg, "_NOTIFY_SEM", _BusySem())
        monkeypatch.setattr(tg.threading, "Thread", _FakeT)
        tg.notify_user("no-such-user", "тест")
        assert spawned == []          # канал занят — поток не создан
