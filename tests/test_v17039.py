# -*- coding: utf-8 -*-
# Ямастер Чек — тесты v1.70.39: сканер MAX отдаёт объект, а не строку.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Дефект, о котором сообщил владелец: в мини-приложении MAX камера находит
# QR-чек, но данные не заносятся — «Не похоже на чек: В QR-коде нет
# обязательных реквизитов чека (fn, i/фд, fp/фпд)». При этом на сайте
# сканирование работает.
#
# Причина: нативный сканер MAX `WebApp.openCodeReader()` возвращает ОБЪЕКТ
# { value: «текст QR» }, а не строку. Документация MAX SDK:
#     const { value } = await openCodeReader()
# тип моста в сторонних обвязках: Promise<{value?: unknown}>; сам
# max-web-app.js отправляет хосту запрос WebAppOpenCodeReader и отдаёт его
# ответ как есть. Код оборачивал результат в String(qr), поэтому на сервер
# уходило «[object Object]» — 15 символов, проверку длины они проходили, и
# парсер честно отвечал, что реквизитов нет.
#
# Критерии приёмки:
#  - текст QR извлекается из любой формы ответа моста (объект, строка,
#    вложенный ответ), а при отсутствии текста ничего не уходит на сервер;
#  - на сервер больше не может попасть «[object Object]» ни из одной точки
#    вызова (нативный сканер, своя камера, поле ввода);
#  - сервер распознаёт объект вместо строки и говорит об этом словами,
#    которые указывают на версию клиента, а не на «нет реквизитов»;
#  - сканирование на сайте не затронуто (требование «не сломай программу»);
#  - замки v1.69.0 (сканер MAX — первый выбор) и v1.70.7 (своя камера —
#    запасной путь) остались целы.
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAXAPP_JS = "app/static/maxapp/maxapp.js"

QR = ("t=20260115T101530&s=1250.50&fn=9900000000016902"
      "&i=12345&fp=3673437411&n=1")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _js_function(src: str, header: str) -> str:
    """Вырезать функцию из JS по первой фигурной скобке (со счётом вложенности)."""
    start = src.index(header)
    i = src.index("{", start + len(header) - 1)
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[start:k + 1]
    raise AssertionError("не нашлась закрывающая скобка")


def _node():
    return shutil.which("node") or shutil.which("nodejs")


# ------------------------------------------------------- извлечение текста
class TestQrFromScan:
    """Настоящая функция из maxapp.js прогоняется на всех формах ответа."""

    @pytest.fixture(scope="class")
    def runner(self, tmp_path_factory):
        node = _node()
        if not node:
            pytest.skip("node недоступен")
        src = _js_function(_read(MAXAPP_JS), "function qrFromScan(raw) {")
        # new Function — потому что eval в ES-модуле не создаёт связывания
        harness = src + """
const QR = process.argv[2];
const cases = [
  ['объект MAX {value}',      { value: QR },                 QR],
  ['строка из поля ввода',     QR,                            QR],
  ['строка с пробелами',      '  ' + QR + '\\n',               QR],
  ['вложенный ответ моста',    { result: { value: QR } },     QR],
  ['поле result',             { result: QR },                QR],
  ['поле data (jsQR)',        { data: QR },                  QR],
  ['поле text',               { text: QR },                  QR],
  ['поле code',               { code: QR },                  QR],
  ['поле qr',                 { qr: QR },                    QR],
  ['пустой объект',           {},                            ''],
  ['null',                    null,                          ''],
  ['undefined',               undefined,                     ''],
  ['число вместо значения',    { value: 123 },                ''],
  ['пробелы в value',         { value: '   ' },              ''],
  ['массив',                  [QR],                          ''],
  ['логическое значение',      true,                          ''],
];
let bad = 0;
for (const [name, input, want] of cases) {
  const got = qrFromScan(input);
  if (got !== want) { bad++; console.log('ПРОВАЛ ' + name + ': ' + JSON.stringify(got)); }
}
console.log(bad ? 'BAD=' + bad : 'OK=' + cases.length);
"""
        f = tmp_path_factory.mktemp("qr") / "qrfromscan.mjs"
        f.write_text(harness, encoding="utf-8")
        return node, str(f)

    def test_all_bridge_shapes_are_handled(self, runner):
        node, script = runner
        code = subprocess.run([node, script, QR], capture_output=True,
                              text=True, check=False, timeout=120)
        assert code.returncode == 0, (code.stdout + code.stderr)[:600]
        assert "ПРОВАЛ" not in code.stdout, code.stdout[:600]
        assert code.stdout.strip().endswith("OK=16"), code.stdout[-300:]

    def test_object_never_becomes_object_object(self, runner, tmp_path):
        """Главная проверка дефекта: String({value:…}) давал «[object Object]»."""
        node, script = runner
        tail = "console.log(bad ? 'BAD=' + bad : 'OK=' + cases.length);"
        src = Path(script).read_text(encoding="utf-8")
        assert src.count(tail) == 1
        probe = tmp_path / "probe.mjs"
        probe.write_text(src.replace(tail,
            "console.log('STR=' + String({ value: QR }).slice(0, 20));\n"
            "console.log('FN=' + qrFromScan({ value: QR }).slice(0, 20));", 1),
            encoding="utf-8")
        code = subprocess.run([node, str(probe), QR], capture_output=True,
                              text=True, check=False, timeout=120)
        out = code.stdout
        assert "STR=[object Object]" in out, out      # так было до правки
        assert "FN=t=20260115T101530" in out, out     # так стало


