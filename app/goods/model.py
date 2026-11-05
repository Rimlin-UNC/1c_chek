"""Лёгкая модель категории товара в духе FastText (T-69, v1.70.101, ADR-31).

Идея из nlp_in_practice_receipts и fastText: строка позиции → слова и символьные n-граммы (3–5 букв с
границами слова «<» и «>»), их веса складываются и усредняются, softmax даёт вероятности категорий.
n-граммы понимают сокращения и опечатки кассы: «шпакл.», «шпатлевка» и «шпаклёвка» делят общие куски.

Вывод — на чистом Python, без numpy и внешних сервисов: словарь «признак → (категория, вес int8)».
Модель грузится при первом вызове (ленивая загрузка), ~10 МБ памяти, доли миллисекунды на позицию.
Обучение — qa/goods_train.py (numpy, только синтетика и ОКПД2, ADR-14); файл модели —
app/goods/assets/goods_model.json.gz. Нет файла — модель молчит, каскад работает без неё.
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import re
import threading
from pathlib import Path

MODEL_PATH = Path(__file__).parent / "assets" / "goods_model.json.gz"
FORMAT = "ym-goods-model"

_TOKEN = re.compile(r"[a-zа-я0-9]+")
# Служебные слова чеков и шаблонные слова ОКПД2 — шум, а не признак товара
STOP = frozenset("""
и в во на по для из с со от до без или к о об при под над за не а у
кроме прочие прочий прочая прочее прочих включенные включенных другие другим группировки аналогичные
арт артикул код шт уп упак кг гр мл мм см
""".split())


def tokens(text: str) -> list[str]:
    """Слова строки: строчные, «ё» → «е»; числа отброшены, цифры в марках → «0» (М500 → м000)."""
    out: list[str] = []
    for tok in _TOKEN.findall(str(text or "").lower().replace("ё", "е")):
        if tok.isdigit() or tok in STOP:
            continue
        if any(ch.isdigit() for ch in tok):
            tok = re.sub(r"\d", "0", tok)
        elif len(tok) < 2:
            continue
        out.append(tok)
        if len(out) >= 16:
            break
    return out


def features(text: str) -> list[str]:
    """Признаки строки: слово целиком («=цемент»), начало первого слова («^цемен»), символьные
    n-граммы 3–5 букв с границами («<це», «емен», «ент>»); марки с цифрами — только целиком («#м000»)."""
    feats: list[str] = []
    first = True
    for tok in tokens(text):
        if any(ch.isdigit() for ch in tok):
            feats.append("#" + tok)
            continue
        if first:
            feats.append("^" + tok[:5])
            first = False
        feats.append("=" + tok)
        padded = "<" + tok + ">"
        size = len(padded)
        for n in (3, 4, 5):
            for i in range(size - n + 1):
                feats.append(padded[i:i + n])
    return feats


class Model:
    """Загруженная модель: классы, смещения и разреженная таблица весов."""

    def __init__(self, data: dict):
        if data.get("format") != FORMAT:
            raise ValueError("не тот формат модели")
        self.classes: list[str] = list(data["classes"])
        self.bias: list[float] = [float(x) for x in data["bias"]]
        self.scale: float = float(data["scale"])
        # температура: уверенность «80 %» должна означать примерно 80 % попаданий (подбор — qa/goods_train.py)
        self.temperature: float = float(data.get("temperature") or 1.0)
        self.table: dict[str, bytes] = {k: base64.b64decode(v) for k, v in data["features"].items()}
        self.meta: dict = {k: data.get(k) for k in ("version", "trained_at", "metrics", "sources")}

    def scores(self, text: str) -> tuple[list[float], int, int]:
        """(вероятности классов, всего признаков, известных модели признаков)."""
        feats = features(text)
        logits = list(self.bias)
        known = 0
        if feats:
            acc = [0] * len(self.classes)
            table = self.table
            for f in feats:
                packed = table.get(f)
                if packed is None:
                    continue
                known += 1
                for i in range(0, len(packed), 2):
                    acc[packed[i]] += packed[i + 1] - 128
            k = self.scale / len(feats)
            logits = [b + a * k for b, a in zip(logits, acc)]
        logits = [x / self.temperature for x in logits]
        top = max(logits)
        exps = [math.exp(x - top) for x in logits]
        total = sum(exps)
        return [e / total for e in exps], len(feats), known

    def predict(self, text: str, top: int = 3) -> dict:
        """{"top": [(категория, вероятность), …], "coverage": доля известных признаков}."""
        probs, n, known = self.scores(text)
        order = sorted(range(len(probs)), key=lambda i: -probs[i])[:top]
        return {"top": [(self.classes[i], probs[i]) for i in order],
                "probs": dict(zip(self.classes, probs)),
                "coverage": (known / n) if n else 0.0}


_LOCK = threading.Lock()
_STATE: dict = {}


def load(path: Path | None = None) -> Model | None:
    """Модель из файла (один раз на процесс). Нет файла или он повреждён — None, каскад работает без неё."""
    key = str(path or MODEL_PATH)
    if key in _STATE:
        return _STATE[key]
    with _LOCK:
        if key not in _STATE:
            try:
                raw = gzip.decompress(Path(key).read_bytes())
                _STATE[key] = Model(json.loads(raw.decode("utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                _STATE[key] = None
    return _STATE[key]


def info() -> dict:
    """Сведения о модели для экрана «О программе» и проверок: есть ли, когда обучена, точность."""
    m = load()
    if m is None:
        return {"loaded": False}
    return {"loaded": True, "classes": len(m.classes), "features": len(m.table), **m.meta}
