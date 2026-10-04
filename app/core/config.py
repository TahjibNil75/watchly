import ipaddress
import json
import re
from functools import lru_cache
from typing import Annotated, NamedTuple

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class RateLimit(NamedTuple):
    """At most `hits` requests every `seconds`."""

    hits: int
    seconds: int


_RATE_LIMIT = re.compile(r"(\d+)\s*/\s*(\d*)\s*(second|minute|hour|day)s?", re.IGNORECASE)
_COUNTRY_CODE = re.compile(r"[A-Z]{2}")
_RATE_LIMIT_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}


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

    # --- Logging ----------------------------------------------------------
    #: Where the per-module log files go (see app/core/logging_config.py).
    #: Relative paths start from the directory the API is run in.
    LOG_DIR: str = "logs"
    #: DEBUG, INFO, WARNING or ERROR: how much goes to the console and files.
    LOG_LEVEL: str = "INFO"
    #: A log file is rotated once it reaches this size, and this many old ones
    #: are kept next to it.
    LOG_MAX_BYTES: int = Field(default=10_485_760, ge=1024)
    LOG_BACKUP_COUNT: int = Field(default=5, ge=0)

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
    #: Accepted and revoked invitations kept, the newest of each; older ones
    #: are deleted. Expired invitations are deleted as soon as they expire.
    INVITATION_HISTORY_KEEP: int = Field(default=50, ge=0)
    #: Approved account requests kept, the newest; older ones are deleted. A
    #: pending request stays until someone answers it, and a rejected one for
    #: good, so a rejected address cannot ask again until it is unblocked.
    ACCOUNT_REQUEST_HISTORY_KEEP: int = Field(default=50, ge=0)
    #: How long the link confirming a new email address stays usable.
    EMAIL_CHANGE_EXPIRE_HOURS: int = Field(default=24, ge=1)
    #: How long a temporary password from "forgot password" can be used to sign in.
    TEMP_PASSWORD_EXPIRE_MINUTES: int = Field(default=60, ge=5)
    #: Wrong passwords in a row at sign-in before sign-in is locked for
    #: LOGIN_LOCKOUT_MINUTES. The lock lifts itself, so knowing someone's
    #: username is not enough to shut them out for good.
    MAX_FAILED_LOGIN_ATTEMPTS: int = Field(default=5, ge=1)
    #: How long sign-in stays locked after MAX_FAILED_LOGIN_ATTEMPTS. A
    #: temporary password from "forgot password" still gets in meanwhile.
    LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1)

    # --- Rate limits on the endpoints that need no sign-in ------------------
    #: Cap what one client (an IPv4 address, or an IPv6 /64) may send to the
    #: public endpoints; past a limit they answer 429 with Retry-After. Clients
    #: are told apart by address, so behind a reverse proxy uvicorn must trust
    #: its X-Forwarded-For (FORWARDED_ALLOW_IPS), or every client shares the
    #: proxy's limit. Turn off only when something in front already limits.
    RATE_LIMIT_ENABLED: bool = True
    #: Each limit is `<requests>/<period>`, e.g. `10/minute`, `5/hour` or
    #: `20/15 minutes`. NoDecode: see ALERT_DEFAULT_EMAILS.
    #:
    #: Sign-in attempts, right or wrong, to any account. MAX_FAILED_LOGIN_ATTEMPTS
    #: guards each account; this stops one client trying a few common
    #: passwords on every account.
    RATE_LIMIT_LOGIN: Annotated[RateLimit, NoDecode] = RateLimit(10, 60)
    #: Accounts opened with `POST /auth/signup`.
    RATE_LIMIT_SIGNUP: Annotated[RateLimit, NoDecode] = RateLimit(5, 3600)
    #: Temporary passwords asked for, whichever addresses they go to, so the
    #: form cannot be used to mail a list of inboxes.
    RATE_LIMIT_FORGOT_PASSWORD: Annotated[RateLimit, NoDecode] = RateLimit(5, 3600)
    #: Tokens from emailed links tried: previewing and accepting invitations,
    #: and confirming a new email address.
    RATE_LIMIT_EMAIL_LINKS: Annotated[RateLimit, NoDecode] = RateLimit(20, 60)
    #: Accounts asked for with `POST /account-requests`, whichever addresses
    #: they name: each new one emails every admin and DevOps user.
    RATE_LIMIT_ACCOUNT_REQUESTS: Annotated[RateLimit, NoDecode] = RateLimit(5, 3600)

    # --- Visitor country ------------------------------------------------
    #: Name of the request header in which whatever sits in front of Watchly
    #: (AWS WAF, CloudFront, Cloudflare...) puts the visitor's two-letter
    #: country code, e.g. `X-Client-Country`. Empty turns the feature off: no
    #: country is read, recorded or blocked. Anyone who can reach Watchly
    #: without passing through that proxy could send the header themselves, so
    #: the proxy must overwrite it and nothing else may reach the API.
    COUNTRY_HEADER: str = ""
    #: Only these countries may use the API; anyone else gets 403. Empty allows
    #: everyone. Two-letter codes, comma-separated. A request whose country is
    #: unknown (no header, or a code like `XX`) is always let through, so a
    #: proxy that stops sending the header cannot lock everyone out.
    #: NoDecode: see ALERT_DEFAULT_EMAILS.
    COUNTRY_ALLOW: Annotated[list[str], NoDecode] = []
    #: These countries get 403 from the API. Cannot be set with COUNTRY_ALLOW.
    COUNTRY_DENY: Annotated[list[str], NoDecode] = []
    #: Email a user when they sign in from a country they have not signed in
    #: from before. Their first sign-in only starts the list.
    COUNTRY_ALERT_NEW: bool = True

    # --- Monitoring -----------------------------------------------------
    MONITORING_ENABLED: bool = True
    #: How often the scheduler wakes up to look for due checks.
    MONITOR_TICK_SECONDS: int = 60
    #: Per-website default; overridable per website.
    DEFAULT_CHECK_INTERVAL_SECONDS: int = 300
    DEFAULT_TIMEOUT_SECONDS: int = 10
    #: Alerts sent per outage: 1 immediate + 3 follow-ups, then silence.
    DEFAULT_MAX_DOWN_ALERTS: int = 4
    #: A failed check is repeated (up to the site's `retries_on_failure` times)
    #: this long after, before it counts, so a one-off blip stays quiet.
    CHECK_RETRY_DELAY_SECONDS: int = Field(default=5, ge=1, le=60)
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
    #: The Watchly logo as a public HTTPS URL, for the channels that fetch
    #: images themselves: Slack draws it above each alert, Telegram as a small
    #: preview. Email embeds the logo in the message and needs no URL. Blank
    #: leaves it out of Slack and Telegram.
    ALERT_LOGO_URL: str = (
        "https://raw.githubusercontent.com/TahjibNil75/watchly/main/"
        "app/monitoring/alerts/assets/watchly-logo.png"
    )

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
    #: Encrypts stored Slack, Telegram and WhatsApp tokens. Falls back to
    #: SECRET_KEY when unset; changing either makes existing stored tokens
    #: unreadable.
    SLACK_TOKEN_ENCRYPTION_KEY: str = ""

    # Telegram. Like Slack, bot token and chat are configured per project (and
    # optionally per site) through the API; these two together are an optional
    # firehose fallback for projects that have no Telegram of their own.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_API_URL: str = "https://api.telegram.org"
    TELEGRAM_TIMEOUT_SECONDS: int = 10

    # WhatsApp, through Meta's WhatsApp Cloud API. Like Slack and Telegram,
    # the sender (access token and phone number id) and the numbers to alert
    # are configured per project through the API; these three together are an
    # optional firehose fallback for projects that have no WhatsApp of their own.
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    #: NoDecode: see ALERT_DEFAULT_EMAILS.
    WHATSAPP_RECIPIENTS: Annotated[list[str], NoDecode] = []
    #: WhatsApp only delivers a pre-approved template to someone who has not
    #: written to the business in the last 24 hours, so alerts are sent as this
    #: one — see app/monitoring/alerts/whatsapp.py for the text to create it
    #: with. Blank sends free-form text instead, which only reaches people
    #: inside that window: fine for a trial, not for real alerting.
    WHATSAPP_TEMPLATE_NAME: str = "watchly_alert"
    WHATSAPP_TEMPLATE_LANGUAGE: str = "en"
    WHATSAPP_API_URL: str = "https://graph.facebook.com/v25.0"
    WHATSAPP_TIMEOUT_SECONDS: int = 10

    ALERT_WEBHOOK_URL: str = ""

    # --- Site problems short of "down" -------------------------------------
    #: Read each HTTPS site's certificate and warn before it expires.
    SSL_CHECK_ENABLED: bool = True
    #: How often a site's certificate is re-read. Expiry moves in days, so
    #: probing on every check would only add load.
    SSL_CHECK_INTERVAL_SECONDS: int = 21_600
    #: Warn when this many days (or fewer) remain, once per threshold. An
    #: already-expired certificate always warns. NoDecode: see ALERT_DEFAULT_EMAILS.
    SSL_EXPIRY_ALERT_DAYS: Annotated[list[int], NoDecode] = [7, 14]
    #: Look for a CDN in front of each HTTP site: in its response headers and
    #: the DNS aliases of its host. Shown on the site's page; never alerts.
    CDN_CHECK_ENABLED: bool = True
    #: How often that is re-read. Putting a CDN in front moves in days.
    CDN_CHECK_INTERVAL_SECONDS: int = 21_600
    #: Look up where each HTTP site is served from: the address it answered on,
    #: its network (ASN, over Team Cymru's DNS service) and reverse name. Sends
    #: that address, in a DNS query, to Team Cymru. Shown on the site's page;
    #: never alerts.
    SERVER_CHECK_ENABLED: bool = True
    #: How often that is re-read, besides whenever a new address answers.
    SERVER_CHECK_INTERVAL_SECONDS: int = 86_400
    #: Look up when each site's domain registration ends (over RDAP, from the
    #: registry) and warn before it lapses.
    DOMAIN_CHECK_ENABLED: bool = True
    #: How often a site's domain is looked up again. A failed lookup is
    #: retried sooner, after an hour.
    DOMAIN_CHECK_INTERVAL_SECONDS: int = 86_400
    #: Warn when this many days (or fewer) remain, once per threshold, and once
    #: more if it lapses. NoDecode: see ALERT_DEFAULT_EMAILS.
    DOMAIN_EXPIRY_ALERT_DAYS: Annotated[list[int], NoDecode] = [7, 14]
    #: IANA's list of which registry answers RDAP for each top-level domain.
    RDAP_BOOTSTRAP_URL: str = "https://data.iana.org/rdap/dns.json"
    #: A successful response slower than this counts as "slow". 0 turns slow
    #: alerts off; a website can set its own threshold.
    SLOW_RESPONSE_THRESHOLD_MS: int = 3000
    #: Consecutive slow checks before alerting, so one bad request stays quiet.
    SLOW_RESPONSE_CHECKS: int = 3
    #: After a slow alert, stay quiet at least this long for the same site.
    SLOW_ALERT_COOLDOWN_SECONDS: int = 21_600
    #: A ping check that loses at least this share of its pings, while the
    #: host still answers, counts as lossy. 0 turns packet-loss alerts off; a
    #: host can set its own threshold.
    PACKET_LOSS_THRESHOLD_PERCENT: int = Field(default=20, ge=0, le=100)
    #: Consecutive lossy checks before alerting, so one bad moment stays quiet.
    PACKET_LOSS_CHECKS: int = Field(default=3, ge=1)
    #: After a packet-loss alert, stay quiet at least this long for the same host.
    PACKET_LOSS_ALERT_COOLDOWN_SECONDS: int = 21_600

    # --- Ping checks ------------------------------------------------------
    #: Ping over raw sockets, which need root or CAP_NET_RAW. Off, pings use
    #: unprivileged ICMP sockets, which Linux allows only to the groups in the
    #: net.ipv4.ping_group_range sysctl; docker-compose.yml opens it for the API.
    PING_PRIVILEGED: bool = False

    # --- DNS checks -------------------------------------------------------
    #: The resolvers every DNS check asks, as `Name=address` (or a bare
    #: address), comma-separated. Each public one is its own anycast network,
    #: so together they show whether the world sees the same records.
    #: NoDecode: see ALERT_DEFAULT_EMAILS.
    DNS_RESOLVERS: Annotated[list[str], NoDecode] = [
        "Cloudflare=1.1.1.1",
        "Google=8.8.8.8",
        "Quad9=9.9.9.9",
        "OpenDNS=208.67.222.222",
    ]

    # --- Monthly uptime report --------------------------------------------
    MONTHLY_REPORTS_ENABLED: bool = True
    #: Day of the month to send the previous month's report. Capped at 28 so
    #: it exists in every month.
    MONTHLY_REPORT_DAY: int = Field(default=1, ge=1, le=28)
    MONTHLY_REPORT_HOUR_UTC: int = Field(default=6, ge=0, le=23)

    @field_validator("ALERT_DEFAULT_EMAILS", "WHATSAPP_RECIPIENTS", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        """Accept `a@b.com,c@d.com` (or `+8801…,+4478…`) as well as a JSON list."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("COUNTRY_ALLOW", "COUNTRY_DENY", mode="before")
    @classmethod
    def _split_countries(cls, value: object, info: ValidationInfo) -> object:
        """Accept `NP,IN` as well as a JSON list; returned upper-case."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                value = json.loads(value)
            else:
                value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            codes = [str(item).strip().upper() for item in value]
            for code in codes:
                if not _COUNTRY_CODE.fullmatch(code):
                    raise ValueError(
                        f"{info.field_name} entry {code!r} is not a two-letter country code"
                    )
            return list(dict.fromkeys(codes))
        return value

    @model_validator(mode="after")
    def _allow_or_deny(self) -> "Settings":
        if self.COUNTRY_ALLOW and self.COUNTRY_DENY:
            raise ValueError("set COUNTRY_ALLOW or COUNTRY_DENY, not both")
        return self

    @field_validator("SSL_EXPIRY_ALERT_DAYS", "DOMAIN_EXPIRY_ALERT_DAYS", mode="before")
    @classmethod
    def _split_days(cls, value: object, info: ValidationInfo) -> object:
        """Accept `14,7,3,1` as well as a JSON list; returned ascending."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                value = json.loads(value)
            else:
                value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            days = sorted({int(item) for item in value})
            if any(day < 1 for day in days):
                raise ValueError(f"{info.field_name} entries must be 1 or more")
            return days
        return value

    @field_validator(
        "RATE_LIMIT_LOGIN",
        "RATE_LIMIT_SIGNUP",
        "RATE_LIMIT_FORGOT_PASSWORD",
        "RATE_LIMIT_EMAIL_LINKS",
        "RATE_LIMIT_ACCOUNT_REQUESTS",
        mode="before",
    )
    @classmethod
    def _parse_rate_limit(cls, value: object) -> object:
        """Accept `10/minute` or `20/15 minutes`."""
        if isinstance(value, str):
            match = _RATE_LIMIT.fullmatch(value.strip())
            if not match:
                raise ValueError(
                    f"{value!r} is not `<requests>/<period>`, e.g. `10/minute`"
                )
            hits, count, unit = match.groups()
            seconds = int(count or 1) * _RATE_LIMIT_UNITS[unit.lower()]
            if int(hits) < 1 or seconds < 1:
                raise ValueError(f"{value!r} must allow at least 1 request per period")
            return RateLimit(int(hits), seconds)
        return value

    @field_validator("DNS_RESOLVERS", mode="before")
    @classmethod
    def _split_resolvers(cls, value: object) -> object:
        """Accept `Cloudflare=1.1.1.1,8.8.8.8` as well as a JSON list; each
        address must be an IP address, since there is nothing to resolve a
        resolver's name with."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                value = json.loads(value)
            else:
                value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            if not value:
                raise ValueError("DNS_RESOLVERS needs at least one resolver")
            for item in value:
                address = str(item).rpartition("=")[2].strip()
                try:
                    ipaddress.ip_address(address)
                except ValueError:
                    raise ValueError(
                        f"DNS_RESOLVERS entry {item!r} is not `Name=IP address` or an IP address"
                    ) from None
        return value

    @property
    def dns_resolvers(self) -> list[tuple[str, str]]:
        """DNS_RESOLVERS as (name, address) pairs; a bare address is its own name."""
        pairs = []
        for item in self.DNS_RESOLVERS:
            name, _, address = item.rpartition("=")
            address = address.strip()
            pairs.append((name.strip() or address, address))
        return pairs

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
