"""
Парсер входящих документов: PDF, изображения.
Извлекает текст для последующей обработки YandexGPT.
OCR через pytesseract для изображений и сканов.
"""

import logging
import base64
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_text_from_pdf(file_path: str | Path) -> str:
    """
    Извлечь текст и таблицы из PDF через pdfplumber.
    Всегда извлекает и текст и таблицы — объединяет всё вместе.
    """
    try:
        import pdfplumber
        all_parts = []
        total_text_len = 0

        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                page_parts = []

                # 1. Обычный текст
                text = page.extract_text()
                if text and text.strip():
                    page_parts.append(text.strip())
                    total_text_len += len(text)

                # 2. Таблицы (спецификации, перечни оборудования)
                try:
                    tables = page.extract_tables()
                    for t_idx, table in enumerate(tables):
                        if not table:
                            continue
                        rows = []
                        for row in table:
                            clean = [str(c).strip().replace("\n", " ") if c else "" for c in row]
                            if any(c for c in clean):  # пропускаем пустые строки
                                rows.append(" | ".join(clean))
                        if rows:
                            page_parts.append(f"[Таблица {t_idx+1}]\n" + "\n".join(rows))
                except Exception:
                    pass

                if page_parts:
                    all_parts.append(f"[Страница {page_num}]\n" + "\n\n".join(page_parts))

        return "\n\n".join(all_parts) if all_parts else ""

    except ImportError:
        logger.error("pdfplumber not installed")
        return ""
    except Exception as exc:
        logger.error("PDF extraction error for %s: %s", file_path, exc)
        return ""


def ocr_image_yandex(file_path: str | Path) -> str:
    """
    OCR через Yandex Vision API — работает намного лучше pytesseract для русского текста.
    Использует тот же API ключ что и YandexGPT.
    """
    import httpx
    import asyncio
    from core.config import settings

    path = Path(file_path)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "folderId": settings.YANDEX_FOLDER_ID,
        "analyze_specs": [{
            "content": b64,
            "features": [{"type": "TEXT_DETECTION", "text_detection_config": {"language_codes": ["ru", "en"]}}],
        }],
    }

    async def _call():
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze",
                headers={"Authorization": f"Api-Key {settings.YANDEX_API_KEY}"},
                json=payload,
            )
            return r.json()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _call())
                data = future.result()
        else:
            data = loop.run_until_complete(_call())
    except Exception as exc:
        logger.warning("Yandex Vision call error: %s", exc)
        return ""

    try:
        blocks = data["results"][0]["results"][0]["textDetection"]["pages"][0]["blocks"]
        lines = []
        for block in blocks:
            for line in block.get("lines", []):
                words = [w.get("text", "") for w in line.get("words", [])]
                lines.append(" ".join(words))
        text = "\n".join(lines)
        logger.info("Yandex Vision OCR для %s: %d символов", path.name, len(text))
        return text
    except Exception as exc:
        logger.warning("Yandex Vision parse error: %s | response: %s", exc, str(data)[:300])
        return ""
    """
    OCR изображения. Сначала пробует Yandex Vision API,
    затем fallback на pytesseract.
    """
    path = Path(file_path)

    # Пробуем Yandex Vision — лучше распознаёт русский технический текст
    result = _ocr_yandex_vision(path)
    if result and len(result.strip()) > 20:
        logger.info("Yandex Vision OCR для %s: %d символов", path.name, len(result))
        return result

    # Fallback — pytesseract
    return _ocr_tesseract(path)


