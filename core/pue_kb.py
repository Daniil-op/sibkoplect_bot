# -*- coding: utf-8 -*-
"""
База знаний ПУЭ-7.

Загружает готовый индекс (data/pue/pue_index.json) и ищет релевантные пункты по
запросу. Поиск — на чистом Python, БЕЗ нейросети и без тяжёлых зависимостей:
по совпадению слов с учётом их редкости (TF-IDF-подобно). Этого достаточно, чтобы
находить нужные пункты ПУЭ; найденный текст потом отдаётся в YandexGPT как опора,
и модель отвечает по нормам / указывает на замечания со ссылкой на пункт.

Индекс строит отдельный скрипт build_pue_index.py (запускается один раз).

Пример использования в боте:
    from core import pue_kb
    if pue_kb.available():
        ctx = pue_kb.context_for("защита трансформатора вводным автоматом 0,4 кВ")
        # ctx передаём в YandexGPT как опору для ответа со ссылками на пункты
"""
import os
import re
import json
import math
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

INDEX_PATH = os.path.join("data", "pue", "pue_index.json")


# грубый стеммер: срезаем частые русские окончания, чтобы «цвету/цвета/цвет»,
# «трансформаторов/трансформатора» и т.п. считались одним словом при поиске
_ENDINGS = ["ого", "его", "ому", "ему", "ыми", "ими", "ая", "яя", "ое", "ее",
            "ый", "ий", "ой", "ей", "ым", "им", "ом", "ем", "ах", "ях", "ам",
            "ям", "ов", "ев", "ью", "ия", "ие", "у", "ю", "ы", "и", "й", "ь",
            "а", "я", "о", "е"]
_ENDINGS.sort(key=len, reverse=True)


def _stem(w: str) -> str:
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[: -len(e)]
    return w


def _tokens(s: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[а-яёa-z0-9]+", (s or "").lower())]


@lru_cache(maxsize=1)
def _load():
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            chunks = json.load(f)
    except FileNotFoundError:
        logger.warning("pue_kb: индекс не найден (%s). Запусти build_pue_index.py", INDEX_PATH)
        return [], {}
    except Exception as e:
        logger.warning("pue_kb: не смог прочитать индекс: %s", e)
        return [], {}

    df = {}
    for c in chunks:
        for t in set(_tokens(c.get("text", ""))):
            df[t] = df.get(t, 0) + 1
    n = max(len(chunks), 1)
    idf = {t: math.log(1 + n / v) for t, v in df.items()}
    for c in chunks:
        tf = {}
        for t in _tokens(c.get("text", "")):
            tf[t] = tf.get(t, 0) + 1
        c["_tf"] = tf
    return chunks, idf


def available() -> int:
    """Сколько пунктов ПУЭ в индексе (0 — индекс не построен)."""
    chunks, _ = _load()
    return len(chunks)


def search(query: str, k: int = 5) -> list[dict]:
    """Топ-k пунктов ПУЭ, наиболее релевантных запросу."""
    chunks, idf = _load()
    if not chunks:
        return []
    q = [t for t in _tokens(query) if len(t) > 2]
    scored = []
    for c in chunks:
        s = sum(c["_tf"][t] * idf.get(t, 0.0) for t in q if t in c["_tf"])
        if s > 0:
            scored.append((s, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:k]]


def context_for(query: str, k: int = 5, max_chars: int = 2500) -> str:
    """Текст найденных пунктов ПУЭ (со ссылками) для передачи в LLM как опоры."""
    out, total = [], 0
    for c in search(query, k):
        piece = f"ПУЭ п. {c['id']}: {c['text']}"
        if total + len(piece) > max_chars:
            break
        out.append(piece)
        total += len(piece)
    return "\n\n".join(out)