"""Application configuration."""
import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    # App
    APP_NAME: str = "TradeAgent"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True

    # Your products (for lead scanning and email generation)
    DEFAULT_PRODUCT_KEYWORDS: str = "embroidery thread, gold metallic yarn, gold thread, textile fabric"
    DEFAULT_TARGET_COUNTRIES: str = "Germany, United States, United Kingdom, France, Italy, Spain"
    COMPANY_NAME: str = "Your Company Name"
    COMPANY_ADDRESS: str = "Ningbo, China"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./tradeagent.db"

    # Email SMTP
    SMTP_HOST: str = "smtp.qq.com"
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True

    # LLM (OpenAI-compatible API: MiMo, DeepSeek, OpenAI, etc.)
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.xiaomi.com/v1"
    OPENAI_MODEL: str = "mimo-v2.5"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()