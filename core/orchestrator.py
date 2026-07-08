"""
Оркестратор: связывает парсинг документов, YandexGPT и ETM API.
Поддерживает загрузку нескольких файлов — позиции объединяются.
"""

import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Optional

from core.doc_parser import prepare_document_for_gpt, get_document_summary
from core.yandex_gpt import yandex_gpt
from core.etm_client import etm_client

logger = logging.getLogger(__name__)


class ChatSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.history: list[dict] = []
        self.uploaded_docs: list[dict] = []
        self.parsed_tz: Optional[dict] = None
        self.kp_data: Optional[dict] = None
        self.product_cards: list[dict] = []
        self.all_items: list[dict] = []
        self.all_filenames: list[str] = []

    def add_message(self, role: str, text: str):
        self.history.append({"role": role, "text": text})

    def reset_products(self):
        self.parsed_tz = None
        self.product_cards = []
        self.all_items = []
        self.all_filenames = []
        self.uploaded_docs = []


import hashlib

_sessions: dict[str, ChatSession] = {}
_parse_cache: dict[str, dict] = {}
_PROMPT_VERSION = "v11"

# ── Наценка за услуги компании ────────────────────────────────────
# 0.10 = +10%, 0.20 = +20%, 0.30 = +30% и т.д.
SERVICE_MARKUP = 0.30


def _file_hash(file_path: Path) -> str:
    h = hashlib.md5()
    h.update(_PROMPT_VERSION.encode())
    h.update(str(SERVICE_MARKUP).encode())
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_or_create_session(session_id: str) -> ChatSession:
    if session_id not in _sessions:
        _sessions[session_id] = ChatSession(session_id)
    return _sessions[session_id]


async def process_uploaded_file(file_path: str | Path, session_id: str) -> dict:
    session = get_or_create_session(session_id)
    file_path = Path(file_path)

    prepared = prepare_document_for_gpt(file_path)
    doc_text = get_document_summary(prepared)

    if not doc_text.strip() and not prepared.get("images"):
        return {"success": False, "message": f"Не удалось извлечь текст из {prepared['filename']}"}

    if prepared["filename"] in session.all_filenames:
        logger.info("Файл '%s' уже обработан в этой сессии, пропускаем", prepared["filename"])
        return {
            "success": True,
            "partial": True,
            "filename": prepared["filename"],
            "added_items": 0,
            "total_items": len(session.all_items),
            "message": f"ℹ️ **{prepared['filename']}** уже обработан в этой сессии",
        }

    session.uploaded_docs.append(prepared)

    logger.info("Parsing TZ from: %s", prepared["filename"])

    file_key = _file_hash(file_path)

    if file_key in _parse_cache:
        parsed = _parse_cache[file_key]
        logger.info("Cache HIT для '%s' (hash=%s)", prepared["filename"], file_key[:8])
        pue_violations = _parse_cache.get(file_key + "_pue", "")
    else:
        images = prepared.get("images") or []
        parse_task = asyncio.create_task(
            yandex_gpt.parse_tz(doc_text, images=images if images else None)
        )
        pue_task = asyncio.create_task(
            yandex_gpt.check_pue(doc_text, prepared["filename"])
        )
        parsed, pue_violations = await asyncio.gather(parse_task, pue_task)

        if parsed and parsed.get("items"):
            _parse_cache[file_key] = parsed
            _parse_cache[file_key + "_pue"] = pue_violations or ""
            logger.info("Cache MISS, сохранён для '%s' (hash=%s)", prepared["filename"], file_key[:8])

    logger.info("PUE check для '%s': %s", prepared["filename"],
                "нарушений нет" if not pue_violations else f"{len(pue_violations)} символов")

    # Файл добавляем в список ВСЕГДА — даже если позиций нет
    session.all_filenames.append(prepared["filename"])

    if not parsed or not parsed.get("items"):
        return {
            "success": False,
            "filename": prepared["filename"],
            "message": f"Не удалось распознать оборудование в {prepared['filename']}",
        }

    new_project = (parsed.get("project_name") or "").strip()
    new_items = parsed.get("items", [])
    existing_names = {i.get("name", "").lower() for i in session.all_items}
    added = 0
    for item in new_items:
        if item.get("name", "").lower() not in existing_names:
            session.all_items.append(item)
            existing_names.add(item.get("name", "").lower())
            added += 1

    if session.parsed_tz is None:
        session.parsed_tz = parsed
        session.parsed_tz["items"] = session.all_items
    else:
        if new_project:
            session.parsed_tz["project_name"] = new_project
        session.parsed_tz["items"] = session.all_items

    return {
        "success": True,
        "partial": True,
        "filename": prepared["filename"],
        "added_items": added,
        "total_items": len(session.all_items),
        "pue_violations": pue_violations or "",
        "message": f"✅ **{prepared['filename']}** — найдено {added} новых позиций",
    }


