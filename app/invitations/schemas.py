from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.auth.schemas import AuthResponse
from app.db.models.invitation import InvitationStatus
from app.db.models.user import UserRole


class InvitationCreate(BaseModel):
    """Body for inviting someone. The role is required — there is no default."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"email": "jane@example.com", "role": "Developer"}}
    )

    email: EmailStr
    role: UserRole = Field(
        description="The role the new account starts with. Which roles you may "
        "grant depends on your own (see INVITABLE_BY)."
    )


class InvitationRead(BaseModel):
    """An invitation as managers see it. Never includes the token."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: UserRole
    status: InvitationStatus
    invited_by_id: int | None = None
    invited_by_name: str | None = None
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class InvitationCreated(InvitationRead):
    """What creating an invitation returns."""

    email_sent: bool = Field(
        description="False when the invitation is saved but the email could not "
        "be delivered (SMTP or ALERT_DASHBOARD_URL not set up, or the mail server "
        "failed). Send it again to replace it."
    )


class InvitationListResponse(BaseModel):
    """A page of invitations, newest first."""

    items: list[InvitationRead]
    total: int = Field(description="Total invitations matching the filters.")
    limit: int
    offset: int


# --- The invitee's side: public, authenticated by the emailed token ------------


class InvitationTokenRequest(BaseModel):
    """The token from an invitation email. Sent in a body, not the URL, so it
    stays out of access logs."""

    token: str = Field(min_length=1, max_length=256)


class InvitationPreview(BaseModel):
    """What an invitee is shown before accepting. Nothing here is secret: the
    caller already holds the token that proves the invitation is theirs."""

    email: str
    role: UserRole
    invited_by_name: str | None = None
    expires_at: datetime


class AcceptInvitationRequest(InvitationTokenRequest):
    """Accept payload. Neither `email` nor `role` is accepted — both come from
    the invitation."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "token": "<from the invitation email>",
                "username": "jane",
                "full_name": "Jane Doe",
                "password": "Str0ng@Pass",
                "confirm_password": "Str0ng@Pass",
            }
        }
    )

    username: str = Field(min_length=3, max_length=50)
    full_name: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "AcceptInvitationRequest":
        if self.password != self.confirm_password:
            raise ValueError("password and confirm_password do not match")
        return self


class AcceptInvitationResponse(AuthResponse):
    """The new user plus a token pair, as signup returns."""
