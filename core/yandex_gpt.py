"""
Клиент YandexGPT (Yandex Foundation Models API).
"""

import logging
import json
import re
import base64
from pathlib import Path
from typing import Optional
import httpx
from core.config import settings

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TZ_PARSER = """Ты — эксперт по электрооборудованию. Извлекаешь ВСЕ позиции оборудования из проектной документации для поиска в каталоге ETM.ru.

ПОРЯДОК ПРИОРИТЕТОВ при поиске позиций:
1. СНАЧАЛА ищи таблицу "Спецификация оборудования, изделий и материалов" — там самый полный список
2. ЗАТЕМ ищи "Ведомость материалов", "Перечень оборудования", таблицы с колонками "Наименование / Кол / Ед.изм"
3. ЗАТЕМ ищи принципиальные схемы — там обозначения на схеме (Т1, Т2, QF1..., В1, В2 и т.д.)
4. Из текста — если явно упоминается оборудование с количеством

КРИТИЧНО — не пропускай:
- Каждую строку спецификации с количеством > 0
- Трансформаторы (Т1, Т2 — это РАЗНЫЕ позиции если qty=2, или одна позиция qty=2)
- Выключатели вводные и секционные
- Счётчики электроэнергии
- Трансформаторы тока
- Конденсаторные установки
- Заземляющие устройства (заземлители, полоса)
- Кабели и провода с метражом

НЕ включай:
- Строительные и земляные работы (рытьё траншей, засыпка грунта, бетонирование)
- Монтажные и пусконаладочные работы как отдельные позиции
- Документацию (опросные листы, альбомы, ведомости документов)
- Повторяющиеся позиции (если трансформатор T1 и T2 одного типа — одна позиция qty=2)
- Блочно-модульные здания, контейнеры, строительные сооружения
- Площадки обслуживания, лестницы, перила, ограждения
- Монтажные профили, консоли, болты, гайки без конкретного артикула ETM

ПРАВИЛА для поля "name":
- Конкретное наименование: марка + основные параметры
- ХОРОШО: "Трансформатор ТМГФ-1000/10-11 6/0,4кВ", "Выключатель S.Pact ACB1 1600А 3р", "Счётчик Меркурий 230AR-03R"
- ПЛОХО: "Силовой трансформатор", "Выключатель автоматический"

ПРАВИЛА для поля "article":
- Только каталожный артикул производителя (NЕ5503, NC2444, 230AR-03R и т.п.)
- НЕ указывай обозначения документов (ТИ-090, ОРТ.135, DKC-2018.J и т.п.)
- Если артикул неизвестен — пустая строка

ПРАВИЛА для поля "search_name":
- 2-4 слова для поиска в ETM
- Примеры: "трансформатор ТМГФ 1000кВА", "выключатель ACB1 1600А", "счётчик Меркурий 230AR"

Отвечай ТОЛЬКО валидным JSON без markdown-блоков:
{
  "project_name": "название объекта из документа",
  "items": [
    {
      "name": "наименование с маркой и параметрами",
      "search_name": "2-4 слова для поиска в ETM",
      "article": "артикул производителя или пустая строка",
      "manufacturer": "производитель если известен",
      "quantity": 1,
      "unit": "шт/компл/м",
      "parameters": "мощность, ток, напряжение — ключевые параметры",
      "notes": ""
    }
  ],
  "delivery_address": "адрес если указан",
  "notes": ""
}"""


SYSTEM_PROMPT_KP_GENERATOR = """Ты — менеджер компании СИБКОМПЛЕКТ — поставщика электрооборудования.
Составляешь коммерческое предложение на русском языке.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков:
{
  "title": "Коммерческое предложение",
  "client": "наименование объекта",
  "items": [
    {
      "pos": 1,
      "name": "наименование",
      "article": "артикул",
      "manufacturer": "производитель",
      "qty": 1,
      "unit": "шт",
      "price_per_unit": 0.0,
      "price_total": 0.0,
      "in_stock": true,
      "delivery_days": "3-5 дней",
      "url": "",
      "notes": ""
    }
  ],
  "total_no_vat": 0.0,
  "vat_amount": 0.0,
  "total_with_vat": 0.0,
  "delivery_note": "условия доставки",
  "validity_days": 10,
  "payment_terms": "50% предоплата, 50% по факту готовности"
}"""


