import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.user import User


class AccountRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AccountRequest(TimestampMixin, Base):
    """Someone without an account asking for one, from the sign-in page.

    Anyone can send one, so it proves nothing about who owns the address.
    Approving it sends that address an invitation, and only the invitation's
    emailed link creates the account. A rejected request is kept for good, and
    stops its address asking again; only the newest
    ACCOUNT_REQUEST_HISTORY_KEEP approved ones are kept.
    """

    __tablename__ = "account_requests"
    __table_args__ = (
        # One open request per address. Closed rows drop out of the index; the
        # service, not the index, refuses a second request after a rejection.
        Index(
            "uq_account_requests_open_email",
            "email",
            unique=True,
            postgresql_where=text("approved_at IS NULL AND rejected_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Lower-cased, like Invitation.email.
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Why they want an account, in their own words.
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: SET NULL on delete: the request outlives whoever answered it.
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # selectin, like Invitation.invited_by.
    decided_by: Mapped[User | None] = relationship(
        "User", foreign_keys=[decided_by_id], lazy="selectin"
    )

    @property
    def status(self) -> AccountRequestStatus:
        if self.approved_at is not None:
            return AccountRequestStatus.APPROVED
        if self.rejected_at is not None:
            return AccountRequestStatus.REJECTED
        return AccountRequestStatus.PENDING

    @property
    def decided_by_name(self) -> str | None:
        if self.decided_by is None:
            return None
        return self.decided_by.full_name or self.decided_by.username

    def __repr__(self) -> str:
        return (
            f"<AccountRequest id={self.id} email={self.email!r} "
            f"status={self.status.value!r}>"
        )
