from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from app.core.password_policy import WeakPasswordError, check_password
from app.schemas.user import EmailInput, NewUsername, UserRead


class SignupRequest(BaseModel):
    """Signup payload. `role` is not accepted here — every signup is a viewer,
    except the first account on a fresh install, which is the admin."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "jane",
                "email": "jane@example.com",
                "full_name": "Jane Doe",
                "password": "Str0ng@Pass",
                "confirm_password": "Str0ng@Pass",
            }
        }
    )

    username: NewUsername
    email: EmailInput
    full_name: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "SignupRequest":
        if self.password != self.confirm_password:
            raise ValueError("password and confirm_password do not match")
        try:
            check_password(
                self.password,
                username=self.username,
                email=self.email,
                full_name=self.full_name,
            )
        except WeakPasswordError as exc:
            # A custom error, so the message is not prefixed "Value error, ".
            raise PydanticCustomError("weak_password", str(exc)) from exc
        return self


class LoginRequest(BaseModel):
    """Login payload. `identifier` is matched against username *or* email."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"identifier": "jane@example.com", "password": "Str0ng@Pass"}
        }
    )

    identifier: str = Field(
        min_length=3,
        max_length=255,
        description="Username or email address.",
    )
    password: str = Field(min_length=1, max_length=128)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"email": "jane@example.com"}})

    email: EmailInput


class ForgotPasswordResponse(BaseModel):
    """The same answer whether or not the address has an account."""

    detail: str


class TokenResponse(BaseModel):
    """The access token. The refresh token is never in a body: it is set as an
    httpOnly cookie, out of reach of the page's scripts."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")


class AuthResponse(BaseModel):
    """Shared shape returned by signup and login."""

    user: UserRead
    tokens: TokenResponse


class SignupResponse(AuthResponse):
    pass


class SignupStatus(BaseModel):
    open: bool = Field(
        description=(
            "Whether `POST /auth/signup` accepts new accounts. False once the "
            "admin exists when `ALLOW_PUBLIC_SIGNUP` is off: people join by "
            "invitation."
        )
    )


class LoginResponse(AuthResponse):
    pass
