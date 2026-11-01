"""
config.py
---------
Application configuration using Pydantic BaseSettings.
Reads environment variables with fallbacks for local development.
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_name: str = "Personal Finance Ingestion API"
    app_version: str = "1.0.0"
    app_env: Literal["development", "testing", "production"] = "development"
    debug: bool = False

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./finance.db"

    # ── API ───────────────────────────────────────────────────────────────────
    api_prefix: str = "/api/v1"
    allowed_origins: list[str] = ["*"]

    # ── Ingestion ─────────────────────────────────────────────────────────────
    max_csv_rows: int = 50_000
    max_upload_size_mb: int = 10

    @field_validator("database_url", mode="before")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        """Ensure the database URL is a non-empty string."""
        if not v or not v.strip():
            raise ValueError("DATABASE_URL must not be empty")
        return v.strip()

    @property
    def max_upload_bytes(self) -> int:
        """Return max upload size in bytes."""
        return self.max_upload_size_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
