import re
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.monitoring.infra.aws.credentials import AccountInput
from app.monitoring.projects.models import ProjectMonitors


class ProjectMemberRead(BaseModel):
    """A person responsible for a project — and therefore an alert recipient."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr
    full_name: str | None
    is_active: bool


class SlackSettings(BaseModel):
    """Write-only Slack configuration. Shared by create and update."""

    slack_bot_token: str | None = Field(
        default=None,
        min_length=8,
        max_length=255,
        description=(
            "Slack bot token (`xoxb-…`). Stored encrypted and never returned. "
            "Send null to remove it."
        ),
    )
    slack_channel_id: str | None = Field(
        default=None,
        max_length=32,
        description="Channel id such as `C0123456789` — not the `#name`.",
    )
    slack_enabled: bool | None = Field(
        default=None, description="Mute Slack without discarding the settings."
    )

    @field_validator("slack_bot_token")
    @classmethod
    def looks_like_a_bot_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        token = value.strip()
        if not token.startswith("xoxb-"):
            raise ValueError(
                "expected a bot token starting with 'xoxb-'; user tokens "
                "(xoxp-) and webhook URLs will not work here"
            )
        return token

    @field_validator("slack_channel_id")
    @classmethod
    def looks_like_a_channel_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        channel = value.strip()
        if channel.startswith("#"):
            raise ValueError(
                "use the channel id (Slack → channel → About → bottom), "
                "not the #name"
            )
        return channel


#: A bot token as @BotFather hands it out: the bot's numeric id, a colon, then
#: the secret.
_TELEGRAM_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")
#: A numeric chat id (groups and channels are negative), or a public
#: `@channelname`.
_TELEGRAM_CHAT = re.compile(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{4,31}")


class TelegramSettings(BaseModel):
    """Write-only Telegram configuration. Shared by create and update."""

    telegram_bot_token: str | None = Field(
        default=None,
        min_length=20,
        max_length=255,
        description=(
            "Telegram bot token from @BotFather (`123456789:AA…`). Stored "
            "encrypted and never returned. Send null to remove it."
        ),
    )
    telegram_chat_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Chat to send to: a numeric id such as `-1001234567890` for a group "
            "or channel (or a person's id), or `@channelname` for a public channel."
        ),
    )
    telegram_enabled: bool | None = Field(
        default=None, description="Mute Telegram without discarding the settings."
    )

    @field_validator("telegram_bot_token")
    @classmethod
    def looks_like_a_telegram_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        token = value.strip()
        if not _TELEGRAM_TOKEN.fullmatch(token):
            raise ValueError(
                "expected the bot token @BotFather gave you, like "
                "'123456789:AAH…' — not a bot URL or @username"
            )
        return token

    @field_validator("telegram_chat_id")
    @classmethod
    def looks_like_a_chat_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        chat = value.strip()
        if not _TELEGRAM_CHAT.fullmatch(chat):
            raise ValueError(
                "use a numeric chat id such as -1001234567890, or @channelname "
                "for a public channel — not a t.me link or a group's title"
            )
        return chat


#: A Meta access token: a long run of letters and digits, usually `EAA…`.
_WHATSAPP_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}")
#: Country code and number, 7 to 15 digits in all (E.164).
_PHONE = re.compile(r"\+[1-9]\d{6,14}")
#: What people write a number with, besides its digits.
_PHONE_PUNCTUATION = re.compile(r"[\s().-]")
#: Each number costs one API call per alert, made one after the other.
MAX_WHATSAPP_RECIPIENTS = 10


def phone_numbers(numbers: list[str] | None) -> list[str] | None:
    """`["+880 1712-345678", "008801712345678"]` -> `["+8801712345678"]`:
    each number normalized to `+` and digits, duplicates dropped."""
    if numbers is None:
        return None
    normalized: dict[str, None] = {}
    for raw in numbers:
        number = _PHONE_PUNCTUATION.sub("", raw)
        if number.startswith("00"):
            number = "+" + number[2:]
        if not _PHONE.fullmatch(number):
            raise ValueError(
                f"{raw!r} is not a phone number with its country code — write it "
                "like +8801712345678"
            )
        normalized.setdefault(number, None)
    return list(normalized)


class WhatsAppSettings(BaseModel):
    """Write-only WhatsApp configuration. Shared by create and update."""

    whatsapp_access_token: str | None = Field(
        default=None,
        min_length=20,
        max_length=1024,
        description=(
            "Meta access token of a system user with the "
            "`whatsapp_business_messaging` permission (`EAA…`). Stored encrypted "
            "and never returned. Send null to remove WhatsApp."
        ),
    )
    whatsapp_phone_number_id: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "The Phone number ID that WhatsApp Manager → API setup shows for the "
            "business number that sends — not the phone number itself."
        ),
    )
    whatsapp_recipients: list[str] | None = Field(
        default=None,
        max_length=MAX_WHATSAPP_RECIPIENTS,
        description=(
            "Numbers to alert, with country code, such as `+8801712345678`. "
            "Replaces the whole list; send null or [] to remove WhatsApp."
        ),
    )
    whatsapp_enabled: bool | None = Field(
        default=None, description="Mute WhatsApp without discarding the settings."
    )

    @field_validator("whatsapp_access_token")
    @classmethod
    def looks_like_an_access_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        token = value.strip().removeprefix("Bearer ").strip()
        if not _WHATSAPP_TOKEN.fullmatch(token):
            raise ValueError(
                "expected the access token Meta gave you, usually starting 'EAA' — "
                "not the app secret, a phone number or a URL"
            )
        return token

    @field_validator("whatsapp_phone_number_id")
    @classmethod
    def looks_like_a_phone_number_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        number_id = value.strip()
        if not number_id.isdigit():
            raise ValueError(
                "use the Phone number ID from WhatsApp Manager → API setup, all "
                "digits — not the phone number itself"
            )
        return number_id

    @field_validator("whatsapp_recipients")
    @classmethod
    def looks_like_phone_numbers(cls, value: list[str] | None) -> list[str] | None:
        return phone_numbers(value)


def check_alert_channels(
    *,
    has_email: bool,
    slack_token: str | None,
    slack_channel: str | None,
    telegram_token: str | None,
    telegram_chat: str | None,
    whatsapp_token: str | None,
    whatsapp_phone_number_id: str | None,
    whatsapp_recipients: list[str] | None,
) -> None:
    """A project must be able to tell someone when a site goes down.

    Shared by the create payload and the update path so the two cannot drift.
    Raises ValueError, which surfaces as a 422.
    """
    if bool(slack_token) != bool(slack_channel):
        raise ValueError(
            "slack_bot_token and slack_channel_id must be set together — "
            "one without the other cannot deliver anything"
        )
    if bool(telegram_token) != bool(telegram_chat):
        raise ValueError(
            "telegram_bot_token and telegram_chat_id must be set together — "
            "one without the other cannot deliver anything"
        )
    whatsapp = (whatsapp_token, whatsapp_phone_number_id, whatsapp_recipients)
    if any(whatsapp) and not all(whatsapp):
        raise ValueError(
            "whatsapp_access_token, whatsapp_phone_number_id and "
            "whatsapp_recipients must be set together — a sender with nobody to "
            "send to, or numbers with no sender, cannot deliver anything"
        )
    if not (has_email or slack_token or telegram_token or whatsapp_token):
        raise ValueError(
            "choose at least one way to be alerted: add member_ids or "
            "extra_emails for email, slack_bot_token with slack_channel_id for "
            "Slack, telegram_bot_token with telegram_chat_id for Telegram, "
            "and/or whatsapp_access_token with whatsapp_phone_number_id and "
            "whatsapp_recipients for WhatsApp. Any combination is fine."
        )


DESCRIPTION_MIN_WORDS = 5
DESCRIPTION_MAX_WORDS = 150


def check_description_length(value: str | None) -> str | None:
    """A description, when given, runs from 5 to 150 words."""
    if value is None:
        return value
    words = len(value.split())
    if words < DESCRIPTION_MIN_WORDS or words > DESCRIPTION_MAX_WORDS:
        raise ValueError(
            f"description must be {DESCRIPTION_MIN_WORDS} to "
            f"{DESCRIPTION_MAX_WORDS} words (it has {words})"
        )
    return value


NAME_MAX_WORDS = 10


def check_name_length(value: str) -> str:
    """A new project's name has no minimum length, a single word will do, but
    runs to at most 10 words. It still cannot be blank."""
    words = len(value.split())
    if not words:
        raise ValueError("name must not be blank")
    if words > NAME_MAX_WORDS:
        raise ValueError(f"name must be at most {NAME_MAX_WORDS} words (it has {words})")
    return value


class ProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    monitors: ProjectMonitors = Field(
        default=ProjectMonitors.WEBSITES,
        description="`websites` or `infrastructure` (AWS). Fixed once the project exists.",
    )
    extra_emails: list[EmailStr] = Field(
        default_factory=list,
        description=(
            "Addresses alerted for every site in this project that are not user "
            "accounts — a client contact, a shared on-call inbox. Members are "
            "alerted regardless."
        ),
    )


class ProjectCreate(ProjectBase, SlackSettings, TelegramSettings, WhatsAppSettings):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Monstar People Marketing Site",
                "description": "Marketing website for our client Monstar People",
                "member_ids": [3, 7],
                "extra_emails": ["oncall@example.com"],
                "slack_bot_token": "xoxb-your-bot-token",
                "slack_channel_id": "C0123456789",
                "telegram_bot_token": "123456789:AAH-your-bot-token-from-botfather",
                "telegram_chat_id": "-1001234567890",
                "whatsapp_access_token": "EAA-your-system-user-access-token",
                "whatsapp_phone_number_id": "106540352242922",
                "whatsapp_recipients": ["+8801712345678"],
            }
        }
    )

    member_ids: list[int] = Field(
        default_factory=list,
        description="Users responsible for this project; they receive its alerts.",
    )
    aws_accounts: list[AccountInput] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "With `monitors: infrastructure`, at least one: the AWS accounts its "
            "resources are read from. Each is tried with AWS before the project is created."
        ),
    )

    _name_length = field_validator("name")(check_name_length)
    _description_length = field_validator("description")(check_description_length)

    @model_validator(mode="after")
    def accounts_suit_what_it_monitors(self) -> "ProjectCreate":
        if self.monitors is ProjectMonitors.INFRASTRUCTURE:
            if not self.aws_accounts:
                raise ValueError("An infrastructure project needs at least one AWS account.")
            names = [account.name.strip().lower() for account in self.aws_accounts]
            if len(set(names)) != len(names):
                raise ValueError("Give each AWS account a name of its own.")
        elif self.aws_accounts:
            raise ValueError("AWS accounts belong to infrastructure projects: set monitors to infrastructure.")
        return self

    @model_validator(mode="after")
    def at_least_one_alert_channel(self) -> "ProjectCreate":
        check_alert_channels(
            has_email=bool(self.member_ids or self.extra_emails),
            slack_token=self.slack_bot_token,
            slack_channel=self.slack_channel_id,
            telegram_token=self.telegram_bot_token,
            telegram_chat=self.telegram_chat_id,
            whatsapp_token=self.whatsapp_access_token,
            whatsapp_phone_number_id=self.whatsapp_phone_number_id,
            whatsapp_recipients=self.whatsapp_recipients,
        )
        return self


class ProjectUpdate(SlackSettings, TelegramSettings, WhatsAppSettings):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    extra_emails: list[EmailStr] | None = Field(
        default=None, description="Replaces the whole list when supplied."
    )

    _description_length = field_validator("description")(check_description_length)


class ProjectMembersUpdate(BaseModel):
    """Add or remove several members in one call."""

    model_config = ConfigDict(json_schema_extra={"example": {"member_ids": [3, 7, 11]}})

    member_ids: list[int] = Field(min_length=1)


class ProjectRead(ProjectBase):
    """What the API returns. The bot tokens are deliberately absent."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int | None
    members: list[ProjectMemberRead]
    slack_channel_id: str | None = None
    slack_enabled: bool = True
    slack_configured: bool = Field(
        default=False,
        description="True when a token and channel are both stored and enabled.",
    )
    alert_channels: list[str] = Field(
        default_factory=list,
        description=(
            "Configured channels, e.g. `[\"email\"]` or "
            "`[\"email\",\"slack\",\"telegram\",\"whatsapp\"]`."
        ),
    )
    slack_token_hint: str | None = Field(
        default=None, description="Masked tail of the stored token, e.g. `xoxb-…9f2a`."
    )
    telegram_chat_id: str | None = None
    telegram_enabled: bool = True
    telegram_configured: bool = Field(
        default=False,
        description="True when a token and chat are both stored and enabled.",
    )
    telegram_token_hint: str | None = Field(
        default=None,
        description="Masked stored token, e.g. `123456789:…wxyz`.",
    )
    whatsapp_phone_number_id: str | None = None
    whatsapp_recipients: list[str] = Field(default_factory=list)
    whatsapp_enabled: bool = True
    whatsapp_configured: bool = Field(
        default=False,
        description="True when a token, phone number id and numbers are all stored and enabled.",
    )
    whatsapp_token_hint: str | None = Field(
        default=None, description="Masked stored token, e.g. `EAA…wxyz`."
    )
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    items: list[ProjectRead]
    total: int
    limit: int
    offset: int