async def process_all_files_and_search(session_id: str) -> dict:
    """
    Обрабатывает загруженные файлы и генерирует КП в формате docx.
    Возвращает путь к файлу для скачивания.
    """
    session = get_or_create_session(session_id)

    if not session.all_items:
        return {"success": False, "message": "Нет позиций для поиска. Загрузите файлы."}

    logger.info("Генерация КП для %d позиций из %d файлов",
                len(session.all_items), len(session.all_filenames))

    # Ищем позиции в ETM для получения реальных цен
    existing_names = {c["source_name"].lower() for c in session.product_cards}
    new_items = [i for i in session.all_items if i.get("name", "").lower() not in existing_names]
    if new_items:
        new_cards = await _search_items_in_etm(new_items)
        session.product_cards = session.product_cards + new_cards

    # ИИ определяет тип объекта (КТП/РП) и группирует позиции
    kp_data = await yandex_gpt.build_kp_positions(
        session.all_items,
        session.product_cards,
        SERVICE_MARKUP,
    )

    object_type = kp_data.get("object_type", "КТП")
    equipment_title = kp_data.get("equipment_title", "Электрооборудование")
    positions = kp_data.get("positions", [])

    if not positions:
        return {
            "success": False,
            "message": "Не удалось сформировать позиции КП. Проверьте документ.",
        }

    # Данные объекта
    parsed = session.parsed_tz or {}
    object_name = parsed.get("project_name", "") or "Объект"
    delivery = parsed.get("delivery_address", "")

    # Генерируем docx
    import kp_generator
    from core.config import settings

    output_dir = settings.UPLOADS_DIR / session_id
    output_dir.mkdir(parents=True, exist_ok=True)

    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    kp_filename = f"КП_{ts}.docx"
    kp_path = output_dir / kp_filename

    try:
        kp_generator.generate_kp(
            output_path=str(kp_path),
            object_name=object_name,
            equipment_title=equipment_title,
            positions=positions,
            delivery_address=delivery,
        )
    except Exception as exc:
        logger.error("Ошибка генерации КП: %s", exc)
        return {"success": False, "message": f"Ошибка генерации КП: {exc}"}

    # Считаем итог для сообщения
    grand_total = sum(float(p.get("price", 0)) * int(p.get("qty", 1)) for p in positions)
    total_str = f"{grand_total:,.2f}".replace(",", " ").replace(".", ",")

    n = len(session.all_filenames)
    if n % 10 == 1 and n % 100 != 11:
        word = "файла"
    elif 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        word = "файлов"
    else:
        word = "файлов"

    message = (
        f"✅ Коммерческое предложение готово!\n\n"
        f"📄 Обработано {n} {word}\n"
        f"🏭 Тип объекта: {object_type}\n"
        f"📋 Позиций в КП: {len(positions)}\n"
        f"💰 Итого с НДС: {total_str} ₽\n\n"
        f"Цены ориентировочные — уточняйте у менеджера"
    )

    return {
        "success": True,
        "message": message,
        "kp_file": str(kp_path),
        "kp_filename": kp_filename,
        "object_type": object_type,
    }


async def _search_items_in_etm(items: list[dict]) -> list[dict]:
    await etm_client.ensure_nomenclature_loaded()

    cards = []

    for item in items:
        name = (item.get("name") or "").strip()
        search_name = (item.get("search_name") or name).strip()
        article = (item.get("article") or "").strip()
        qty = item.get("quantity", 1)
        unit = item.get("unit", "шт")
        params = item.get("parameters", "")

        if not name and not article:
            continue

        logger.info("ETM search: name='%s', article='%s', qty=%s", name[:40], article[:20], qty)

        nom = etm_client._find_in_nomenclature(name=search_name, article=article)

        # Если несколько кандидатов — GPT выбирает лучший
        if nom and "_candidates" in nom:
            candidates = nom.pop("_candidates")
            nom = await yandex_gpt.select_best_candidate(
                query=f"{name} {params}".strip(),
                candidates=candidates
            )

        if nom:
            etm_code = str(nom.get("id", ""))
            card = {
                "source_name": name,
                "source_qty": qty,
                "source_unit": unit,
                "source_article": article,
                "source_params": params,
                "found": True,
                "found_via": "nomenclature",
                "etm_code": etm_code,
                "name": nom.get("name") or name,
                "article": nom.get("article") or nom.get("brand_code") or article,
                "manufacturer": nom.get("brand", ""),
                "unit": "шт",
                "price_with_vat": 0.0,
                "price_no_vat": 0.0,
                "in_stock": False,
                "url": f"https://www.etm.ru/cat/nn/{etm_code}" if etm_code else "",
            }
        else:
            query = article or search_name
            card = {
                "source_name": name,
                "source_qty": qty,
                "source_unit": unit,
                "source_article": article,
                "source_params": params,
                "found": False,
                "found_via": "",
                "etm_code": None,
                "name": name,
                "article": article,
                "manufacturer": "",
                "unit": unit,
                "price_with_vat": 0.0,
                "price_no_vat": 0.0,
                "in_stock": False,
                "url": f"https://www.etm.ru/cat/search/?q={urllib.parse.quote(query)}",
            }
        cards.append(card)

    found_cards = [c for c in cards if c.get("found") and c.get("etm_code")]
    if found_cards:
        await etm_client.fetch_prices_batch(found_cards)

    return cards


