"""Названия ОКПД2 → обучающие строки для модели понимания чеков (T-69, v1.70.101).

ОКПД2 (ОК 034-2014) — официальный классификатор Росстата; как документ государственного органа он не
охраняется авторским правом (ГК РФ, ст. 1259, п. 6). Машиночитаемую копию берём с GitHub
(prog815/okpd2, выгрузка архива Росстата OKPD2.rar от 01.10.2026) или из архива Росстата напрямую.

Скрипт оставляет только разделы, которые соответствуют категориям товаров программы (таблица MAP ниже,
побеждает самый длинный префикс кода), и пишет qa/goods_corpus/okpd2_goods.tsv.gz: код, категория, название.
Персональных и рабочих данных здесь нет — только текст классификатора (ADR-14).

Запуск:  .venv/bin/python qa/goods_okpd2.py путь/к/okpd2.json.gz
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "qa" / "goods_corpus" / "okpd2_goods.tsv.gz"

# (префикс кода ОКПД2, категория программы или None — ветку не брать)
MAP: list[tuple[str, str | None]] = [
    ("01.1", "food"), ("01.19", None), ("01.2", "food"), ("01.29", None), ("01.3", None),
    ("01.47.2", "food"), ("01.49.21", "food"),
    ("02.20", "build_base"), ("02.30", "food"),
    ("03", "food"),
    ("08.11", "build_base"), ("08.12", "build_base"),
    ("10", "food"), ("10.91", None), ("10.92", "pets"),
    ("11.01", "alcohol"), ("11.02", "alcohol"), ("11.03", "alcohol"), ("11.04", "alcohol"), ("11.05", "alcohol"),
    ("11.07", "food"),
    ("12", "tobacco"),
    ("13.92", "household"), ("13.93", "furniture"),
    ("14", "clothes"), ("14.12", "build_ppe"),
    ("15.12", "clothes"), ("15.20", "clothes"),
    ("16.10", "build_base"), ("16.21", "build_base"), ("16.22", "build_finish"), ("16.23", "build_base"),
    ("17.12", "office"), ("17.22", "hygiene"), ("17.23", "office"), ("17.24", "build_finish"),
    ("19.20.21", "fuel"), ("19.20.26", "fuel"), ("19.20.29", "auto"), ("19.20.3", "fuel"), ("19.20.42", "build_base"),
    ("20.20", "chemicals"), ("20.30", "build_finish"), ("20.41.31", "hygiene"), ("20.41.32", "chemicals"),
    ("20.41.4", "chemicals"), ("20.42", "hygiene"), ("20.52", "build_finish"),
    ("21", "pharma"),
    ("22.11", "auto"), ("22.21", "build_plumbing"), ("22.22", "household"), ("22.23", "build_base"),
    ("22.23.1", "build_finish"), ("22.23.12", "build_plumbing"), ("22.29.23", "household"),
    ("23.12", "build_base"), ("23.13", "household"), ("23.20", "build_base"), ("23.31", "build_finish"),
    ("23.32", "build_base"), ("23.41", "household"), ("23.42", "build_plumbing"), ("23.5", "build_base"),
    ("23.6", "build_base"), ("23.70", "build_base"), ("23.91", "build_consumables"), ("23.99", "build_base"),
    ("24.10", "build_base"), ("24.20", "build_base"), ("24.31", "build_base"), ("24.32", "build_base"),
    ("24.33", "build_base"), ("24.34", "build_base"), ("24.42", "build_base"),
    ("25.11", "build_base"), ("25.12", "build_base"), ("25.21", "build_plumbing"), ("25.71", "household"),
    ("25.72", "build_fasteners"), ("25.73", "build_tools"), ("25.93", "build_base"), ("25.93.14", "build_fasteners"),
    ("25.94", "build_fasteners"), ("25.99.11", "build_plumbing"), ("25.99.12", "household"),
    ("25.99.22", "office"), ("25.99.23", "office"),
    ("26.20", "computers"), ("26.30", "gadgets"), ("26.40", "appliances"), ("26.60", "pharma"),
    ("26.70", "gadgets"), ("26.80", "computers"),
    ("27.12", "build_electric"), ("27.20.1", "electro_small"), ("27.20.2", "auto"), ("27.31", "build_electric"),
    ("27.32", "build_electric"), ("27.33", "build_electric"), ("27.40.1", "electro_small"),
    ("27.40.2", "furniture"), ("27.51", "appliances"), ("27.52", "appliances"),
    ("28.24", "build_tools"),
    ("29.31", "auto"), ("29.32", "auto"),
    ("30.92", "sport"),
    ("31", "furniture"),
    ("32.12", "jewelry"), ("32.13", "jewelry"), ("32.30", "sport"), ("32.40", "kids"), ("32.50", "pharma"),
    ("32.91.11", "household"), ("32.91.12", "hygiene"), ("32.91.19", "build_tools"), ("32.99.11", "build_ppe"),
    ("32.99.12", "office"), ("32.99.13", "office"), ("32.99.15", "office"), ("32.99.16", "office"),
    # услуги — только самые частые в подотчёте
    ("33", "repair"), ("43", "repair"), ("45.20", "repair"),
    ("49.10", "transport"), ("49.3", "transport"), ("49.41", "delivery"), ("50.30", "transport"),
    ("51.10", "transport"), ("52.21", "transport"), ("52.23", "transport"), ("53", "delivery"),
    ("55", "lodging"), ("56", "catering"),
    ("58.11", "books"), ("58.13", "books"), ("58.14", "books"),
    ("61", "telecom"), ("68.20", "rent"), ("77", "rent"),
    ("69", "services_other"), ("70", "services_other"), ("71", "services_other"), ("73", "services_other"),
    ("74", "services_other"), ("80", "services_other"), ("81", "services_other"), ("82", "services_other"),
    ("85", "services_other"), ("86", "services_other"), ("93", "services_other"), ("95", "repair"),
    ("96.01", "repair"), ("96.02", "services_other"), ("96.04", "services_other"),
]


def category_of(code: str) -> str | None:
    """Категория по самому длинному подходящему префиксу кода (None — ветку не брать).

    Коды ОКПД2 иерархические: потомки «23.5» — «23.51», «23.52», потомки «27.20.1» — «27.20.11»;
    поэтому достаточно простого префикса строки.
    """
    best: tuple[int, str | None] = (-1, None)
    for prefix, cat in MAP:
        if code.startswith(prefix) and len(prefix) > best[0]:
            best = (len(prefix), cat)
    return best[1]


def build(src: Path) -> list[tuple[str, str, str]]:
    raw = gzip.open(src).read() if src.suffix == ".gz" else src.read_bytes()
    rows = []
    for rec in json.loads(raw):
        code, name = str(rec.get("c") or ""), str(rec.get("n") or "").strip()
        if not code or not name:
            continue
        cat = category_of(code)
        if cat:
            rows.append((code, cat, " ".join(name.split())))
    return rows


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    rows = build(Path(sys.argv[1]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write("# ОКПД2 (ОК 034-2014, Росстат, редакция 01.10.2026) → категории товаров Ямастер Чек\n")
        for code, cat, name in rows:
            fh.write(f"{code}\t{cat}\t{name}\n")
    counts: dict[str, int] = {}
    for _, cat, _ in rows:
        counts[cat] = counts.get(cat, 0) + 1
    print(f"строк: {len(rows)} → {OUT.relative_to(ROOT)}")
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:18s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
