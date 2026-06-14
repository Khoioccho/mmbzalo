from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MMBZALO_",
        extra="ignore",
    )

    app_name: str = "MMBZalo Automation Tool"
    app_version: str = "1.0.0"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/mmbzalo"
    secret_key: str = "change-me"
    encryption_key: str = Field(
        default="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        description="Base64 Fernet key used to encrypt sensitive settings.",
    )

    session_cookie_name: str = "mmbzalo_session"
    session_ttl_hours: int = 24
    cookie_secure: bool = False
    cookie_domain: str | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    host_identity: str = "local-dev"
    browser_profiles_root: Path = BASE_DIR / "runtime" / "profiles"
    login_display: str = ":99"

    auth_state_root: Path = BASE_DIR / "auth_state"
    sync_debug_root: Path = BASE_DIR / "debug_sync"
    frontend_dir: Path = BASE_DIR / "frontend"
    legacy_contacts_db_path: Path = BASE_DIR / "contacts.sqlite3"
    legacy_settings_path: Path = BASE_DIR / "settings.json"

    worker_poll_interval_seconds: float = 3.0
    worker_heartbeat_interval_seconds: float = 10.0
    job_lease_seconds: int = 60
    job_max_attempts: int = 3


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.browser_profiles_root.mkdir(parents=True, exist_ok=True)
    settings.auth_state_root.mkdir(parents=True, exist_ok=True)
    settings.sync_debug_root.mkdir(parents=True, exist_ok=True)
    return settings
