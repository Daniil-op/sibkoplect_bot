# -*- coding: utf-8 -*-
"""
Диалог опросника — связывает всю цепочку в рабочий флоу веб-чата:

  загрузка файла(ов)
     → begin(): определить изделие + предзаполнить из документа + собрать состав
     → задать только недостающие вопросы (по одному)
     → подтверждение конфигурации
     → generate(): Σ(покупные из ETM) × k(сегмент) → одно изделие → КП по шаблону

Состояние диалога живёт на session.dialog. Роутинг:
  /api/search → dialog.begin(session)
  /api/chat   → если is_active(session): dialog.handle(session, text)
"""

import re
import logging
from typing import Optional

from core import questionnaire as Q
from core import izdelie as IZ

logger = logging.getLogger(__name__)


# ─────────────────────────── извлечение марки изделия ───────────────────────────
# Марка СибКомплект всегда содержит "-СК-": КТПк-СК-КК-1000кВА-10/0,4кВ У1,
# 2КТПБ-СК-КК-25кВА-6/0,4кВ УХЛ1, КРУН-СК-10-У1.
_MARKA_RE = re.compile(
    r'(\d?\s?(?:2?КТП[а-яА-Яa-zA-Z]*|КРУН|КСО|ЯКНО)[\-\s]?СК[\-]'
    r'[А-Яа-яA-Za-z0-9\-/,\.]+?(?:\s?(?:УХЛ\d|У\d)))',
    re.IGNORECASE,
)


def extract_marka(text: str) -> Optional[str]:
    """Достаёт марку изделия из текста документа (она обычно написана в шапке)."""
    if not text:
        return None
    m = _MARKA_RE.search(text)
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).strip()
    return None


# ─────────────────────────── толкование ответа клиента ───────────────────────────
def interpret_answer(question: dict, text: str):
    """Переводит свободный ответ клиента в значение поля (по номеру или по смыслу)."""
    text = (text or "").strip()
    qtype = question.get("type")
    opts = question.get("options", [])

    if qtype in ("single", "multi"):
        picked = []
        # 1) по номеру варианта ("2", "1 и 3")
        for n in re.findall(r'\d+', text):
            i = int(n) - 1
            if 0 <= i < len(opts):
                picked.append(opts[i]["value"])
        # 2) по тексту (значение/подпись)
        if not picked:
            low = text.lower()
            for o in opts:
                label = o["label"].lower()
                if o["value"].lower() in low or label in low or label.split()[0] in low:
                    picked.append(o["value"])
        if qtype == "single":
            return picked[0] if picked else None
        return picked or None

    if qtype == "int":
        m = re.search(r'\d+', text)
        return int(m.group()) if m else None

    # text / matrix — берём как есть
    return text or None


# ─────────────────────────── состав и параметры ───────────────────────────
def _composition_from_items(items: list[dict]) -> list[str]:
    """Строит строки состава из распознанных позиций (parse_tz)."""
    lines = []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        qty = it.get("quantity", 1)
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1
        lines.append(f"{name} ({qty}шт)" if qty > 1 else name)
    return lines


def _corpus_from_answers(answers: dict, product_type: str) -> Optional[str]:
    if product_type == "KRUN":
        return "outdoor"
    c = answers.get("ktp_corpus_type")
    return {"kiosk": "kiosk", "block": "block"}.get(c)


def _power_from_answers(answers: dict) -> Optional[float]:
    p = answers.get("ktp_power_kva")
    try:
        return float(p) if p is not None else None
    except (ValueError, TypeError):
        return None


def _combined_text(session) -> str:
    return "\n".join((d.get("text") or "") for d in getattr(session, "uploaded_docs", []))


def _config_summary(state: dict) -> str:
    """Человекочитаемая сводка собранной конфигурации для подтверждения."""
    ptype = state["product_type"]
    lines = [f"Изделие: **{state.get('marka') or ('КТП' if ptype == 'KTP' else 'КРУН')}**", "", "Собранная конфигурация:"]
    for q in Q._questions(ptype):
        qid = q["id"]
        if qid not in state["answers"]:
            continue
        val = state["answers"][qid]
        label = _value_label(q, val)
        lines.append(f"  • {q['label']}: {label}")
    lines.append("")
    lines.append("Сформировать КП? (да / нет)")
    return "\n".join(lines)


def _value_label(q: dict, val) -> str:
    opts = {o["value"]: o["label"] for o in q.get("options", [])}
    # matrix / text / int или значение без вариантов — рендерим кратко
    if q.get("type") == "matrix" or not opts:
        if isinstance(val, list):
            if val and isinstance(val[0], dict):
                return f"{len(val)} поз."
            return ", ".join(str(v) for v in val)
        return str(val)
    if isinstance(val, list):
        return ", ".join(opts.get(v, str(v)) for v in val)
    return opts.get(val, str(val))


# ─────────────────────────── публичный API диалога ───────────────────────────
def is_active(session) -> bool:
    st = getattr(session, "dialog", None)
    return bool(st) and st.get("phase") in ("asking", "confirm")


