"""
Конфигурация приложения — читает переменные из .env
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env из корня проекта
_BASE_DIR = Path(__file__).parent.parent
load_dotenv(_BASE_DIR / ".env")


class Settings:
    # ETM API
    ETM_LOGIN: str = os.getenv("ETM_LOGIN", "")
    ETM_PASSWORD: str = os.getenv("ETM_PASSWORD", "")
    ETM_API_BASE: str = os.getenv("ETM_API_BASE", "https://ipro.etm.ru/api/v1")

    # Yandex GPT
    YANDEX_API_KEY: str = os.getenv("YANDEX_API_KEY", "")
    YANDEX_FOLDER_ID: str = os.getenv("YANDEX_FOLDER_ID", "")
    YANDEX_GPT_MODEL: str = os.getenv("YANDEX_GPT_MODEL", "yandexgpt-lite")
    YANDEX_GPT_URL: str = (
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    )

    # Приложение
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"

    # Пути
    BASE_DIR: Path = _BASE_DIR
    UPLOADS_DIR: Path = _BASE_DIR / "uploads"
    STATIC_DIR: Path = _BASE_DIR / "static"
    KNOWLEDGE_DIR: Path = _BASE_DIR / "knowledge"

    def __post_init__(self):
        self.UPLOADS_DIR.mkdir(exist_ok=True)


settings = Settings()
settings.UPLOADS_DIR.mkdir(exist_ok=True)