# ------------------------------------------------------- замки исходника
class TestMaxAppSource:
    def test_scanner_result_is_unwrapped(self):
        js = _read(MAXAPP_JS)
        block = _js_function(js, "async function scanQr() {")
        assert "qrFromScan(raw)" in block, "результат сканера не раскрывается"
        assert "submit(raw)" not in block, "объект по-прежнему уходит в submit"

    def test_submit_sends_text_only(self):
        js = _read(MAXAPP_JS)
        block = _js_function(js, "async function submit(qr) {")
        assert "String(qr)" not in block, "осталось оборачивание объекта в строку"
        assert "qrFromScan(qr)" in block
        assert "{qr: text," in block

    def test_empty_result_does_not_reach_server(self):
        """При пустом тексте — понятный совет и возврат, а не запрос."""
        js = _read(MAXAPP_JS)
        scan = _js_function(js, "async function scanQr() {")
        sub = _js_function(js, "async function submit(qr) {")
        assert scan.count("if (!text)") == 1 and "return;" in scan
        assert "if (!text)" in sub
        assert "не вернул текст" in scan or "Не удалось прочитать" in sub

    def test_max_scanner_stays_first_choice(self):
        """Замок v1.70.7: нативный сканер первым, своя камера — запасной путь."""
        js = _read(MAXAPP_JS)
        assert js.index("openCodeReader") < js.index("scanCamera")
        m = re.search(r"if \(!app \|\| !app\.openCodeReader\)[^}]*}", js, re.S)
        assert m and "scanCamera" in m.group(0)

    def test_camera_and_manual_input_still_work(self):
        """Своя камера (jsQR) и поле ввода отдают строку — их путь не сломан."""
        js = _read(MAXAPP_JS)
        assert "code.data" in js                     # jsQR → строка
        assert re.search(r"\$\('qr-input'\)\.value\.trim\(\)", js)


