# -*- coding: utf-8 -*-
"""
Сборка одной позиции-изделия для КП.

Связывает всё воедино:
  опросник/документ  →  марка изделия + состав  →  цена = Sum(покупные) × k  →  позиция для kp_generator.generate_kp()

Цена считается по коэффициентному методу заказчика:
  итог = Sum(рыночная стоимость покупных узлов) × k(сегмент),
где k покрывает корпус, сборку, работу и маржу (см. pricing_coefficients.json).

Sum передаётся снаружи (её даёт ценовой слой: ETM/номенклатура или рыночная
прикидка) — этот модуль отвечает за идентификацию изделия, состав и применение k.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

COEFF_PATH = Path(__file__).resolve().parent.parent / "pricing_coefficients.json"

_COEFF_CACHE: Optional[dict] = None


def _coeffs() -> dict:
    global _COEFF_CACHE
    if _COEFF_CACHE is None:
        try:
            with open(COEFF_PATH, encoding="utf-8") as f:
                _COEFF_CACHE = json.load(f)
        except FileNotFoundError:
            logger.error("pricing_coefficients.json не найден: %s", COEFF_PATH)
            _COEFF_CACHE = {"segments": [], "default_k": 3.0}
    return _COEFF_CACHE


def get_coefficient(product_type: str, power_kva: Optional[float] = None,
                    corpus: Optional[str] = None) -> dict:
    """
    Подбирает k для сегмента. Возвращает {k, segment, matched}.
    Матч по типу изделия + (если задано) диапазону мощности и типу корпуса.
    Если точного сегмента нет — default_k.
    """
    data = _coeffs()
    best = None
    for seg in data.get("segments", []):
        if seg.get("product_type") != product_type:
            continue
        if power_kva is not None:
            lo = seg.get("power_min_kva", 0)
            hi = seg.get("power_max_kva", 10 ** 9)
            if not (lo <= power_kva <= hi):
                continue
        if corpus and seg.get("corpus") and seg["corpus"] != corpus:
            continue
        best = seg
        break
    if best:
        return {"k": best["k"], "segment": best, "matched": True}
    return {"k": data.get("default_k", 3.0), "segment": None, "matched": False}


def build_izdelie_position(
    marka: str,
    composition_lines: list[str],
    sum_components: float,
    product_type: str,
    power_kva: Optional[float] = None,
    corpus: Optional[str] = None,
    extra_lines: Optional[list[str]] = None,
) -> dict:
    """
    Собирает ОДНУ позицию-изделие для КП.

    marka             — «КТПк-СК-КК-1000кВА-10/0,4кВ У1» (из документа или собранная из опросника)
    composition_lines — состав: ['Трансформатор ТМГ-1000 ...', 'ВНА 10-630', ...]
    sum_components    — Sum рыночной/ETM стоимости покупных узлов (даёт ценовой слой)
    product_type      — 'KTP' | 'KRUN'
    power_kva, corpus — для выбора коэффициента сегмента

    Возвращает dict, готовый для kp_generator.generate_kp(positions=[...]):
      {'name': 'марка в составе: ...', 'price': итог, 'qty': 1, + служебные поля}
    """
    coeff = get_coefficient(product_type, power_kva, corpus)
    price = round(float(sum_components) * coeff["k"], 2)

    # Формат имени под шаблон: генератор сам разобьёт часть после "в составе:" на подпункты
    lines = list(composition_lines)
    if extra_lines:
        lines += extra_lines
    name = f"{marka} в составе: " + "; ".join(l.strip().rstrip(";") for l in lines if l.strip())

    if not coeff["matched"]:
        logger.warning("Коэффициент для %s (%s кВА, %s) не найден — использован default k=%.2f",
                       product_type, power_kva, corpus, coeff["k"])

    return {
        "name": name,
        "price": price,
        "qty": 1,
        # служебные поля (в КП не идут, но полезны для лога/отладки/подтверждения)
        "_marka": marka,
        "_sum_components": round(float(sum_components), 2),
        "_coefficient": coeff["k"],
        "_coefficient_matched": coeff["matched"],
        "_estimate": True,  # бюджетная оценка — как в шаблоне КП
    }


if __name__ == "__main__":
    # Демонстрация на КТПк-1000: Sum покупных ~950к, k подберётся для kiosk/1000
    pos = build_izdelie_position(
        marka="КТПк-СК-КК-1000кВА-10/0,4кВ У1",
        composition_lines=[
            "Трансформатор ТМГ-1000 кВА 10/0,4 кВ Д/Ун-11",
            "РУВН: выключатель нагрузки ВНА 10-630, предохранители ПТ 1.3-10-100 (3шт), ОПН-П-10 (3шт)",
            "РУНН: разъединитель РЕ-19-43, автоматы ВА57-39-400А (4шт), ВА57-35-250А (1шт), ОПН-П-0,4 (3шт)",
            "Сборные шины АД31Т 10х100",
        ],
        sum_components=950_000,
        product_type="KTP",
        power_kva=1000,
        corpus="kiosk",
    )
    import json as _j
    print(_j.dumps(pos, ensure_ascii=False, indent=2))
    print(f"\nИтог = {pos['_sum_components']:,.0f} × k{pos['_coefficient']} = {pos['price']:,.2f} ₽"
          .replace(",", " "))
