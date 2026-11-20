# -*- coding: utf-8 -*-
"""Замок выпуска 1.73.6: печатный бланк УПД сверен с образцами PDF из ЭДО.

Что закреплено:
 1. геометрия 16 граф и строка «Всего к оплате» — по линиям эталонного PDF;
 2. числа и ставки печатаются так же, как в PDF (точка в дробной части, «20/120» без %);
 3. подпись руководителя стоит на строке должности, подпись получателя берётся
    из второго подписанта XML;
 4. блок электронной подписи в бланк по-прежнему не попадает;
 5. точки синхронизации выпуска переведены на 1.73.6.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.primary import printupd, upd
from app.primary.models import PrimaryDocument
from app.services import updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.6"
PREV = "1.73.5"
PRIMARY_BLOCK = "Первичка (электронные УПД)"
# ширины, снятые с PDF из ЭДО по вертикальным линиям таблицы товаров
PDF_COLUMNS = (65.2, 16.6, 138.8, 36.7, 20.3, 44.2, 42.8, 38.3, 58.5, 40.5, 30.8, 55.5, 57.0,
               39.8, 39.7, 80.3)


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _doc(**kwargs) -> PrimaryDocument:
    doc = PrimaryDocument()
    doc.id = "d1"
    doc.kind = "УПД"
    doc.function = "СЧФДОП"
    doc.doc_number = "118050913"
    doc.doc_date = date(2026, 9, 7)
    doc.seller_name = "Интернет Решения, ООО"
    doc.buyer_name = 'ООО "ЯМАСТЕР"'
    for key, value in kwargs.items():
        setattr(doc, key, value)
    return doc


class TestGeometryMatchesPdf:
    def test_column_widths_are_taken_from_the_reference(self):
        """Ширины граф — с PDF (шаг 0.1 pt); сумма 805 pt — по границам рамки бланка."""
        assert printupd.COLUMN_WIDTHS == PDF_COLUMNS
        assert len(printupd.COLUMN_WIDTHS) == 16
        assert round(sum(printupd.COLUMN_WIDTHS), 1) == 805.0

    def test_table_and_headers_sit_where_the_pdf_puts_them(self):
        css = printupd.CSS
        assert "table.grid { width: 805pt;" in css          # таблица товаров внутри рамки 813.2
        assert ".hdr .hl { padding-left: 5pt; }" in css      # подписи шапки (2)–(8), как в PDF

    def test_blank_prints_on_a_single_page(self):
        """v1.73.6 (аудит): «A4 landscape» = 595.28 pt, а лист 594.96 pt — «М.П.» уезжало.

        Страница задаётся точными пунктами, а высота листа при печати чуть меньше листа,
        чтобы округления браузера не отправили хвост бланка на вторую страницу.
        """
        css = printupd.CSS
        assert "@page { size: 841.92pt 594.96pt; margin: 0; }" in css
        assert "size: A4 landscape" not in css
        assert "table.page { margin: 0; box-shadow: none; height: 593.6pt; }" in css

    def test_title_and_appendix_share_the_first_rows(self):
        """Заголовок слева, приложение к постановлению справа — как в PDF (19.2 / 20.5)."""
        page = printupd.html(_doc())
        assert '<table class="headtop">' in page
        assert "table.headtop td.apx { text-align: right; }" in printupd.CSS

    def test_blank_does_not_leak_print_ticket_through_referer(self):
        """Адрес бланка несёт ?ticket=… — Referer не должен уносить его в журналы."""
        page = printupd.html(_doc())
        assert '<meta name="referrer" content="no-referrer">' in page

    def test_bank_requisites_are_optional(self):
        """В бланке 1137 ред. 23.01.2026 строки банковских реквизитов нет — только по заполнению."""
        assert "банковские реквизиты" not in printupd.html(_doc())
        filled = printupd.html(_doc(), {"seller_bank": "Р/с 40702810400000000123"})
        assert "(банковские реквизиты продавца)" in filled

    def test_totals_row_stops_at_column_nine(self):
        """В PDF «Всего к оплате» обрывается на графе 9 — справа от неё рамки нет."""
        row = printupd.totals_row(_doc(), printupd.defaults(_doc()))
        assert 'colspan="8"' in row and 'colspan="3"' in row
        assert row.count("<td") == 7     # метка + 5 граф + пустой хвост


class TestNumbersLikePaper:
    def test_amounts_use_dot_like_the_reference(self):
        assert printupd.money(155.74) == "155.74"
        assert printupd.money(35500.0) == "35 500.00"
        assert printupd.qty(1234.5) == "1 234.5"
        assert printupd.money(None) == "—"

    def test_settlement_rates_have_no_percent_sign(self):
        assert printupd.vat_label("20/120") == "20/120"
        assert printupd.vat_label("22/122") == "22/122"
        assert printupd.vat_label("22") == "22%"

    def test_blank_prints_same_amounts_as_pdf(self):
        from app.primary.models import PrimaryDocumentLine

        doc = _doc(total_without_vat=155.74, vat_total=34.26, total_with_vat=190.0)
        doc.lines = [PrimaryDocumentLine(position=1, name="Прокладка", unit_code="796", unit_name="шт",
                                         quantity=1, price=155.74, sum_without_vat=155.74,
                                         vat_rate="22", vat_sum=34.26, sum_with_vat=190.0)]
        page = printupd.html(doc)
        for needle in (">155.74<", ">34.26<", ">190.00<", ">22%<", "Российский рубль, код - 643"):
            assert needle in page, needle
        assert "155,74" not in page and "34,26" not in page


class TestSignaturesLikePdf:
    def test_chief_signature_is_on_the_role_line(self):
        doc = _doc()
        doc.print_json = printupd.dump({"signer_chief_role": "Руководитель группы",
                                        "signer_chief": "Мельникова Анна Валерьевна"})
        page = printupd.html(doc)
        assert 'class="roleline"' in page and "table.roleline td.sgn" in printupd.CSS
        block = page.split('class="roleline"', 1)[1].split("</table>", 1)[0]
        assert "Руководитель группы" in block and "электронно" in block
        assert "Мельникова Анна Валерьевна" in block

    def test_receiver_signature_comes_from_the_second_signer(self):
        """В PDF правая половина подписей заполнена подписью получателя."""
        parsed = {"signer": {"role": "Руководитель группы", "name": "Мельникова Анна Валерьевна"},
                  "signers": [{"role": "Руководитель группы", "name": "Мельникова Анна Валерьевна"},
                              {"role": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР", "name": "ШПАКОВ РОМАН ВЛАДИМИРОВИЧ"}]}
        auto = printupd.auto_from(parsed)
        assert auto["accept_position"] == "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"
        assert auto["accept_name"] == "ШПАКОВ РОМАН ВЛАДИМИРОВИЧ"
        assert auto["accept_sign"] == "электронно"

    def test_single_signer_keeps_receiver_fields_empty(self):
        parsed = {"signer": {"role": "Руководитель группы", "name": "Мельникова Анна Валерьевна"},
                  "signers": [{"role": "Руководитель группы", "name": "Мельникова Анна Валерьевна"}]}
        auto = printupd.auto_from(parsed)
        assert "accept_name" not in auto and "accept_position" not in auto

    def test_signers_are_all_parsed_from_xml(self):
        body = """
        <Файл ИдФайл="X" ВерсФорм="5.03" xmlns="urn:x"><Документ Функция="СЧФДОП">
          <СвСчФакт НомерДок="1" ДатаДок="07.09.2026">
            <СвПрод><ИдСв><СвЮЛУч НаимОрг="П" ИННЮЛ="7704217370" КПП="997750001"/></ИдСв></СвПрод>
            <СвПокуп><ИдСв><СвЮЛУч НаимОрг="Покупатель" ИННЮЛ="4712030034" КПП="471201001"/></ИдСв></СвПокуп>
          </СвСчФакт>
          <Подписант Должн="Руководитель группы"><ФИО Фамилия="Мельникова" Имя="Анна"
              Отчество="Валерьевна"/></Подписант>
          <Подписант Должн="ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"><ФИО Фамилия="ШПАКОВ" Имя="РОМАН"
              Отчество="ВЛАДИМИРОВИЧ"/></Подписант>
        </Документ></Файл>"""
        parsed = upd.parse(body.encode("utf-8"))
        assert [s["name"] for s in parsed["signers"]] == ["Мельникова Анна Валерьевна",
                                                          "ШПАКОВ РОМАН ВЛАДИМИРОВИЧ"]
        assert parsed["signer"]["role"] == "Руководитель группы"    # первый — продавец


class TestNoElectronicSignature:
    def test_signature_block_is_still_absent(self):
        doc = _doc()
        doc.print_json = printupd.dump({"edo_operator": 'ООО "Компания "Тензор""',
                                        "edo_sent_cert": "50D9410CDF"})
        page = printupd.html(doc)
        for absent in ("ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ", "ОТПРАВЛЕНО", "УТВЕРЖДЕНО",
                       "Сертификат", "Оператор ЭДО"):
            assert absent not in page, absent


class TestRelease1736:
    def test_version_and_updater_visibility(self):
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        for older in (PREV, "1.73.4", "1.73.3"):
            assert updater._is_newer(cur, older) is True, older
            assert updater._is_newer(older, cur) is False, older

    def test_all_static_cache_busters_synced(self):
        cur = settings.APP_VERSION
        sw = read("app/static/sw.js")
        assert f'APP_VERSION: str = "{cur}"' in read("app/config.py")
        assert f"ymaster-check-v{cur}" in sw and sw.count(f"?v={cur}") == 5
        assert read("app/static/index.html").count(f"?v={cur}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={cur}") == 2
        assert f"/maxapp/maxapp.js?v={cur}" in read("app/static/maxapp/index.html")
        assert f'"version": "{cur}"' in read("app/static/manifest.webmanifest")
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/osm.js")
        about = TestClient(app).get("/api/v1/about")
        assert about.status_code == 200 and about.json()["version"] == cur

    def test_primary_block_bumped(self):
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert _ver(reg[PRIMARY_BLOCK]) >= _ver(VERSION), reg[PRIMARY_BLOCK]
        changed = {i["name"]: i for i in updater.diff_blocks(dict(reg, **{PRIMARY_BLOCK: PREV}), reg)}
        assert changed[PRIMARY_BLOCK]["from"] == PREV

    def test_changelog_entry(self):
        ch = read("CHANGELOG.md")
        assert ch.count(f"## [{VERSION}]") == 1
        assert ch.index(f"## [{VERSION}]") < ch.index(f"## [{PREV}]")
        entry = ch.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 5000, len(entry)
        for needle in ("805", "20/120", "ШПАКОВ", "дробной части", "16 граф"):
            assert needle in entry, needle

    def test_whats_new_and_docs(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        assert js.index(f"'{VERSION}': [") < js.index(f"'{PREV}': [")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("один в один", "получател"):
            assert needle in block, needle

        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", read("README.md"))
        assert match and _ver(match.group(1)) >= _ver(VERSION)
        assert f"| Последний выпуск | **v{settings.APP_VERSION}**" in read("docs/kb/README.md")

        journal = read("docs/ui_journal.md")
        assert journal.count(f"## v{VERSION} —") == 1
        assert journal.index(f"## v{VERSION} —") < journal.index(f"## v{PREV} —")

        rows = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        row = next(r for r in rows if r["Версия"] == VERSION)
        assert row["Дата"] == "2026-11-20"
        assert not [k for k, v in row.items() if "," in v]