# ------------------------------------------------------- защита на сервере
class TestServerRecognisesObject:
    def test_parse_qr_names_the_real_cause(self):
        from app.services.qr import QRParseError, parse_qr
        for raw in ("[object Object]", "[object Promise]", "[Object]"):
            with pytest.raises(QRParseError) as exc:
                parse_qr(raw)
            assert "объект" in str(exc.value).lower()
            # главное: не валить всё на «нет реквизитов чека»
            assert "обязательных реквизитов" not in str(exc.value)

    def test_precheck_reports_bad_qr_with_clear_message(self):
        """Отказ называет настоящую причину: объект вместо текста QR.

        v1.70.39: тест сам включает Чек-Пул и восстанавливает прежнее
        значение. precheck_ingest проверяет pool_enabled ДО разбора QR,
        поэтому без подготовки ответом было «Пул выключен» (result=disabled),
        если предыдущий тест пул выключил — та же порядкозависимость, что
        устранена в test_v1670 для вебхуков.
        """
        from app.database import SessionLocal
        from app.pool import ingest
        from app.services import appsettings

        def _pool(value=None):
            sess = SessionLocal()
            try:
                if value is None:
                    return appsettings.get_setting(sess, "pool_enabled", "") or ""
                appsettings.set_setting(sess, "pool_enabled", value)
                sess.commit()
                return None
            finally:
                sess.rollback(); sess.close()

        def _precheck(raw):
            db = SessionLocal()
            try:
                return ingest.precheck_ingest(db, raw, None)
            finally:
                db.close()

        prev = _pool()
        _pool("1")
        try:
            parsed, err = _precheck("[object Object]")
            assert parsed is None and err is not None
            assert err["result"] == "bad_qr"
            assert err["points"] == 0
            assert "объект" in err["message"].lower()

            # приоритет проверок: выключенный пул отвечает раньше разбора QR
            _pool("0")
            parsed2, err2 = _precheck(QR)
            assert parsed2 is None and err2["result"] == "disabled"
        finally:
            _pool(prev)

    def test_normal_qr_is_still_parsed(self):
        """Регресс: правильный QR разбирается так же, как до правки."""
        from app.services.qr import parse_qr
        parsed = parse_qr(QR)
        assert parsed.fn == "9900000000016902"
        assert parsed.fd == "12345"
        assert parsed.fp == "3673437411"
        assert abs(parsed.total_sum - 1250.50) < 0.005


# ------------------------------------------------------- сайт не затронут
class TestWebScanningUnaffected:
    """Требование владельца: на сайте сканирование работает «чётко и хорошо»."""

    def test_site_scan_accepts_qr(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/scan",
                        json={"qr_data": QR, "verify": False}, headers=hdr)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("duplicate") is False or "receipt" in body

    def test_site_scan_rejects_object_string_clearly(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/scan",
                        json={"qr_data": "[object Object]"}, headers=hdr)
        assert r.status_code == 422, r.text[:200]
        assert "объект" in r.text.lower()

    def test_web_client_parser_untouched(self):
        js = _read("app/static/js/app.js")
        assert "parseQrClient" in js
        assert "qr_data: text" in js


