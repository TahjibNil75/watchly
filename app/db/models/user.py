import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserRole(str, enum.Enum):
    VIEWER = "Viewer"
    ADMIN = "Admin"
    DEVOPS = "DevOps"
    PROJECT_MANAGER = "Project Manager"
    DEVELOPER = "Developer"


# Store the *values* above as the Postgres enum labels rather than SQLAlchemy's
# default of the member names ("PROJECT_MANAGER" etc.).
user_role_enum = SAEnum(
    UserRole,
    name="user_role",
    native_enum=True,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        user_role_enum,
        default=UserRole.VIEWER,
        server_default=text(f"'{UserRole.VIEWER.value}'::user_role"),
        nullable=False,
        index=True,
    )
    last_activity: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: Wrong passwords at sign-in since the last successful one. Reaching
    #: MAX_FAILED_LOGIN_ATTEMPTS suspends the account; reinstating it resets this.
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # --- an email change waiting for the new address to be confirmed -------
    # One at a time: asking again replaces it, and the old link stops working.
    pending_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: SHA-256 of the emailed token, never the token itself (see hash_token).
    email_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    email_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- forgot password ---------------------------------------------------
    #: A temporary password emailed by "forgot password", kept beside the real
    #: one, which goes on working: asking for a reset must not be a way to lock
    #: someone out. Signing in with it makes it the password and sets
    #: must_change_password.
    temp_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    temp_password_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Signed in with a temporary password. Until a new one is chosen the
    #: account may only read itself and change its password.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    def clear_temporary_password(self) -> None:
        self.temp_password_hash = None
        self.temp_password_expires_at = None

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role.value!r}>"
