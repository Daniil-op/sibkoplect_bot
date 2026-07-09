"""
ETM API client — ipro.etm.ru/api/v1
Поиск через номенклатуру клиента (JSON файл cligds).
"""

import os
import gc
import json
import sys
import tempfile
import asyncio
import time
import logging
import re
import urllib.parse
from typing import Optional
import httpx
from core.config import settings

logger = logging.getLogger(__name__)

_session_cache: dict = {"key": None, "obtained_at": 0.0, "ttl": 8 * 3600 - 300}
_session_lock = asyncio.Lock()

_nom_items: list[dict] = []
_nom_by_article: dict[str, dict] = {}
_nom_by_words: dict[str, list[dict]] = {}
_nom_loaded: bool = False
_nom_lock = asyncio.Lock()

NOMENCLATURE_FILE_URL = "https://ipro.etm.ru/upload/report/cligds_690007583_2026061335878_40798836.json"

_STOPWORDS = {
    "для", "при", "на", "по", "до", "из", "без", "над", "под",
    "новый", "новая", "тип", "серия", "класс", "упак", "упаковка",
    "шт", "компл", "штук", "метр", "100шт", "в.а", "ва",
}


def _tokenize(text: str) -> list[str]:
    text = text.lower().strip()
    parts = re.split(r'[\s,;:()\[\]]+', text)
    result = []
    for p in parts:
        p = p.strip('.')
        if len(p) >= 2 and p not in _STOPWORDS:
            result.append(p)
    return result


def _is_significant(token: str) -> bool:
    if token in _STOPWORDS:
        return False
    clean = re.sub(r'[,./]', '', token)
    if clean.isdigit():
        if ',' in token or ('/' in token and len(token) >= 4):
            return True
        return len(clean) >= 4
    if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
        return True
    if re.search(r'[a-z]', token) and len(token) >= 3:
        return True
    if len(token) >= 3 and not any(c.isdigit() for c in token):
        return True
    return False


def _categories_compatible(query: str, found: str) -> bool:
    """
    Проверяет совместимость категорий запроса и найденного товара.
    Предотвращает явный мусор: силовой трансформатор ≠ трансформатор тока,
    автоматический выключатель ≠ АВДТ, греющий кабель ≠ стриппер и т.д.
    """
    q = query.lower()
    f = found.lower()

    # Силовой трансформатор не должен быть трансформатором тока
    if any(w in q for w in ["тмгф", "тмг", "тсл", "тсзи", "силовой трансформатор", "трансформатор 1000", "трансформатор 1250"]):
        if any(w in f for w in ["трансформатор тока", "тол-", "тол ", "ттол", "тол-нтз"]):
            return False

    # Трансформатор тока не должен быть силовым
    if any(w in q for w in ["трансформатор тока", "тшл", "тол ", "100/5", "200/5", "300/5", "1000/5", "1500/5"]):
        if any(w in f for w in ["тсзи", "тмг", "силовой", "понижающий тсзи"]):
            return False

    # Автоматический выключатель не должен быть АВДТ (дифавтоматом)
    if any(w in q for w in ["tgw1n", "tgm1n", "tgm1ne", "автоматический выключатель"]):
        if any(w in f for w in ["авдт", "дифференциального тока", "дифф"]):
            return False

    # Греющий кабель не должен быть стриппером/инструментом
    if "греющий кабель" in q or "греющий" in q:
        if any(w in f for w in ["стриппер", "инструмент", "зачистк", "монтажный"]):
            return False

    # Шинопровод не должен быть светильником на шинопровод
    if "шинопровод" in q and ("0,4 кв" in q or "комплектный" in q):
        if any(w in f for w in ["светильник", "трековый", "декоративный"]):
            return False

    # Конденсаторная установка не должна быть монтажным комплектом
    if "конденсаторная установка" in q or "укм58" in q:
        if any(w in f for w in ["комплект поддержки", "монтажный", "крепёж"]):
            return False

    # Вентилятор не должен быть светильником
    if "светодиодн" in q and "светильник" in q:
        if "вентилятор" in f:
            return False

    # Переносной светильник не должен быть контроллером
    if "переносной светильник" in q or "переносные светильники" in q:
        if any(w in f for w in ["контроллер", "rgb", "пду"]):
            return False

    # Обогреватель конвекторный не должен быть разъёмом
    if "обогреватель" in q or "конвекторного типа" in q:
        if any(w in f for w in ["разъем", "зажим", "клемм"]):
            return False

    # Фотореле не должно быть светильником
    if "фотореле" in q:
        if "светильник" in f and "фотореле" not in f:
            return False

    return True


