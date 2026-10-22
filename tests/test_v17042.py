# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.42: ОДИН поток сканирования чеков.
#
# Требование владельца: «Подумай как правильно объединить пакетное
# сканирование и обычное сканирование qr-кода чеком… не стоит плодить
# пакетное сканирование и сканирование qr-чеком… главное чтобы это
# работало идеально и хорошо».
#
# Решение (подробно — docs/ui_journal.md, раздел v1.70.42):
#   1) одна кнопка сканирования на точку сдачи — второй кнопки «пачкой» нет
#      ни на сайте, ни в мини-приложении MAX;
#   2) камера непрерывная: после чека она не закрывается, а сканер MAX
#      открывается снова сам — «обычное» сканирование и есть поточное;
#   3) сессия начинается лениво, с первого чека ЛЮБЫМ способом ввода
#      (сканер, своя камера, фото, вставка строки, ручной ввод) — счётчики,
#      проверка дубликата и уточнение статусов у всех общие;
#   4) конвейер отправки один: на сайте /public/pool/check вызывается только
#      из pbHandle, в MAX /maxapp/scan — только из batchHandle; второй ветки
#      отправки больше нет (раньше одиночный скан и «пачка» соревновались за
#      один суточный лимит и показывали человеку разные картины);
#   5) рубеж 100 чеков и окно «продолжаем или закончим?» сохранены;
#   6) сервер не менялся: те же /check, /scan и /batch-status, тот же дедуп
#      по ФН+ФД+ФП и те же тексты отказов.
#
# Здесь — ритуал выпуска, замки на исходник (чтобы единый поток не распался
# обратно на два режима при будущей правке) и проверка серверной стороны.
#
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
import re
from pathlib import Path

import pytest

from app.database import engine
from app.pool.models import ensure_pool_schema

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/static/js/app.js"
MAX_JS = ROOT / "app/static/maxapp/maxapp.js"
MAX_HTML = ROOT / "app/static/maxapp/index.html"
CSS = ROOT / "app/static/css/app.css"

ensure_pool_schema(engine)      # добавляющая миграция (idempotent) до тестов


