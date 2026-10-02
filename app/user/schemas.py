from datetime import datetime

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.db.models.user import UserRole
from app.schemas.user import EmailInput, UserRead


class RoleUpdateRequest(BaseModel):
    """Body for changing a user's role."""

    model_config = ConfigDict(json_schema_extra={"example": {"role": "Developer"}})

    role: UserRole = Field(description="The role to assign.")


class ProfileRead(UserRead):
    """The signed-in user's own account, including what only they should see."""

    pending_email: str | None = Field(
        default=None,
        description=(
            "A new address waiting to be confirmed from the link emailed to it. "
            "`email` stays in use until then."
        ),
    )
    pending_email_expires_at: datetime | None = Field(
        default=None,
        # The column's name when read from a User, the field's own otherwise.
        validation_alias=AliasChoices("email_token_expires_at", "pending_email_expires_at"),
        description="When the confirmation link stops working.",
    )


class ProfileUpdate(BaseModel):
    """What you may change about yourself directly. Your email has its own
    endpoint, since a new one has to be confirmed; your role and username are
    not yours to change."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"full_name": "Jane Doe"}}
    )

    full_name: str | None = Field(
        default=None, max_length=255, description='Send null or "" to clear it.'
    )

    @field_validator("full_name")
    @classmethod
    def blank_is_none(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "current_password": "Admin@123",
                "new_password": "N3w@Passw0rd",
                "confirm_password": "N3w@Passw0rd",
            }
        }
    )

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "PasswordChangeRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("new_password and confirm_password do not match")
        return self


class EmailChangeRequest(BaseModel):
    """Ask to move your account to another address. Nothing changes until the
    link emailed to that address is opened."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"new_email": "jane@newdomain.com", "current_password": "Str0ng@Pass"}
        }
    )

    new_email: EmailInput
    current_password: str = Field(min_length=1, max_length=128)


class EmailChangeRequested(ProfileRead):
    email_sent: bool = Field(
        description="False when the change is saved but the confirmation email "
        "could not be sent (SMTP or ALERT_DASHBOARD_URL not set up, or the mail "
        "server failed). Ask again to send a new link."
    )


class EmailConfirmRequest(BaseModel):
    """The token from the confirmation email. Sent in a body, not the URL, so it
    stays out of access logs."""

    token: str = Field(min_length=1, max_length=256)


class UserListResponse(BaseModel):
    """A page of users."""

    items: list[UserRead]
    total: int = Field(description="Total users matching the filters.")
    limit: int
    offset: int


class SignInCountry(BaseModel):
    """One country a user has signed in from."""

    model_config = ConfigDict(from_attributes=True)

    country: str = Field(description="Two-letter ISO country code, e.g. `NP`.")
    first_seen_at: datetime = Field(description="The first sign-in from this country.")
    last_seen_at: datetime = Field(description="The latest sign-in from this country.")
    sign_ins: int = Field(description="How many sign-ins came from it.")


class SignInCountries(BaseModel):
    """Where the signed-in user has signed in from."""

    current: str | None = Field(
        description=(
            "The country of this request, or null when it is unknown or "
            "`COUNTRY_HEADER` is not set."
        )
    )
    countries: list[SignInCountry] = Field(description="Most recent first.")