async def begin(session) -> dict:
    """
    Стартует опрос после загрузки файлов: определяет изделие,
    предзаполняет из документа и задаёт первый недостающий вопрос.
    """
    text = _combined_text(session)
    if not text.strip():
        return {"response": "Не вижу текста в загруженных файлах. Прикрепите проект (однолинейную схему / спецификацию) ещё раз."}

    pre = await Q.prefill_from_text(text)
    ptype = pre.get("product_type")
    if not ptype:
        return {"response": "Не смог определить тип изделия по документу. Это КТП или КРУН? Напишите, и приложите проект."}

    state = {
        "phase": "asking",
        "product_type": ptype,
        "answers": pre.get("answers", {}),
        "marka": extract_marka(text),
        "composition": _composition_from_items(getattr(session, "all_items", [])),
        "pending": [q["id"] for q in Q.remaining_questions(ptype, pre.get("answers", {}))],
        "idx": 0,
    }
    session.dialog = state

    title = state["marka"] or ("КТП" if ptype == "KTP" else "КРУН")
    filled = len(state["answers"])
    if not state["pending"]:
        state["phase"] = "confirm"
        return {"response": f"Определил изделие: **{title}**. Из документа заполнил {filled} параметров.\n\n" + _config_summary(state)}

    intro = (f"Определил изделие: **{title}**. Из документа заполнил {filled} параметров, "
             f"осталось уточнить {len(state['pending'])}.\n\n")
    return {"response": intro + Q.render_question(Q._by_id(ptype, state["pending"][0]))}


async def handle(session, user_text: str) -> dict:
    """Обрабатывает ответ клиента в активном диалоге. Может вернуть kp_file/kp_filename."""
    state = session.dialog
    ptype = state["product_type"]

    if state["phase"] == "confirm":
        ans = (user_text or "").strip().lower()
        if any(w in ans for w in ["да", "сформир", "готов", "ок", "давай", "yes"]):
            return await generate(session)
        if any(w in ans for w in ["нет", "отмен", "стоп", "измен"]):
            session.dialog = None
            return {"response": "Ок, отменил. Можете прислать другой проект или начать заново."}
        return {"response": "Сформировать КП? Ответьте «да» или «нет»."}

    # phase == "asking"
    qid = state["pending"][state["idx"]]
    q = Q._by_id(ptype, qid)
    val = interpret_answer(q, user_text)

    if val is None:
        return {"response": "Не понял ответ. " + Q.render_question(q)}

    state["answers"][qid] = val
    state["idx"] += 1

    # пересчитываем оставшиеся (ответ мог закрыть зависимые вопросы)
    remaining = [x["id"] for x in Q.remaining_questions(ptype, state["answers"])]
    state["pending"] = remaining
    state["idx"] = 0

    if not remaining:
        state["phase"] = "confirm"
        return {"response": _config_summary(state)}

    return {"response": Q.render_question(Q._by_id(ptype, remaining[0]))}


async def generate(session) -> dict:
    """Считает Σ, применяет k, собирает изделие и генерит КП по шаблону."""
    state = session.dialog
    ptype = state["product_type"]
    answers = state["answers"]

    # Σ покупных узлов — реальные цены из ETM по распознанным позициям
    sum_info = await compute_sum_components(getattr(session, "all_items", []))
    sum_components = sum_info["sum"]

    if sum_components <= 0:
        session.dialog = None
        return {"response": "Не удалось получить цены по позициям (ни одна не нашлась в номенклатуре). "
                            "Проверьте доступ к ETM или уточните имена позиций."}

    marka = state.get("marka") or _compose_marka(ptype, answers)
    corpus = _corpus_from_answers(answers, ptype)
    power = _power_from_answers(answers)

    position = IZ.build_izdelie_position(
        marka=marka,
        composition_lines=state["composition"],
        sum_components=sum_components,
        product_type=ptype,
        power_kva=power,
        corpus=corpus,
    )

    # генерим КП по шаблону (одна позиция-изделие)
    import datetime
    import kp_generator
    from core.config import settings

    output_dir = settings.UPLOADS_DIR / session.session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    kp_filename = f"КП_{ts}.docx"
    kp_path = output_dir / kp_filename

    parsed = getattr(session, "parsed_tz", None) or {}
    try:
        kp_generator.generate_kp(
            output_path=str(kp_path),
            object_name=parsed.get("project_name", "") or "Объект",
            equipment_title=marka,
            positions=[position],
            delivery_address=parsed.get("delivery_address", ""),
        )
    except Exception as exc:
        logger.error("Ошибка генерации КП: %s", exc)
        session.dialog = None
        return {"response": f"Ошибка генерации КП: {exc}"}

    session.dialog = None
    price_str = f"{position['price']:,.2f}".replace(",", " ").replace(".", ",")
    cov = f"{sum_info['found']}/{sum_info['total']}"
    resp = (f"✅ КП готово: **{marka}**\n\n"
            f"💰 Итого с НДС: **{price_str} ₽** "
            f"(Σ покупных × k{position['_coefficient']}; цены по {cov} позициям)\n\n"
            f"⚠️ Бюджетная оценка — может быть скорректирована.")
    return {"response": resp, "kp_file": str(kp_path), "kp_filename": kp_filename}


async def compute_sum_components(items: list[dict]) -> dict:
    """Σ рыночной/ETM стоимости покупных узлов по распознанным позициям."""
    from core.orchestrator import _search_items_in_etm  # ленивый импорт, чтобы не было цикла
    cards = await _search_items_in_etm(items)
    total = 0.0
    found = 0
    for c in cards:
        price = float(c.get("price_with_vat", 0) or 0)
        if price > 0:
            qty = int(c.get("source_qty", 1) or 1)
            total += price * qty
            found += 1
    return {"sum": round(total, 2), "found": found, "total": len(cards), "cards": cards}


def _compose_marka(ptype: str, answers: dict) -> str:
    """Запасная сборка марки из ответов, если в документе её не нашли."""
    if ptype == "KRUN":
        v = answers.get("krun_voltage", "10")
        return f"КРУН-СК-{v}-У1"
    count = answers.get("ktp_transformer_count", "1")
    prefix = "2КТПБ" if str(count) == "2" and _corpus_from_answers(answers, ptype) == "block" else "КТП"
    power = answers.get("ktp_power_kva", "")
    vn = answers.get("ktp_vn_class", "10")
    return f"{prefix}-СК-{power}кВА-{vn}/0,4кВ У1".replace("--", "-")
