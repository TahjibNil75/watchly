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

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role.value!r}>"
