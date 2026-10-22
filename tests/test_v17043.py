# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.43: прилёт чека в свою фазу виден после
# сканирования, в том числе в окне камеры.
#
# Требование владельца: «Добавь анимацию прилета чека в ту или иную фазу
# после сканирования, проверь всё на ошибки по тестам».
#
# Что оказалось не так (найдено разбором, а не на глаз):
#   анимация «чек улетает в пул» существовала с v1.70.40, но при
#   сканировании камерой человек её не видел. Значок рисовался в теле
#   страницы на слое 75, а окно камеры — модальное, слой 80: полёт шёл ПОД
#   окном. Целью были плитки счётчиков формы, которые в этот момент тоже
#   закрыты окном. В мини-приложении MAX источник полёта — кнопка
#   сканирования, а при открытой запасной камере она скрыта: скрытый
#   элемент возвращает нулевой прямоугольник, и значок летел из левого
#   верхнего угла экрана.
#
# Решение (подробно — docs/ui_journal.md, раздел v1.70.43):
#   1) ОДНА механика прилёта: pulsePhase + flyChipTo, ею пользуются окно
#      камеры, форма донора и кабинет сотрудника — третьего рисунка полёта
#      не появилось, два прежних дубля убраны;
#   2) полёт может идти ВНУТРИ окна (host): окно камеры position: relative,
#      значок в нём position: absolute — слой 75 у .fly-chip не менялся,
#      замок v1.70.40 «ниже модальных окон и тостов» цел;
#   3) строка счётчиков окна камеры стала тремя фазами фирменными цветами,
#      и каждая фаза — цель прилёта (оранжевый «принят», синий «на
#      модерации», терракота «не принят»);
#   4) в MAX источник и цель выбираются по факту открытой камеры: видео и
#      фаза в строке счётчиков камеры, иначе кнопка и плитки карточки;
#   5) настройка «меньше движения» уважается: фаза вспыхивает, полёт не
#      рисуется — смысл «куда попал чек» сохраняется и без анимации.
#
# Сервер не менялся: фазы по-прежнему считаются из статусов чека
# (verified → принято, pending → на модерации, rejected → не принято).
#
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
import re
from pathlib import Path

from app.pool.batch import count_by_status, norm_key

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/static/js/app.js"
MAX_JS = ROOT / "app/static/maxapp/maxapp.js"
MAX_HTML = ROOT / "app/static/maxapp/index.html"
CSS = ROOT / "app/static/css/app.css"


