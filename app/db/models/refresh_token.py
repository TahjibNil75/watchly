from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class RefreshToken(TimestampMixin, Base):
    """A refresh token, good for one trip to `POST /auth/refresh`.

    Redeeming it stamps `used_at` and issues its successor in the same
    `family_id`: every token descended from one sign-in shares it. A token
    presented after it was used means two parties hold a copy, so the whole
    family is revoked and both have to sign in again.

    The token travels only in an httpOnly cookie, and only its SHA-256 digest
    is stored, like invitation tokens: a database read cannot be turned into a
    working session.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    family_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: Indexed for the sweep that deletes expired rows on every scheduler tick
    #: and at each sign-in.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    #: Exchanged for its successor. Presenting it again revokes the family.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Signed out, suspended, or caught being reused.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<RefreshToken id={self.id} user_id={self.user_id} "
            f"family_id={self.family_id!r}>"
        )
