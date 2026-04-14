from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ─────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://xpuser:changeme@localhost:5432/itselfservice"

    # ── Celery ────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # ── API ───────────────────────────────────────────────────────
    API_SECRET_KEY: str = "change_me_in_production_min_32_chars"
    CORS_ORIGINS: str = "https://localhost"
    WEBHOOK_SECRET_TOKEN: str = "change_me_webhook_secret"
    ADMIN_API_KEY: str = "change_me_admin_key_min_32_chars"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]


settings = Settings()
