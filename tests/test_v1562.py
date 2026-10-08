# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.56.2: WebSocket /ws/status переживает
# отключение клиента без traceback в журнале.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1562:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.56.2 перенесён в tests/test_v1570.py (версия ушла вперёд)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 56, 2)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.56.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.56.2':") < js.index("'1.56.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.56.2]") == 1
        assert ch.index("## [1.56.2]") < ch.index("## [1.56.1]")


class TestWsQuietDisconnect:
    def test_disconnect_handled_structurally(self):
        """except охватывает и первый send, и цикл с ping/send_text."""
        mp = open("app/main.py", encoding="utf-8").read()
        seg = mp.split("pump = asyncio.create_task(_pump_incoming())")[1]
        assert seg.index("except (WebSocketDisconnect, RuntimeError):") \
            < seg.index("finally:")
        assert 'await ws.send_json({"type": "ping"' in seg
        assert "await ws.send_text(message)" in seg
        # cleanup остался в finally — подписка снимается всегда
        assert "pump.cancel()" in seg and "unsubscribe(queue)" in seg

    def test_ws_connect_receive_close(self, client):
        """Функционально: подключение → connected → чистое закрытие без
        исключения на стороне приложения; сервис продолжает отвечать."""
        with client.websocket_connect("/ws/status") as ws:
            data = ws.receive_json()
            assert data["type"] == "connected"
            assert data["payload"]["app"]
        r = client.get("/health")
        assert r.status_code == 200

    def test_pump_still_detects_disconnect(self):
        """Внутренний монитор разрыва (_pump_incoming) не сломан."""
        mp = open("app/main.py", encoding="utf-8").read()
        assert 'except WebSocketDisconnect:\n            connected["ok"] = False' in mp


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Сканирование чеков"] == "1.56.2"   # живые статусы чеков
        assert reg["Чек-Пул"] == "1.56.1"              # не задет
        assert reg["Адаптивный интерфейс"] == "1.56.1"  # не задет
        assert reg["Обновления"] == "1.55.1"           # не задет