def _read(path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p.read_text(encoding="utf-8")


def _js_block(src: str, marker: str) -> str:
    """Вырезать функцию по первой фигурной скобке (со счётом вложенности).

    Регулярным выражением client-код не берётся: внутри тысячи вложенных
    скобок и шаблонных строк.
    """
    start = src.index(marker)
    i = src.index("{", start + len(marker) - 1)
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[start:k + 1]
    raise AssertionError("не нашлась закрывающая скобка")


# ======================================================================
# 1. Ритуал выпуска
# ======================================================================
class TestRelease17042:
    VERSION = "1.70.42"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read("app/config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read("app/static/sw.js")
        assert f'"version": "{self.VERSION}"' in _read(
            "app/static/manifest.webmanifest")
        idx = _read("app/static/index.html")
        assert f"app.css?v={self.VERSION}" in idx
        assert f"app.js?v={self.VERSION}" in idx
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        sw = _read("app/static/sw.js")
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?\'([\d.]+)\'", js)
        assert m and m.group(1) == self.VERSION, "запись выпуска не первая"

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.41': [", 1)[0]
        assert block.count("{ icon:") == 4
        for part in ("одн", "скан", "счётчик", "100", "камер"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_whats_new_has_no_foreign_letters(self):
        """В прозе для донора не должно быть посторонних письмен и латинских
        слов, кроме названий, которые человек видит в интерфейсе."""
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.41': [", 1)[0]
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", block)
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), f"постороннее письмо {ch!r}"
        allowed = {"MAX", "QR"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read("CHANGELOG.md")
        assert f"## [{self.VERSION}]" in text
        sec = text.split(f"## [{self.VERSION}]", 1)[1].split("## [1.70.41]", 1)[0]
        for needle in ("одна кнопка", "openPoolCameraScan", "ensureSession",
                       "pbEnsure", "batchScanOnce", "100"):
            assert needle in sec, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        j = _read("docs/ui_journal.md")
        assert f"## v{self.VERSION} —" in j, "в журнале нет раздела выпуска"
        sec = j.split(f"## v{self.VERSION} —", 1)[1].split("## v1.70.41 —", 1)[0]
        for needle in ("плодить", "Отказ", "openPoolCameraBatch", "submit",
                       "batchHandle", "pbEnsure", "ensureSession", "Решение"):
            assert needle in sec, f"в журнале нет «{needle}»"

    def test_previous_release_stays_in_history(self):
        """Прокат версии не стирает запись предыдущего выпуска."""
        assert "'1.70.41': [" in _read(APP_JS)
        assert "## [1.70.41]" in _read("CHANGELOG.md")
        assert "## v1.70.41 —" in _read("docs/ui_journal.md")


# ======================================================================
# 2. Одна кнопка на точку сдачи — двух режимов больше нет
# ======================================================================
class TestOneButtonPerSurface:
    def test_site_has_single_scan_button(self):
        js = _read(APP_JS)
        assert js.count('id="pub-cam"') == 1
        assert 'id="pub-batch-start"' not in js, "вернулась вторая кнопка «пачкой»"
        # «Сканировать пачкой» осталось только в истории «Что нового» за 1.70.40
        form = _js_block(js, "function publicFormHTML()")
        assert "Сканировать пачкой" not in form, "второй режим вернулся в форму"

    def test_site_has_single_camera_function(self):
        """Две функции камеры означали два режима для одного действия."""
        js = _read(APP_JS)
        assert js.count("function openPoolCameraScan(") == 1
        assert "openPoolCameraBatch" not in js

    def test_max_has_single_scan_button(self):
        html = _read(MAX_HTML)
        assert html.count('id="btn-scan"') == 1
        assert 'id="btn-batch"' not in html, "вернулась вторая кнопка «пачкой»"
        assert "Сканировать пачкой" not in html

    def test_max_scan_button_starts_the_flow(self):
        js = _read(MAX_JS)
        assert "$('btn-scan').onclick = startBatch;" in js
        assert "$('btn-batch')" not in js

    def test_cabinet_keeps_single_session_panel(self):
        """Кабинет сотрудника уже был единым — панель и состояние на месте."""
        js = _read(APP_JS)
        assert "const scanSess = {" in js
        for needle in ("scan-bc-ok", "scan-bc-pend", "scan-bc-bad",
                       "scan-batch-prog", "btn-scan-sess-reset"):
            assert needle in js, needle

    def test_wording_does_not_split_the_flow(self):
        """«Пачка» как отдельный режим исчезла из видимых заголовков."""
        js = _read(APP_JS)
        html = _read(MAX_HTML)
        assert js.count("🧾 Сдача чеков") == 2          # сайт + кабинет
        assert "Сдача чеков" in html
        assert "Сдача пачкой" not in js and "Сдача пачкой" not in html


# ======================================================================
# 3. Один конвейер отправки
# ======================================================================
class TestOnePipeline:
    def test_site_sends_from_one_place(self):
        js = _read(APP_JS)
        assert js.count("'/api/v1/public/pool/check'") == 1
        fn = _js_block(js, "const pbHandle = async (text, parsed, fromCamera) =>")
        assert "'/api/v1/public/pool/check'" in fn

    def test_site_polls_statuses_from_one_place(self):
        js = _read(APP_JS)
        assert js.count("'/api/v1/public/pool/batch-status'") == 1

    def test_max_sends_from_one_place(self):
        js = _read(MAX_JS)
        assert js.count("'/api/v1/maxapp/scan'") == 1
        pipe = _js_block(js, "async function batchHandle(text)")
        assert "'/api/v1/maxapp/scan'" in pipe
        assert "{qr: String(text).slice(0, 4000), form_ms: 5000}" in pipe

    def test_max_submit_has_no_own_request(self):
        """Раньше submit сам ходил на сервер — это и был второй путь приёма."""
        js = _read(MAX_JS)
        sub = _js_block(js, "async function submit(qr)")
        assert "api(" not in sub
        assert "batchHandle(text)" in sub
        assert "qrFromScan(qr)" in sub            # v1.70.39: объект → строка
        assert "String(qr)" not in sub

    def test_max_polls_statuses_from_one_place(self):
        assert _read(MAX_JS).count("'/api/v1/maxapp/batch-status'") == 1

    def test_single_reader_per_transport(self):
        """Нативный сканер читает scanQr (он же сдаёт чек), своя камера —
        cameraBatchOnce. Второго читателя кода нет."""
        js = _read(MAX_JS)
        step = _js_block(js, "async function batchScanOnce()")
        assert "await scanQr();" in step
        assert "cameraBatchOnce()" in step
        assert "typeof app.openCodeReader === 'function'" in step
        assert step.count("openCodeReader") == 1, "появился второй читатель кода"
        assert "openCodeReader(false)" not in step, "диспетчер сам читает код"


# ======================================================================
# 4. Сессия начинается с первого чека любым способом ввода
# ======================================================================
class TestLazySession:
    def test_site_session_starts_on_first_receipt(self):
        js = _read(APP_JS)
        ensure = _js_block(js, "const pbEnsure = () =>")
        assert "PB.active = true" in ensure
        assert "show('pub-batch', true)" in ensure
        handle = _js_block(js, "const pbHandle = async (text, parsed, fromCamera) =>")
        assert "pbEnsure();" in handle

    def test_max_session_starts_on_first_receipt(self):
        js = _read(MAX_JS)
        ensure = _js_block(js, "function ensureSession()")
        assert "BATCH.active = true" in ensure
        assert "card.classList.remove('hidden')" in ensure
        sub = _js_block(js, "async function submit(qr)")
        assert "ensureSession();" in sub
        loop = _js_block(js, "async function startBatch()")
        assert "ensureSession();" in loop

    def test_site_camera_button_starts_the_same_session(self):
        js = _read(APP_JS)
        assert "if (camBtn) camBtn.onclick = () => pbStart(false);" in js
        start = _js_block(js, "const pbStart = (resume) =>")
        assert "pbEnsure();" in start
        assert "openPoolCameraScan(" in start

    def test_landing_autostart_uses_the_same_flow(self):
        """Приход с главной «Сканировать чек» открывает тот же поток."""
        assert "if (camWanted) pbStart(false);" in _read(APP_JS)

    def test_first_accepted_receipt_shows_details(self):
        """Одиночная сдача не потеряла подробный статус: первый принятый чек
        по-прежнему показывает статус, электронный чек и письмо на почту."""
        js = _read(APP_JS)
        handle = _js_block(js, "const pbHandle = async (text, parsed, fromCamera) =>")
        assert "if (PB.accepted === 1 && d.fn) pollStatus(d.fn);" in handle
        mx = _read(MAX_JS)
        pipe = _js_block(mx, "async function batchHandle(text)")
        assert "if (BATCH.accepted === 1) loadSummary();" in pipe


# ======================================================================
# 5. Камера непрерывная, завершение явное
# ======================================================================
class TestContinuousCamera:
    def test_site_camera_does_not_close_after_receipt(self):
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        cb = fn.split("new CameraScanner(video, (text) => {", 1)[1]
        cb = cb.split("});", 1)[0]
        assert "onText(text, p)" in cb
        assert "close()" not in cb, "камера закрывается после чека"
        assert "cam.stop()" not in cb, "камера останавливается после чека"

    def test_non_receipt_qr_is_ignored(self):
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        cb = fn.split("new CameraScanner(video, (text) => {", 1)[1]
        assert "if (!p) return;" in cb

    def test_counters_visible_inside_camera_window(self):
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        assert 'id="poolcam-counts"' in fn
        assert 'class="batch-cam-counts"' in fn
        assert "countsEl" in fn

    def test_done_button_stops_camera_and_session(self):
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        assert 'id="poolcam-done"' in fn
        assert "cam.stop()" in fn
        assert "typeof handle.onFinish === 'function'" in fn
        start = _js_block(js, "const pbStart = (resume) =>")
        assert "cam.onFinish = () => pbStop('manual');" in start

    def test_max_loop_reopens_scanner_until_cancelled(self):
        js = _read(MAX_JS)
        loop = _js_block(js, "async function startBatch()")
        assert "while (BATCH.active && BATCH.loop)" in loop
        assert "if (!ok) break;" in loop
        assert "stopBatch('done')" in loop

    def test_max_loop_flag_separated_from_session(self):
        """Кнопки гасятся только на время непрерывного потока: сессия,
        начатая вставкой строки, не должна выключать сканирование."""
        js = _read(MAX_JS)
        loop = _js_block(js, "async function startBatch()")
        assert "BATCH.loop = true;" in loop
        assert "batchSetButtons(true);" in loop
        stop = _js_block(js, "function stopBatch(reason)")
        assert "BATCH.loop = false;" in stop
        assert "batchSetButtons(false);" in stop

    def test_max_fallback_camera_not_opened_inside_loop(self):
        """Одиночный кадр своей камеры внутри потока не открывается — иначе
        два окна камеры боролись бы за один сканер."""
        js = _read(MAX_JS)
        scan = _js_block(js, "async function scanQr()")
        assert "if (!BATCH.loop) scanCamera();" in scan

    def test_scanner_locks_still_hold(self):
        """Замки v1.70.7/v1.70.2/v1.70.39 проверены после объединения."""
        js = _read(MAX_JS)
        assert js.index("openCodeReader") < js.index("scanCamera")
        found = re.findall(r"if \(!app \|\| !app\.openCodeReader\)[^}]*}", js, re.S)
        assert len(found) == 1 and "scanCamera" in found[0]
        assert "waitForBridge" in js and "mx-retry" in js


# ======================================================================
# 6. Рубеж 100 и суточный лимит сохранены
# ======================================================================
class TestThresholdKept:
    def test_site_threshold(self):
        js = _read(APP_JS)
        assert "PB.items.length >= PB.cap" in js
        assert "pbAskMore" in js
        assert "PB.cap += PUB_BATCH_CAP" in js
        assert "const PUB_BATCH_CAP = 100;" in js

    def test_max_threshold(self):
        js = _read(MAX_JS)
        assert "BATCH.items.length >= BATCH.cap" in js
        assert "batchAskMore" in js
        assert "BATCH.cap += BATCH_CAP" in js
        assert "const BATCH_CAP = 100;" in js

    def test_threshold_question_wording_kept(self):
        """Формулировка владельца: «вы отсканировал 100 чеков продолжаем или
        закончим?» — окно рубежа осталось прежним."""
        js = _read(APP_JS)
        assert "Продолжить — ещё" in js
        assert "Закончить и посмотреть итог" in js
        mx = _read(MAX_JS)
        assert "Продолжаем сдачу или закончим" in mx

    def test_session_cap_equals_daily_limit(self):
        """Рубеж сессии совпадает с суточным лимитом — честная сдача не
        упирается в отказ."""
        from app.pool import batch, ingest
        assert batch.SESSION_CAP == ingest.DAILY_LIMIT == 100


# ======================================================================
# 7. Согласие 152-ФЗ — до отправки, чек при этом не теряется
# ======================================================================
class TestConsentBeforeSend:
    def test_site_does_not_send_without_consent(self):
        js = _read(APP_JS)
        handle = _js_block(js, "const pbHandle = async (text, parsed, fromCamera) =>")
        gate = handle.split("if (offerta && !offerta.checked) {", 1)[1]
        gate = gate.split("pbEnsure();", 1)[0]
        assert "return;" in gate, "без согласия выполнение идёт дальше"
        assert "poolApi(" not in gate

    def test_scanned_receipt_is_parked_not_lost(self):
        """Чек, прочитанный до отметки согласия, паркуется в поле: донор видит
        реквизиты и что осталось сделать."""
        js = _read(APP_JS)
        assert "pbPark(raw, parsed)" in js
        park = _js_block(js, "const pbPark = (text, p) =>")
        assert "QR отсканирован камерой: ФН " in park
        assert "Осталось отметить оферту и отправить." in park

    def test_field_submit_warns_about_consent(self):
        js = _read(APP_JS)
        handle = _js_block(js, "const pbHandle = async (text, parsed, fromCamera) =>")
        assert "Отметьте согласие с офертой — без него чек принять нельзя" in handle

    def test_max_consent_gate_untouched(self):
        """В MAX согласие спрашивается карточкой до сдачи — механизм не менялся."""
        html = _read(MAX_HTML)
        assert "btn-consent-toggle" in html


# ======================================================================
# 8. Сервер не менялся: те же конечные точки и тот же дедуп
# ======================================================================
class TestServerSideUnchanged:
    def test_public_batch_status_answers_guest(self, client):
        r = client.post("/api/v1/public/pool/batch-status", json={"keys": []})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["items"] == []
        assert d["session_cap"] == 100
        assert d["limit"] == 100
        assert set(d["counts"]) == {"ok", "moderation", "bad", "points"}

    def test_max_batch_status_needs_token(self, client):
        r = client.post("/api/v1/maxapp/batch-status", json={"keys": []})
        assert r.status_code == 401, r.text

    def test_batch_service_is_read_only(self):
        """Сервис сессии только читает — приём остался в ingest."""
        src = _read("app/pool/batch.py")
        assert "def statuses(" in src
        assert "def count_by_status(" in src
        assert "db.add(" not in src and "db.delete(" not in src

    def test_dedup_wording_untouched(self):
        """Тексты дубликата — существующий механизм, их не переписывали."""
        js = _read(APP_JS)
        assert "Такой чек уже есть в базе — не принят" in js
        mx = _read(MAX_JS)
        assert "уже есть в базе" in mx or "Этот чек уже в пуле" in mx

    def test_no_new_endpoints(self):
        """Объединение клиентское: новых маршрутов не появилось."""
        pub = _read("app/pool/router_public.py")
        mx = _read("app/routers/maxapp.py")
        assert pub.count('@router.post("/batch-status"') == 1
        assert mx.count('@router.post("/batch-status"') == 1
        assert pub.count('@router.post("/check"') == 1


# ======================================================================
# 9. Ничего постороннего не сломано
# ======================================================================
class TestNothingElseBroken:
    def test_brand_palette_untouched(self):
        css = _read(CSS)
        head = css.split(":root", 2)[1]
        for token in ("--accent", "--info", "--bad", "--ok"):
            assert token in head, token

    def test_counter_classes_still_there(self):
        css = _read(CSS)
        for needle in (".batch-panel", ".batch-counts", ".bcount", ".brow",
                       ".batch-cam-counts", ".fly-chip"):
            assert needle in css, needle

    def test_max_markup_accessibility(self):
        html = _read(MAX_HTML)
        for bid in ("btn-scan", "btn-batch-stop", "btn-batch-more",
                    "btn-batch-finish"):
            i = html.index(f'id="{bid}"')
            tag = html[html.rindex("<button", 0, i):html.index(">", i)]
            assert 'type="button"' in tag, bid
        assert 'aria-live="polite"' in html

    def test_site_markup_accessibility(self):
        js = _read(APP_JS)
        for bid in ("pub-cam", "pub-batch-stop", "pub-batch-hide", "poolcam-done"):
            i = js.index(f'id="{bid}"')
            tag = js[js.rindex("<button", 0, i):js.index(">", i)]
            assert 'type="button"' in tag, bid
        assert 'role="status" aria-live="polite"' in js

    def test_no_dead_second_mode_leftovers(self):
        """Ни одного следа второго режима: ни кнопок, ни функций, ни текстов."""
        js = _read(APP_JS)
        mx = _read(MAX_JS)
        html = _read(MAX_HTML)
        for hay, needle in ((js, "pub-batch-start"), (js, "openPoolCameraBatch"),
                            (js, "camPick"), (html, 'id="btn-batch"'),
                            (mx, "$('btn-batch')")):
            assert needle not in hay, needle

    def test_release_record(self):
        assert "## [1.70.42]" in _read("CHANGELOG.md")
        assert "## v1.70.42 —" in _read("docs/ui_journal.md")
        assert (ROOT / "app/pool/batch.py").is_file()


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