def _parse_price(val) -> float:
    try:
        return float(str(val).replace(",", ".").replace(" ", ""))
    except Exception:
        return 0.0


class ETMClient:

    BASE_URL = settings.ETM_API_BASE

    # ── Авторизация ───────────────────────────────────────────────────

    async def _login(self) -> str:
        if not settings.ETM_LOGIN or not settings.ETM_PASSWORD:
            raise RuntimeError("ETM credentials not configured")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.BASE_URL}/user/login",
                params={"log": settings.ETM_LOGIN, "pwd": settings.ETM_PASSWORD},
            )
            data = r.json()
        if data.get("status", {}).get("code") != 200:
            raise RuntimeError(f"ETM login failed: {data.get('status',{}).get('message')}")
        logger.info("ETM session refreshed")
        return data["data"]["session"]

    async def _get_session_key(self) -> str:
        async with _session_lock:
            now = time.time()
            if _session_cache["key"] and (now - _session_cache["obtained_at"]) < _session_cache["ttl"]:
                return _session_cache["key"]
            key = await self._login()
            _session_cache["key"] = key
            _session_cache["obtained_at"] = now
            return key

    # ── Загрузка номенклатуры ─────────────────────────────────────────

    async def ensure_nomenclature_loaded(self):
        global _nom_loaded
        async with _nom_lock:
            if _nom_loaded and _nom_items:
                return
            _nom_loaded = False
            for attempt in range(3):
                try:
                    await self._load_and_index()
                    if _nom_items:
                        _nom_loaded = True
                        return
                    logger.warning("ETM: номенклатура пустая, попытка %d/3", attempt + 1)
                except Exception as exc:
                    logger.warning("ETM: ошибка загрузки попытка %d/3: %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(2)
            logger.error("ETM: не удалось загрузить номенклатуру после 3 попыток")

    async def _load_and_index(self):
        global _nom_items, _nom_by_article, _nom_by_words

        try:
            session_key = await self._get_session_key()
        except Exception as exc:
            logger.warning("ETM auth failed: %s", exc)
            return

        logger.info("ETM: загружаю номенклатуру клиента...")

        # Стримим JSON во временный файл — не держим весь ответ в памяти.
        # На 512 МБ хостинга это критично: полный ответ + распарсенные объекты не влезают.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = tmp.name
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream(
                        "GET", NOMENCLATURE_FILE_URL, params={"session-id": session_key}
                    ) as r:
                        async for chunk in r.aiter_bytes(chunk_size=262144):
                            tmp.write(chunk)
        except Exception as exc:
            logger.warning("ETM: ошибка загрузки: %s", exc)
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return

        # Парсим файл, оставляя только нужные поля.
        # Полные записи ETM содержат 20+ полей — на 140k позиций это сотни МБ.
        items = []
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                raw_items = json.load(f)

            if not isinstance(raw_items, list):
                raw_items = raw_items.get("data", []) if isinstance(raw_items, dict) else []

            for raw in raw_items:
                items.append({
                    "id": raw.get("id"),
                    "name": str(raw.get("name", "") or ""),
                    "article": str(raw.get("article", "") or ""),
                    "brand": sys.intern(str(raw.get("brand", "") or "")),
                    "brand_code": str(raw.get("brand_code", "") or ""),
                    "cli_code": str(raw.get("cli_code", "") or ""),
                })

            raw_items.clear()
            del raw_items
            gc.collect()
        except Exception as exc:
            logger.warning("ETM: ошибка парсинга номенклатуры: %s", exc)
            return
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        _nom_items = items
        logger.info("ETM: загружено %d позиций", len(items))

        by_article: dict[str, dict] = {}
        by_words: dict[str, list] = {}

        for item in items:
            for field in ("article", "brand_code", "cli_code"):
                val = item[field].strip()
                if val and len(val) >= 3:
                    by_article[val.lower()] = item

            name = item["name"].strip()
            if not name:
                continue
            for token in _tokenize(name):
                if token not in by_words:
                    by_words[token] = []
                by_words[token].append(item)

        _nom_by_article = by_article
        _nom_by_words = by_words
        gc.collect()
        logger.info("ETM: индекс — %d артикулов, %d токенов", len(by_article), len(by_words))

    # ── Поиск в номенклатуре ─────────────────────────────────────────

    def _find_in_nomenclature(self, name: str, article: str) -> Optional[dict]:
        if not _nom_items:
            return None

        # 1. Точный артикул
        if article:
            art_low = article.strip().lower()
            if art_low in _nom_by_article:
                item = _nom_by_article[art_low]
                logger.info("NOM exact article '%s' → %s", article, item.get("name", "")[:60])
                return item

        # 2. Частичное совпадение артикула (>= 6 символов)
        if article and len(article) >= 6:
            art_low = article.strip().lower()
            best_partial = None
            best_len = 0
            for key, item in _nom_by_article.items():
                if len(key) < 4:
                    continue
                common = 0
                for a, b in zip(art_low, key):
                    if a == b:
                        common += 1
                    else:
                        break
                if common >= 6 and common > best_len:
                    best_len = common
                    best_partial = item
            if best_partial:
                logger.info("NOM partial article '%s' → %s", article, best_partial.get("name", "")[:60])
                return best_partial

        # 3. Поиск по токенам названия
        query_tokens = _tokenize(name)
        significant_tokens = [t for t in query_tokens if _is_significant(t)]

        if not significant_tokens:
            return None

        has_alphanumeric = any(
            any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
            for t in significant_tokens
        )
        required = 1 if has_alphanumeric else 2

        candidates: dict[int, dict] = {}
        for token in significant_tokens:
            for item in _nom_by_words.get(token, []):
                iid = id(item)
                if iid not in candidates:
                    candidates[iid] = {"item": item, "score": 0}
                candidates[iid]["score"] += 1

        # Собираем топ-5 кандидатов по score
        sorted_cands = sorted(
            [c for c in candidates.values() if c["score"] >= required],
            key=lambda x: x["score"],
            reverse=True
        )[:5]

        if not sorted_cands:
            if candidates:
                top = max(candidates.values(), key=lambda x: x["score"])
                logger.info("NOM: лучший score=%d (нужно %д) для '%s' → %s",
                            top["score"], required, name[:40], top["item"].get("name", "")[:60])
            else:
                logger.info("NOM: нет совпадений токенов для '%s'", name[:40])
            logger.info("NOM: не найдено для '%s' / '%s'", name[:40], article[:20])
            return None

        best = sorted_cands[0]

        # Если лучший кандидат явно лидирует (score в 2+ раза больше) — берём без GPT
        if len(sorted_cands) == 1 or best["score"] >= sorted_cands[1]["score"] * 2:
            item = best["item"]
            if not _categories_compatible(name, item.get("name", "")):
                logger.info("NOM: категория несовместима для '%s' → '%s', пропускаем",
                            name[:40], item.get("name", "")[:60])
                return None
            logger.info("NOM token match score=%d/%d for '%s' → %s",
                        best["score"], required, name[:40], item.get("name", "")[:60])
            return item

        # Несколько близких кандидатов — отправляем GPT на валидацию
        result = best["item"].copy()
        result["_candidates"] = [c["item"] for c in sorted_cands]
        logger.info("NOM: %d кандидатов для '%s', отправим GPT на валидацию",
                    len(sorted_cands), name[:40])
        return result

    # ── Получение цен батчем ──────────────────────────────────────────

    async def fetch_prices_batch(self, cards: list[dict]) -> None:
        """
        Получает цены для списка карточек батч-запросом (до 50 товаров в одном запросе).
        Коды передаются через %2C согласно документации ETM API.
        Использует одну сессию 8 часов — не логинится на каждый запрос.
        """
        if not cards:
            return

        # Группируем карточки по etm_code
        code_to_cards: dict[str, list[dict]] = {}
        for card in cards:
            code = str(card.get("etm_code", "")).strip()
            if code:
                code_to_cards.setdefault(code, []).append(card)

        codes = list(code_to_cards.keys())
        if not codes:
            return

        logger.info("ETM: запрашиваю цены для %d товаров", len(codes))

        # Одиночные запросы — батч с %2C не работает для аккаунта
        # Получаем свежую сессию перед ценами и делаем паузу
        _session_cache["key"] = None
        session_key = None
        for attempt in range(3):
            try:
                session_key = await self._get_session_key()
                await asyncio.sleep(2)
                break
            except Exception as exc:
                logger.warning("ETM: попытка %d/3 получить сессию для цен: %s", attempt + 1, exc)
                await asyncio.sleep(3)
        if not session_key:
            logger.warning("ETM: не удалось получить сессию для цен — цены недоступны")
            return

        for code in codes:
            success = False
            for attempt in range(2):
                try:
                    url = f"{self.BASE_URL}/goods/{code}/price?type=etm&session-id={session_key}"
                    async with httpx.AsyncClient(timeout=15) as client:
                        r = await client.get(url)
                        data = r.json()
                    status = data.get("status", {}).get("code")
                    if status == 200:
                        row = data.get("data", {})
                        rows = row.get("rows", []) if isinstance(row, dict) else []
                        if not rows and isinstance(row, dict) and row.get("gdscode"):
                            rows = [row]
                        if not rows:
                            # Нет rows — ищем карточку напрямую по коду запроса
                            rows = [{"gdscode": int(code), "price_retail": 0, "pricewnds": 0, "price": 0}]
                        for item in rows:
                            price_retail = _parse_price(item.get("price_retail", 0))
                            pricewnds = _parse_price(item.get("pricewnds", 0))
                            final_price = price_retail if price_retail else pricewnds
                            gdscode_str = str(item.get("gdscode", code))
                            logger.info("ETM price code=%s gdscode=%s retail=%.2f → %.2f",
                                        code, gdscode_str, price_retail, final_price)
                            # Ищем карточку по gdscode из ответа, потом по коду запроса
                            target_cards = code_to_cards.get(gdscode_str) or code_to_cards.get(code, [])
                            if not target_cards:
                                logger.warning("ETM price: карточка не найдена для code=%s gdscode=%s keys=%s",
                                               code, gdscode_str, list(code_to_cards.keys())[:5])
                            for card in target_cards:
                                if final_price:
                                    card["price_with_vat"] = final_price
                                    card["price_no_vat"] = _parse_price(item.get("price", 0))
                                    card["in_stock"] = True
                        success = True
                        break
                    elif status == 403 and attempt == 0:
                        logger.info("ETM price 403 для code=%s — обновляю сессию с паузой", code)
                        _session_cache["key"] = None
                        await asyncio.sleep(2)
                        session_key = await self._get_session_key()
                        await asyncio.sleep(2)
                    else:
                        logger.warning("ETM price status=%s code=%s", status, code)
                        break
                except Exception as exc:
                    logger.warning("ETM price error code=%s: %s", code, exc)
                    break
            await asyncio.sleep(1)  # 1 запрос/сек по документации

    # ── Одиночный запрос цены (используется в KP) ────────────────────

    async def get_price(self, etm_code: str) -> Optional[dict]:
        if not etm_code:
            return None
        try:
            session_key = await self._get_session_key()
            url = f"{self.BASE_URL}/goods/{etm_code}/price?type=etm&session-id={session_key}"
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
                data = r.json()
                if data.get("status", {}).get("code") == 200:
                    return data.get("data")
                if data.get("status", {}).get("code") == 403:
                    _session_cache["key"] = None
                return None
        except Exception as exc:
            logger.warning("ETM price error %s: %s", etm_code, exc)
            return None

    async def get_goods(self, etm_code: str) -> Optional[dict]:
        if not etm_code:
            return None
        try:
            session_key = await self._get_session_key()
        except Exception:
            return None
        url = f"{self.BASE_URL}/goods/{etm_code}?type=etm&session-id={session_key}"
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(url)
                data = r.json()
            except Exception:
                return None
        if data.get("status", {}).get("code") != 200:
            return None
        raw = data.get("data", {})
        if isinstance(raw, dict) and raw.get("gdsNameTitle"):
            return self._normalize_goods(raw)
        rows = raw.get("rows", []) if isinstance(raw, dict) else []
        if rows:
            row = rows[0]
            return {
                "name": row.get("name", ""),
                "article": row.get("art", ""),
                "manufacturer": row.get("mnf_name", ""),
                "unit": row.get("edizm", "шт"),
                "price_with_vat": _parse_price(row.get("price", 0)),
            }
        return None

    def _normalize_goods(self, raw: dict) -> dict:
        avail_str = raw.get("gdsAvailOP", "") or raw.get("gdsAvailLC", "") or ""
        stock_qty = 0
        in_stock = False
        if avail_str:
            m = re.search(r"(\d+)\s*шт", avail_str)
            if m:
                stock_qty = int(m.group(1))
                in_stock = True
        rem_crs = int(raw.get("rem_crs", 0) or 0)
        if rem_crs > 0:
            in_stock = True
            stock_qty = max(stock_qty, rem_crs)
        images = raw.get("gdsImages", [])
        image_url = None
        if images and isinstance(images[0], dict):
            img_path = images[0].get("gdsImgRef") or images[0].get("gdsImgSrc") or ""
            if img_path:
                image_url = f"https://cdn.etm.ru{img_path}"
        return {
            "name": raw.get("gdsNameTitle", ""),
            "article": raw.get("gdsArt", "") or raw.get("gdsExtArt", ""),
            "manufacturer": raw.get("gdsMnfName", ""),
            "unit": raw.get("gdsUnitName", "шт"),
            "price_with_vat": _parse_price(raw.get("gdsPrice1", 0)),
            "in_stock": in_stock,
            "stock_qty": stock_qty,
            "avail_text": avail_str,
            "image": image_url,
        }


etm_client = ETMClient()
