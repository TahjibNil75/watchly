from datetime import datetime

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

from app.monitoring.projects.schemas import ProjectMemberRead
from app.monitoring.websites.models import WebsiteStatus

HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})


class WebsiteBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: AnyHttpUrl
    method: str = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    check_interval_seconds: int = Field(default=300, ge=30, le=86_400)
    max_down_alerts: int = Field(
        default=4,
        ge=1,
        le=50,
        description="Alerts per outage: the immediate one plus follow-ups.",
    )
    is_enabled: bool = True
    alert_emails: list[EmailStr] = Field(
        default_factory=list,
        description=(
            "Addresses alerted for this site only that are not user accounts, "
            "such as a client contact or a shared inbox."
        ),
    )
    inherit_project_recipients: bool = Field(
        default=True,
        description=(
            "Also email the project's members and extra_emails. Set false so "
            "only this site's own recipients and alert_emails are emailed."
        ),
    )
    slack_channel_id: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Post this site's alerts to its own channel instead of the "
            "project's. Uses the project's bot token either way."
        ),
    )

    @field_validator("method")
    @classmethod
    def known_method(cls, value: str) -> str:
        upper = value.upper()
        if upper not in HTTP_METHODS:
            raise ValueError(f"method must be one of {sorted(HTTP_METHODS)}")
        return upper


class WebsiteCreate(WebsiteBase):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "project_id": 1,
                "name": "Marketing site",
                "url": "https://www.monstarpeople.com/",
                "check_interval_seconds": 300,
                "recipient_ids": [3, 7],
                "alert_emails": ["client@theirdomain.com"],
            }
        }
    )

    project_id: int = Field(ge=1, description="The project this site belongs to.")
    recipient_ids: list[int] = Field(
        default_factory=list,
        description="Users alerted about this site only, in addition to alert_emails.",
    )


class WebsiteUpdate(BaseModel):
    """Partial update; omitted fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: AnyHttpUrl | None = None
    method: str | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    check_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    max_down_alerts: int | None = Field(default=None, ge=1, le=50)
    is_enabled: bool | None = None
    alert_emails: list[EmailStr] | None = Field(
        default=None, description="Replaces the whole list when supplied."
    )
    inherit_project_recipients: bool | None = None
    slack_channel_id: str | None = Field(default=None, max_length=32)

    _known_method = field_validator("method")(WebsiteBase.known_method.__func__)


class WebsiteRecipientsUpdate(BaseModel):
    """Add several site recipients in one call."""

    model_config = ConfigDict(json_schema_extra={"example": {"recipient_ids": [3, 7]}})

    recipient_ids: list[int] = Field(min_length=1)


class WebsiteRecipientRead(ProjectMemberRead):
    """A user alerted about this one site."""


class WebsiteCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    checked_at: datetime
    is_up: bool
    status_code: int | None
    response_time_ms: int | None
    error: str | None
    final_url: str | None


class WebsiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    url: str
    method: str
    expected_status: int
    timeout_seconds: int
    check_interval_seconds: int
    max_down_alerts: int
    is_enabled: bool
    recipients: list[WebsiteRecipientRead]
    alert_emails: list[str]
    inherit_project_recipients: bool
    alert_channels: list[str] = Field(
        default_factory=list,
        description="Channels this site's alerts reach, e.g. `[\"email\"]`.",
    )
    slack_channel_id: str | None = None
    status: WebsiteStatus
    last_checked_at: datetime | None
    down_since: datetime | None
    consecutive_failures: int
    down_alerts_sent: int
    created_at: datetime
    updated_at: datetime


class WebsiteListResponse(BaseModel):
    items: list[WebsiteRead]
    total: int
    limit: int
    offset: int


class CheckNowResponse(BaseModel):
    """Result of an on-demand probe, including any alert it triggered."""

    website: WebsiteRead
    check: WebsiteCheckRead
    alert_sent: str | None = Field(
        default=None, description="Which alert fired, if any: down/still_down/recovered."
    )
