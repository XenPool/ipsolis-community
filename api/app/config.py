from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Umgebung ──────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    ADMIN_AUTH_DISABLED: bool = False  # Set true to bypass X-Admin-Key in browser/dev use

    # ── Datenbank ─────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://xpuser:changeme@localhost:5432/itselfservice"

    # ── Celery ────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # ── API ───────────────────────────────────────────────────────
    API_SECRET_KEY: str = "dev_secret_key_not_for_production"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8000"
    WEBHOOK_SECRET_TOKEN: str = "dev_webhook_token"
    ADMIN_API_KEY: str = "xpdev_admin_key_change_in_production"

    # ── vSphere ───────────────────────────────────────────────────
    VSPHERE_SERVER: str = "vcenter.example.com"
    VSPHERE_USER: str = "svc@vsphere.local"
    VSPHERE_PASSWORD: str = ""
    VSPHERE_DATACENTER: str = "DC01"
    VSPHERE_CLUSTER: str = "CL-VDI"

    # ── Active Roles ──────────────────────────────────────────────
    AR_WINRM_HOST: str = "ar-server.example.com"
    AR_WINRM_PORT: int = 5985
    AR_WINRM_USER: str = ""
    AR_WINRM_PASSWORD: str = ""
    AR_GROUP_PREFIX_RDP: str = "VDI-RDP-"
    AR_GROUP_PREFIX_ADMIN: str = "VDI-ADM-"

    # ── SCCM ─────────────────────────────────────────────────────
    SCCM_WINRM_HOST: str = "sccm-server.example.com"
    SCCM_WINRM_USER: str = ""
    SCCM_WINRM_PASSWORD: str = ""
    SCCM_TASK_SEQUENCE_ID: str = "TSQ00001"
    SCCM_SITE_CODE: str = "XP1"

    # ── SMTP ─────────────────────────────────────────────────────
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_TLS: bool = True
    MAIL_FROM: str = "noreply@example.com"
    MAIL_FROM_NAME: str = "XenPool IT Selfservice"

    # ── Scheduling ───────────────────────────────────────────────
    REMINDER_HOURS_BEFORE_EXPIRY: int = 24

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"


settings = Settings()
