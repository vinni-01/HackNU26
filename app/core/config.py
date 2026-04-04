"""
app/core/config.py — Centralised application settings.

All values are loaded from environment variables (or a .env file at project root).
No secret is hardcoded here.  pydantic-settings validates types at startup so a
missing required variable surfaces immediately rather than at runtime.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Brainstorm Canvas"
    environment: str = "development"          # "development" | "production"
    debug: bool = False

    secret_key: str                            # Required — no default
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24

    database_url: str = "sqlite:///./app.db"

    redis_url: str = "redis://localhost:6379"

    livekit_url: str                           # wss://your-project.livekit.cloud
    livekit_api_key: str
    livekit_api_secret: str

    gemini_api_key: str
    gemini_model: str = "gemini-2.0-flash-live-001"

    higgsfield_api_key: str
    higgsfield_base_url: str = "https://api.higgsfield.ai/v1"

    r2_endpoint: str
    r2_access_key: str
    r2_secret_key: str
    r2_bucket: str = "brainstorm-assets"
    r2_public_base: str                        # e.g. https://pub.r2.dev/brainstorm-assets

    cf_account_id: str
    cf_api_token: str
    vectorize_index: str = "spatial-rag-index"

    cf_sync_ws_url: str = ""                   # wss://your-worker.workers.dev/sync
    cf_webhook_secret: str = ""               # Shared secret for webhook validation

    api_base_url: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()