from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.models.user import UserRole


class UserBase(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)
    role: UserRole = UserRole.VIEWER
    is_active: bool = True


class UserCreate(UserBase):
    """Payload for creating a user; the plaintext password is hashed before storage."""

    password: str = Field(min_length=8, max_length=128)


class UserUpdate(BaseModel):
    """Partial update; every field is optional."""

    username: str | None = Field(default=None, min_length=3, max_length=50)
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: UserRole | None = None
    is_active: bool | None = None
    last_activity: datetime | None = None


class UserRead(UserBase):
    """What the API returns — never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    last_activity: datetime | None = None
    must_change_password: bool = Field(
        default=False,
        description=(
            "Signed in with a temporary password: until a new one is set with "
            "`POST /users/me/password`, every other endpoint answers 403."
        ),
    )
    failed_login_attempts: int = Field(
        default=0,
        description=(
            "Wrong passwords at sign-in since the last successful one or the "
            "last lockout. At `MAX_FAILED_LOGIN_ATTEMPTS` sign-in is locked."
        ),
    )
    locked_until: datetime | None = Field(
        default=None,
        description=(
            "Sign-in is refused until then after too many wrong passwords, "
            "except with a temporary password. Lifts by itself; in the past "
            "or null means not locked."
        ),
    )
    created_at: datetime
    updated_at: datetime


class UserInDB(UserRead):
    """Internal representation, including the stored hash."""

    password_hash: str
