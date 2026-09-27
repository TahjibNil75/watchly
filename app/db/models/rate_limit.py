from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RateLimitBucket(Base):
    """How many requests one client has sent to one group of public endpoints
    in its current window; see `app/core/rate_limit.py`.

    The window opens with the client's first request and closes at
    `resets_at`; the first request after that opens the next one. There is a
    single row per client and group, and it is bumped in place.
    """

    __tablename__ = "rate_limit_buckets"

    #: The group of endpoints, named after its RATE_LIMIT_* setting: `login`, ...
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: An IPv4 address, or an IPv6 /64.
    client: Mapped[str] = mapped_column(String(64), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Indexed for the sweep that deletes closed windows on every scheduler tick.
    resets_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<RateLimitBucket scope={self.scope!r} client={self.client!r} "
            f"hits={self.hits}>"
        )
