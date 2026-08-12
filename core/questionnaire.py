# -*- coding: utf-8 -*-
"""
Движок опросника СибКомплект.

Логика «как менеджер»:
  1) determine product type (KTP / KRUN) из текста документа;
  2) prefill_from_text — прочитать документ и заполнить те поля опросника,
     что реально в нём есть (через YandexGPT, строгий JSON по схеме);
  3) remaining_questions — что осталось спросить у клиента (только пробелы);
  4) после подтверждения конфигурации → идентификация изделия и КП.

Схема вопросов лежит в questionnaire_schema.json (корень проекта).
"""

import json
import logging
from pathlib import Path
from typing import Optional

from core.yandex_gpt import yandex_gpt, _clean_json

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "questionnaire_schema.json"

_SCHEMA_CACHE: Optional[dict] = None


def load_schema() -> dict:
    """Загружает и кэширует схему опросника."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        try:
            with open(SCHEMA_PATH, encoding="utf-8") as f:
                _SCHEMA_CACHE = json.load(f)
        except FileNotFoundError:
            logger.error("questionnaire_schema.json не найден: %s", SCHEMA_PATH)
            _SCHEMA_CACHE = {"product_types": {}}
    return _SCHEMA_CACHE


def _product(product_type: str) -> dict:
    return load_schema().get("product_types", {}).get(product_type, {})


def _questions(product_type: str) -> list[dict]:
    return _product(product_type).get("questions", [])


def _by_id(product_type: str, qid: str) -> Optional[dict]:
    for q in _questions(product_type):
        if q["id"] == qid:
            return q
    return None


# ───────────────────────── определение типа изделия ─────────────────────────
def detect_product_type(text: str) -> Optional[str]:
    """
    Грубое определение типа изделия по ключевым словам (detect_hints).
    Возвращает 'KTP' | 'KRUN' | None. При равенстве — None (спросим клиента).
    """
    if not text:
        return None
    low = text.lower()
    scores: dict[str, int] = {}
    for ptype, pdata in load_schema().get("product_types", {}).items():
        scores[ptype] = sum(1 for h in pdata.get("detect_hints", []) if h.lower() in low)
    if not scores:
        return None
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None
    # если два типа набрали одинаково — неоднозначно
    top = sorted(scores.values(), reverse=True)
    if len(top) > 1 and top[0] == top[1]:
        return None
    return best


# ───────────────────────── детерминированный hint-scan ─────────────────────────
def hint_scan(text: str, product_type: str) -> dict[str, bool]:
    """
    По каждому вопросу: встречаются ли его hints в тексте документа.
    Не извлекает значение — только сигнал «поле, вероятно, есть в файле».
    Используется как fallback и для оценки покрытия.
    """
    low = (text or "").lower()
    out: dict[str, bool] = {}
    for q in _questions(product_type):
        hints = q.get("prefill", {}).get("hints", [])
        out[q["id"]] = any(h.lower() in low for h in hints)
    return out


# ───────────────────────── предзаполнение через GPT ─────────────────────────
_PREFILL_SYSTEM = (
    "Ты — инженер-менеджер завода электрощитового оборудования. "
    "Тебе дают текст проектного документа (однолинейная схема / спецификация / "
    "опросный лист, часто через OCR) и анкету полей. "
    "Твоя задача — заполнить ТОЛЬКО те поля, значение которых прямо есть в документе. "
    "Правила:\n"
    "- Для полей с вариантами (options) верни значение поля 'value' из подходящего варианта.\n"
    "- Для multi верни список values.\n"
    "- Для int верни число, для text — строку как в документе.\n"
    "- Если значения в документе НЕТ или не уверен — верни null. Ничего не выдумывай.\n"
    "- Ответ — СТРОГО JSON вида {\"id_поля\": значение, ...}. Без пояснений и текста вокруг."
)


def _fields_for_prompt(product_type: str) -> str:
    """Компактное описание полей для промпта (id, что спрашиваем, варианты, подсказки)."""
    lines = []
    for q in _questions(product_type):
        parts = [f'{q["id"]} — {q["label"]} [{q["type"]}]']
        if q.get("options"):
            opts = ", ".join(f'{o["value"]}={o["label"]}' for o in q["options"])
            parts.append(f"варианты: {opts}")
        hints = q.get("prefill", {}).get("hints", [])
        if hints:
            parts.append(f"искать по: {', '.join(hints)}")
        lines.append("• " + " | ".join(parts))
    return "\n".join(lines)


async def prefill_from_text(text: str, product_type: Optional[str] = None) -> dict:
    """
    Читает документ и предзаполняет анкету.
    Возвращает: {product_type, answers, filled, missing, hint_coverage}.
    Если ключа GPT нет — answers пустые, но hint_coverage показывает,
    какие поля, судя по всему, есть в файле (для честного fallback-диалога).
    """
    if product_type is None:
        product_type = detect_product_type(text)
    if not product_type:
        return {"product_type": None, "answers": {}, "filled": [],
                "missing": [], "hint_coverage": {}}

    coverage = hint_scan(text, product_type)

    # без ключа — не выдумываем значения, отдаём только сигнал покрытия
    if not yandex_gpt.api_key or not yandex_gpt.folder_id:
        logger.info("prefill: mock-режим (нет ключа GPT) — только hint-scan")
        return {"product_type": product_type, "answers": {}, "filled": [],
                "missing": [q["id"] for q in _questions(product_type) if q.get("required")],
                "hint_coverage": coverage}

    user_msg = (
        f"Тип изделия: {product_type}\n\n"
        f"ПОЛЯ АНКЕТЫ:\n{_fields_for_prompt(product_type)}\n\n"
        f"ДОКУМЕНТ:\n{text[:12000]}"
    )
    try:
        raw = await yandex_gpt._call(
            system_prompt=_PREFILL_SYSTEM,
            user_message=user_msg,
            temperature=0.0,
            max_tokens=2000,
        )
        data = json.loads(_clean_json(raw))
    except Exception as exc:
        logger.warning("prefill GPT error: %s", exc)
        data = {}

    answers = _validate_answers(product_type, data)
    required_ids = [q["id"] for q in _questions(product_type) if q.get("required")]
    filled = [qid for qid in answers]
    missing = [qid for qid in required_ids if qid not in answers]
    return {"product_type": product_type, "answers": answers, "filled": filled,
            "missing": missing, "hint_coverage": coverage}


def _validate_answers(product_type: str, raw: dict) -> dict:
    """Отбрасывает null и значения вне списка допустимых. Приводит типы."""
    clean: dict = {}
    if not isinstance(raw, dict):
        return clean
    for qid, val in raw.items():
        q = _by_id(product_type, qid)
        if q is None or val is None:
            continue
        qtype = q["type"]
        if qtype in ("single",):
            allowed = {o["value"] for o in q.get("options", [])}
            if val in allowed:
                clean[qid] = val
        elif qtype == "multi":
            allowed = {o["value"] for o in q.get("options", [])}
            picked = [v for v in val if v in allowed] if isinstance(val, list) else []
            if picked:
                clean[qid] = picked
        elif qtype == "int":
            try:
                clean[qid] = int(str(val).strip().split()[0])
            except (ValueError, IndexError):
                pass
        else:  # text / matrix
            if str(val).strip():
                clean[qid] = val
    return clean


# ───────────────────────── что осталось спросить ─────────────────────────
def remaining_questions(product_type: str, answers: dict) -> list[dict]:
    """
    Обязательные вопросы, на которые ещё нет ответа, в порядке этап→вопрос.
    Учитывает depends_on: если зависимость не заполнена, вопрос помечается
    флагом 'blocked' (спросим зависимость раньше).
    """
    out = []
    for q in _questions(product_type):
        if not q.get("required", True):
            continue
        if q["id"] in answers:
            continue
        item = dict(q)
        dep = q.get("depends_on")
        item["blocked"] = bool(dep and dep.get("id") not in answers)
        out.append(item)
    out.sort(key=lambda x: (x.get("stage", 99), x.get("blocked", False)))
    return out


def is_complete(product_type: str, answers: dict) -> bool:
    """Все обязательные поля заполнены?"""
    return len(remaining_questions(product_type, answers)) == 0


# ───────────────────────── рендер вопроса для веб-чата ─────────────────────────
def render_question(q: dict) -> str:
    """Человекочитаемый вопрос с вариантами (для показа в чате)."""
    lines = [q.get("ask") or q["label"]]
    if q.get("help"):
        lines.append(f"({q['help']})")
    if q.get("options"):
        for i, o in enumerate(q["options"], 1):
            lines.append(f"  {i}. {o['label']}")
    elif q.get("unit"):
        lines.append(f"  (в {q['unit']})")
    return "\n".join(lines)
