import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.user import User, UserRole, user_role_enum


class InvitationStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    #: Never acted on and past `expires_at`. Derived, not stored, and only
    #: until the next purge deletes it — see InvitationService.purge.
    EXPIRED = "expired"


class Invitation(TimestampMixin, Base):
    """An offer of an account, sent by email, that fixes the role up front.

    The emailed token is the only proof of owning the address, so only its
    SHA-256 digest is stored — a database read cannot be turned into a working
    link. Accepting creates the user and stamps `accepted_at`. Expired
    invitations are deleted, and only the newest INVITATION_HISTORY_KEEP
    accepted and revoked ones are kept, so who invited whom stays on record for
    recent arrivals only.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        # One live invitation per address. Accepted and revoked rows drop out of
        # the index, so history can build up while re-inviting stays possible.
        Index(
            "uq_invitations_open_email",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Lower-cased, so `Jane@x.com` and `jane@x.com` cannot both be invited.
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    role: Mapped[UserRole] = mapped_column(user_role_enum, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: SET NULL on delete: the invitation outlives its sender. Accepting one
    #: whose sender is gone is refused — see InvitationService.
    invited_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # selectin, like Project.owner: the sender's name is read while building
    # responses, where a lazy load would raise MissingGreenlet.
    invited_by: Mapped[User | None] = relationship(
        "User", foreign_keys=[invited_by_id], lazy="selectin"
    )

    @property
    def status(self) -> InvitationStatus:
        if self.accepted_at is not None:
            return InvitationStatus.ACCEPTED
        if self.revoked_at is not None:
            return InvitationStatus.REVOKED
        if self.expires_at <= datetime.now(UTC):
            return InvitationStatus.EXPIRED
        return InvitationStatus.PENDING

    @property
    def invited_by_name(self) -> str | None:
        if self.invited_by is None:
            return None
        return self.invited_by.full_name or self.invited_by.username

    def __repr__(self) -> str:
        return (
            f"<Invitation id={self.id} email={self.email!r} "
            f"role={self.role.value!r} status={self.status.value!r}>"
        )
