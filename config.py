import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PROCESSED_DIR = DATA_DIR / "processed"


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 50 * 1024 * 1024))

    LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
    LLM_API_BASE_URL = os.environ.get("LLM_API_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5-mini")
    LLM_PRICE_INPUT_PER_1K = float(os.environ.get("LLM_PRICE_INPUT_PER_1K", "0"))
    LLM_PRICE_OUTPUT_PER_1K = float(os.environ.get("LLM_PRICE_OUTPUT_PER_1K", "0"))
    LLM_IMAGE_TOKENS_PER_IMAGE = int(os.environ.get("LLM_IMAGE_TOKENS_PER_IMAGE", "0"))
    LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "1200"))
    LLM_REQUEST_TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "120"))
    LLM_USE_JSON_MODE = os.environ.get("LLM_USE_JSON_MODE", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    REDIS_URL = os.environ.get("REDIS_URL", "")
    PDF_DPI = int(os.environ.get("PDF_DPI", 300))
