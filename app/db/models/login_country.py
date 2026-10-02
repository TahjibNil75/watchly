from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LoginCountry(Base):
    """A country one user has signed in from, see `app/auth/countries.py`.

    One row per user and country, bumped in place at each sign-in, so a first
    sign-in from a country is the row appearing. The country is the one the
    proxy in front of Watchly reported (`COUNTRY_HEADER`); a sign-in with no
    known country leaves no row.
    """

    __tablename__ = "login_countries"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: Two-letter ISO country code, upper case.
    country: Mapped[str] = mapped_column(String(2), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sign_ins: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def __repr__(self) -> str:
        return f"<LoginCountry user_id={self.user_id} country={self.country!r}>"
