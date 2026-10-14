"""A project's counts come from the database, grouped by project, not from
counting one page of a list. The tests have no database, so the SQL is
checked as sent and the rows it would return are fed back in; the paging
itself is exercised against a real one in the QA pass."""

from sqlalchemy.dialects import postgresql

from app.db.models.user import User, UserRole
from app.monitoring.infra.aws.service import InfraService
from app.monitoring.websites.service import WebsiteService


class RowsSession:
    """Remembers the statement it was asked to run and answers with `rows`."""

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.sql = ""

    async def execute(self, statement):
        self.sql = str(statement.compile(dialect=postgresql.dialect()))
        return self.rows


ADMIN = User(id=1, role=UserRole.ADMIN)


async def test_website_counts_are_grouped_by_project_in_one_query():
    # (project_id, total, up, down, unknown, maintenance, paused)
    session = RowsSession([(7, 105, 100, 3, 1, 0, 1), (9, 1, 1, 0, 0, 0, 0)])
    counts = await WebsiteService(session).summary_by_project(ADMIN)
    assert "GROUP BY websites.project_id" in session.sql
    assert "LIMIT" not in session.sql
    assert counts[7].total == 105
    assert (counts[7].up, counts[7].down, counts[7].unknown, counts[7].paused) == (100, 3, 1, 1)
    assert counts[9].total == 1


async def test_resource_counts_are_grouped_by_project_and_state():
    session = RowsSession([(4, "healthy", 150), (4, "down", 2), (4, "degraded", 60), (5, "paused", 1)])
    counts = await InfraService(session).summary_by_project(ADMIN)
    assert "GROUP BY aws_resources.project_id" in session.sql
    assert "LIMIT" not in session.sql
    assert counts[4].total == 212
    assert (counts[4].healthy, counts[4].degraded, counts[4].down) == (150, 60, 2)
    assert counts[5].paused == 1
