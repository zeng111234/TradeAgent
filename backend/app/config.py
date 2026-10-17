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
    OPENAI_BASE_URL: str = "https://api.xiaomimimo.com/v1"
    OPENAI_MODEL: str = "mimo-v2.5"
    # One completion's request timeout. The SDK default is 600s, which lets a
    # hung provider hold an HTTP request for ten minutes.
    OPENAI_TIMEOUT_SECONDS: float = 120.0
    # What a million tokens cost, per model, as a JSON object:
    #   {"deepseek-chat": {"inputPerMtok": 0.27, "cachedInputPerMtok": 0.07,
    #                      "outputPerMtok": 1.10, "currency": "USD"}}
    # Empty by default on purpose: a price baked into the code is wrong the day
    # the provider changes its table, and wrong silently. Until an operator sets
    # it, ``app.llm_usage`` reports spend as token counts and cost as unpriced.
    LLM_PRICES_JSON: str = ""

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()