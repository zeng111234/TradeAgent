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
    #: The public origin this service is reachable at from the outside world,
    #: e.g. "https://tradeagent.example.com". Used to make tracking-pixel URLs
    #: absolute. A relative URL resolves against nothing inside a mail client,
    #: so the pixel is a dead link and opens never record. Empty by default:
    #: a local tool has no public origin, and inventing one would silently
    #: point pixels at somebody else's host.
    PUBLIC_BASE_URL: str = ""

    #: Verify TLS certificates when reading a prospect's website. On by default:
    #: an unverified page is an unauthenticated page, and the content is handed
    #: to a model as evidence about a company. The switch exists for an operator
    #: who has decided to accept that for one known-broken site -- it is not a
    #: convenience for making a fetch succeed.
    WEBSITE_TLS_VERIFY: bool = True

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