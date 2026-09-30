from pydantic import BaseModel, ConfigDict, Field

from app.monitoring.alerts.base import NotificationKind
from app.monitoring.notifications.catalog import CATALOG
from app.monitoring.notifications.service import EffectiveSetting
from app.monitoring.notifications.templating import BODY_MAX, SUBJECT_MAX


class NotificationOverrides(BaseModel):
    """What one level (global, or one project) stores itself. Null inherits."""

    email_enabled: bool | None = None
    slack_enabled: bool | None = None
    telegram_enabled: bool | None = None
    whatsapp_enabled: bool | None = None
    subject: str | None = None
    body: str | None = None


class NotificationSettingRead(BaseModel):
    """One notification kind as it behaves at one level, after inheritance."""

    kind: NotificationKind
    label: str
    description: str
    audience: str
    email_enabled: bool
    slack_enabled: bool
    telegram_enabled: bool
    whatsapp_enabled: bool
    subject: str = Field(description="The subject template in effect.")
    body: str = Field(description="The body template in effect.")
    sources: dict[str, str] = Field(
        description=(
            "Where each field's value comes from: `project`, `global` or `default`."
        )
    )
    overrides: NotificationOverrides = Field(
        description="What this level itself sets; null means it inherits."
    )
    inherited_subject: str = Field(
        description="What a blank subject falls back to at this level."
    )
    inherited_body: str = Field(description="What a blank body falls back to at this level.")
    default_subject: str
    default_body: str
    placeholders: dict[str, str] = Field(
        description="`{{name}}` -> what it becomes, for subject and body."
    )

    @classmethod
    def of(cls, setting: EffectiveSetting) -> "NotificationSettingRead":
        info = CATALOG[setting.kind]
        return cls(
            kind=setting.kind,
            label=info.label,
            description=info.description,
            audience=info.audience,
            email_enabled=setting.email_enabled,
            slack_enabled=setting.slack_enabled,
            telegram_enabled=setting.telegram_enabled,
            whatsapp_enabled=setting.whatsapp_enabled,
            subject=setting.subject,
            body=setting.body,
            sources=setting.sources,
            overrides=NotificationOverrides(**setting.overrides),
            inherited_subject=setting.inherited_subject,
            inherited_body=setting.inherited_body,
            default_subject=info.default_subject,
            default_body=info.default_body,
            placeholders=info.placeholders,
        )


class NotificationSettingsResponse(BaseModel):
    items: list[NotificationSettingRead]


class NotificationSettingUpdate(BaseModel):
    """Replaces what this level overrides for one kind.

    Send every field: one left null (or blank) inherits from the level above,
    and sending all of them as null removes the override entirely.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email_enabled": True,
                "slack_enabled": False,
                "telegram_enabled": True,
                "whatsapp_enabled": False,
                "subject": "[{{project}}] {{website}} is down",
                "body": "Heads up — {{website}} stopped responding: {{summary}}",
            }
        }
    )

    email_enabled: bool | None = None
    slack_enabled: bool | None = None
    telegram_enabled: bool | None = None
    whatsapp_enabled: bool | None = None
    subject: str | None = Field(
        default=None,
        max_length=SUBJECT_MAX,
        description="One line. Use `{{placeholders}}`; see the kind's `placeholders`.",
    )
    body: str | None = Field(
        default=None,
        max_length=BODY_MAX,
        description=(
            "Plain text; line breaks are kept. In Slack it is mrkdwn, so `*bold*` "
            "and mentions such as `<!channel>` work there. Telegram shows it as "
            "plain text, and WhatsApp as plain text on a single line."
        ),
    )


class NotificationPreviewRequest(BaseModel):
    kind: NotificationKind
    subject: str | None = Field(
        default=None, max_length=SUBJECT_MAX, description="Omit for the built-in default."
    )
    body: str | None = Field(
        default=None, max_length=BODY_MAX, description="Omit for the built-in default."
    )


class NotificationPreviewResponse(BaseModel):
    """A sample notification, rendered exactly as it would be sent."""

    subject: str
    email_html: str
    email_text: str
    slack_text: str
    slack_blocks: list[dict]
    telegram_html: str = Field(
        description="The Telegram message: text in Telegram's HTML subset."
    )
    logo_url: str | None = Field(
        default=None,
        description=(
            "The logo Slack draws above the message and Telegram shows as its "
            "preview (ALERT_LOGO_URL); null when it is off."
        ),
    )
    whatsapp_text: str = Field(
        description=(
            "The WhatsApp message as it reads on the phone: the approved template "
            "with its variables filled in, or the free-form text (with `*bold*` "
            "and `_italic_`) when WHATSAPP_TEMPLATE_NAME is blank."
        )
    )


class ReportSendRequest(BaseModel):
    month: str | None = Field(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="`YYYY-MM`, a month that has ended. Defaults to last month.",
        examples=["2026-08"],
    )


class ReportSendResponse(BaseModel):
    month: str
    sites: int = Field(description="Websites included in the report.")
    email_recipients: int
    slack_configured: bool
    telegram_configured: bool
    whatsapp_configured: bool
    delivered_by: list[str] = Field(
        description=(
            "Channels that delivered it. Empty means it was not delivered: the "
            "notification is switched off for the project, no channel is "
            "configured, or sending failed (see the API log)."
        )
    )