def _ocr_yandex_vision(file_path: Path) -> str:
    """OCR через Yandex Vision API."""
    try:
        import httpx
        import base64
        from core.config import settings

        if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
            return ""

        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        # Определяем MIME тип
        suffix = file_path.suffix.lower()
        mime_map = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
                    ".gif": "GIF", ".webp": "WEBP", ".bmp": "BMP"}
        mime = mime_map.get(suffix, "JPEG")

        payload = {
            "folderId": settings.YANDEX_FOLDER_ID,
            "analyze_specs": [{
                "content": b64,
                "features": [{"type": "TEXT_DETECTION",
                               "text_detection_config": {"language_codes": ["ru", "en"]}}]
            }]
        }

        import httpx as _httpx
        response = _httpx.post(
            "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze",
            headers={
                "Authorization": f"Api-Key {settings.YANDEX_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        data = response.json()
        results = data.get("results", [])
        if not results:
            return ""

        text_parts = []
        pages = results[0].get("results", [{}])[0].get("textDetection", {}).get("pages", [])
        for page in pages:
            for block in page.get("blocks", []):
                for line in block.get("lines", []):
                    words = [w.get("text", "") for w in line.get("words", [])]
                    if words:
                        text_parts.append(" ".join(words))

        return "\n".join(text_parts)

    except Exception as exc:
        logger.debug("Yandex Vision error: %s", exc)
        return ""


def _ocr_tesseract(file_path: Path) -> str:
    """OCR через pytesseract (fallback)."""
    try:
        from PIL import Image
        import pytesseract

        img = Image.open(file_path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        w, h = img.size
        if w < 1000 or h < 1000:
            scale = max(1000 / w, 1000 / h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        text = pytesseract.image_to_string(img, lang="rus+eng", config="--psm 6").strip()

        if text:
            logger.info("Tesseract OCR для %s: %d символов", file_path.name, len(text))
        else:
            logger.warning("Tesseract вернул пустой текст для %s", file_path.name)

        return text

    except ImportError:
        logger.warning("pytesseract или Pillow не установлены")
        return ""
    except Exception as exc:
        logger.warning("Tesseract OCR ошибка для %s: %s", file_path, exc)
        return ""


def image_to_base64(file_path: str | Path) -> tuple[str, str]:
    """Конвертирует изображение в base64."""
    path = Path(file_path)
    media_type_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
    }
    media_type = media_type_map.get(path.suffix.lower(), "image/jpeg")
    with open(path, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode("utf-8"), media_type


def prepare_document_for_gpt(file_path: str | Path) -> dict:
    """
    Подготавливает документ для отправки в GPT.
    Для изображений — всегда делает OCR.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    result: dict = {"filename": path.name, "images": [], "text": ""}

    image_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

    if suffix == ".pdf":
        text = extract_text_from_pdf(path)
        if text and len(text) > 50:
            result["type"] = "text"
            # Для технических PDF дополнительно прогоняем страницы через Vision
            # чтобы не пропустить данные из чертежей и схем
            try:
                images = _pdf_pages_to_images(path)
                if images:
                    vision_texts = []
                    import tempfile, os
                    # Берём последние страницы — там обычно спецификация
                    # и первую страницу (принципиальная схема)
                    total = len(images)
                    indices = [0] + list(range(max(1, total-3), total))
                    indices = sorted(set(indices))[:4]
                    for i in indices:
                        if i >= len(images):
                            continue
                        img_data = images[i]
                        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                        try:
                            tmp.write(base64.b64decode(img_data["base64"]))
                            tmp.close()
                            page_ocr = _ocr_yandex_vision(Path(tmp.name))
                            if page_ocr and len(page_ocr) > 100:
                                vision_texts.append(f"[Vision стр.{i+1}]\n{page_ocr[:2000]}")
                        except Exception:
                            pass
                        finally:
                            try:
                                os.unlink(tmp.name)
                            except Exception:
                                pass
                    if vision_texts:
                        vision_combined = "\n\n".join(vision_texts)
                        # Жёстко ограничиваем текст чтобы не превысить токены GPT
                        text_trimmed = text[:6000]
                        combined = text_trimmed + "\n\n=== ДАННЫЕ ИЗ СХЕМ (Vision OCR) ===\n\n" + vision_combined[:4000]
                        result["text"] = combined
                        logger.info("PDF %s: pdfplumber %d симв + Vision %d симв",
                                    path.name, len(text), len(vision_combined))
                    else:
                        result["text"] = text[:10000]
                else:
                    result["text"] = text[:10000]
            except Exception as exc:
                logger.warning("Vision OCR для PDF: %s", exc)
                result["text"] = text[:12000]
        else:
            # Скановый PDF — конвертируем страницы в изображения и OCR
            result["type"] = "pdf_scanned"
            ocr_texts = []
            images = _pdf_pages_to_images(path)
            for i, img_data in enumerate(images):
                import tempfile, os
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                try:
                    tmp.write(base64.b64decode(img_data["base64"]))
                    tmp.close()
                    page_text = ocr_image_yandex(tmp.name)
                    if page_text:
                        ocr_texts.append(f"[Страница {i+1}]\n{page_text}")
                finally:
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
            result["text"] = "\n\n".join(ocr_texts) if ocr_texts else f"[Файл: {path.name}]"
            result["images"] = images
        return result

    if suffix in image_suffixes:
        # OCR через Yandex Vision (намного лучше pytesseract для русского)
        ocr_text = ocr_image_yandex(path)
        if not ocr_text:
            # Fallback на pytesseract
            ocr_text = ocr_image_yandex(path)
        b64, media_type = image_to_base64(path)
        result["images"] = [{"base64": b64, "media_type": media_type}]
        result["text"] = ocr_text if ocr_text else f"[Изображение: {path.name} — текст не распознан]"
        result["type"] = "image"
        return result

    # Текстовые файлы
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        result["type"] = "text"
        result["text"] = text
    except Exception:
        result["type"] = "unknown"

    return result


def _pdf_pages_to_images(pdf_path: Path) -> list[dict]:
    """Конвертирует страницы PDF в изображения base64."""
    images = []
    try:
        import pdf2image
        import io
        pages = pdf2image.convert_from_path(str(pdf_path), dpi=150, fmt="jpeg")
        for page_img in pages[:5]:
            buf = io.BytesIO()
            page_img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            images.append({"base64": b64, "media_type": "image/jpeg"})
    except ImportError:
        logger.info("pdf2image не установлен")
    except Exception as exc:
        logger.warning("PDF to image error: %s", exc)
    return images


def get_document_summary(prepared: dict) -> str:
    """Собирает текстовое содержимое документа."""
    parts = []
    if prepared.get("filename"):
        parts.append(f"Документ: {prepared['filename']}")
    if prepared.get("text"):
        parts.append(prepared["text"])
    return "\n\n".join(parts)
