# ======================================================================
# Ямастер Чек — тесты v1.24.0: кнопка «Напечатать чек» (нарезка А4
# на 3 колонки с позиционным переносом) и АО-1: поля страницы
# 20/15/10/10 мм, полные данные для налоговой.
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import os
import shutil
import subprocess
import tempfile

JS = "app/static/js/app.js"
CSS = "app/static/css/app.css"
PACK = "app/static/js/printpack.js"


def _node(code: str, tmp: str = "") -> str:
    """Запуск node-скрипта (ES-модуль): printpack.js импортируется по абсолютному пути."""
    node = shutil.which("node")
    if not node:
        return ""
    pack_abs = os.path.abspath(PACK).replace("\\", "/")
    code = code.replace("__PACK__", "file://" + pack_abs)
    path = os.path.join(tempfile.mkdtemp(), "t.mjs")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    r = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# ======================================================================
# Нарезка чеков: геометрия 3 колонок и укладка (printpack.js v2)
# ======================================================================
class TestPrintCutGeometry:
    def test_grid_is_a4_3cols(self):
        out = _node(f"""
import {{ PAGE_W, COL_W, MARGIN_X, COL_GAP, COLS, COL_H, SCALE, RCPT_W }}
  from '__PACK__';
const total = MARGIN_X * 2 + COL_W * 3 + COL_GAP * 2;
console.log([total === PAGE_W, COLS === 3, COL_H === 281,
             Math.abs(SCALE - 62 / 80) < 1e-9, RCPT_W === 80].map(x => x ? 1 : 0).join(''));
""")
        if not out:
            import pytest
            pytest.skip("node недоступен")
        assert out == "11111", "сетка листа: 3 колонки точно на ширине А4"

    def test_packcut_fills_columns_dense(self):
        out = _node(f"""
import {{ packCut, COLS }} from '__PACK__';
const pieces = [100, 100, 100, 50, 50, 200, 81];
const pages = packCut(pieces);
const flat = pages.flat();
// все куски размещены, колонок не больше COLS, y внутри колонки
const ok = flat.length === pieces.length &&
  pages.every(p => p.every(x => x.col >= 0 && x.col < COLS && x.y >= 0));
const onePage = pages.length === 1 ? 1 : 0;
console.log([ok ? 1 : 0, onePage].join(''));
""")
        assert out == "11", "7 кусков по 100/50/200/81 мм должны лечь на 1 лист в 3 колонки"

    def test_packcut_order_and_oversize(self):
        out = _node(f"""
import {{ packCut, COL_H }} from '__PACK__';
// порядок внутри страницы: y возрастает в каждой колонке
const pages = packCut([120, 30, 120, 120]);
let ordered = true;
for (const page of pages) {{
  const perCol = {{}};
  for (const pl of page) {{
    perCol[pl.col] = perCol[pl.col] || [];
    perCol[pl.col].push(pl.y);
  }}
  for (const ys of Object.values(perCol))
    for (let i = 1; i < ys.length; i++) if (ys[i] < ys[i - 1]) ordered = false;
}}
let threw = 0;
try {{ packCut([COL_H + 1]); }} catch {{ threw = 1; }}
console.log([ordered ? 1 : 0, threw].join(''));
""")
        assert out == "11", "порядок в колонке не нарушается; кусок выше колонки — ошибка"

    def test_split_blocks_positional(self):
        out = _node(f"""
import {{ splitBlocks, COL_H }} from '__PACK__';
// 40 позиций по 12 мм + шапка: нарезка только по границам блоков
const blocks = [10, 4, ...Array(40).fill(12), 8];
const parts = splitBlocks(blocks);
const covered = parts.every(([a, b]) => a < b) &&
  parts[0][0] === 0 && parts[parts.length - 1][1] === blocks.length;
const sequential = parts.every((p, i) => i === 0 || parts[i - 1][1] === p[0]);
console.log([covered ? 1 : 0, sequential ? 1 : 0, parts.length > 1 ? 1 : 0].join(''));
""")
        assert out == "111", "позиционный перенос: части стыкуются без пропусков и повторов"

    def test_ui_uses_new_pack(self):
        js = open(JS, encoding="utf-8").read()
        assert "SCALE, RCPT_W" in js and "hFull *= SCALE" in js
        assert "× 3 колонки" in js or "3 колонки" in js

    def test_button_renamed(self):
        js = open(JS, encoding="utf-8").read()
        assert "🖨 Напечатать чек</button>" in js
        assert "🖨 Напечатать чек (выбранные)" in js
        # старое имя убрано с кнопок (в истории WHATS_NEW v1.7.0 упоминание остаётся)
        assert '<button class="btn btn-sm" id="btn-print-pdf">🖨 Печать PDF' not in js
        assert "Печать PDF (выбранные)" not in js
        assert 'id="btn-print-pdf"' in js                  # id сохранён (тесты v1.7.0)


