from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_DIR = Path(__file__).resolve().parents[1]  # backend/
_DATA_DIR = _BACKEND_DIR / "data"
_DEFAULT_DB = _DATA_DIR / "app.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_DIR / ".env"),
        extra="ignore",
        case_sensitive=False,
    )

    google_client_id: str = ""
    google_client_secret: str = ""

    frontend_redirect_url: str = "http://localhost:9000/"
    mobile_frontend_redirect_url: str = "com.recetas.saludables://auth/callback"

    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 60 * 24 * 7

    cookie_secure: bool = False
    cookie_samesite: str = "lax"  # lax|strict|none

    cors_origins: str = "http://localhost:9000"

    host: str = "127.0.0.1"
    port: int = 8000

    database_url: str = f"sqlite:///{_DEFAULT_DB.as_posix()}"
    database_echo: bool = False

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True

    # OpenAI (asistente de recetas con IA)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_fallback_models: str = ""  # separados por coma
    openai_retries: int = 1
    openai_retry_delay_ms: int = 100


settings = Settings()
