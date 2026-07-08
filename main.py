"""
FastAPI приложение — главная точка входа.
СИБКОМПЛЕКТ — AI Ассистент по подбору электрооборудования.
"""

import os
import uuid
import logging
import shutil
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.config import settings
from core.orchestrator import (
    process_uploaded_file,
    process_all_files_and_search,
    chat_message,
    generate_kp_for_session,
    get_or_create_session,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Создаём папку uploads при старте
os.makedirs("uploads", exist_ok=True)

app = FastAPI(title="СИБКОМПЛЕКТ — AI Ассистент", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = settings.BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class PhoneRequest(BaseModel):
    session_id: str
    phone: str


class GenerateKPRequest(BaseModel):
    session_id: str


class SearchRequest(BaseModel):
    session_id: str


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = settings.BASE_DIR / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>СИБКОМПЛЕКТ Bot — static/index.html не найден</h1>")


@app.get("/api/session")
async def create_session():
    session_id = str(uuid.uuid4())
    get_or_create_session(session_id)
    return {"session_id": session_id}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not req.session_id or not req.message.strip():
        raise HTTPException(status_code=400, detail="session_id и message обязательны")
    try:
        response = await chat_message(req.session_id, req.message)
        return {"success": True, "response": response}
    except Exception as exc:
        logger.error("Chat error: %s", exc, exc_info=True)
        return {"success": False, "response": f"Ошибка: {exc}"}


@app.post("/api/upload")
async def upload_document(
    session_id: str = Form(...),
    file: UploadFile = File(...),
):
    """
    Загружает один файл и парсит из него позиции.
    Позиции накапливаются в сессии.
    После загрузки всех файлов вызовите /api/search для поиска в ETM.
    """
    allowed_ext = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".txt", ".docx", ".xlsx"}
    suffix = Path(file.filename or "").suffix.lower()

    logger.info(f"Upload attempt: {file.filename} ({suffix}), content_type: {file.content_type}")

    if suffix not in allowed_ext:
        raise HTTPException(status_code=400, detail=f"Формат {suffix} не поддерживается")

    save_dir = settings.UPLOADS_DIR / session_id
    save_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "document").name
    save_path = save_dir / safe_name

    try:
        with open(save_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        logger.info(f"File saved: {save_path} ({save_path.stat().st_size} bytes)")
    except Exception as exc:
        logger.error(f"Save error for {file.filename}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка сохранения файла: {exc}")

    try:
        result = await process_uploaded_file(save_path, session_id)
        return result
    except Exception as exc:
        logger.error("File processing error: %s", exc, exc_info=True)
        return {"success": False, "message": f"Ошибка обработки файла: {exc}"}


@app.post("/api/search")
async def search_in_etm(req: SearchRequest):
    """
    Запускает поиск по ETM для всех накопленных позиций из загруженных файлов.
    Вызывается после того как все файлы загружены.
    """
    try:
        result = await process_all_files_and_search(req.session_id)
        return result
    except Exception as exc:
        logger.error("Search error: %s", exc, exc_info=True)
        return {"success": False, "message": f"Ошибка поиска: {exc}"}


@app.get("/api/download_kp/{session_id}/{filename}")
async def download_kp(session_id: str, filename: str):
    """Отдаёт сгенерированный КП файл для скачивания."""
    file_path = settings.UPLOADS_DIR / session_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.post("/api/generate_kp")
async def generate_kp_endpoint(req: GenerateKPRequest):
    result = await generate_kp_for_session(req.session_id)
    return result


@app.get("/api/session/{session_id}/tz")
async def get_session_tz(session_id: str):
    session = get_or_create_session(session_id)
    return {"parsed_tz": session.parsed_tz, "product_cards": session.product_cards}


@app.get("/api/session/{session_id}/kp")
async def get_session_kp(session_id: str):
    session = get_or_create_session(session_id)
    if not session.kp_data:
        raise HTTPException(status_code=404, detail="КП ещё не сформировано")
    return session.kp_data


@app.post("/api/register_phone")
async def register_phone(req: PhoneRequest):
    """
    Сохраняет номер телефона клиента в папку сессии.
    Вызывается при первом обращении через чат.
    """
    import datetime
    phone = req.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Номер телефона обязателен")

    session_dir = settings.UPLOADS_DIR / req.session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Сохраняем номер в файл клиента
    info_path = session_dir / "client_info.txt"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(info_path, "w", encoding="utf-8") as f:
        f.write(f"Телефон: {phone}\n")
        f.write(f"Дата обращения: {timestamp}\n")
        f.write(f"Session ID: {req.session_id}\n")

    logger.info("Зарегистрирован клиент: %s, сессия: %s", phone, req.session_id)
    return {"success": True}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "etm_configured": bool(settings.ETM_LOGIN),
        "yandex_configured": bool(settings.YANDEX_API_KEY),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=settings.DEBUG)