# ======================================================================
# АО-1: поля страницы 20/15/10/10 и полнота данных для налоговой
# ======================================================================
class TestAO1PageAndTax:
    def test_page_margins_2015_10_10(self):
        js = open(JS, encoding="utf-8").read()
        # CSS-порядок: top right bottom left → лево 20, право 15, верх 10, низ 10
        assert "@page { size: A4 portrait; margin: 10mm 15mm 10mm 20mm; }" in js
        assert "setPrintPageMargin" in js
        css = open(CSS, encoding="utf-8").read()
        assert "body.print-ao1 .print-page" in css

    def test_receipts_sheet_margin_zero(self):
        js = open(JS, encoding="utf-8").read()
        assert "@page { size: A4 portrait; margin: 0; }" in js

    def test_back_table_full_requisites(self):
        js = open(JS, encoding="utf-8").read()
        # ФН и ФП в оборотной таблице + суммы (не коды счетов) в трёх суммовых колонках
        assert "ФН ${esc(r.fn" in js and "ФП ${esc(r.fp" in js
        i0 = js.index("const rowsBack")
        seg = js[i0:js.index("let root =", i0)]
        assert seg.count('${sum(r.total_sum)}') == 3, "по отчёту / принято к учёту / сумма по чеку"
        assert 'Дебет счёта' in js  # колонка шапки оборотной таблицы
        # дата составления под подписью подотчётного лица
        assert "Дата составления" in js

    def test_tax_required_fields_present(self):
        js = open(JS, encoding="utf-8").read()
        required = [
            '0302001',            # форма по ОКУД
            'по ОКПО',            # код ОКПО организации
            'УТВЕРЖДАЮ',          # гриф утверждения руководителем
            'Подотчётное лицо',   # ФИО, табельный, должность
            'Назначение аванса',
            'Итого получено',     # расчёты: аванс/израсходовано/остаток/перерасход
            'Израсходовано',
            'Перерасход',
            'Бухгалтерская запись',   # дебет/кредит счёта
            'Приложение',         # количество документов и листов
            'Отчёт проверен',     # сумма к утверждению + прописью
            'Расписка',           # расписка бухгалтера
            'Главный бухгалтер',
            'Оборотная сторона',
        ]
        missing = [x for x in required if x not in js]
        assert not missing, f"нет обязательных полей АО-1: {missing}"
        # сумма прописью
        assert "function ruMoney" in js and "руб" in js

    def test_rko_and_payment_fields(self):
        js = open(JS, encoding="utf-8").read()
        assert 'id="ao-rko"' in js and 'id="ao-pay"' in js          # модалка
        assert "РКО ${esc(meta['ao-rko'])}" in js                   # печатная форма
        assert "РКО: meta['ao-rko']" in js                          # JSON для 1С
        assert "ПлатёжноеПоручение: meta['ao-pay']" in js
        d = open("docs/ao1-1c.md", encoding="utf-8").read()
        assert "РКО" in d and "ПлатёжноеПоручение" in d


# ======================================================================
# Версии синхронно 1.24.0
# ======================================================================
class TestVersion1240:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.24.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_and_changelog(self):
        js = open(JS, encoding="utf-8").read()
        assert "'1.24.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.24.0]" in ch
