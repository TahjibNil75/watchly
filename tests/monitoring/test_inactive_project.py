"""An inactive project is paused: nothing of it is probed and nothing it owns
alerts. The scheduled reads filter on `projects.is_active`, and the notifier
refuses its events as a backstop (a monthly report asked for by hand excepted).

The reads are checked by the SQL they send, since the tests have no database;
`due_for_check` and the others are exercised against one in the QA pass."""

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.monitoring.alerts.base import Alerter, NotificationKind
from app.monitoring.docker.monitor import DockerMonitor
from app.monitoring.infra.aws.service import InfraService
from app.monitoring.notifications.dispatcher import Notifier
from app.monitoring.notifications.samples import sample_event
from app.monitoring.websites.service import WebsiteService

ACTIVE_ONLY = "projects.is_active IS true"


class RecordingSession:
    """Remembers the statements it was asked to run and finds nothing."""

    def __init__(self, active: bool | None = True) -> None:
        self.statements: list[str] = []
        self.active = active

    async def scalars(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return []

    async def scalar(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return self.active

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class Recorder(Alerter):
    name = "recorder"

    def __init__(self) -> None:
        self.sent = 0

    async def is_configured(self, event) -> bool:
        return True

    async def send(self, event, message) -> bool:
        self.sent += 1
        return True


def _filters_on_active_projects(sql: str) -> bool:
    return ACTIVE_ONLY in sql.replace("= true", "IS true")


async def test_websites_of_an_inactive_project_are_not_due():
    session = RecordingSession()
    await WebsiteService(session).due_for_check()
    assert _filters_on_active_projects(session.statements[0])


async def test_resources_of_an_inactive_project_are_not_due():
    session = RecordingSession()
    await InfraService(session).due_resources()
    assert _filters_on_active_projects(session.statements[0])


async def test_docker_hosts_of_an_inactive_project_are_not_marked_offline():
    session = RecordingSession()
    await DockerMonitor(session, notifier=Notifier(session, alerters=[])).mark_offline()
    assert _filters_on_active_projects(session.statements[0])


@pytest.fixture
def alerts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALERTS_ENABLED", True)


async def test_an_inactive_project_sends_no_alerts(alerts_on):
    recorder = Recorder()
    notifier = Notifier(RecordingSession(active=False), alerters=[recorder])
    delivered = await notifier.dispatch(sample_event(NotificationKind.DOWN))
    assert delivered == ()
    assert recorder.sent == 0


async def test_an_active_project_still_alerts(alerts_on, monkeypatch: pytest.MonkeyPatch):
    recorder = Recorder()
    notifier = Notifier(RecordingSession(active=True), alerters=[recorder])
    delivered = await notifier.dispatch(sample_event(NotificationKind.DOWN))
    assert delivered == ("recorder",)


async def test_a_report_asked_for_by_hand_is_sent_for_an_inactive_project(alerts_on):
    recorder = Recorder()
    notifier = Notifier(RecordingSession(active=False), alerters=[recorder])
    delivered = await notifier.dispatch(sample_event(NotificationKind.MONTHLY_REPORT))
    assert delivered == ("recorder",)
