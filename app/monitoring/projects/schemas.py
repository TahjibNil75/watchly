from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


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


def check_alert_channels(
    *, has_email: bool, slack_token: str | None, slack_channel: str | None
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
    if not has_email and not slack_token:
        raise ValueError(
            "choose at least one way to be alerted: add member_ids or "
            "extra_emails for email, and/or slack_bot_token with "
            "slack_channel_id for Slack. Both together is fine."
        )


class ProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    extra_emails: list[EmailStr] = Field(
        default_factory=list,
        description=(
            "Addresses alerted for every site in this project that are not user "
            "accounts — a client contact, a shared on-call inbox. Members are "
            "alerted regardless."
        ),
    )


class ProjectCreate(ProjectBase, SlackSettings):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Monstar People",
                "description": "Client marketing site",
                "member_ids": [3, 7],
                "extra_emails": ["oncall@example.com"],
                "slack_bot_token": "xoxb-your-bot-token",
                "slack_channel_id": "C0123456789",
            }
        }
    )

    member_ids: list[int] = Field(
        default_factory=list,
        description="Users responsible for this project; they receive its alerts.",
    )

    @model_validator(mode="after")
    def at_least_one_alert_channel(self) -> "ProjectCreate":
        check_alert_channels(
            has_email=bool(self.member_ids or self.extra_emails),
            slack_token=self.slack_bot_token,
            slack_channel=self.slack_channel_id,
        )
        return self


class ProjectUpdate(SlackSettings):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    extra_emails: list[EmailStr] | None = Field(
        default=None, description="Replaces the whole list when supplied."
    )


class ProjectMembersUpdate(BaseModel):
    """Add or remove several members in one call."""

    model_config = ConfigDict(json_schema_extra={"example": {"member_ids": [3, 7, 11]}})

    member_ids: list[int] = Field(min_length=1)


class ProjectRead(ProjectBase):
    """What the API returns. The Slack bot token is deliberately absent."""

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
        description="Configured channels, e.g. `[\"email\"]` or `[\"email\",\"slack\"]`.",
    )
    slack_token_hint: str | None = Field(
        default=None, description="Masked tail of the stored token, e.g. `xoxb-…9f2a`."
    )
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    items: list[ProjectRead]
    total: int
    limit: int
    offset: int
