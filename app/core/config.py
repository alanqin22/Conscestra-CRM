"""Unified configuration for the CRM Agent application.

A single Settings class is shared across all agent modules.
Each agent sub-module imports settings from here — no per-agent config files.

Adding a new agent:
  No changes needed here.  Simply import settings from app.core.config in
  the new agent module.  If the agent requires unique settings (e.g. a
  module-specific timeout), add them as optional fields below.
"""

from typing import Literal, Optional
from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv(override=True)


class Settings(BaseSettings):
    """Application-wide settings for all CRM agents."""

    # ── LLM Provider ──────────────────────────────────────────────────────────
    llm_provider: Literal["openai", "ollama"] = "ollama"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # ── Ollama ────────────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gpt-oss:20b"

    # ── Database ──────────────────────────────────────────────────────────────
    db_dsn: str = "postgresql://postgres:aria@localhost:5434/crmdb"
    database_url: Optional[str] = None  # Railway injects this automatically

    # ── Application ───────────────────────────────────────────────────────────
    debug: bool = True
    log_level: str = "INFO"

    # ── Server ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000

    # ── Memory ────────────────────────────────────────────────────────────────
    memory_window_size: int = 5

    # ── SMTP (OTP email verification) ─────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = "noreply@agentorc.ca"
    smtp_tls: bool = True

    # ── Web access (free internet tools — see app/core/web_tools.py) ──────────
    # Search runs on ddgs (DuckDuckGo, free, no key). Tavily is an optional
    # fallback: free tier = 1,000 searches/month, used only when ddgs fails.
    web_search_enabled: bool = True
    web_fetch_max_chars: int = 4000
    tavily_api_key: str = ""

    # ── Azure Speech (browser STT — Bing-style engine for Edge sensitivity) ───
    # If unset, /voice/azure-token returns 503 and the frontend falls back to
    # the browser's built-in Web Speech API.
    azure_speech_key: str = ""
    azure_speech_region: str = "eastus"

    @model_validator(mode='after')
    def apply_railway_overrides(self) -> 'Settings':
        """Let Railway's DATABASE_URL override db_dsn when present."""
        if self.database_url:
            self.db_dsn = self.database_url
        return self

    @property
    def llm_model(self) -> str:
        return self.openai_model if self.llm_provider == "openai" else self.ollama_model

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
