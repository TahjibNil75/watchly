"""How an AWS account is reached, as the API takes it.

Apart from `schemas.py` so that projects can take a new project's AWS accounts
without importing the rest of infrastructure monitoring.
"""

import enum
import re
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.monitoring.websites.models import WebsiteEnvironment

_ACCESS_KEY_ID = re.compile(r"^[A-Z0-9]{16,128}$")
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")


class AccountAuth(str, enum.Enum):
    """Which credentials an account is reached with. Either may then assume
    the account's `role_arn`."""

    #: Watchly's own, as boto3 finds them: the instance role on EC2, or the
    #: environment.
    DEFAULT = "default"
    #: An IAM user's access key, stored for the account.
    ACCESS_KEY = "access_key"


def _blank_to_none(value: str | None) -> str | None:
    value = value.strip() if isinstance(value, str) else value
    return value or None


class AccountFields(BaseModel):
    """What adding and changing an account share."""

    description: str | None = Field(default=None, max_length=2000)
    access_key_id: str | None = Field(default=None, examples=["AKIAIOSFODNN7EXAMPLE"])
    secret_access_key: str | None = Field(
        default=None, min_length=16, max_length=128, description="Write-only; stored encrypted."
    )
    role_arn: str | None = Field(
        default=None,
        examples=["arn:aws:iam::123456789012:role/watchly-monitor"],
        description="A role in the account to assume with the credentials above.",
    )
    external_id: str | None = Field(default=None, min_length=2, max_length=1224)
    default_region: str | None = Field(
        default=None, examples=["ap-southeast-1"], description="Where its VPCs are listed by default."
    )
    environment: WebsiteEnvironment | None = Field(
        default=None,
        description="The environment its resources get when none is given as they are added.",
    )
    watch_deployments: bool | None = Field(
        default=None,
        description=(
            "Announce its CodeDeploy deployments, and pause the down alerts of the resources each "
            "deploys to while it runs. Needs codedeploy:ListDeployments, BatchGetDeployments and "
            "GetDeploymentGroup. Off when not given."
        ),
    )

    @field_validator("access_key_id", "secret_access_key", "role_arn", "external_id", "default_region", mode="before")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("access_key_id")
    @classmethod
    def _key_id(cls, value: str | None) -> str | None:
        if value is not None and not _ACCESS_KEY_ID.match(value):
            raise ValueError("access_key_id must look like AKIAIOSFODNN7EXAMPLE")
        return value

    @field_validator("role_arn")
    @classmethod
    def _role(cls, value: str | None) -> str | None:
        if value is not None and not _ROLE_ARN.match(value):
            raise ValueError("role_arn must look like arn:aws:iam::123456789012:role/watchly-monitor")
        return value

    @field_validator("default_region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if value is not None and not _REGION.match(value):
            raise ValueError("default_region must look like ap-southeast-1")
        return value


class AccountInput(AccountFields):
    """A new AWS account: Watchly's own credentials (`default`, with a
    `role_arn` to reach another account) or an access key of its own. Given
    when an infrastructure project is created, or added to one later."""

    name: str = Field(min_length=1, max_length=255, examples=["Production"])
    auth_type: AccountAuth = AccountAuth.ACCESS_KEY

    @model_validator(mode="after")
    def _credentials(self) -> Self:
        if self.auth_type is AccountAuth.ACCESS_KEY:
            if not (self.access_key_id and self.secret_access_key):
                raise ValueError("An access_key account needs access_key_id and secret_access_key.")
        elif self.access_key_id or self.secret_access_key:
            raise ValueError("A default account uses Watchly's own credentials: leave the access key out.")
        if self.external_id and not self.role_arn:
            raise ValueError("external_id goes with role_arn.")
        return self
