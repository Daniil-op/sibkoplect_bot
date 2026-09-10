# -*- coding: utf-8 -*-
"""
Проверка проекта на соответствие ключевым нормам ПУЭ-7 — на чистом Python,
БЕЗ обращения к нейросети. Берёт распознанные позиции оборудования и ответы
опросника, считает токи и проверяет конкретные, считаемые правила из
pue_rules.py, а на выходе даёт список замечаний с рекомендациями («корректировки»).

Работает даже когда ключ YandexGPT недоступен — это чистая арифметика/логика.
Набор правил — стартовый и расширяемый: добавить новое правило = дописать функцию.

Использование:
    from core import compliance
    block = compliance.run(items, answers, product_type, power_kva)
    # block — готовый текст «Проверка по нормам ПУЭ-7: ...» для КП/ответа
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── разбор токов / мощности ───────────────────────────
def _amps(text: str) -> list[int]:
    """Все номиналы тока (А), которые видно в строке: 'In=400', '400А', '50A'."""
    t = text.lower()
    vals = []
    for m in re.finditer(r'in\s*=?\s*(\d{1,4})', t):
        vals.append(int(m.group(1)))
    for m in re.finditer(r'(\d{1,4})\s*[аa]\b', t):   # 400А (кир.) и 50A (лат.)
        vals.append(int(m.group(1)))
    return [v for v in vals if 5 <= v <= 6300]


def _amp(text: str) -> Optional[int]:
    v = _amps(text)
    return max(v) if v else None


def _kva(text: str) -> Optional[float]:
    m = re.search(r'(\d{2,4})\s*(?:ква|kva|кв\.?\s?а)', text.lower())
    if m:
        return float(m.group(1))
    for n in re.findall(r'\d{2,4}', text):
        if int(n) in (25, 40, 63, 100, 160, 250, 400, 630, 1000, 1250, 1600, 2500, 3200):
            return float(n)
    return None


# ─────────────────────────── распознавание типов ───────────────────────────
def _is_transformer(t: str) -> bool:
    if not re.search(r'тмг|\bтм[\-\s]?\d|тсл|масл|силовой трансформатор', t):
        return False
    return not re.search(r'тока|напряж|собствен|нулев|\bтсн\b|олсп|\bтол\b|знолп|тзлк', t)


def _is_dry_transformer(t: str) -> bool:
    return bool(re.search(r'тсл|тсз|сух', t))


def _is_lv_breaker(t: str) -> bool:
    if 'нагрузк' in t:            # выключатель нагрузки — это не автомат
        return False
    return bool(re.search(r'ва\s?57|\btem\b|tem7|\btgb\b|tgb3|автоматич|автомат', t))


def _is_input_device(t: str) -> bool:
    return bool(re.search(r'рубильник|\bре[\-\s]?19\b|ре19|вводн', t))


def _is_meter(t: str) -> bool:
    return bool(re.search(r'меркурий|сч[её]тчик|прибор\s*уч', t))


def _is_ct(t: str) -> bool:
    return bool(re.search(r'\bтол\b|трансформатор тока|\d+/5', t))


# ─────────────────────────── правила ───────────────────────────
def check_compliance(items: list[dict], answers: Optional[dict] = None,
                     product_type: Optional[str] = None,
                     power_kva: Optional[float] = None) -> list[dict]:
    """Возвращает список замечаний: [{level, msg, rec}]. Пусто = замечаний нет."""
    answers = answers or {}
    findings: list[dict] = []

    texts: list[tuple[str, int]] = []
    for it in items:
        name = (it.get("name") or "").strip()
        params = (it.get("parameters") or "").strip()
        if not name and not params:
            continue
        try:
            qty = int(it.get("quantity", 1) or 1)
        except (ValueError, TypeError):
            qty = 1
        texts.append((f"{name} {params}".lower(), qty))

    def any_match(pred) -> bool:
        return any(pred(t) for t, _ in texts)

    # --- мощность трансформатора и ток Iн.тр на 0,4 кВ ---
    kva: Optional[float] = None
    try:
        kva = float(power_kva) if power_kva else None
    except (ValueError, TypeError):
        kva = None
    if kva is None:
        for t, _ in texts:
            if _is_transformer(t):
                kva = _kva(t)
                if kva:
                    break
    intr = kva * 1000 / (1.732 * 400) if kva else None   # ток трансформатора на 0,4 кВ

    # --- число трансформаторов ---
    tcount = sum(q for t, q in texts if _is_transformer(t))
    try:
        tcount = max(tcount, int(answers.get("ktp_transformer_count") or 0))
    except (ValueError, TypeError):
        pass

    # --- напряжение сети ВН ---
    volt = str(answers.get("krun_voltage") or answers.get("ktp_vn_class") or "").strip()
    if volt not in ("6", "10", "35"):
        if any_match(lambda t: 'опн' in t and '-6' in t):
            volt = "6"
        else:
            volt = "10"

    is_ru = bool(product_type) or any_match(_is_transformer) or any_match(lambda t: 'опн' in t)

    # ── Правило 1: сумма отходящих 0,4 кВ vs ток трансформатора (+5%) ──
    # (только для однотрансформаторных — иначе сумма по секциям даёт ложные срабатывания)
    if intr and tcount <= 1:
        out_sum = sum((_amp(t) or 0) * q for t, q in texts if _is_lv_breaker(t))
        if out_sum and out_sum > intr * 1.05:
            findings.append({
                "level": "warning",
                "msg": (f"Сумма номиналов отходящих автоматов 0,4 кВ ≈ {out_sum:.0f} А превышает "
                        f"номинальный ток трансформатора ({intr:.0f} А) даже с учётом 5% перегруза "
                        f"({intr * 1.05:.0f} А)."),
                "rec": ("Проверить одновременность нагрузок. При полной загрузке всех линий возможна "
                        "перегрузка трансформатора — рассмотреть трансформатор большей мощности или "
                        "снижение номиналов отходящих."),
            })

    # ── Правило 2: вводной аппарат 0,4 кВ ≤ 1,4·Iн.тр (защита трансформатора) ──
    if intr:
        for t, _ in texts:
            if _is_input_device(t):
                a = _amp(t)
                if a and a > 1.4 * intr * 1.02:
                    findings.append({
                        "level": "violation",
                        "msg": (f"Номинал вводного аппарата 0,4 кВ ({a} А) превышает 1,4·Iн.тр "
                                f"({1.4 * intr:.0f} А) — защита трансформатора по току не обеспечена."),
                        "rec": ("Снизить номинал вводного автомата/расцепителя до ≤ 1,4·Iн.тр либо "
                                "увеличить мощность трансформатора."),
                    })
                break

    # ── Правило 3: ОПН на стороне ВН — наличие и класс напряжения ──
    if is_ru:
        hv_opn = [t for t, _ in texts if 'опн' in t and not ('0,4' in t or '0.4' in t)]
        if not hv_opn:
            findings.append({
                "level": "warning",
                "msg": "Не найдены ОПН на стороне ВН (защита от коммутационных и грозовых перенапряжений).",
                "rec": f"Предусмотреть ограничители перенапряжений ОПН-{volt} на вводе ВН.",
            })
        else:
            for t in hv_opn:
                m = re.search(r'опн[\-\s]*п?[\-\s]*(\d{1,3})', t)
                cls = m.group(1) if m else None
                if cls in ("6", "10", "35") and cls != volt:
                    findings.append({
                        "level": "violation",
                        "msg": f"Класс ОПН ({cls} кВ) не соответствует напряжению сети ({volt} кВ).",
                        "rec": f"Установить ОПН класса {volt} кВ.",
                    })
                    break

    # ── Правило 4: два масляных трансформатора > 0,63 МВ·А в одном сооружении (п. 4.2.98) ──
    if tcount >= 2 and kva and kva > 630 and any_match(lambda t: _is_transformer(t) and not _is_dry_transformer(t)):
        findings.append({
            "level": "violation",
            "msg": (f"Два масляных трансформатора по {kva:.0f} кВА (> 630 кВА) в одном сооружении. "
                    f"П. 4.2.98 ПУЭ допускает в одном помещении до двух масляных трансформаторов "
                    f"мощностью ≤ 0,63 МВ·А каждый."),
            "rec": ("Разделить трансформаторы противопожарной перегородкой (предел огнестойкости 45 мин) "
                    "или применить сухие трансформаторы (ТСЛ/ТСЗ)."),
        })

    # ── Правило 5: учёт при токах > 100 А без трансформаторов тока ──
    if any_match(_is_meter) and not any_match(_is_ct):
        if any((_amp(t) or 0) > 100 for t, _ in texts if _is_lv_breaker(t)):
            findings.append({
                "level": "warning",
                "msg": "Учёт электроэнергии при токах свыше 100 А без трансформаторов тока (прямое включение счётчика).",
                "rec": "Установить трансформаторы тока (…/5 А) для учёта на линиях с током более 100 А.",
            })

    return findings


# ─────────────────────────── формат для КП/ответа ───────────────────────────
_ICON = {"violation": "❗", "warning": "⚠️"}


def format_findings(findings: list[dict]) -> str:
    if not findings:
        return "✅ Проверка по ключевым нормам ПУЭ-7: замечаний не выявлено."
    viol = sum(1 for f in findings if f["level"] == "violation")
    warn = sum(1 for f in findings if f["level"] == "warning")
    head = f"🔎 Проверка по нормам ПУЭ-7: нарушений — {viol}, предупреждений — {warn}."
    lines = [head, ""]
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. {_ICON.get(f['level'], '•')} {f['msg']}")
        lines.append(f"   Рекомендация: {f['rec']}")
    lines.append("")
    lines.append("Проверка предварительная, на основе распознанного оборудования; не заменяет экспертизу проекта.")
    return "\n".join(lines)


def run(items: list[dict], answers: Optional[dict] = None,
        product_type: Optional[str] = None, power_kva: Optional[float] = None) -> str:
    """Готовый текстовый блок проверки для вставки в ответ/КП."""
    try:
        return format_findings(check_compliance(items, answers, product_type, power_kva))
    except Exception as exc:                       # проверка не должна ронять генерацию КП
        logger.warning("compliance check error: %s", exc)
        return ""


if __name__ == "__main__":
    examples = {
        "КТПк-СК-КК-1000 (1 тр-р, 1000 кВА)": (
            "KTP", 1000, {"ktp_transformer_count": "1", "ktp_vn_class": "10"},
            [
                {"name": "Трансформатор ТМГ-1000 кВА 10/0,4 кВ", "quantity": 1},
                {"name": "Выключатель нагрузки ВНА 10-630", "quantity": 1},
                {"name": "Предохранители ПТ 1.3-10-100", "quantity": 3},
                {"name": "ОПН-П-10", "quantity": 3},
                {"name": "Разъединитель РЕ-19-43 In=1600А", "quantity": 1},
                {"name": "Автомат ВА57-39-400А", "quantity": 4},
                {"name": "Автомат ВА57-35-250А", "quantity": 1},
                {"name": "ОПН-П-0,4", "quantity": 3},
            ],
        ),
        "2КТПБ-СК-КК-25 (2 тр-ра, 25 кВА)": (
            "KTP", 25, {"ktp_transformer_count": "2", "ktp_vn_class": "6"},
            [
                {"name": "Трансформатор ТМГ-25 кВА 6/0,4 кВ", "quantity": 2},
                {"name": "Выключатель нагрузки ВНАл-10/630", "quantity": 2},
                {"name": "ОПН-П-6", "quantity": 6},
                {"name": "Автомат TEM7-125L 50A", "quantity": 4},
                {"name": "Автомат TGB3-63H 16A", "quantity": 10},
                {"name": "ОПН-П-0,4", "quantity": 6},
                {"name": "Счётчик Меркурий 230", "quantity": 2},
                {"name": "Трансформатор тока ТОЛ 50/5", "quantity": 12},
            ],
        ),
        "КРУН-СК-10 (без тр-ра)": (
            "KRUN", None, {"krun_voltage": "10"},
            [
                {"name": "Силовой выключатель КЭПС-КМ 10-25/1000", "quantity": 1},
                {"name": "Разъединитель РВЗ 10/1000", "quantity": 2},
                {"name": "ОПН-П-10", "quantity": 3},
                {"name": "Трансформатор тока ТОЛ-10 200/5", "quantity": 2},
                {"name": "Счётчик Меркурий 230 ART", "quantity": 1},
            ],
        ),
        "Пример с нарушением ОПН (ОПН-6 в сети 10 кВ, нет ОПН-0,4)": (
            "KTP", 400, {"ktp_transformer_count": "1", "ktp_vn_class": "10"},
            [
                {"name": "Трансформатор ТМГ-400 кВА 10/0,4 кВ", "quantity": 1},
                {"name": "ОПН-П-6", "quantity": 3},
                {"name": "Автомат ВА57-39-250А", "quantity": 2},
            ],
        ),
    }
    for title, (ptype, kva, ans, items) in examples.items():
        print("=" * 70)
        print(title)
        print(run(items, ans, ptype, kva))
        print()