SYSTEM_PROMPT_CONSULTANT = """Ты — технический консультант компании СИБКОМПЛЕКТ (Новосибирск).
Специализация: электрооборудование, трансформаторные подстанции, низковольтное оборудование, кабельная продукция.

ВАЖНО: Отвечай ТОЛЬКО на вопросы связанные с электрооборудованием, подбором товаров, техническими характеристиками, аналогами, сроками поставки, ценами.

Если вопрос НЕ связан с электрооборудованием или работой с ТЗ — вежливо откажи одной фразой, например:
"Я специализируюсь только на вопросах электрооборудования. Загрузите ТЗ или задайте вопрос по оборудованию."

Отвечай по-русски, конкретно и профессионально."""


class YandexGPTClient:

    def __init__(self):
        self.api_key = settings.YANDEX_API_KEY
        self.folder_id = settings.YANDEX_FOLDER_ID
        self.model_uri = f"gpt://{self.folder_id}/{settings.YANDEX_GPT_MODEL}"
        self.url = settings.YANDEX_GPT_URL

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
            "x-folder-id": self.folder_id,
        }

    async def _call(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        images: list[dict] | None = None,
    ) -> Optional[str]:
        if not self.api_key or not self.folder_id:
            return None

        messages = [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_message},
        ]

        payload = {
            "modelUri": self.model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),
            },
            "messages": messages,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(
                    self.url,
                    headers=self._build_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                return (
                    data.get("result", {})
                    .get("alternatives", [{}])[0]
                    .get("message", {})
                    .get("text", "")
                )
            except Exception as exc:
                logger.error("YandexGPT call failed: %s", exc)
                return None

    async def parse_tz(self, tz_text: str, images: list[dict] | None = None) -> Optional[dict]:
        if images:
            if tz_text and len(tz_text.strip()) > 20:
                user_msg = (
                    "Извлеки ВСЕ позиции оборудования из текста (получен через OCR из изображения).\n"
                    "Ищи таблицы со спецификацией, перечни оборудования, обозначения на схемах.\n"
                    "Не пропускай ни одну позицию с количеством.\n\n"
                    + tz_text
                )
                logger.info("YandexGPT: изображение → используем OCR текст (%d символов)", len(tz_text))
            else:
                logger.info("YandexGPT: изображение без OCR текста — пропускаем")
                return None
        else:
            user_msg = (
                "Извлеки ВСЕ позиции оборудования из документа.\n\n"
                "ВАЖНО: Сначала найди таблицу 'Спецификация оборудования' или 'Ведомость материалов' — "
                "там самый точный список с количествами. "
                "Затем проверь принципиальную схему на предмет оборудования которого нет в спецификации.\n\n"
                "Не пропускай ни одну строку с оборудованием и количеством.\n\n"
                + tz_text
            )

        result = await self._call(
            system_prompt=SYSTEM_PROMPT_TZ_PARSER,
            user_message=user_msg,
            temperature=0.0,
            max_tokens=4000,
            images=images,
        )

        if result is None:
            return self._mock_tz_parse(tz_text or "")

        try:
            clean = _clean_json(result)
            return json.loads(clean)
        except Exception as exc:
            logger.error("TZ JSON parse error: %s | raw: %s", exc, result[:300])
            return None

    async def estimate_project_cost(
        self,
        items: list[dict],
        parsed_tz: dict,
        markup: float = 0.30,
        product_cards: list[dict] | None = None,
    ) -> str:
        """
        Считает оценку стоимости:
        - Найденные в ETM: реальные цены × количество
        - Ненайденные: GPT оценивает рыночную стоимость
        Итого × наценка.
        """
        if not items:
            return ""

        markup_pct = int(markup * 100)
        m = 1 + markup

        # Реальные цены из карточек ETM
        etm_total = 0.0
        etm_found_names = set()
        if product_cards:
            for card in product_cards:
                if card.get("found") and card.get("price_with_vat", 0) > 0:
                    qty = card.get("source_qty") or 1
                    try:
                        qty = float(qty)
                    except Exception:
                        qty = 1
                    etm_total += card["price_with_vat"] * qty
                    etm_found_names.add(card.get("source_name", "").lower())

        # GPT оценивает только ненайденные позиции
        not_found_items = [
            i for i in items
            if i.get("name", "").lower() not in etm_found_names
        ]

        gpt_total_min = 0.0
        gpt_total_max = 0.0

        if not_found_items:
            prompt = (
                "Ты — эксперт по ценам на электрооборудование в России 2024-2025. "
                "Укажи рыночную цену за единицу в рублях с НДС для каждой позиции.\n\n"
                "Справочные цены (используй как ориентир):\n"
                "- Трансформатор силовой масляный 1000кВА 6/0,4кВ: 1500000 — 2500000 руб\n"
                "- Трансформатор силовой 1600кВА: 2000000 — 3500000 руб\n"
                "- КТП/2КТП комплектная подстанция 1000кВА: 3000000 — 6000000 руб\n"
                "- Ячейка КСО-298/КСО-393 10кВ: 150000 — 350000 руб\n"
                "- Конденсаторная установка УКМ58 250квар: 300000 — 600000 руб\n"
                "- РУНН шкаф НН: 80000 — 200000 руб\n"
                "- Выключатель вакуумный 10кВ 630А: 300000 — 600000 руб\n"
                "- Автоматический выключатель 2000-4000А (TGW1N): 350000 — 700000 руб\n"
                "- Автоматический выключатель 630-1250А (TGM1NE): 80000 — 250000 руб\n"
                "- Автоматический выключатель 100-250А: 15000 — 60000 руб\n\n"
                "СТРОГО отвечай ТОЛЬКО валидным JSON без markdown, без пояснений:\n"
                '[{"n": 1, "price_min": 1500000, "price_max": 2500000, "qty": 1}]\n\n'
                "Поле n — номер позиции из запроса (целое число). "
                "НЕ включай текстовые описания в name. Только числа. Один объект на позицию."
            )

            items_list = "\n".join(
                f"{idx+1}. {i.get('name','')} {i.get('parameters','')} — {i.get('quantity',1)} {i.get('unit','шт')}"
                for idx, i in enumerate(not_found_items[:20])
            )
            user_msg = f"Укажи цену за единицу для каждой позиции:\n{items_list}"

            result = await self._call(
                system_prompt=prompt,
                user_message=user_msg,
                temperature=0.1,
                max_tokens=1000,
            )
            if result:
                try:
                    clean = re.sub(r'```[a-z]*|```', '', result).strip()
                    prices = json.loads(clean)
                    if isinstance(prices, list):
                        for idx2, p in enumerate(prices):
                            qty = float(p.get("qty", 1))
                            p_min = float(p.get("price_min", 0))
                            p_max = float(p.get("price_max", p_min * 1.3))
                            gpt_total_min += p_min * qty
                            gpt_total_max += p_max * qty
                            # Получаем название позиции по номеру n
                            n = int(p.get("n", idx2 + 1)) - 1
                            item_name = not_found_items[n]["name"] if 0 <= n < len(not_found_items) else str(p.get("n", idx2+1))
                            logger.info("GPT price: %s × %.0f = %.0f–%.0f ₽",
                                        item_name[:40], qty, p_min*qty, p_max*qty)
                    elif isinstance(prices, dict):
                        gpt_total_min = float(prices.get("total_min", 0))
                        gpt_total_max = float(prices.get("total_max", 0))
                except Exception as exc:
                    logger.warning("estimate GPT parse error: %s | raw: %s", exc, result[:300])

        # Итоговые суммы с наценкой
        base_min = (etm_total + gpt_total_min) * m
        base_max = (etm_total + (gpt_total_max or gpt_total_min)) * m
        if base_max < base_min * 1.05:
            base_max = base_min * 1.15

        std_min = base_min * 1.20
        std_max = base_max * 1.20
        max_min = base_min * 1.50
        max_max = base_max * 1.50

        def fmt(n: float) -> str:
            n = int(n)
            if n >= 1_000_000:
                val = n / 1_000_000
                s = f"{val:.1f}" if val % 1 != 0 else f"{int(val)}"
                return f"{s} млн ₽"
            return f"{n:,} ₽".replace(",", " ")

        parts = []
        if etm_total:
            parts.append(f"ETM: {fmt(etm_total)}")
        if gpt_total_min:
            parts.append(f"ненайденные: ~{fmt(gpt_total_min)}–{fmt(gpt_total_max or gpt_total_min)}")
        source_note = " | ".join(parts)

        lines = [
            f"Предварительная оценка стоимости (в рублях с НДС, включая услуги +{markup_pct}%)",
            "Ориентировочно — уточняйте у менеджера\n",
        ]

        lines += [
            "Базовая (минимальная) комплектация",
            f"~ {fmt(base_min)} — {fmt(base_max)}",
            "Примечание: Основное оборудование, минимальный монтаж и пусконаладка.\n",
            "Стандартная комплектация",
            f"~ {fmt(std_min)} — {fmt(std_max)}",
            "Примечание: Расширенный монтаж, более детальная пусконаладка.\n",
            "Максимальная (полная по ТЗ) стоимость",
            f"~ {fmt(max_min)} — {fmt(max_max)}",
            "Примечание: Полный комплекс работ, дополнительные инженерные решения.",
        ]
        return "\n".join(lines)

    async def check_pue(self, doc_text: str, filename: str = "") -> str:
        # Для фото-схем текст короткий (только подписи/номиналы), но проверять нужно
        if not doc_text or len(doc_text.strip()) < 40:
            return ""

        from core.pue_rules import get_pue_rules

        system_prompt = (
            "Ты — главный инженер проекта (ГИП) по электротехнике с 20-летним опытом. "
            "Ты проводишь ЭКСПЕРТИЗУ проектной документации КТП/РП/РУ на соответствие ПУЭ-7. "
            "Твоя задача — найти КОНКРЕТНЫЕ технические нарушения с точными расчётами, "
            "как это делает реальный эксперт при проверке проекта для заказчика.\n\n"
            + get_pue_rules()
            + """

========================================================================
МЕТОДИКА ПРОВЕРКИ — работай как эксперт, проверяй КАЖДЫЙ применимый пункт
========================================================================

Для КАЖДОЙ проверки ниже: если в документе ЕСТЬ нужные данные — ОБЯЗАТЕЛЬНО выполни расчёт
и сравнение. Показывай вычисления. Не пропускай проверки для которых есть данные.

ПРОВЕРКА 1 — ЗАЩИТА ТРАНСФОРМАТОРА ВВОДНЫМ АВТОМАТОМ:
Найди в проекте: мощность трансформатора (кВА) и номинальный ток расцепителя вводного автомата (А).
Расчёт: Iн.тр = Sном / (1,73 × 0,4). Затем макс.допустимый = 1,4 × Iн.тр.
Сравни: если Iн.расцепителя > 1,4 × Iн.тр → НАРУШЕНИЕ.
Пример вычисления: тр-р 1000 кВА → Iн.тр = 1000000/(1,73×400) = 1443 А → предел 1,4×1443 = 2020 А.
Если автомат имеет расцепитель 2500 А > 2020 А → нарушение п. 4.2 ПУЭ.

ПРОВЕРКА 2 — ТРАНСФОРМАТОРЫ ТОКА НА ВВОДАХ:
Найди: коэффициент трансформации вводных ТТ (напр. 1000/5) и сумму номиналов отходящих автоматов секции.
Сравни: если первичный ток ТТ < суммы токов отходящих линий → ТТ перегружен, НАРУШЕНИЕ.
Покажи: "ТТ 1000/5, сумма отходящих = 630+400+250+... = X А. X > 1000 → нарушение".

ПРОВЕРКА 3 — ПРЯМОЕ ВКЛЮЧЕНИЕ СЧЁТЧИКОВ:
Найди отходящие линии где есть счётчик учёта и номинал автомата.
Если автомат > 100 А, а счётчик включён напрямую (без ТТ) → НАРУШЕНИЕ (нужен ТТ).

ПРОВЕРКА 4 — РАЗМЕРЫ ПРОХОДОВ И КОРИДОРОВ (если есть чертёж плана с размерами в мм):
- РУ-0,4 кВ (РУНН): проход в свету < 800 мм → нарушение п. 4.1.23.
- РУ выше 1 кВ с приводами выключателей: коридор < 1500 мм (односторонний) → нарушение п. 4.2.90.
- Коридор КТП с задней стороны < 800 мм → нарушение п. 4.2.91.
Сравни конкретные размеры с чертежа с нормой.

ПРОВЕРКА 5 — ВЫХОДЫ ИЗ РУ:
Найди длину РУ (сумма ширин ячеек/шкафов) и количество выходов/дверей.
Если длина > 7 м и только один выход → нарушение п. 4.2.94.
Покажи: "длина РУНН = 6 шкафов × 0,8 м = 4,8 м" или "= X м, выходов Y".

ПРОВЕРКА 6 — СЕЧЕНИЕ ШИН РУ-0,4 кВ:
Найди сечение сборных шин и расчётный ток секции.
Сравни с допустимым током сечения (5×50=250А, 5×80=390А, 8×80=480А, 10×100=610А).
Если расчётный ток > допустимого → перегрев, нарушение.

ПРОВЕРКА 7 — КЛАСС НАПРЯЖЕНИЯ ОПН:
Найди ОПН и класс напряжения сети. Если класс ОПН не совпадает с сетью
(напр. ОПН-110 в сети 10 кВ) → грубая ошибка.

ПРОВЕРКА 8 — ПЕРЕГОРОДКИ И ВЕНТИЛЯЦИЯ:
Масляные тр-ры без перегородки между собой (п. 4.2.98). Отсутствие вентиляции камер трансформаторов.

========================================================================
ФОРМАТ ОТВЕТА
========================================================================

Для КАЖДОГО найденного нарушения пиши развёрнуто, как эксперт заказчику:
"N. [Что не так] — [расчёт/сравнение с конкретными числами из проекта] — противоречит п. X.X.XX ПУЭ-7. [Рекомендация что сделать]."

ПРИМЕР ХОРОШЕГО ЗАМЕЧАНИЯ:
"1. Вводной автомат секции 1 выбран неверно. Трансформатор ТМГ-1000 кВА имеет номинальный ток на стороне
0,4 кВ равный 1443 А. Максимально допустимый номинал расцепителя по ПУЭ — 1,4×1443 = 2020 А. В проекте
установлен автомат с расцепителем 2500 А, что превышает допустимое значение на 480 А. Это противоречит
требованиям защиты трансформатора от перегрузки. Рекомендуется применить автомат с расцепителем не более 2000 А."

СТРОГИЕ ПРАВИЛА:
- Проверяй ТОЧНО с расчётами, а не приблизительно. Показывай вычисления с реальными числами из проекта.
- Бери числа ТОЛЬКО из документа. Не выдумывай данные которых нет.
- НЕ пиши общие фразы "рекомендуется проверить", "возможно нарушение", "нет данных".
- Если для проверки нет данных в документе — просто пропусти эту проверку молча, не упоминай её.
- Если реальных нарушений НЕТ — ответь строго одной строкой: НЕТ НАРУШЕНИЙ
- Пиши простым текстом. Не используй LaTeX, символы $, квадратные скобки [], спецсимволы.
- Нумеруй нарушения: 1. 2. 3.
- До 12 нарушений. Каждое — конкретное, с числами и пунктом ПУЭ."""
        )

        user_msg = (
            f"Проведи экспертизу проекта '{filename}' на соответствие ПУЭ-7. "
            f"Внимательно найди в документе: мощности трансформаторов, номиналы вводных и отходящих "
            f"автоматов, коэффициенты трансформации ТТ, сечения шин, размеры проходов, счётчики учёта. "
            f"Выполни расчёты и сравнения по методике. Пиши конкретные нарушения с числами.\n\n"
            f"=== ПОЛНОЕ СОДЕРЖИМОЕ ПРОЕКТА ===\n{doc_text[:16000]}"
        )

        result = await self._call(
            system_prompt=system_prompt,
            user_message=user_msg,
            temperature=0.0,
            max_tokens=3000,
        )

        if not result:
            return ""

        result = result.strip()
        if "НЕТ НАРУШЕНИЙ" in result.upper() or len(result) < 20:
            return ""

        # Чистим LaTeX и математическую разметку которую иногда добавляет GPT
        result = _clean_latex(result)

        return result

    async def generate_kp(
        self,
        parsed_items: list[dict],
        product_cards: list[dict],
        project_name: str = "",
        delivery_address: str = "",
        notes: str = "",
    ) -> Optional[dict]:
        context = {
            "project_name": project_name,
            "delivery_address": delivery_address,
            "notes": notes,
            "requested_items": parsed_items,
            "found_in_catalog": product_cards,
        }
        result = await self._call(
            system_prompt=SYSTEM_PROMPT_KP_GENERATOR,
            user_message="Сформируй КП:\n\n" + json.dumps(context, ensure_ascii=False, indent=2),
            temperature=0.2,
            max_tokens=6000,
        )
        if result is None:
            return self._mock_kp(parsed_items, product_cards)
        try:
            return json.loads(_clean_json(result))
        except Exception as exc:
            logger.error("KP JSON parse error: %s", exc)
            return None

    async def chat(self, message: str, history: list[dict] | None = None) -> str:
        if not self.api_key or not self.folder_id:
            return self._mock_chat(message)

        messages = [{"role": "system", "text": SYSTEM_PROMPT_CONSULTANT}]
        if history:
            for item in history[-10:]:
                messages.append({"role": item.get("role", "user"), "text": item.get("text", "")})
        messages.append({"role": "user", "text": message})

        payload = {
            "modelUri": self.model_uri,
            "completionOptions": {"stream": False, "temperature": 0.5, "maxTokens": "2000"},
            "messages": messages,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(self.url, headers=self._build_headers(), json=payload)
                response.raise_for_status()
                data = response.json()
                return (
                    data.get("result", {})
                    .get("alternatives", [{}])[0]
                    .get("message", {})
                    .get("text", "Не удалось получить ответ.")
                )
            except Exception as exc:
                logger.error("YandexGPT chat failed: %s", exc)
                return f"Ошибка: {exc}"

    def _mock_tz_parse(self, tz_text: str) -> dict:
        return {
            "project_name": "Демо проект",
            "items": [
                {
                    "name": "Трансформатор масляный ТМГ-1000/10/0,4",
                    "search_name": "трансформатор ТМГ 1000",
                    "article": "ТМГ-1000/10/0,4",
                    "quantity": 2, "unit": "шт",
                    "parameters": "1000 кВА, 10/0,4 кВ", "notes": "",
                },
            ],
            "delivery_address": "",
            "notes": "⚠️ ДЕМО-режим.",
        }

    def _mock_kp(self, items: list, cards: list) -> dict:
        return {
            "title": "Коммерческое предложение", "client": "",
            "items": [], "total_no_vat": 0.0, "vat_amount": 0.0,
            "total_with_vat": 0.0, "delivery_note": "", "validity_days": 10,
            "payment_terms": "50% предоплата, 50% по факту готовности",
        }

    def _mock_chat(self, message: str) -> str:
        return "⚠️ *Демо-режим*: YandexGPT API не настроен."


    async def select_best_candidate(self, query: str, candidates: list[dict]) -> Optional[dict]:
        """
        GPT выбирает наиболее подходящий товар из кандидатов ETM.
        Возвращает выбранный товар или None если ни один не подходит.
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        items_desc = "\n".join(
            f"{i+1}. {c.get('name', '')} | Арт: {c.get('article', c.get('brand_code', ''))}"
            for i, c in enumerate(candidates)
        )

        prompt = (
            "Ты — эксперт по электрооборудованию. "
            "Выбери из списка наиболее подходящий товар для запроса. "
            "Критерии выбора:\n"
            "- Совпадает тип оборудования (выключатель, трансформатор, ОПН и т.д.)\n"
            "- Совпадает класс напряжения (10кВ ≠ 110кВ, 0.4кВ ≠ 10кВ)\n"
            "- Совпадает номинальный ток или мощность (допускается ±20%)\n"
            "- НЕ выбирай если тип совсем другой (патч-корд вместо кабеля, стриппер вместо кабеля)\n"
            "- НЕ выбирай если напряжение в 5+ раз отличается\n\n"
            "Отвечай ТОЛЬКО цифрой: номер подходящего товара (1-5) или 0 если ни один не подходит."
        )
        user_msg = f"Запрос: {query}\n\nКандидаты:\n{items_desc}"

        result = await self._call(
            system_prompt=prompt,
            user_message=user_msg,
            temperature=0.0,
            max_tokens=10,
        )

        if result:
            try:
                n = int(result.strip())
                if 1 <= n <= len(candidates):
                    chosen = candidates[n - 1]
                    logger.info("GPT выбрал кандидата %d: %s", n, chosen.get("name", "")[:60])
                    return chosen
                else:
                    logger.info("GPT: ни один кандидат не подошёл для '%s'", query[:40])
                    return None
            except Exception:
                return candidates[0]  # fallback на первого
        return candidates[0]


    async def build_kp_positions(self, items: list[dict], product_cards: list[dict], markup: float = 0.30) -> dict:
        """
        Определяет тип объекта (КТП/РП) и группирует позиции в формат КП.
        Возвращает: {
            "object_type": "КТП" | "РП",
            "equipment_title": "...",
            "positions": [{"name": "...", "price": ..., "qty": ...}]
        }
        """
        # Собираем ВСЕ распознанные позиции с параметрами и количеством
        items_text = "\n".join(
            f"{idx+1}. {i.get('name', '')} | параметры: {i.get('parameters', '')} | кол-во: {i.get('quantity', 1)} {i.get('unit', 'шт')}"
            for idx, i in enumerate(items[:60])
        )

        # Цены из ETM
        etm_prices = {}
        for card in (product_cards or []):
            if card.get("found") and card.get("price_with_vat", 0) > 0:
                etm_prices[card.get("source_name", "").lower()] = card["price_with_vat"]

        prompt = """Ты — инженер СибКомплект, составляешь технико-коммерческое предложение (КП) на электрооборудование КТП/РП.

ЗАДАЧА: определи тип объекта и сгруппируй ВСЁ оборудование проекта в позиции КП с МАКСИМАЛЬНО ПОДРОБНЫМ описанием состава.

ТИПЫ ОБЪЕКТОВ:
1. КТП (комплектная трансформаторная подстанция) — есть трансформатор + РУ. Три позиции:
   - РУВН — высоковольтная часть 6-10кВ: ячейки КСО, выключатели нагрузки ВНА, вакуумные выключатели, разъединители, трансформаторы тока ВН, ОПН, предохранители ПКТ
   - РУНН — низковольтная часть 0,4кВ: шкафы НКУ (вводные, секционные, линейные), автоматические выключатели, трансформаторы тока НН, счётчики учёта, приборы
   - Трансформатор силовой — ТМГ/ТСЛ/ТМГФ

2. РП (распределительный пункт) — НЕТ трансформатора. Позиции:
   - РУВН (ячейки КСО, вакуумные выключатели)
   - РЗА (релейная защита, микропроцессорные терминалы)

КРИТИЧЕСКИ ВАЖНО про описание "name":
Описание КАЖДОЙ позиции должно быть ОЧЕНЬ ПОДРОБНЫМ — перечисли ВСЁ оборудование с ТОЧНЫМИ марками и КОЛИЧЕСТВОМ, как в спецификации.

ОБРАЗЕЦ правильного описания РУВН:
"Распределительное устройство высокого напряжения РУВН (с кабельными перемычками РУ-10кВ - Трансформаторы), в составе ячеек серии КСО-СК-312 одностороннего обслуживания на базе выключателей типа ВНА-10/630-20з: вводная ячейка с выключателем нагрузки ВНА-10/630 – 2 шт.; трансформаторная ячейка с предохранителями ПКТ-10 – 2 шт.; ячейка секционного выключателя – 1 шт.; ячейка секционного разъединителя – 1 шт.; трансформаторы тока ТОЛ-10 – 6 шт.; ОПН-10 – 6 шт."

ОБРАЗЕЦ правильного описания РУНН:
"Распределительное устройство низкого напряжения РУНН на базе НКУ, степень секционирования 2b, с автоматическими выключателями: шкаф вводной с автоматом TGW1N-4000 4000А – 2 шт.; шкаф секционный с автоматом TGW1N-2000 2000А – 1 шт.; шкаф линейный с автоматами TGM1NE-250 250А – 4 шт., TGM1NE-100 100А – 6 шт.; трансформаторы тока ТШП-0,66 – 12 шт.; счётчик Меркурий 230 ART-03 – 2 шт.; шкаф АВР."

ОБРАЗЕЦ описания трансформатора:
"Трансформатор силовой масляный ТМГФ-1000/10-11 У1, мощность 1000 кВА, напряжение 6/0,4 кВ, схема соединения D/Yн-11, с системой охлаждения, вводами ВН и НН."

ПРАВИЛА:
- Бери ТОЧНЫЕ марки и количество из списка оборудования проекта
- Перечисляй КАЖДЫЙ тип оборудования — не обобщай "коммутационные аппараты", а пиши конкретно "выключатель ВНА-10/630 – 2 шт."
- Указывай количество каждой позиции через тире с "шт."
- В поле "includes" — точные названия для расчёта цены
- Количество комплектов: РУВН=1, РУНН=1, трансформаторы по факту (если 2 одинаковых → qty=2)
- НЕ включай: здания, БМЗ, лестницы, площадки, строительные/монтажные работы, заземлители, полосу, траншеи

Отвечай СТРОГО в JSON без markdown:
{
  "object_type": "КТП",
  "equipment_title": "Комплектная трансформаторная подстанция внутреннего исполнения 2КТПВ-... (полное название из проекта)",
  "positions": [
    {"category": "РУВН", "name": "ПОДРОБНОЕ описание со всеми марками и количеством как в образце выше", "qty": 1, "includes": ["ВНА-10/630", "КСО-СК-312", "ТОЛ-10", "ОПН-10"]},
    {"category": "РУНН", "name": "ПОДРОБНОЕ описание со всеми шкафами, автоматами, счётчиками и количеством", "qty": 1, "includes": ["TGW1N-4000", "TGM1NE-250", "Меркурий 230", "ТШП-0,66"]},
    {"category": "Трансформатор", "name": "ПОДРОБНОЕ описание трансформатора с мощностью, напряжением, схемой", "qty": 2, "includes": ["ТМГФ-1000"]}
  ]
}"""

        result = await self._call(
            system_prompt=prompt,
            user_message=f"ВСЁ оборудование из проекта (используй ВСЕ позиции в описании):\n{items_text}",
            temperature=0.2,
            max_tokens=4000,
        )

        if not result:
            return {"object_type": "КТП", "equipment_title": "Комплектная трансформаторная подстанция", "positions": []}

        try:
            clean = re.sub(r'```[a-z]*|```', '', result).strip()
            data = json.loads(clean)
        except Exception as exc:
            logger.warning("build_kp_positions parse error: %s | raw: %s", exc, result[:300])
            return {"object_type": "КТП", "equipment_title": "Оборудование", "positions": []}

        # Считаем цену каждой позиции: суммируем реальные цены ETM входящих товаров
        positions = data.get("positions", [])
        m = 1 + markup

        # Минимальные адекватные цены по категориям (для проверки ETM-цен)
        min_prices = {
            "трансформатор": 500000,   # трансформатор дешевле 500к — точно ошибка
            "рувн": 300000,
            "рунн": 500000,
            "вакуумный выключатель": 100000,
        }

        for pos in positions:
            cat = pos.get("category", "").lower()
            includes = pos.get("includes", [])

            # Суммируем реальные цены ETM для входящих товаров
            etm_sum = 0.0
            matched = 0
            for inc_name in includes:
                inc_lower = inc_name.lower()
                # Ищем совпадение в ценах ETM
                for card in (product_cards or []):
                    if not card.get("found") or card.get("price_with_vat", 0) <= 0:
                        continue
                    card_name = card.get("source_name", "").lower()
                    # Совпадение по ключевым словам
                    inc_words = set(w for w in inc_lower.split() if len(w) > 3)
                    card_words = set(card_name.split())
                    if inc_words & card_words:
                        qty_item = card.get("source_qty", 1) or 1
                        try:
                            qty_item = float(qty_item)
                        except Exception:
                            qty_item = 1
                        etm_sum += card["price_with_vat"] * qty_item
                        matched += 1
                        break

            # Проверяем адекватность ETM-суммы
            min_ok = min_prices.get(cat, 50000)

            if etm_sum >= min_ok:
                # ETM цены адекватны — используем их
                pos_price = etm_sum
                logger.info("КП позиция '%s': ETM цена %.0f (найдено %d товаров)", cat, etm_sum, matched)
            else:
                # ETM цен нет или они неадекватны — рыночная оценка GPT
                pos_price = await self._estimate_single_position(pos.get("name", ""), pos.get("category", ""))
                logger.info("КП позиция '%s': рыночная оценка %.0f (ETM было %.0f, мало)", cat, pos_price, etm_sum)

            pos["price"] = round(pos_price * m, 2)
            pos.pop("category", None)
            pos.pop("includes", None)

        return data

    async def _estimate_single_position(self, name: str, category: str) -> float:
        """Оценивает рыночную стоимость одной позиции КП"""
        refs = {
            "рувн": "РУВН (ячейки КСО, выключатели ВНА): 800000-1500000 руб",
            "рунн": "РУНН (НКУ, автоматы 0,4кВ): 2000000-3500000 руб",
            "трансформатор": "Трансформатор силовой 1000-1250кВА: 1500000-2500000 руб",
            "вакуумный выключатель": "Вакуумный выключатель 10кВ: 300000-600000 руб",
            "рза": "РЗА (релейная защита): 200000-500000 руб",
        }
        ref_text = "\n".join(f"- {v}" for v in refs.values())

        prompt = (
            "Оцени рыночную стоимость позиции для КП (в рублях БЕЗ наценки, с НДС). "
            "Ориентиры:\n" + ref_text + "\n\n"
            "Отвечай ТОЛЬКО числом (целое, рубли), без текста."
        )
        result = await self._call(
            system_prompt=prompt,
            user_message=f"Категория: {category}\nОписание: {name[:300]}",
            temperature=0.1,
            max_tokens=20,
        )
        if result:
            try:
                num = re.sub(r'[^\d]', '', result.strip())
                return float(num) if num else 1000000.0
            except Exception:
                return 1000000.0
        return 1000000.0


def _clean_latex(text: str) -> str:
    """Убирает LaTeX-разметку из текста ПУЭ-замечаний для чистого отображения."""
    # 1. СНАЧАЛА дроби \frac{a}{b} -> (a)/(b), пока скобки на месте
    text = re.sub(r'\\frac\s*\{([^}]*)\}\s*\{([^}]*)\}', r'(\1)/(\2)', text)
    # 2. Умножение
    text = re.sub(r'\\times', '×', text)
    text = re.sub(r'\\cdot', '×', text)
    # 3. Индексы I_{н.тр} -> Iн.тр, степени ^{2} -> 2
    text = re.sub(r'_\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\^\{([^}]*)\}', r'\1', text)
    # 4. Убираем $...$ обёртки, оставляя содержимое
    text = re.sub(r'\$([^$]+)\$', r'\1', text)
    # 5. Прочие \команды (\sqrt, \le и т.п.)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    # 6. Остатки скобок и символов
    text = text.replace('{', '').replace('}', '').replace('$', '').replace('\\', '')
    text = re.sub(r'  +', ' ', text)
    return text.strip()


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```[a-z]*\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    return text.strip()


yandex_gpt = YandexGPTClient()