def _read(path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p.read_text(encoding="utf-8")


def _js_block(src: str, marker: str) -> str:
    """Вырезать функцию по первой фигурной скобке (со счётом вложенности)."""
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


def _rule(css: str, selector: str) -> str:
    """Тело первого правила с таким селектором.

    Пробел перед скобкой не нормирован: в стилях MAX правила выровнены
    колонками (`.b-ok   {`), поэтому ищем регулярным выражением.
    """
    m = re.search(re.escape(selector) + r"\s*\{", css)
    assert m, f"в стилях нет правила {selector}"
    return css[m.end():].split("}", 1)[0]


# ======================================================================
# 1. Ритуал выпуска
# ======================================================================
class TestRelease17043:
    VERSION = "1.70.43"

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
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.42': [", 1)[0]
        assert block.count("{ icon:") == 4
        for part in ("фаз", "камер", "цвета", "меньше движения"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_whats_new_has_no_foreign_letters(self):
        """В прозе для донора — только русские буквы и имена MAX, QR."""
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.42': [", 1)[0]
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", block)
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), f"постороннее письмо {ch!r}"
        allowed = {"MAX", "QR"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read("CHANGELOG.md")
        assert f"## [{self.VERSION}]" in text
        sec = text.split(f"## [{self.VERSION}]", 1)[1].split("## [1.70.42]", 1)[0]
        for needle in ("flyChipTo", "pulsePhase", "in-host", "poolcam-seg",
                       "меньше движения", "75"):
            assert needle in sec, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        j = _read("docs/ui_journal.md")
        assert f"## v{self.VERSION} —" in j, "в журнале нет раздела выпуска"
        sec = j.split(f"## v{self.VERSION} —", 1)[1].split("## v1.70.42 —", 1)[0]
        for needle in ("прилет", "модальн", "flyChipTo", "camSlot", "cam-seg",
                       "Отказ", "Решение"):
            assert needle in sec, f"в журнале нет «{needle}»"

    def test_previous_release_stays_in_history(self):
        """Прокат версии не стирает запись предыдущего выпуска."""
        assert "'1.70.42': [" in _read(APP_JS)
        assert "## [1.70.42]" in _read("CHANGELOG.md")
        assert "## v1.70.42 —" in _read("docs/ui_journal.md")


# ======================================================================
# 2. ОДНА механика прилёта — без третьего рисунка полёта
# ======================================================================
class TestOneFlyMechanic:
    def test_shared_helpers_exist(self):
        js = _read(APP_JS)
        assert "function pulsePhase(el) {" in js
        assert "function flyChipTo(state, srcEl, targetEl, hostEl) {" in js
        assert "function camCountsHTML(c) {" in js

    def test_chip_is_created_in_one_place(self):
        """Значок создаётся и удаляется ровно один раз на весь сайт."""
        js = _read(APP_JS)
        assert js.count("appendChild(chipEl)") == 1
        assert js.count("createElement('div')") >= 1
        fly = _js_block(js, "function flyChipTo(")
        assert "(hostEl || document.body).appendChild(chipEl);" in fly
        assert "chipEl.remove();" in fly

    def test_pulse_is_written_once(self):
        """Перезапуск импульса — только в pulsePhase: дублей механики нет."""
        js = _read(APP_JS)
        assert js.count("classList.remove('pulse')") == 1
        pulse = _js_block(js, "function pulsePhase(")
        assert "void el.offsetWidth;" in pulse
        assert "el.classList.add('pulse');" in pulse

    def test_both_surfaces_use_the_shared_helper(self):
        js = _read(APP_JS)
        pub = js.split("const pbFly = (state) => {", 1)[1].split("};", 1)[0]
        assert "flyChipTo(" in pub, "форма донора рисует свой полёт"
        emp = _js_block(js, "function scanSessFly(")
        assert "flyChipTo(state, src, target, null);" in emp
        assert "getBoundingClientRect" not in emp, "кабинет снова рисует полёт сам"

    def test_max_has_its_own_single_mechanic(self):
        """MAX — отдельное приложение: своя копия помощников, но тоже одна."""
        js = _read(MAX_JS)
        assert "function pulsePhase(el) {" in js
        assert "function camCountsHTML(c) {" in js
        assert js.count("appendChild(chip)") == 1
        assert js.count("classList.remove('pulse')") == 1


# ======================================================================
# 3. Прилёт виден во время сканирования камерой
# ======================================================================
class TestArrivalInsideCameraWindow:
    def test_session_remembers_the_camera_window(self):
        js = _read(APP_JS)
        assert "camSlot: null," in js
        start = _js_block(js, "const pbStart = (resume) =>")
        assert "PB.camSlot = cam.slot;" in start
        stop = _js_block(js, "const pbStop = (reason) =>")
        assert "PB.camSlot = null;" in stop

    def test_window_is_forgotten_at_the_threshold(self):
        """На рубеже 100 чеков окно закрывается — и цель полёта с ним."""
        js = _read(APP_JS)
        handle = _js_block(js, "const pbHandle = async (text, parsed, fromCamera) => {")
        assert "PB.camSlot = null;" in handle

    def test_flight_targets_the_phase_inside_the_window(self):
        js = _read(APP_JS)
        fly = js.split("const pbFly = (state) => {", 1)[1].split("  };", 1)[0]
        assert "const slot = PB.camSlot;" in fly
        assert "slot.querySelector(`#poolcam-seg-${id}`)" in fly
        assert "flyChipTo(state, slot.querySelector('#poolcam-video') || slot, seg, slot);" in fly
        # плитка счётчика в форме мигает вместе с фазой в окне
        assert "pulsePhase(formTarget);" in fly
        # а когда окна нет — прежний полёт по странице к плитке формы
        assert "flyChipTo(state, $p('pub-cam'), formTarget, null);" in fly

    def test_host_moves_the_chip_into_the_window_layer(self):
        """Внутри окна значок абсолютно позиционирован и не всплывает выше
        модальных окон: замок слоя 70 < z < 80 у .fly-chip остаётся в силе."""
        js = _read(APP_JS)
        fly = _js_block(js, "function flyChipTo(")
        assert "if (hostEl) chipEl.classList.add('in-host');" in fly
        assert "const base = hostEl ? hostEl.getBoundingClientRect() : { left: 0, top: 0 };" in fly
        # координаты пересчитываются от окна, иначе значок уедет за его край
        assert "- base.left" in fly and "- base.top" in fly
        css = _read(CSS)
        rule = _rule(css, ".fly-chip.in-host")
        assert "position: absolute" in rule
        m = re.search(r"z-index:\s*(\d+)", rule)
        assert m and int(m.group(1)) < 80, "слой значка в окне перекрывает модальные"

    def test_outer_chip_layer_untouched(self):
        """Слой значка на странице не менялся — замок v1.70.40 цел."""
        css = _read(CSS)
        m = re.search(r"z-index:\s*(\d+)", _rule(css, ".fly-chip"))
        assert m and 70 < int(m.group(1)) < 80

    def test_camera_window_shows_three_phases(self):
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        assert 'id="poolcam-counts"' in fn
        assert 'class="batch-cam-counts"' in fn
        assert 'role="status" aria-live="polite"' in fn
        # разметка фаз одна и та же при открытии окна и при отрисовке
        assert "camCountsHTML({ ok: 0, pend: 0, bad: 0 })" in fn
        render = js.split("const pbRender = () => {", 1)[1].split("  };", 1)[0]
        assert "PB.camCounts.innerHTML = camCountsHTML(c);" in render

    def test_phase_ids_are_unique_and_named_by_state(self):
        js = _read(APP_JS)
        fn = _js_block(js, "function camCountsHTML(")
        for state in ("ok", "pend", "bad"):
            assert f'id="poolcam-seg-{state}"' in fn
        # числа целые и свои: разметка не принимает пользовательский текст
        assert "${c.ok}" in fn and "${c.pend}" in fn and "${c.bad}" in fn
        assert "innerHTML" not in fn


# ======================================================================
# 4. Цвета фаз — фирменные, один смысл = один цвет
# ======================================================================
class TestPhaseColours:
    def test_camera_line_uses_brand_tokens(self):
        css = _read(CSS)
        assert "var(--accent-text)" in _rule(css, ".batch-cam-counts .seg-ok")
        assert "var(--info-text)" in _rule(css, ".batch-cam-counts .seg-pend")
        assert "var(--bad-text)" in _rule(css, ".batch-cam-counts .seg-bad")

    def test_camera_line_matches_counter_tiles(self):
        """Строка окна камеры и плитки формы окрашены одними токенами."""
        css = _read(CSS)
        for state, token in (("ok", "--accent-text"), ("pend", "--info-text"),
                             ("bad", "--bad-text")):
            tile = _rule(css, f".bcount.b-{state}")
            seg = _rule(css, f".batch-cam-counts .seg-{state}")
            assert f"var({token})" in tile, state
            assert f"var({token})" in seg, state

    def test_chip_colours_untouched(self):
        css = _read(CSS)
        assert "var(--accent-text)" in _rule(css, ".fly-chip.s-ok")
        assert "var(--info-text)" in _rule(css, ".fly-chip.s-pend")
        assert "var(--bad-text)" in _rule(css, ".fly-chip.s-bad")

    def test_no_new_hex_in_the_batch_block(self):
        css = _read(CSS)
        block = css[css.index(".batch-panel"):css.index("@keyframes batchIn")]
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", block)

    def test_max_segments_match_max_chip(self):
        """В MAX цвета фаз строки камеры совпадают с цветами значка."""
        html = _read(MAX_HTML)
        for state in ("ok", "pend", "bad"):
            seg = _rule(html, f".cam-counts .seg-{state}")
            chip = _rule(html, f".fly-chip.s-{state}")
            m1 = re.search(r"#[0-9a-fA-F]{6}", seg)
            m2 = re.search(r"#[0-9a-fA-F]{6}", chip)
            assert m1 and m2 and m1.group(0) == m2.group(0), state

    def test_max_segments_match_counter_tiles(self):
        html = _read(MAX_HTML)
        for state, tile in (("ok", ".b-ok"), ("pend", ".b-pend"), ("bad", ".b-bad")):
            seg = re.search(r"#[0-9a-fA-F]{6}", _rule(html, f".cam-counts .seg-{state}"))
            body = _rule(html, tile)
            assert seg and seg.group(0) in body, state


# ======================================================================
# 5. Настройка «меньше движения»: смысл остаётся, полёт не рисуется
# ======================================================================
class TestReducedMotion:
    def test_pulse_happens_before_the_motion_check(self):
        """Фаза вспыхивает всегда — иначе при «меньше движения» человек не
        увидел бы, куда попал чек."""
        js = _read(APP_JS)
        fly = _js_block(js, "function flyChipTo(")
        assert fly.index("pulsePhase(targetEl);") < fly.index("prefers-reduced-motion")
        assert "if (reduced) return;" in fly

    def test_motion_lookup_is_guarded(self):
        js = _read(APP_JS)
        fly = _js_block(js, "function flyChipTo(")
        assert "} catch (e) { reduced = false; }" in fly

    def test_css_damps_every_animation(self):
        css = _read(CSS)
        assert "@media (prefers-reduced-motion: reduce) {" in css
        assert "animation-duration: .01ms !important" in css

    def test_max_damps_the_new_pulse_explicitly(self):
        html = _read(MAX_HTML)
        block = html.split("@media (prefers-reduced-motion: reduce) {", 1)[1]
        block = block.split("}", 1)[0]
        assert ".cam-counts .seg.pulse" in block
        assert ".fly-chip" in block

    def test_max_flight_also_checks_the_setting(self):
        js = _read(MAX_JS)
        fly = _js_block(js, "function batchFly(")
        assert fly.index("pulsePhase(target);") < fly.index("prefers-reduced-motion")
        assert "if (reduced) return;" in fly


# ======================================================================
# 6. MAX: источник и цель — по факту открытой камеры
# ======================================================================
class TestMaxArrival:
    def test_fallback_camera_is_detected(self):
        js = _read(MAX_JS)
        fly = _js_block(js, "function batchFly(")
        assert "const camOn = !!$('cam-v');" in fly
        assert "const seg = camOn ? $('cam-seg-' + id) : null;" in fly
        assert "const target = seg || $('bc-' + id);" in fly

    def test_source_is_the_video_not_the_hidden_button(self):
        """Скрытая кнопка даёт нулевой прямоугольник — значок летел из угла."""
        js = _read(MAX_JS)
        fly = _js_block(js, "function batchFly(")
        assert "const src = (camOn ? $('cam-v') : $('btn-scan')) || target;" in fly

    def test_card_tile_blinks_together_with_the_camera_phase(self):
        js = _read(MAX_JS)
        fly = _js_block(js, "function batchFly(")
        assert "if (seg) pulsePhase($('bc-' + id));" in fly

    def test_camera_line_is_rendered_from_the_same_helper(self):
        js = _read(MAX_JS)
        render = _js_block(js, "function batchRender(")
        assert "cam.innerHTML = camCountsHTML(c);" in render
        open_cam = _js_block(js, "async function batchCamOpen(")
        assert "camCountsHTML({ok: 0, pend: 0, bad: 0})" in open_cam

    def test_phase_ids_are_unique_per_state(self):
        js = _read(MAX_JS)
        fn = _js_block(js, "function camCountsHTML(")
        for state in ("ok", "pend", "bad"):
            assert f"id=\"cam-seg-{state}\"" in fn

    def test_no_template_literals_in_max(self):
        """Файл MAX остаётся в своём стиле: шаблонных строк не появилось."""
        assert "`" not in _read(MAX_JS)


# ======================================================================
# 7. Сервер не менялся: фазы считаются из статусов чека
# ======================================================================
class TestServerPhasesUnchanged:
    def test_statuses_map_to_the_three_phases(self):
        items = [
            {"status": "verified", "points": 1},
            {"status": "verified", "points": 1},
            {"status": "pending", "points": 0},
            {"status": "rejected", "points": 0},
        ]
        assert count_by_status(items) == {"ok": 2, "moderation": 1, "bad": 1,
                                         "points": 2}

    def test_unknown_status_is_not_counted_as_accepted(self):
        assert count_by_status([{"status": "что-то новое"}]) == {
            "ok": 0, "moderation": 0, "bad": 0, "points": 0}

    def test_client_state_names_match_server_statuses(self):
        """Клиент красит фазы по тем же трём статусам, что отдаёт сервер."""
        js = _read(APP_JS)
        fn = _js_block(js, "function pubBatchState(")
        assert "'ok'" in fn and "'pend'" in fn and "'bad'" in fn
        assert "verified" in fn and "pending" in fn and "rejected" in fn

    def test_dedup_key_is_the_same_triple(self):
        assert norm_key(" 1 ", "2", "3") == "1|2|3"


# ======================================================================
# 8. Ничего остального не сломано
# ======================================================================
class TestNothingElseBroken:
    def test_add_still_renders_then_flies(self):
        """Сначала отрисовка (фаза появляется в разметке), потом прилёт."""
        js = _read(APP_JS)
        assert "const pbAdd = (item) => { PB.items.push(item); pbRender(); pbFly(item.state); };" in js
        maxjs = _read(MAX_JS)
        add = _js_block(maxjs, "function batchAdd(")
        assert add.index("batchRender();") < add.index("batchFly(item.state);")

    def test_status_update_flies_too(self):
        """Уточнение статуса источником тоже показывает прилёт в фазу."""
        js = _read(APP_JS)
        poll = _js_block(js, "const pbPoll = async () => {")
        assert "pbFly(st);" in poll

    def test_camera_window_contract_intact(self):
        """Единый поток из v1.70.42 цел: одна камера, непрерывность, «Готово»."""
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        assert "function openPoolCameraScan(onText) {" in js
        assert js.count("openPoolCameraBatch") == 0
        assert 'id="poolcam-done"' in fn
        assert "cam.stop()" in fn
        assert "typeof handle.onFinish === 'function'" in fn

    def test_one_button_per_surface(self):
        html = _read(MAX_HTML)
        assert html.count('id="btn-scan"') == 1
        assert 'id="btn-batch"' not in html
        js = _read(APP_JS)
        assert js.count("pub-batch-start") == 0

    def test_brand_palette_untouched(self):
        css = _read(CSS)
        head = css.split(":root", 2)[1]
        for token in ("--accent", "--info", "--bad", "--ok"):
            assert token in head, token

    def test_batch_css_classes_still_there(self):
        css = _read(CSS)
        for needle in (".batch-panel", ".batch-counts", ".bcount", ".brow",
                       ".batch-cam-counts", ".fly-chip", "@keyframes batchFly",
                       "@keyframes batchPulse"):
            assert needle in css, needle

    def test_camera_line_stays_one_line(self):
        """«Не перегружено»: в окне камеры одна строка фаз, а не три плитки."""
        js = _read(APP_JS)
        fn = _js_block(js, "function openPoolCameraScan(")
        assert fn.count('class="batch-cam-counts"') == 1
        assert fn.count("bcount") == 0
