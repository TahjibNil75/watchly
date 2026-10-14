"""PATCH with an explicit null for a NOT NULL column (`name`, `is_active`,
`extra_emails`) leaves the field alone instead of reaching the database, and a
rename that collides names the attempted name without touching the expired row.

There is no database in these tests, so the session is a stand-in whose
rollback expires the project the way SQLAlchemy does."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.monitoring.projects.schemas import ProjectUpdate
from app.monitoring.projects.service import DuplicateProjectError, ProjectService


class Project:
    """Raises on any read after the session was rolled back, like an expired row."""

    expired = False

    def __init__(self) -> None:
        self.__dict__.update(
            name="Shop",
            is_active=True,
            extra_emails=["ops@example.com"],
            description=None,
            slack_bot_token=None,
            slack_channel_id=None,
            telegram_bot_token=None,
            telegram_chat_id=None,
            whatsapp_access_token=None,
            whatsapp_phone_number_id=None,
            whatsapp_recipients=[],
            members=[],
        )

    def __getattribute__(self, item):
        if item != "expired" and object.__getattribute__(self, "expired"):
            raise RuntimeError("MissingGreenlet: expired attribute read")
        return object.__getattribute__(self, item)

    @property
    def has_email_alerting(self) -> bool:
        return bool(self.members or self.extra_emails)


class Session:
    def __init__(self, project: Project, fail_commit: bool = False) -> None:
        self.project = project
        self.fail_commit = fail_commit

    async def commit(self) -> None:
        if self.fail_commit:
            raise IntegrityError("UPDATE projects", {}, Exception("duplicate"))

    async def rollback(self) -> None:
        self.project.expired = True

    async def refresh(self, project) -> None:
        pass


def _service(project: Project, **kwargs) -> ProjectService:
    service = ProjectService(Session(project, **kwargs))

    async def get_for_write(project_id, actor):
        return project

    service.get_for_write = get_for_write
    return service


@pytest.mark.parametrize("field", ["name", "is_active", "extra_emails"])
async def test_explicit_null_is_ignored(field):
    project = Project()
    before = getattr(project, field)

    result = await _service(project).update(1, ProjectUpdate(**{field: None}), actor=None)

    assert result is project
    assert getattr(project, field) == before


async def test_duplicate_name_is_reported_without_reading_expired_row():
    project = Project()
    service = _service(project, fail_commit=True)

    with pytest.raises(DuplicateProjectError) as caught:
        await service.update(1, ProjectUpdate(name="Taken"), actor=None)

    assert caught.value.name == "Taken"


async def test_failed_commit_without_rename_names_current_project():
    project = Project()
    service = _service(project, fail_commit=True)

    with pytest.raises(DuplicateProjectError) as caught:
        await service.update(1, ProjectUpdate(is_active=False), actor=None)

    assert caught.value.name == "Shop"