def _build_tz_summary(parsed: dict, cards: list[dict], filenames: list[str], market_estimate: str = "") -> str:
    lines = []

    project = parsed.get("project_name", "")
    if project:
        lines.append(f"📋 **{project}**\n")

    if filenames:
        n = len(filenames)
        if n % 10 == 1 and n % 100 != 11:
            word = "файл"
        elif 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
            word = "файла"
        else:
            word = "файлов"
        lines.append(f"📄 Обработано: **{n} {word}**")

    total = len(parsed.get("items", []))
    found = sum(1 for c in cards if c.get("found"))
    not_found = total - found

    lines.append(f"Распознано позиций: **{total}**")
    lines.append(f"Найдено в каталоге ETM: **{found}** из {total}")
    if not_found:
        lines.append(f"Не найдено (ссылка на поиск): **{not_found}**")

    if market_estimate:
        lines.append(f"\n{market_estimate}")

    delivery = parsed.get("delivery_address", "")
    if delivery:
        lines.append(f"📍 Адрес: {delivery}")

    return "\n".join(lines)


async def generate_kp_for_session(session_id: str) -> dict:
    session = get_or_create_session(session_id)
    if not session.parsed_tz:
        return {"success": False, "message": "Сначала загрузите файлы и дождитесь поиска."}

    kp = await yandex_gpt.generate_kp(
        parsed_items=session.parsed_tz.get("items", []),
        product_cards=session.product_cards,
        project_name=session.parsed_tz.get("project_name", ""),
        delivery_address=session.parsed_tz.get("delivery_address", ""),
        notes=session.parsed_tz.get("notes", ""),
    )

    if not kp:
        return {"success": False, "message": "Ошибка генерации КП."}

    session.kp_data = kp
    return {"success": True, "kp": kp}


async def chat_message(session_id: str, message: str) -> str:
    session = get_or_create_session(session_id)
    msg_lower = message.lower().strip()

    if any(kw in msg_lower for kw in ["сформировать кп", "создать кп", "кп"]):
        result = await generate_kp_for_session(session_id)
        if result["success"]:
            return _format_kp_text(result["kp"])
        return result["message"]

    response = await yandex_gpt.chat(message, session.history)
    session.add_message("user", message)
    session.add_message("assistant", response)
    return response


def _format_kp_text(kp: dict) -> str:
    lines = [f"## 📄 {kp.get('title', 'Коммерческое предложение')}"]
    client = kp.get("client", "")
    if client:
        lines.append(f"**Объект:** {client}\n")

    for item in kp.get("items", []):
        pos = item.get("pos", "")
        name = item.get("name", "")
        art = f" ({item.get('article')})" if item.get("article") else ""
        qty = item.get("qty", 0)
        unit = item.get("unit", "шт")
        price = item.get("price_per_unit", 0)
        total = item.get("price_total", 0)
        delivery = item.get("delivery_days", "уточняется")
        url = item.get("url", "")
        link = f" [→ ETM]({url})" if url else ""
        lines.append(f"**{pos}.** {name}{art}{link}")
        lines.append(f"   {qty} {unit} × {price:,.2f} ₽ = **{total:,.2f} ₽** | Срок: {delivery}")

    lines.append(f"\n**Итого с НДС:** {kp.get('total_with_vat', 0):,.2f} ₽")
    lines.append(f"**Итого без НДС:** {kp.get('total_no_vat', 0):,.2f} ₽")
    lines.append(f"📅 Срок действия КП: {kp.get('validity_days', 10)} дней")
    lines.append(f"💳 Условия оплаты: {kp.get('payment_terms', '')}")
    return "\n".join(lines)