# ------------------------------------------------- независимость от порядка
class TestOrderIndependence:
    """Замки на починку плавающих падений (v1.70.39).

    Сеансовая база прогона одна на все проверки, поэтому тест обязан готовить
    своё состояние сам и не зависеть от того, сколько данных накопилось.
    Эти замки не дают вернуть прежние приёмы.
    """

    def test_conftest_isolates_app_settings(self):
        """Фикстура снимка/восстановления настроек — на месте и autouse."""
        src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert "@pytest.fixture(autouse=True)" in src
        assert "def _isolate_app_settings():" in src
        # снимок до теста и восстановление после
        assert "_snapshot()" in src
        assert "AppSetting" in src
        # секретные значения сохраняются как есть (шифрование не затрагивается)
        assert "sess.delete(row)" in src or "sess.delete(row)" in src.replace(" ", "")

    def test_transfer_receipts_checked_from_employee(self):
        """Видимость после передачи проверяется от имени сотрудников, не админа."""
        src = (ROOT / "tests" / "test_admin_update.py").read_text(encoding="utf-8")
        body = src.split("def test_transfer_receipts", 1)[1].split("def ", 1)[0]
        assert "_scan(client, uha)" in body, "чек должен создавать сотрудник"
        assert 'headers=uhb' in body and 'headers=uha' in body
        assert 'params={"ids"' in body, "выборка конкретного чека, не первая страница"
        assert "не виден прежнему владельцу" in body

    def test_receipt_lookups_are_not_page_dependent(self):
        src = (ROOT / "tests" / "test_roles.py").read_text(encoding="utf-8")
        assert 'params={"q": "60001"}' in src, "поиск по ФД вместо первой страницы"

    def test_creator_ids_come_from_the_user(self):
        src = (ROOT / "tests" / "test_v2510.py").read_text(encoding="utf-8")
        assert 'client.get("/api/v1/auth/me", headers=U1).json()["id"]' in src
        assert "next(x[\"id\"] for x in cr if x[\"username\"]" not in src

    def test_webhook_test_prepares_url_itself(self):
        src = (ROOT / "tests" / "test_v1670.py").read_text(encoding="utf-8")
        body = src.split("def test_verify_flow_enqueues", 1)[1].split("def _verify_flow_body", 1)[0]
        assert 'webhook_url' in body and "_set_url(prev_url)" in body

    def test_bot_username_fallback_is_prepared(self):
        src = (ROOT / "tests" / "test_v1701.py").read_text(encoding="utf-8")
        body = src.split("def test_fallback_when_unknown", 1)[1].split("def ", 1)[0]
        assert 'appsettings.set_setting(s, "max_bot_username", "")' in body
        assert "_username_cache" in body

    def test_geo_coverage_counts_own_stream(self):
        src = (ROOT / "tests" / "test_v1330.py").read_text(encoding="utf-8")
        body = src.split("def test_coverage_criteria", 1)[1].split("class ", 1)[0]
        assert "pool_user_id=u.id" in body
        assert 'ov["geo_coverage"] >= 70' not in body

    def test_properties_do_not_filter_or_burn_entropy(self):
        """Причины плавающих FailedHealthCheck устранены, а не подавлены."""
        src = (ROOT / "tests" / "test_properties.py").read_text(encoding="utf-8")
        # отказ «слишком много отсеянного» не подавлен: подавлять можно только
        # медлительность (PBKDF2), а причину плавания — нет
        assert "filter_too_much" not in src
        assert "data_too_large" not in src
        # assume не импортируется — значит, и не используется для отсева
        imports = [ln for ln in src.splitlines()
                   if ln.startswith("from hypothesis import")]
        assert imports and "assume" not in imports[0], imports
        # энтропия примера не сжигается: нет случайного хвоста в 4000 символов
        assert "min_size=3990" not in src
        assert 'min_size=0, max_size=60' in src


# ------------------------------------------------------- ритуал выпуска
class TestRelease17039:
    """v1.70.40: выпуск 1.70.39 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведут test_v17033…17037
    и test_v17040, а здесь остаётся запись выпуска в истории (как сделано для
    1.70.38 в test_v17038). Требование «своя запись первая» по смыслу
    принадлежит текущему выпуску и проверяется в test_v17040.
    """

    VERSION = "1.70.39"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read("app/config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 39)

    def test_release_record_stays_in_history(self):
        """Запись выпуска не потерялась при последующих релизах."""
        assert f"## [{self.VERSION}]" in _read("CHANGELOG.md")
        assert f"'{self.VERSION}': [" in _read("app/static/js/app.js")
        assert f"## v{self.VERSION} —" in _read("docs/ui_journal.md")

    def test_scanner_fix_stays_in_code(self):
        """Исправление сканера MAX переживает следующие выпуски."""
        js = _read("app/static/maxapp/maxapp.js")
        assert "qrFromScan" in js, "функция раскрытия ответа сканера пропала"
        assert "[object" in _read("app/services/qr.py"), \
            "серверная защита от строки вместо объекта пропала"

    def test_whats_new_entry(self):
        js = _read("app/static/js/app.js")
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.38': [", 1)[0]
        assert block.count("{ icon:") >= 1
        for part in ("openCodeReader", "сканер", "MAX"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_changelog_entry(self):
        assert f"## [{self.VERSION}]" in _read("CHANGELOG.md")

    def test_journal_entry(self):
        j = _read("docs/ui_journal.md")
        assert f"## v{self.VERSION} —" in j, "в журнале нет раздела выпуска"
        sec = j.split(f"## v{self.VERSION} —", 1)[1].split("## Бэклог", 1)[0]
        for needle in ("openCodeReader", "[object Object]", "value"):
            assert needle in sec, f"в журнале нет «{needle}»"

    def test_manual_mentions_scanner_fix(self):
        """Инструкция MINI-приложения остаётся правдой о сканировании."""
        src = _read("app/services/manual_content.py")
        assert "сканер" in src.lower() or "сканирован" in src.lower()
