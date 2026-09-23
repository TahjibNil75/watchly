import json
from functools import lru_cache
from typing import Annotated

from pydantic import EmailStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables and/or a .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    PROJECT_NAME: str = "Watchly"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # PostgreSQL
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "watchly"

    # JWT — override SECRET_KEY in every real deployment.
    # Generate one with: openssl rand -hex 32
    SECRET_KEY: str = "dev-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Bootstrap admin, created by app/db/seed.py
    FIRST_ADMIN_USERNAME: str = "admin"
    FIRST_ADMIN_EMAIL: EmailStr = "admin@gmail.com"
    FIRST_ADMIN_FULL_NAME: str = "Admin User"
    FIRST_ADMIN_PASSWORD: str = "Admin@123"

    # --- Monitoring -----------------------------------------------------
    MONITORING_ENABLED: bool = True
    #: How often the scheduler wakes up to look for due checks.
    MONITOR_TICK_SECONDS: int = 60
    #: Per-website default; overridable per website.
    DEFAULT_CHECK_INTERVAL_SECONDS: int = 300
    DEFAULT_TIMEOUT_SECONDS: int = 10
    #: Alerts sent per outage: 1 immediate + 3 follow-ups, then silence.
    DEFAULT_MAX_DOWN_ALERTS: int = 4

    # --- Alerting -------------------------------------------------------
    ALERTS_ENABLED: bool = True
    ALERT_EMAIL_ENABLED: bool = True
    #: Recipients added to every alert, on top of each website's own list.
    #: NoDecode hands the raw string to _split_emails; without it the settings
    #: source insists on JSON and rejects `a@b.com,c@d.com` at startup.
    ALERT_DEFAULT_EMAILS: Annotated[list[str], NoDecode] = []
    #: Public base URL used to build links in alert bodies.
    ALERT_DASHBOARD_URL: str = ""

    # SMTP — works with AWS SES, Resend, Mailgun, Postmark, Gmail, ...
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    #: STARTTLS on a plaintext port (587). Mutually exclusive with SMTP_USE_SSL.
    SMTP_USE_TLS: bool = True
    #: Implicit TLS from the first byte (465).
    SMTP_USE_SSL: bool = False
    SMTP_FROM_EMAIL: str = "alerts@example.com"
    SMTP_FROM_NAME: str = "Watchly Alerts"
    SMTP_TIMEOUT_SECONDS: int = 15

    # Slack. Bot token and channel are configured per project (and optionally
    # per site) through the API; this URL is an optional firehose fallback for
    # projects that have no Slack of their own.
    SLACK_WEBHOOK_URL: str = ""
    SLACK_API_URL: str = "https://slack.com/api/chat.postMessage"
    SLACK_TIMEOUT_SECONDS: int = 10
    #: Encrypts stored Slack bot tokens. Falls back to SECRET_KEY when unset;
    #: changing either makes existing stored tokens unreadable.
    SLACK_TOKEN_ENCRYPTION_KEY: str = ""

    ALERT_WEBHOOK_URL: str = ""

    @field_validator("ALERT_DEFAULT_EMAILS", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        """Accept `a@b.com,c@d.com` as well as a JSON list."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def database_url(self) -> str:
        """Async DSN used by the app and by Alembic (asyncpg driver)."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sync_database_url(self) -> str:
        """Sync DSN, handy for psql-style tooling that cannot speak asyncpg."""
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
