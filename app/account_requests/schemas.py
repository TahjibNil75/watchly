from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.account_request import AccountRequestStatus
from app.schemas.user import EmailInput


class AccountRequestCreate(BaseModel):
    """Body for asking for an account. No role: an approved request always
    starts as a Viewer."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "jane@example.com",
                "full_name": "Jane Doe",
                "message": "I am on the support team and need to see site status.",
            }
        }
    )

    email: EmailInput
    full_name: str | None = Field(default=None, max_length=255)
    message: str | None = Field(
        default=None, max_length=500, description="Why you want an account."
    )


class AccountRequestReceived(BaseModel):
    """The same answer whether or not the request was saved."""

    detail: str


class AccountRequestRead(BaseModel):
    """An account request as the people answering it see it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str | None = None
    message: str | None = None
    status: AccountRequestStatus
    decided_by_id: int | None = None
    decided_by_name: str | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    created_at: datetime


class AccountRequestDecided(AccountRequestRead):
    """What approving or rejecting a request returns."""

    email_sent: bool = Field(
        description="False when the decision is saved but the email telling the "
        "requester could not be delivered (SMTP or ALERT_DASHBOARD_URL not set "
        "up, or the mail server failed). After an approval, resend the "
        "invitation it created."
    )


class AccountRequestListResponse(BaseModel):
    """A page of account requests, newest first."""

    items: list[AccountRequestRead]
    total: int = Field(description="Total requests matching the filters.")
    limit: int
    offset: int
