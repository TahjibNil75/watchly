import json
from functools import lru_cache
from typing import Annotated

from pydantic import EmailStr, Field, field_validator
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
    #: Each refresh restarts the clock, so this is how long a session survives
    #: going unused.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    #: Send the refresh cookie over HTTPS only. Browsers make an exception for
    #: http://localhost; turn it off only to try the app over plain HTTP on
    #: another host.
    REFRESH_COOKIE_SECURE: bool = True

    #: How long an emailed invitation link stays usable.
    INVITATION_EXPIRE_DAYS: int = Field(default=7, ge=1)
    #: How long the link confirming a new email address stays usable.
    EMAIL_CHANGE_EXPIRE_HOURS: int = Field(default=24, ge=1)
    #: How long a temporary password from "forgot password" can be used to sign in.
    TEMP_PASSWORD_EXPIRE_MINUTES: int = Field(default=60, ge=5)
    #: Wrong passwords in a row at sign-in before the account is suspended. Only
    #: a user allowed to reinstate it (see SUSPENDABLE_BY) can lift it.
    MAX_FAILED_LOGIN_ATTEMPTS: int = Field(default=5, ge=1)

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
    #: Days of raw check history kept; charts and uptime past that come from
    #: hourly rollups, which are kept. At least 35 so the monthly report has
    #: its month — and whatever it is set to, checks last month's report may
    #: still read are kept until this month ends.
    CHECK_RETENTION_DAYS: int = Field(default=90, ge=35)

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

    # --- Site problems short of "down" -------------------------------------
    #: Read each HTTPS site's certificate and warn before it expires.
    SSL_CHECK_ENABLED: bool = True
    #: How often a site's certificate is re-read. Expiry moves in days, so
    #: probing on every check would only add load.
    SSL_CHECK_INTERVAL_SECONDS: int = 21_600
    #: Warn when this many days (or fewer) remain, once per threshold. An
    #: already-expired certificate always warns. NoDecode: see ALERT_DEFAULT_EMAILS.
    SSL_EXPIRY_ALERT_DAYS: Annotated[list[int], NoDecode] = [1, 3, 7, 14]
    #: A successful response slower than this counts as "slow". 0 turns slow
    #: alerts off; a website can set its own threshold.
    SLOW_RESPONSE_THRESHOLD_MS: int = 3000
    #: Consecutive slow checks before alerting, so one bad request stays quiet.
    SLOW_RESPONSE_CHECKS: int = 3
    #: After a slow alert, stay quiet at least this long for the same site.
    SLOW_ALERT_COOLDOWN_SECONDS: int = 21_600

    # --- Monthly uptime report --------------------------------------------
    MONTHLY_REPORTS_ENABLED: bool = True
    #: Day of the month to send the previous month's report. Capped at 28 so
    #: it exists in every month.
    MONTHLY_REPORT_DAY: int = Field(default=1, ge=1, le=28)
    MONTHLY_REPORT_HOUR_UTC: int = Field(default=6, ge=0, le=23)

    @field_validator("ALERT_DEFAULT_EMAILS", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        """Accept `a@b.com,c@d.com` as well as a JSON list."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("SSL_EXPIRY_ALERT_DAYS", mode="before")
    @classmethod
    def _split_days(cls, value: object) -> object:
        """Accept `14,7,3,1` as well as a JSON list; returned ascending."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                value = json.loads(value)
            else:
                value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            days = sorted({int(item) for item in value})
            if any(day < 1 for day in days):
                raise ValueError("SSL_EXPIRY_ALERT_DAYS entries must be 1 or more")
            return days
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
