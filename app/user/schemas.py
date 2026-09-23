from pydantic import BaseModel, ConfigDict, Field

from app.db.models.user import UserRole
from app.schemas.user import UserRead


class RoleUpdateRequest(BaseModel):
    """Body for changing a user's role."""

    model_config = ConfigDict(json_schema_extra={"example": {"role": "Developer"}})

    role: UserRole = Field(description="The role to assign.")


class UserListResponse(BaseModel):
    """A page of users."""

    items: list[UserRead]
    total: int = Field(description="Total users matching the filters.")
    limit: int
    offset: int
