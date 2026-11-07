"""Обучение лёгкой модели категорий товаров (T-69, v1.70.101, ADR-31).

Подход — из открытых решений по чекам, без копирования их кода (лицензий у репозиториев нет):
- nlp_in_practice_receipts (ODS): fastText — символьные n-граммы слов понимают сокращения кассы;
- Data Fusion 2021 «Goodsification» (ВТБ): TF-IDF и линейная модель по названию, затем признак
  «тип чека» — его добавляет каскад в app/goods/service.py, не модель.

Данные — только законные и синтетические (ADR-14):
- qa/goods_corpus/okpd2_goods.tsv.gz — названия ОКПД2 (официальный классификатор, qa/goods_okpd2.py);
- qa/goods_corpus/goods_examples.tsv — строки в стиле кассовых чеков, написаны вручную;
- признаки словаря app/services/goods.py и марки из списка BRANDS ниже;
- аугментация: сокращения «шпакл.», опечатки, размеры и фасовка, обрезка длинных названий.
Проверка — tests/fixtures/goods_eval.tsv: отдельный набор, модель его не видит.

Модель: многоклассовая логистическая регрессия на усреднённых признаках app.goods.model.features
(как fastText без скрытого слоя), обучение мини-батчами SGD на numpy; затем отсев слабых признаков
и веса int8 — файл app/goods/assets/goods_model.json.gz (≤ 5 МБ). Обучение детерминировано (SEED).

Запуск:  .venv/bin/python qa/goods_train.py [--epochs 8] [--per-class 2600]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.goods import model as gm  # noqa: E402
from app.services import goods  # noqa: E402

EXAMPLES = ROOT / "qa" / "goods_corpus" / "goods_examples.tsv"
OKPD = ROOT / "qa" / "goods_corpus" / "okpd2_goods.tsv.gz"
EVAL = ROOT / "tests" / "fixtures" / "goods_eval.tsv"
OUT = gm.MODEL_PATH
SEED = 20261105

# Марки для аугментации: однозначные в чеках (только названия — факты, не данные покупателей)
BRANDS: dict[str, list[str]] = {
    "build_base": ["кнауф", "knauf", "волма", "основит", "технониколь", "rockwool", "изовер", "пеноплэкс",
                   "евроцемент", "старооскольский", "ursa", "грандлайн", "metall profil", "квик"],
    "build_finish": ["церезит", "ceresit", "ветонит", "weber", "тиккурила", "dulux", "текс", "unis", "юнис",
                     "bergauf", "старатели", "момент", "титан", "макрофлекс", "квилид", "оптимист", "prestige"],
    "build_tools": ["makita", "bosch", "dewalt", "интерскол", "зубр", "stayer", "kraftool", "matrix", "patriot",
                    "ресанта", "hammer", "metabo", "hitachi", "вихрь", "sturm"],
    "build_consumables": ["луга", "hammer", "dexter", "esab", "кедр", "unitape", "профи"],
    "build_fasteners": ["fischer", "sormat", "tech-krep", "стройкреп", "кронверк"],
    "build_electric": ["iek", "legrand", "schneider", "abb", "wago", "ekf", "кабэкс", "камкабель", "werkel"],
    "build_plumbing": ["valtec", "grohe", "rehau", "vesta", "termica", "оскольская керамика", "сантек",
                       "джилекс", "rifar", "ariston", "thermex"],
    "build_ppe": ["3м", "исток", "восток-сервис", "trud", "фаворит", "zubr"],
    "pharma": ["фармстандарт", "озон фарм", "вертекс", "эвалар", "байер", "тева", "акрихин", "реневал"],
    "fuel": ["лукойл", "роснефть", "газпромнефть", "татнефть", "shell", "ekto"],
    "catering": ["кофейня", "ресторан", "столовая", "кафе"],
    "alcohol": ["балтика", "жигули", "клинское", "абрау-дюрсо", "кубань", "фанагория", "царская",
                "хортиця", "absolut", "jack daniels", "старейшина", "киновский"],
    "tobacco": ["parliament", "marlboro", "winston", "kent", "camel", "lucky strike", "ld", "rothmans",
                "sobranie", "chesterfield", "bond", "philip morris", "петр i", "ява"],
    "food": ["простоквашино", "домик в деревне", "parmalat", "valio", "danone", "вкуснотеево", "мираторг",
             "черкизово", "макфа", "барилла", "heinz", "махеев", "jacobs", "nescafe", "lipton", "greenfield",
             "добрый", "j7", "агуша", "савушкин", "бабаевский", "красный октябрь", "alpen gold", "milka"],
    "computers": ["lenovo", "hp", "asus", "acer", "samsung", "logitech", "kingston", "canon", "epson",
                  "kyocera", "tp-link", "keenetic", "xerox", "sandisk"],
    "gadgets": ["xiaomi", "samsung", "apple", "iphone", "honor", "huawei", "realme", "jbl", "borofone",
                "baseus", "hoco", "tecno"],
    "appliances": ["bosch", "philips", "tefal", "redmond", "polaris", "vitek", "scarlett", "lg", "indesit",
                   "атлант", "delonghi", "ballu", "timberk"],
    "electro_small": ["duracell", "energizer", "gp", "camelion", "космос", "фаzа", "navigator", "gauss"],
    "clothes": ["gloria jeans", "ostin", "befree", "демикс", "adidas", "nike", "puma", "henderson"],
    "hygiene": ["nivea", "dove", "colgate", "oral-b", "gillette", "rexona", "palmolive", "zewa", "pampers",
                "always", "head&shoulders", "pantene", "чистая линия", "невская косметика", "splat"],
    "chemicals": ["fairy", "tide", "ariel", "persil", "domestos", "comet", "mr proper", "vanish", "finish",
                  "somat", "lenor", "aos", "миф", "ласка", "белизна", "cillit bang", "glade"],
    "household": ["paclan", "фрекен бок", "york", "vileda", "мультидом", "idea", "lotus"],
    "furniture": ["ikea", "hoff", "шатура", "лазурит"],
    "auto": ["castrol", "mobil", "shell helix", "лукойл генезис", "total", "liqui moly", "sintec", "felix",
             "bosch", "ngk", "varta", "mann", "аляска", "hi-gear", "kerry"],
    "office": ["erich krause", "brauberg", "berlingo", "svetocopy", "снегурочка", "staff", "bic", "stabilo",
               "attache", "officespace"],
    "pets": ["whiskas", "kitekat", "felix", "purina", "pedigree", "royal canin", "chappi", "sheba", "gourmet",
             "мнямс"],
    "kids": ["lego", "hasbro", "mattel", "барби", "фиксики", "huggies", "fisher-price"],
    "sport": ["demix", "torneo", "decathlon", "stern", "nordway", "larsen"],
    "jewelry": ["sokolov", "sunlight", "адамас", "585", "925"],
    "telecom": ["мтс", "билайн", "мегафон", "теле2", "ростелеком", "дом ру"],
    "transport": ["яндекс такси", "ситимобил", "ржд", "аэрофлот", "делимобиль", "автодор"],
    "delivery": ["сдэк", "почта россии", "boxberry", "dpd", "яндекс доставка"],
}

NOISE_SIZES = ["50кг", "25 кг", "1,5л", "0,5 л", "200шт", "10 шт", "3х2,5", "М500", "100мм", "12,5мм", "2м",
               "400г", "1кг", "930мл", "№20", "5л", "20кг", "6х0,5л", "р.52", "арт. 123456", "4607001234567"]


def load_tsv(path: Path) -> list[tuple[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    rows = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2:
                rows.append((parts[0], parts[1]))
            elif len(parts) == 3:                      # ОКПД2: код, категория, название
                rows.append((parts[1], parts[2]))
    return rows


def okpd_parts(name: str) -> list[str]:
    """Название ОКПД2 и его части по запятым и «;» — короткие части ближе к строке чека."""
    out = [name]
    for part in re.split(r"[;,]| и ", name):
        part = part.strip()
        if len(gm.tokens(part)) >= 1 and part != name:
            out.append(part)
    return out


def abbreviate(word: str, rng: random.Random) -> str:
    if len(word) < 6:
        return word
    return word[:rng.randint(3, min(6, len(word) - 2))] + "."


def typo(word: str, rng: random.Random) -> str:
    if len(word) < 5:
        return word
    i = rng.randint(1, len(word) - 2)
    kind = rng.random()
    if kind < 0.4:
        return word[:i] + word[i + 1:]                 # пропуск буквы
    if kind < 0.7:
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]  # перестановка
    return word[:i] + word[i] + word[i:]               # удвоение


def augment(text: str, cat: str, rng: random.Random) -> str:
    words = text.split()
    if len(words) > 3 and rng.random() < 0.3:
        words = words[:rng.randint(2, 3)]              # касса обрезает длинное название
    if rng.random() < 0.4:
        j = rng.randrange(len(words))
        words[j] = abbreviate(words[j], rng)
        if len(words) > 1 and rng.random() < 0.4:
            k = rng.randrange(len(words))
            words[k] = abbreviate(words[k], rng)
    if rng.random() < 0.15:
        j = rng.randrange(len(words))
        words[j] = typo(words[j], rng)
    if cat in BRANDS and rng.random() < 0.3:
        words.insert(rng.randint(1, len(words)), rng.choice(BRANDS[cat]))
    if rng.random() < 0.4:
        words.append(rng.choice(NOISE_SIZES))
    out = " ".join(words)
    return out.upper() if rng.random() < 0.3 else out


def dictionary_forms(vocab: set[str]) -> dict[str, list[str]]:
    """Признаки словаря → настоящие слова корпуса («шпаклев» → «шпаклевка»); нет в корпусе — сам признак."""
    forms: dict[str, list[str]] = {}
    for cat in goods.categories():
        out = []
        for word in cat.words:
            stem = word.strip().replace("ё", "е")
            if " " in stem:
                out.append(stem)
                continue
            full = sorted(w for w in vocab if w.startswith(stem))[:4]
            out.extend(full or [stem])
        forms[cat.code] = out
    return forms


def build_dataset(per_class: int, rng: random.Random) -> tuple[list[str], list[int], list[str]]:
    classes = [c.code for c in goods.categories()]
    examples = load_tsv(EXAMPLES)
    okpd = load_tsv(OKPD)
    by_cat_ex: dict[str, list[str]] = {c: [] for c in classes}
    by_cat_okpd: dict[str, list[str]] = {c: [] for c in classes}
    vocab: set[str] = set()
    for cat, name in examples:
        by_cat_ex[cat].append(name)
        vocab.update(gm.tokens(name))
    for cat, name in okpd:
        by_cat_okpd[cat].extend(okpd_parts(name))
        vocab.update(gm.tokens(name))
    forms = dictionary_forms(vocab)
    texts: list[str] = []
    labels: list[int] = []
    for ci, cat in enumerate(classes):
        pools = [(0.38, by_cat_ex[cat]), (0.32, by_cat_okpd[cat]), (0.30, forms[cat])]
        pools = [(w, p) for w, p in pools if p]
        weights = [w for w, _ in pools]
        # исходные строки — каждая хотя бы раз как есть
        for name in by_cat_ex[cat] + by_cat_okpd[cat][:400] + forms[cat]:
            texts.append(name)
            labels.append(ci)
        for _ in range(per_class):
            pool = rng.choices([p for _, p in pools], weights=weights)[0]
            base = rng.choice(pool)
            if pool is forms[cat] and rng.random() < 0.5:
                base = base + " " + rng.choice(forms[cat])   # два признака категории рядом
            texts.append(augment(base, cat, rng))
            labels.append(ci)
    return texts, labels, classes


def vectorize(texts: list[str], vocab: dict[str, int] | None = None, min_count: int = 2):
    feats = [gm.features(t) for t in texts]
    if vocab is None:
        counts: dict[str, int] = {}
        for fs in feats:
            for f in set(fs):
                counts[f] = counts.get(f, 0) + 1
        vocab = {f: i for i, f in enumerate(sorted(f for f, n in counts.items() if n >= min_count))}
    indptr = [0]
    indices: list[int] = []
    denom: list[float] = []
    for fs in feats:
        ids = [vocab[f] for f in fs if f in vocab]
        indices.extend(ids)
        indptr.append(len(indices))
        denom.append(float(len(fs) or 1))
    return vocab, np.array(indptr, dtype=np.int64), np.array(indices, dtype=np.int64), np.array(denom)


def logits_of(W, b, indptr, indices, denom, rows):
    lens = indptr[rows + 1] - indptr[rows]
    idx = np.concatenate([indices[indptr[r]:indptr[r + 1]] for r in rows]) if len(rows) else np.array([], int)
    seg = np.repeat(np.arange(len(rows)), lens)
    vals = np.repeat(1.0 / denom[rows], lens)
    out = np.tile(b, (len(rows), 1))
    if len(idx):
        np.add.at(out, seg, W[idx] * vals[:, None])
    return out, idx, seg, vals


def train(texts, labels, classes, epochs: int, rng_np):
    vocab, indptr, indices, denom = vectorize(texts)
    y = np.array(labels)
    n, c, v = len(texts), len(classes), len(vocab)
    W = np.zeros((v, c), dtype=np.float32)
    b = np.zeros(c, dtype=np.float32)
    batch = 64
    steps = epochs * ((n + batch - 1) // batch)
    step = 0
    lr0 = 2.0
    for epoch in range(epochs):
        order = rng_np.permutation(n)
        loss = 0.0
        for s in range(0, n, batch):
            rows = order[s:s + batch]
            lr = lr0 * (1.0 - step / steps) + 0.02
            step += 1
            z, idx, seg, vals = logits_of(W, b, indptr, indices, denom, rows)
            z -= z.max(axis=1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(axis=1, keepdims=True)
            loss -= float(np.log(p[np.arange(len(rows)), y[rows]] + 1e-12).sum())
            g = p
            g[np.arange(len(rows)), y[rows]] -= 1.0
            if len(idx):
                np.add.at(W, idx, (-lr * g[seg] * vals[:, None]).astype(np.float32))
            b -= (lr * g.mean(axis=0) * 0.1).astype(np.float32)
        print(f"  эпоха {epoch + 1}/{epochs}: потери {loss / n:.4f}", flush=True)
    return vocab, W, b


def export(vocab, W, b, classes, keep_min: float, top_k: int, temperature: float) -> dict:
    inv = {i: f for f, i in vocab.items()}
    strength = np.abs(W).max(axis=1)
    keep = np.where(strength >= keep_min)[0]
    scale = float(np.abs(W[keep]).max() / 127.0) if len(keep) else 1.0
    table: dict[str, str] = {}
    for i in keep:
        row = W[i]
        cls = np.argsort(-np.abs(row))[:top_k]
        packed = bytearray()
        for ci in cls:
            q = int(round(float(row[ci]) / scale))
            if q == 0:
                continue
            packed += bytes((int(ci), max(1, min(255, q + 128))))
        if packed:
            table[inv[int(i)]] = base64.b64encode(bytes(packed)).decode("ascii")
    return {"format": gm.FORMAT, "version": 1, "classes": classes, "temperature": temperature,
            "bias": [round(float(x), 5) for x in b], "scale": scale, "features": table}


def evaluate(model: gm.Model, rows) -> dict:
    """Точность модели, словаря и «словарь, затем модель» на проверочном наборе."""
    def grp(code):
        return goods.category(code).group
    total = len(rows)
    m_cat = m_grp = d_cat = d_grp = d_cov = c_cat = c_grp = k_cov = k_cat = 0
    for cat, name in rows:
        pred, prob = model.predict(name)["top"][0]
        m_cat += pred == cat
        m_grp += grp(pred) == grp(cat)
        hit = goods.match(name)
        if hit:
            d_cov += 1
            d_cat += hit[0].code == cat
            d_grp += hit[0].group == grp(cat)
        comb = hit[0].code if hit else pred
        c_cat += comb == cat
        c_grp += grp(comb) == grp(cat)
        # каскад без контекста чека: словарь, иначе модель с уверенностью от 0,5, иначе «не определено»
        casc = hit[0].code if hit else (pred if prob >= 0.5 else None)
        if casc:
            k_cov += 1
            k_cat += casc == cat
    pct = lambda x: round(100.0 * x / total, 1)  # noqa: E731
    return {"rows": total, "model_category": pct(m_cat), "model_group": pct(m_grp),
            "dictionary_coverage": pct(d_cov), "dictionary_category": pct(d_cat), "dictionary_group": pct(d_grp),
            "combined_category": pct(c_cat), "combined_group": pct(c_grp),
            "cascade_coverage": pct(k_cov), "cascade_category": pct(k_cat),
            "cascade_precision": round(100.0 * k_cat / k_cov, 1) if k_cov else 0.0}


def corrupt(rows, rng: random.Random):
    """Тот же проверочный набор «глазами кассы»: сокращения и опечатки в каждой строке."""
    out = []
    for cat, name in rows:
        words = name.split()
        j = max(range(len(words)), key=lambda i: len(words[i]))
        words[j] = abbreviate(words[j], rng) if rng.random() < 0.6 else typo(words[j], rng)
        out.append((cat, " ".join(words).upper()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--per-class", type=int, default=2600)
    ap.add_argument("--keep-min", type=float, default=0.35)
    ap.add_argument("--top-k", type=int, default=6)
    # 0,5 — минимум потерь на проверочном наборе около 0,4; берём осторожное круглое значение
    ap.add_argument("--temperature", type=float, default=0.5)
    args = ap.parse_args()
    rng = random.Random(SEED)
    rng_np = np.random.default_rng(SEED)
    t0 = time.time()
    texts, labels, classes = build_dataset(args.per_class, rng)
    print(f"обучающих строк: {len(texts)}, категорий: {len(classes)}")
    vocab, W, b = train(texts, labels, classes, args.epochs, rng_np)
    data = export(vocab, W, b, classes, args.keep_min, args.top_k, args.temperature)
    model = gm.Model(data)
    rows = load_tsv(EVAL)
    clean = evaluate(model, rows)
    noisy = evaluate(model, corrupt(rows, random.Random(SEED + 1)))
    data.update({
        "trained_at": date.today().isoformat(),
        "metrics": {"eval_clean": clean, "eval_noisy": noisy, "train_rows": len(texts),
                    "features_total": len(vocab), "features_kept": len(data["features"])},
        "sources": {"okpd2": "ОК 034-2014, Росстат, редакция 01.10.2026 (qa/goods_corpus/okpd2_goods.tsv.gz)",
                    "examples": "qa/goods_corpus/goods_examples.tsv", "eval": "tests/fixtures/goods_eval.tsv",
                    "seed": SEED, "epochs": args.epochs, "per_class": args.per_class,
                    "temperature": args.temperature},
    })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    OUT.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    print(f"признаков: {len(vocab)} → оставлено {len(data['features'])}; файл {OUT.stat().st_size // 1024} КБ")
    print("чистые строки:", json.dumps(clean, ensure_ascii=False))
    print("с сокращениями и опечатками:", json.dumps(noisy, ensure_ascii=False))
    print(f"время: {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
